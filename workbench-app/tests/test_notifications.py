#!/usr/bin/env python3
"""Tests for Story 2.2 (P0 notifications and notification actions).

Two layers:

* ``NotificationEngine`` unit tests with an injected clock and a logger
  "notify-send" replacement, covering classification, batching, escalation,
  daily digest, slots, snooze, event ids and command failures.
* HTTP integration tests for ``GET/POST /api/workbench/notifications*``,
  actions reused through 2.1 ``verify``, upstream failure retention, guards,
  and the background poller firing within 10 seconds.

Run with::

    python3 workbench-app/tests/test_notifications.py -v
"""

from __future__ import annotations

import json
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib import parse as urlparse

TESTS_DIR = Path(__file__).resolve().parent
APP_DIR = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(APP_DIR))

from notifications import (  # noqa: E402
    NotificationEngine,
    P0,
    P1,
    P2,
)
import test_dispatch as base  # noqa: E402
import test_verify as verifytests  # noqa: E402

REVIEW_TTL = 48 * 60 * 60


def make_logger(
    root: Path, *, name: str = "notify-logger.sh", fail: bool = False
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    script = root / name
    log = root / f"{name}.log"
    script.write_text(
        "#!/bin/sh\n"
        "{\n"
        '  printf \'%s\\n\' "$@"\n'
        "  printf -- '---\\n'\n"
        f"}} >> {shlex.quote(str(log))}\n"
        + ('echo "forced notify failure" >&2\nexit 1\n' if fail else "exit 0\n"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, log


def read_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    calls: list[list[str]] = []
    current: list[str] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            calls.append(current)
            current = []
        else:
            current.append(line)
    if current:
        calls.append(current)
    return calls


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def midnight_plus(hour: int, minute: int = 0) -> float:
    now = datetime.now()
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def board_card(
    task_id: str,
    status: str = "review",
    *,
    title: str | None = None,
    block_kind: str | None = None,
    block_recurrences: int = 0,
    progress: dict | None = None,
    parents: list[str] | None = None,
    review_requested_at: int | None = None,
) -> dict:
    return {
        "id": task_id,
        "title": title or task_id,
        "status": status,
        "block_kind": block_kind,
        "block_recurrences": block_recurrences,
        "progress": progress,
        "parent_ids": list(parents or []),
        "review_requested_at": review_requested_at,
    }


def board(*cards: dict, ttl: int = REVIEW_TTL) -> dict:
    return {"ok": True, "review_ttl_seconds": ttl, "tasks": list(cards)}


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wb-notify-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.script, self.log = make_logger(self.root)
        self.clock = FakeClock(midnight_plus(7))
        self.engine = self.make_engine()

    def make_engine(self, **overrides) -> NotificationEngine:
        params = {
            "clock": self.clock,
            "notify_command": [str(self.script)],
            "review_ttl_seconds": REVIEW_TTL,
            "batch_size": 3,
            "batch_seconds": 30 * 60,
            "escalate_seconds": 30 * 60,
            "snooze_seconds": 10 * 60,
            "daily_hour": 8,
        }
        params.update(overrides)
        return NotificationEngine(**params)

    def calls(self) -> list[list[str]]:
        return read_calls(self.log)

    def item(self, snapshot: dict, task_id: str) -> dict | None:
        for level in (P0, P1, P2):
            for item in snapshot[level]:
                if item["task_id"] == task_id:
                    return item
        return None

    # -- classification ---------------------------------------------------

    def test_blocked_needs_input_is_p0_and_notifies_once(self):
        now = int(self.clock.now)
        snapshot = self.engine.collect(
            board(
                board_card(
                    "t1",
                    "blocked",
                    title="等你回答",
                    block_kind="needs_input",
                    block_recurrences=2,
                )
            )
        )
        items = snapshot[P0]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "n:t1:blocked:needs_input:2")
        self.assertEqual(items[0]["actions"], ["remind"])
        self.assertNotIn("channels", items[0])
        self.assertEqual(snapshot["channels"]["desktop"], str(self.script))
        self.assertEqual(snapshot["channels"]["mobile"], "reserved")
        self.assertIsNone(snapshot["sweep"]["last_at"])
        self.assertEqual(snapshot["counts"], {P0: 1, P1: 0, P2: 0, "total": 1})
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--urgency=critical", calls[0])
        self.assertIn("等你回答", calls[0][-2])
        self.assertIn("t1", calls[0][-1])
        self.assertEqual(snapshot["desktop"]["last_kind"], P0)
        self.assertGreaterEqual(items[0]["first_seen_at"], now)

        # Repeated collection for the same event never re-notifies.
        again = self.engine.collect(
            board(
                board_card(
                    "t1",
                    "blocked",
                    block_kind="needs_input",
                    block_recurrences=2,
                )
            )
        )
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(again[P0]), 1)

    def test_blocked_capability_is_p0(self):
        snapshot = self.engine.collect(
            board(board_card("t1", "blocked", block_kind="capability"))
        )
        self.assertEqual(snapshot[P0][0]["task_id"], "t1")
        self.assertIn("--urgency=critical", self.calls()[0])

    def test_blocked_with_unfinished_children_is_p0(self):
        snapshot = self.engine.collect(
            board(
                board_card(
                    "t1",
                    "blocked",
                    block_kind="dependency",
                    progress={"done": 1, "total": 3},
                )
            )
        )
        self.assertEqual(snapshot[P0][0]["reason"], "有未完成子卡，下游被堵")
        self.assertEqual(len(self.calls()), 1)

    def test_other_blocked_is_p1_until_batch(self):
        snapshot = self.engine.collect(
            board(
                board_card("t1", "blocked", block_kind="dependency"),
                board_card("t2", "blocked", block_kind=None),
            )
        )
        self.assertEqual(snapshot["counts"][P1], 2)
        self.assertEqual(snapshot[P0], [])
        self.assertEqual(self.calls(), [])

    def test_review_is_p2_with_countdown_and_actions(self):
        now = int(self.clock.now)
        snapshot = self.engine.collect(
            board(
                board_card(
                    "t1",
                    "review",
                    review_requested_at=now - 3600,
                    parents=["p1"],
                )
            )
        )
        item = snapshot[P2][0]
        self.assertEqual(item["actions"], ["accept", "reject"])
        self.assertNotIn("review_age_seconds", item)
        self.assertEqual(item["auto_collect_in_seconds"], REVIEW_TTL - 3600)
        self.assertEqual(item["parent_ids"], ["p1"])
        self.assertEqual(self.calls(), [])  # before 08:00 there is no digest

    def test_review_with_unfinished_children_is_p0(self):
        snapshot = self.engine.collect(
            board(
                board_card(
                    "t1",
                    "review",
                    progress={"done": 0, "total": 1},
                    review_requested_at=int(self.clock.now) - 60,
                )
            )
        )
        self.assertEqual(snapshot[P0][0]["task_id"], "t1")
        self.assertIn("remind", snapshot[P0][0]["actions"])
        self.assertEqual(len(self.calls()), 1)

    # -- batching / escalation / daily -----------------------------------

    def test_p1_batches_at_three_items(self):
        first = board_card("t1", "blocked", block_kind="transient")
        second = board_card("t2", "blocked", block_kind="dependency")
        third = board_card("t3", "blocked", block_kind=None)
        self.engine.collect(board(first, second))
        self.assertEqual(self.calls(), [])
        snapshot = self.engine.collect(board(first, second, third))
        self.assertEqual(snapshot["counts"][P1], 3)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--urgency=normal", calls[0])
        self.assertIn("3 张卡", calls[0][-2])

    def test_p1_escalates_when_oldest_reaches_30_minutes(self):
        card = board_card("t1", "blocked", block_kind="dependency")
        self.engine.collect(board(card))
        self.assertEqual(self.calls(), [])
        self.clock.advance(30 * 60 - 1)
        self.engine.collect(board(card))
        self.assertEqual(self.calls(), [])
        self.clock.advance(1)
        snapshot = self.engine.collect(board(card))
        # The 30-minute half of the P1 batching rule is the escalation push:
        # one critical notification, and the event is now pinned as P0.
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("--urgency=critical", self.calls()[0])
        self.assertEqual(snapshot["counts"][P1], 0)
        self.assertEqual(snapshot[P0][0]["task_id"], "t1")
        self.assertTrue(snapshot[P0][0]["escalated"])

    def test_p1_escalates_to_p0_exactly_once(self):
        card = board_card("t1", "blocked", block_kind="dependency", title="等依赖")
        self.engine.collect(board(card))
        self.clock.advance(30 * 60)
        snapshot = self.engine.collect(board(card))  # escalation at t+30
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("--urgency=critical", self.calls()[0])
        self.assertEqual(snapshot[P0][0]["task_id"], "t1")
        self.assertTrue(snapshot[P0][0]["escalated"])
        self.clock.advance(30 * 60)
        self.engine.collect(board(card))
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(self.engine.snapshot()[P0][0]["escalated"])

    def test_p1_remind_after_snooze_stays_p1_and_normal_urgency(self):
        card = board_card("t1", "blocked", block_kind="dependency", title="等依赖")
        snapshot = self.engine.collect(board(card))
        event_id = snapshot[P1][0]["id"]
        code, body = self.engine.action(event_id, "remind")
        self.assertEqual(code, 200, body)
        self.clock.advance(10 * 60 + 1)
        snapshot = self.engine.collect(board(card))
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--urgency=normal", calls[0])
        self.assertIn("P1 再提醒", calls[0][-2])
        self.assertEqual(snapshot["counts"][P1], 1)
        self.assertEqual(snapshot[P0], [])
        self.assertFalse(snapshot[P1][0]["escalated"])

    def test_p1_batch_flush_before_escalation_window_stays_p1(self):
        engine = self.make_engine(batch_seconds=100, escalate_seconds=1000)
        card = board_card("t1", "blocked", block_kind="dependency")
        engine.collect(board(card))
        self.assertEqual(read_calls(self.log), [])
        self.clock.advance(100)
        snapshot = engine.collect(board(card))
        calls = read_calls(self.log)
        self.assertEqual(len(calls), 1)
        self.assertIn("--urgency=normal", calls[0])
        self.assertEqual(snapshot["counts"][P1], 1)
        self.assertEqual(snapshot["counts"][P0], 0)
        self.assertFalse(snapshot[P1][0]["escalated"])
        self.assertIsNotNone(snapshot[P1][0]["notified_at"])

    def test_p1_escalation_group_is_one_critical_push(self):
        first = board_card("t1", "blocked", block_kind="dependency", title="等依赖甲")
        second = board_card("t2", "blocked", block_kind="transient", title="等依赖乙")
        self.engine.collect(board(first, second))
        self.assertEqual(self.calls(), [])
        self.clock.advance(30 * 60)
        snapshot = self.engine.collect(board(first, second))
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--urgency=critical", calls[0])
        self.assertIn("2 张卡", calls[0][-2])
        self.assertIn("2 张卡", calls[0][-1])
        self.assertEqual(snapshot["counts"][P0], 2)
        self.assertEqual(snapshot["counts"][P1], 0)
        for item in snapshot[P0]:
            self.assertTrue(item["escalated"])

    def test_p1_escalation_group_failure_keeps_p1_and_backs_off(self):
        script, log = make_logger(self.root, name="fail-escalation.sh", fail=True)
        engine = self.make_engine(notify_command=[str(script)])
        first = board_card("t1", "blocked", block_kind="dependency")
        second = board_card("t2", "blocked", block_kind="transient")
        engine.collect(board(first, second))
        self.clock.advance(30 * 60)
        snapshot = engine.collect(board(first, second))
        calls = read_calls(log)
        self.assertEqual(len(calls), 1)
        self.assertIn("--urgency=critical", calls[0])
        self.assertEqual(snapshot["counts"][P1], 2)
        self.assertEqual(snapshot["counts"][P0], 0)
        for item in snapshot[P1]:
            self.assertFalse(item["escalated"])
            self.assertIsNone(item["notified_at"])
        # Backoff: the failed group is not retried on the very next poll.
        engine.collect(board(first, second))
        self.assertEqual(len(read_calls(log)), 1)

    def test_body_truncation(self):
        long_title = "很长的标题" * 80
        cards = [
            board_card(f"t{index}", "blocked", block_kind="transient", title=long_title)
            for index in range(3)
        ]
        self.engine.collect(board(*cards))
        body = self.calls()[0][-1]
        self.assertLessEqual(len(body), 240)
        self.assertTrue(body.endswith("…"))

    def test_queued_bundle_promoted_after_accept(self):
        verify_calls: list[tuple] = []

        def verify(task_id, action, reason, expected_review_at):
            verify_calls.append((task_id, action, expected_review_at))
            return 200, {"ok": True, "action": action, "status": "done"}

        engine = self.make_engine(verify=verify)
        now = int(self.clock.now)
        cards = [
            board_card("r1", "review", review_requested_at=now - 40),
            board_card("r2", "review", review_requested_at=now - 30),
            board_card("r3", "review", review_requested_at=now - 20),
            board_card("r4", "review", review_requested_at=now - 10),
        ]
        snapshot = engine.collect(board(*cards))
        self.assertEqual(snapshot["slots"]["queued_count"], 1)
        queued = next(
            item for item in snapshot[P2] if item["slot"] == "queued"
        )
        self.assertEqual(queued["task_id"], "r4")

        event_id = next(
            item["id"] for item in snapshot[P2] if item["task_id"] == "r2"
        )
        code, body = engine.action(event_id, "accept")
        self.assertEqual(code, 200, body)
        snapshot = engine.collect(board(cards[0], cards[2], cards[3]))
        promoted = next(item for item in snapshot[P2] if item["task_id"] == "r4")
        self.assertEqual(promoted["slot"], "active")
        self.assertEqual(snapshot["slots"]["queued_count"], 0)

    def test_p0_reminds_once_after_30_minutes(self):
        card = board_card("t1", "blocked", block_kind="needs_input")
        self.engine.collect(board(card))
        self.assertEqual(len(self.calls()), 1)
        self.clock.advance(30 * 60)
        self.engine.collect(board(card))
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("再提醒", calls[1][-2])
        self.assertTrue(self.engine.snapshot()[P0][0]["escalated"])
        self.clock.advance(30 * 60)
        self.engine.collect(board(card))
        self.assertEqual(len(self.calls()), 2)

    def test_p2_daily_digest_is_once_per_day(self):
        self.clock.now = midnight_plus(8)
        card = board_card(
            "t1",
            "review",
            title="日报卡",
            review_requested_at=int(self.clock.now) - 100,
        )
        snapshot = self.engine.collect(board(card))
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--urgency=low", calls[0])
        self.assertTrue(snapshot["daily"]["sent"])
        self.assertIn("P2", snapshot["daily"]["line"])
        self.engine.collect(board(card))
        self.assertEqual(len(self.calls()), 1)
        self.clock.advance(24 * 60 * 60)
        self.engine.collect(board(card))
        self.assertEqual(len(self.calls()), 2)

    # -- slots -------------------------------------------------------------

    def test_slots_bundle_siblings_and_queue_overflow(self):
        cards = [
            board_card("a1", "review", parents=["p1"], review_requested_at=10),
            board_card("a2", "review", parents=["p1"], review_requested_at=20),
            board_card("b1", "review", parents=["p2"], review_requested_at=30),
            board_card("c1", "review", review_requested_at=40),
            board_card("d1", "review", review_requested_at=50),
        ]
        snapshot = self.engine.collect(board(*cards))
        slots = snapshot["slots"]
        self.assertEqual(slots["capacity"], 3)
        self.assertEqual(len(slots["active"]), 3)
        self.assertEqual(slots["queued_count"], 1)
        self.assertEqual(slots["queued_cards"], 1)
        sibling = next(bundle for bundle in slots["active"] if bundle["key"] == "parent:p1")
        self.assertEqual(sibling["count"], 2)
        self.assertEqual(sibling["task_ids"], ["a1", "a2"])
        parentless = [
            bundle for bundle in slots["active"] if bundle["key"].startswith("task:")
        ]
        self.assertEqual(len(parentless), 1)
        self.assertEqual(parentless[0]["count"], 1)
        queued_item = next(item for item in snapshot[P2] if item["task_id"] == "d1")
        self.assertEqual(queued_item["slot"], "queued")
        active_ids = {
            item["task_id"] for bundle in slots["active"] for item in bundle["cards"]
        }
        self.assertEqual(active_ids, {"a1", "a2", "b1", "c1"})

    # -- snooze / actions --------------------------------------------------

    def test_remind_snoozes_and_reappears_after_ten_minutes(self):
        card = board_card("t1", "blocked", block_kind="needs_input")
        snapshot = self.engine.collect(board(card))
        event_id = snapshot[P0][0]["id"]
        code, body = self.engine.action(event_id, "remind")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["action"], "remind")
        self.assertIn("snooze_until", body)
        self.engine.collect(board(card))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.engine.snapshot()[P0][0]["snooze_until"], body["snooze_until"])
        self.clock.advance(10 * 60 - 1)
        self.engine.collect(board(card))
        self.assertEqual(len(self.calls()), 1)
        self.clock.advance(1)
        self.engine.collect(board(card))
        self.assertEqual(len(self.calls()), 2)
        self.assertIsNone(self.engine.snapshot()[P0][0]["snooze_until"])

    def test_action_rules(self):
        review = board_card("r1", "review", review_requested_at=int(self.clock.now))
        blocked = board_card("b1", "blocked", block_kind="dependency")
        snapshot = self.engine.collect(board(review, blocked))
        review_item = self.item(snapshot, "r1")
        blocked_item = self.item(snapshot, "b1")
        code, body = self.engine.action(review_item["id"], "remind")
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_action")
        code, body = self.engine.action(blocked_item["id"], "accept")
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_action")
        code, body = self.engine.action(blocked_item["id"], "dance")
        self.assertEqual(code, 400, body)
        code, body = self.engine.action("n:missing", "remind")
        self.assertEqual(code, 404, body)

    def test_accept_and_reject_delegate_to_verify(self):
        calls: list[tuple] = []
        review = board_card("r1", "review", review_requested_at=int(self.clock.now))
        blocked = board_card("b1", "blocked", block_kind="dependency")

        def verify(task_id, action, reason, expected_review_at):
            calls.append((task_id, action, reason, expected_review_at))
            if reason == "bad":
                return 502, {"ok": False, "error": "upstream_unavailable"}
            return 200, {
                "ok": True,
                "action": action,
                "status": "done" if action == "accept" else "triage",
                "comment_written": action == "reject",
            }

        engine = self.make_engine(verify=verify)
        snapshot = engine.collect(board(review, blocked))
        review_item = next(item for item in snapshot[P2] if item["task_id"] == "r1")
        code, body = engine.action(review_item["id"], "accept")
        self.assertEqual(code, 200, body)
        self.assertEqual(calls[-1][:3], ("r1", "accept", None))
        self.assertEqual(calls[-1][3], int(self.clock.now))
        self.assertNotIn(review_item["id"], [item["id"] for item in engine.snapshot()[P2]])

        snapshot = engine.collect(board(review, blocked))
        review_item = next(item for item in snapshot[P2] if item["task_id"] == "r1")
        code, body = engine.action(review_item["id"], "reject", "bad")
        self.assertEqual(code, 502, body)
        self.assertIn(review_item["id"], [item["id"] for item in engine.snapshot()[P2]])

    # -- event ids / command failure --------------------------------------

    def test_review_round_change_creates_a_new_event(self):
        now = int(self.clock.now)
        first = board_card("r1", "review", review_requested_at=now - 100)
        snapshot = self.engine.collect(board(first))
        first_id = snapshot[P2][0]["id"]
        self.assertEqual(first_id, f"n:r1:review:{now - 100}")
        self.engine.collect(board(first))
        self.assertEqual(len(self.calls()), 0)  # 07:00, no daily digest
        second = board_card("r1", "review", review_requested_at=now - 10)
        snapshot = self.engine.collect(board(second))
        self.assertNotEqual(snapshot[P2][0]["id"], first_id)
        self.assertEqual(snapshot[P2][0]["id"], f"n:r1:review:{now - 10}")

    def test_resolved_then_reappeared_resets_state(self):
        card = board_card("t1", "blocked", block_kind="needs_input")
        self.engine.collect(board(card))
        self.clock.advance(60)
        self.engine.collect(board())  # resolved
        self.assertEqual(self.engine.snapshot()["counts"]["total"], 0)
        snapshot = self.engine.collect(board(card))
        self.assertEqual(snapshot[P0][0]["first_seen_at"], int(self.clock.now))
        self.assertFalse(snapshot[P0][0]["escalated"])
        self.assertEqual(len(self.calls()), 2)

    def test_missing_notify_command_keeps_snapshot_usable(self):
        engine = self.make_engine(notify_command=["/nonexistent/wb-notify"])
        snapshot = engine.collect(
            board(board_card("t1", "blocked", block_kind="needs_input"))
        )
        self.assertEqual(snapshot["counts"][P0], 1)
        self.assertFalse(snapshot["desktop"]["available"])
        self.assertTrue(snapshot["desktop"]["last_error"])
        self.assertIsNone(snapshot[P0][0]["notified_at"])
        self.assertEqual(len(snapshot[P0]), 1)
        code, body = engine.action(snapshot[P0][0]["id"], "remind")
        self.assertEqual(code, 200, body)
        self.assertEqual(engine.snapshot()["counts"][P0], 1)

    def test_malformed_notify_command_does_not_break_startup(self):
        engine = self.make_engine(notify_command='notify-send "unbalanced')
        snapshot = engine.collect(
            board(board_card("t1", "blocked", block_kind="needs_input"))
        )
        self.assertEqual(snapshot["counts"][P0], 1)
        self.assertFalse(snapshot["desktop"]["available"])
        self.assertIn("invalid WORKBENCH_NOTIFY_COMMAND", snapshot["desktop"]["last_error"])
        self.assertEqual(snapshot["channels"]["desktop"], "")
        self.assertIsNone(snapshot[P0][0]["notified_at"])

    def test_failed_notify_is_not_marked_and_backs_off(self):
        script, log = make_logger(self.root, name="fail-basic.sh", fail=True)
        engine = self.make_engine(notify_command=[str(script)])
        card = board_card("t1", "blocked", block_kind="needs_input")
        snapshot = engine.collect(board(card))
        calls = read_calls(log)
        self.assertEqual(len(calls), 1)
        self.assertTrue(snapshot["desktop"]["last_error"])
        self.assertIsNone(snapshot["desktop"]["last_sent_at"])
        self.assertIsNone(snapshot[P0][0]["notified_at"])
        # Backoff: no retry on the next poll, one retry after 60s.
        engine.collect(board(card))
        self.assertEqual(len(read_calls(log)), 1)
        self.clock.advance(59)
        engine.collect(board(card))
        self.assertEqual(len(read_calls(log)), 1)
        self.clock.advance(2)
        engine.collect(board(card))
        self.assertEqual(len(read_calls(log)), 2)
        self.assertIsNone(engine.snapshot()[P0][0]["notified_at"])

    def test_failing_channel_is_attempted_once_per_burst(self):
        script, log = make_logger(self.root, name="fail-burst.sh", fail=True)
        engine = self.make_engine(notify_command=[str(script)])
        first = board_card("t1", "blocked", block_kind="needs_input")
        second = board_card("t2", "blocked", block_kind="capability")
        snapshot = engine.collect(board(first, second))
        self.assertEqual(snapshot["counts"][P0], 2)
        # Only the first event reaches the broken command; the rest of the
        # burst is parked by the channel backoff instead of spawning N times.
        self.assertEqual(len(read_calls(log)), 1)
        self.assertIsNone(snapshot[P0][0]["notified_at"])
        self.assertIsNone(snapshot[P0][1]["notified_at"])
        engine.collect(board(first, second))
        self.assertEqual(len(read_calls(log)), 1)
        self.clock.advance(61)
        engine.collect(board(first, second))
        self.assertEqual(len(read_calls(log)), 2)

    def test_failed_p2_digest_is_not_marked_sent(self):
        self.clock.now = midnight_plus(8)
        script, log = make_logger(self.root, name="fail-daily.sh", fail=True)
        engine = self.make_engine(notify_command=[str(script)])
        card = board_card(
            "t1",
            "review",
            title="日报卡",
            review_requested_at=int(self.clock.now) - 100,
        )
        snapshot = engine.collect(board(card))
        self.assertEqual(len(read_calls(log)), 1)
        self.assertFalse(snapshot["daily"]["sent"])
        self.assertIn("P2", snapshot["daily"]["line"])
        engine.collect(board(card))
        self.assertEqual(len(read_calls(log)), 1)  # 60s backoff
        self.clock.advance(61)
        engine.collect(board(card))
        self.assertEqual(len(read_calls(log)), 2)
        self.assertFalse(engine.snapshot()["daily"]["sent"])

    def test_record_sweep_exposes_freshness_and_error(self):
        self.engine.record_sweep()
        snapshot = self.engine.snapshot()
        self.assertEqual(snapshot["sweep"]["last_at"], int(self.clock.now))
        self.assertIsNone(snapshot["sweep"]["last_error"])
        self.clock.advance(5)
        self.engine.record_sweep("upstream_unavailable")
        snapshot = self.engine.snapshot()
        self.assertEqual(snapshot["sweep"]["last_at"], int(self.clock.now))
        self.assertEqual(snapshot["sweep"]["last_error"], "upstream_unavailable")


class NotificationIntegrationTests(unittest.TestCase):
    """HTTP endpoints against an isolated workbench + real kanban DB."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wb-notify-http-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp)
        self.script, self.log = make_logger(self.root)

    def start(self, *, env_extra=None, upstream=None) -> base.WorkbenchServer:
        env = {
            "WORKBENCH_NOTIFY_SWEEP_SECONDS": "0",
            "WORKBENCH_NOTIFY_COMMAND": str(self.script),
        }
        env.update(env_extra or {})
        server = verifytests.start_server(
            self.root / "srv", env_extra=env, upstream=upstream
        )
        self.addCleanup(server.stop)
        return server

    def calls(self) -> list[list[str]]:
        return read_calls(self.log)

    def seed_review(
        self, server: base.WorkbenchServer, title: str, *, age: int = 100
    ) -> str:
        card = server.create_task(title, triage=True)
        server.seed_metadata(
            card,
            {"bucket": "today", "contract": {"accept": "ok", "input": "x", "ready": True}},
        )
        code, body = server.dispatch(card)
        self.assertEqual(code, 200, body)
        code, body = server.patch_status(
            card,
            "review",
            summary="完成",
            metadata={"solidified": verifytests.DEFAULT_SOLIDIFIED},
        )
        self.assertEqual(code, 200, body)
        conn = sqlite3.connect(str(server.kanban_db), timeout=5)
        try:
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'review_requested'",
                (int(time.time()) - age, card),
            )
            conn.commit()
        finally:
            conn.close()
        return card

    def set_blocked(
        self, server: base.WorkbenchServer, task_id: str, kind: str = "needs_input"
    ) -> None:
        conn = sqlite3.connect(str(server.kanban_db), timeout=5)
        try:
            conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = ?, "
                "block_recurrences = 1 WHERE id = ?",
                (kind, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def sweep(self, server: base.WorkbenchServer):
        return base.http_json(
            "POST", server.base + "/api/workbench/notifications/sweep", {}
        )

    def snapshot(self, server: base.WorkbenchServer):
        return base.http_json("GET", server.base + "/api/workbench/notifications")

    def action(self, server, event_id, action, reason=None, expected=None):
        payload = {"action": action}
        if reason is not None:
            payload["reason"] = reason
        if expected is not None:
            payload["expected_review_at"] = expected
        return base.http_json(
            "POST",
            server.base
            + "/api/workbench/notifications/"
            + urlparse.quote(event_id, safe="")
            + "/action",
            payload,
        )

    def test_sweep_snapshot_countdown_and_actions(self):
        server = self.start()
        card = self.seed_review(server, "待验收卡", age=100)
        code, body = self.sweep(server)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["counts"]["total"], 1)
        self.assertEqual(body["slots"]["capacity"], 3)
        self.assertEqual(len(body["slots"]["active"]), 1)
        item = body[P2][0]
        self.assertEqual(item["task_id"], card)
        self.assertEqual(item["kind"], "review")
        self.assertEqual(item["actions"], ["accept", "reject"])
        self.assertGreater(item["auto_collect_in_seconds"], REVIEW_TTL - 200)
        self.assertLessEqual(item["auto_collect_in_seconds"], REVIEW_TTL)

        code, body = self.action(server, item["id"], "reject")
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "reason_required")
        self.assertEqual(self.snapshot(server)[1]["counts"]["total"], 1)

        code, body = self.action(server, item["id"], "accept")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "done")
        self.assertEqual(server.status(card), "done")
        code, snapshot = self.snapshot(server)
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(snapshot["counts"]["total"], 0)

        rejected = self.seed_review(server, "打回卡", age=50)
        code, snapshot = self.sweep(server)
        self.assertEqual(code, 200, snapshot)
        item = snapshot[P2][0]
        code, body = self.action(server, item["id"], "reject", "  没跑测试  ")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["comment_written"])
        self.assertEqual(server.status(rejected), "triage")
        self.assertEqual(self.snapshot(server)[1]["counts"]["total"], 0)

    def test_blocked_p0_remind_and_non_review_action_rules(self):
        server = self.start()
        card = server.create_task("阻塞卡", triage=True)
        self.set_blocked(server, card)
        code, body = self.sweep(server)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["counts"][P0], 1)
        item = body[P0][0]
        self.assertEqual(item["id"], f"n:{card}:blocked:needs_input:1")
        self.assertEqual(item["actions"], ["remind"])
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--urgency=critical", calls[0])

        code, body = self.action(server, item["id"], "accept")
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_action")

        code, body = self.action(server, item["id"], "remind")
        self.assertEqual(code, 200, body)
        self.assertIn("snooze_until", body)
        code, snapshot = self.snapshot(server)
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(snapshot[P0][0]["snooze_until"], body["snooze_until"])

    def test_notification_endpoints_guard_json_and_origin(self):
        server = self.start()
        code, body = base.http_json(
            "POST",
            server.base + "/api/workbench/notifications/sweep",
            {},
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(code, 415, body)
        code, body = base.http_json(
            "POST",
            server.base + "/api/workbench/notifications/sweep",
            {},
            headers={
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
            },
        )
        self.assertEqual(code, 403, body)
        code, snapshot = self.snapshot(server)
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(snapshot["counts"]["total"], 0)
        self.assertEqual(snapshot["review_ttl_seconds"], REVIEW_TTL)

    def test_upstream_failure_keeps_the_previous_snapshot(self):
        now = int(time.time())
        with verifytests.fake_kanban(
            [verifytests.fake_task("t1", review_events=[(1, now - 100)])]
        ) as fake:
            server = self.start(upstream=fake.base)
            code, body = self.sweep(server)
            self.assertEqual(code, 200, body)
            self.assertEqual(body["counts"]["total"], 1)
        # The fake upstream is gone; the sweep must fail closed and keep state.
        code, body = self.sweep(server)
        self.assertEqual(code, 502, body)
        self.assertEqual(body, {"ok": False, "error": "upstream_unavailable"})
        code, snapshot = self.snapshot(server)
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(snapshot["counts"]["total"], 1)
        self.assertEqual(snapshot["sweep"]["last_error"], "upstream_unavailable")
        self.assertIsNotNone(snapshot["sweep"]["last_at"])

    def test_detail_failure_is_502(self):
        now = int(time.time())
        with verifytests.fake_kanban(
            [verifytests.fake_task("t1", review_events=[(1, now - 100)])]
        ) as fake:
            fake.detail_fail_ids = {"t1"}
            server = self.start(upstream=fake.base)
            code, body = self.sweep(server)
            self.assertEqual(code, 502, body)
            self.assertEqual(body["error"], "upstream_unavailable")
            snapshot = self.snapshot(server)[1]
            self.assertEqual(snapshot["counts"]["total"], 0)
            self.assertEqual(snapshot["sweep"]["last_error"], "upstream_unavailable")

    def test_sweep_records_freshness_on_success(self):
        server = self.start()
        self.seed_review(server, "新鲜度卡")
        code, body = self.sweep(server)
        self.assertEqual(code, 200, body)
        self.assertIsNotNone(body["sweep"]["last_at"])
        self.assertIsNone(body["sweep"]["last_error"])
        code, snapshot = self.snapshot(server)
        self.assertEqual(code, 200, snapshot)
        self.assertIsNotNone(snapshot["sweep"]["last_at"])
        self.assertIsNone(snapshot["sweep"]["last_error"])
        self.assertEqual(snapshot["channels"]["mobile"], "reserved")
        self.assertTrue(snapshot["channels"]["desktop"])

    def test_threshold_env_wiring(self):
        server = self.start(
            env_extra={
                "WORKBENCH_NOTIFY_BATCH_SIZE": "5",
                "WORKBENCH_NOTIFY_BATCH_SECONDS": "42",
                "WORKBENCH_NOTIFY_ESCALATE_SECONDS": "43",
                "WORKBENCH_NOTIFY_SNOOZE_SECONDS": "44",
                "WORKBENCH_NOTIFY_DAILY_HOUR": "9",
            }
        )
        code, snapshot = self.sweep(server)
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(
            snapshot["thresholds"],
            {
                "batch_size": 5,
                "batch_seconds": 42,
                "escalate_seconds": 43,
                "snooze_seconds": 44,
                "daily_hour": 9,
            },
        )

    def test_stale_review_round_is_409(self):
        server = self.start()
        card = self.seed_review(server, "轮次卡", age=100)
        code, snapshot = self.sweep(server)
        self.assertEqual(code, 200, snapshot)
        item = snapshot[P2][0]
        old_review_at = item["review_requested_at"]
        self.assertIsInstance(old_review_at, int)

        # A new review round starts; the old notification must not accept it.
        server.add_kanban_event(card, "review_requested", {})
        code, body = self.action(
            server, item["id"], "accept", expected=old_review_at
        )
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "stale_review")
        self.assertNotEqual(body["review_requested_at"], old_review_at)
        self.assertEqual(server.status(card), "review")
        self.assertEqual(self.snapshot(server)[1]["counts"]["total"], 1)

        # A fresh sweep yields the new round; accepting that one works.
        code, snapshot = self.sweep(server)
        self.assertEqual(code, 200, snapshot)
        fresh = snapshot[P2][0]
        self.assertEqual(fresh["task_id"], card)
        self.assertNotEqual(fresh["review_requested_at"], old_review_at)
        code, body = self.action(
            server,
            fresh["id"],
            "accept",
            expected=fresh["review_requested_at"],
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(server.status(card), "done")

    def test_background_poller_notifies_within_ten_seconds(self):
        server = self.start(
            env_extra={"WORKBENCH_NOTIFY_SWEEP_SECONDS": "0.2"}
        )
        card = server.create_task("后台通知卡", triage=True)
        self.set_blocked(server, card)
        deadline = time.time() + 10
        while time.time() < deadline and not self.calls():
            time.sleep(0.1)
        calls = self.calls()
        self.assertTrue(calls, "background poller did not notify within 10s")
        self.assertIn("--urgency=critical", calls[0])
        self.assertTrue(any("后台通知卡" in arg for arg in calls[0]))
        code, snapshot = self.snapshot(server)
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(snapshot["counts"][P0], 1)

    def test_default_five_second_poller_notifies(self):
        server = self.start(
            env_extra={"WORKBENCH_NOTIFY_SWEEP_SECONDS": "5"}
        )
        card = server.create_task("五秒轮询卡", triage=True)
        self.set_blocked(server, card)
        started = time.time()
        deadline = started + 10
        while time.time() < deadline and not self.calls():
            time.sleep(0.1)
        calls = self.calls()
        self.assertTrue(calls, "5s background poller did not notify within 10s")
        self.assertIn("--urgency=critical", calls[0])
        self.assertTrue(any("五秒轮询卡" in arg for arg in calls[0]))
        self.assertGreaterEqual(time.time() - started, 3.0)
        code, snapshot = self.snapshot(server)
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(snapshot["counts"][P0], 1)
        self.assertIsNotNone(snapshot["sweep"]["last_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
