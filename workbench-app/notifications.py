#!/usr/bin/env python3
"""Workbench notification engine (Story 2.2).

The engine is deliberately stdlib-only so it can be unit tested without the
Hermes runtime. ``WorkbenchService.board_snapshot()`` feeds it a read-only
board slice; the engine classifies ``blocked`` / ``review`` cards, keeps the
notification state in memory, and calls ``notify-send`` (or a test command)
for desktop notifications.

Rules (see ``_bmad-output/implementation-artifacts/spec-2-2-p0-notifications.md``):

* ``blocked`` + (``needs_input`` / ``capability`` or unfinished children) → P0,
  other ``blocked`` → P1.
* ``review`` + unfinished children → P0, other ``review`` → P2.
* P0 is pushed immediately; P1 is batched (3 items or 30 minutes); P2 is a
  once-a-day 08:00 digest.
* An event notified 30 minutes ago and still unhandled escalates once
  (P1→P0) or reminds once (P0). Snooze suppresses notifications for 10
  minutes but keeps the event visible.
* Event ids are stable and scoped to one round:
  ``n:{task}:review:{review_requested_at}`` /
  ``n:{task}:blocked:{block_kind}:{block_recurrences}``.

The engine never talks to kanban itself and never writes card state. Actions
``accept`` / ``reject`` call the injected ``verify`` callable (Story 2.1).
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

P0 = "p0"
P1 = "p1"
P2 = "p2"
LEVELS = (P0, P1, P2)

BLOCKED = "blocked"
REVIEW = "review"
HIGH_PRIORITY_BLOCK_KINDS = ("needs_input", "capability")

DEFAULT_BATCH_SIZE = 3
DEFAULT_BATCH_SECONDS = 30 * 60
DEFAULT_ESCALATE_SECONDS = 30 * 60
DEFAULT_SNOOZE_SECONDS = 10 * 60
DEFAULT_DAILY_HOUR = 8
DEFAULT_REVIEW_TTL_SECONDS = 48 * 60 * 60
DEFAULT_APP_NAME = "Jarvis 工作台"
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 5.0
# Failed desktop pushes are retried at most once a minute, not on every poll.
NOTIFY_RETRY_SECONDS = 60.0
MAX_BODY_CHARS = 240
MAX_DAILY_LINE_CHARS = 300
SLOT_CAPACITY = 3

VerifyCallable = Callable[[str, Any, Any, Any], tuple[int, dict[str, Any]]]


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _unfinished_children(card: dict[str, Any]) -> bool:
    """True when the board's progress rollup says children are still open."""
    progress = card.get("progress")
    if isinstance(progress, dict):
        done = _as_int(progress.get("done"))
        total = _as_int(progress.get("total"))
        if done is not None and total is not None:
            return total > done
    return False


def _classify(status: str, block_kind: Optional[str], unfinished: bool) -> Optional[str]:
    if status == BLOCKED:
        if block_kind in HIGH_PRIORITY_BLOCK_KINDS or unfinished:
            return P0
        return P1
    if status == REVIEW:
        return P0 if unfinished else P2
    return None


@dataclass
class _Event:
    id: str
    task_id: str
    kind: str
    status: str
    title: str
    level: str
    block_kind: Optional[str]
    block_recurrences: int
    review_requested_at: Optional[int]
    parent_ids: tuple[str, ...]
    unfinished_children: bool
    first_seen_at: float
    notified_at: Optional[float] = None
    escalated: bool = False
    snooze_until: Optional[float] = None
    reminder_due: bool = False
    retry_after: Optional[float] = None


@dataclass
class _DesktopState:
    available: bool
    command: str
    last_error: Optional[str] = None
    last_sent_at: Optional[int] = None
    last_kind: Optional[str] = None
    last_urgency: Optional[str] = None


class NotificationEngine:
    """In-memory notification state machine.

    ``clock`` and ``notify_command`` are injectable so tests can advance time
    and capture desktop calls without a real notification daemon.
    """

    def __init__(
        self,
        *,
        verify: Optional[VerifyCallable] = None,
        clock: Callable[[], float] = time.time,
        notify_command: Optional[Sequence[str] | str] = None,
        app_name: str = DEFAULT_APP_NAME,
        review_ttl_seconds: int = DEFAULT_REVIEW_TTL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_seconds: float = DEFAULT_BATCH_SECONDS,
        escalate_seconds: float = DEFAULT_ESCALATE_SECONDS,
        snooze_seconds: float = DEFAULT_SNOOZE_SECONDS,
        daily_hour: int = DEFAULT_DAILY_HOUR,
        notify_timeout_seconds: float = DEFAULT_NOTIFY_TIMEOUT_SECONDS,
    ) -> None:
        self._verify = verify
        self._clock = clock
        self._app_name = app_name
        self._review_ttl_seconds = max(0, int(review_ttl_seconds))
        self._batch_size = max(1, int(batch_size))
        self._batch_seconds = max(0.0, float(batch_seconds))
        self._escalate_seconds = max(0.0, float(escalate_seconds))
        self._snooze_seconds = max(0.0, float(snooze_seconds))
        self._daily_hour = min(23, max(0, int(daily_hour)))
        self._notify_timeout = max(0.1, float(notify_timeout_seconds))
        self._lock = threading.RLock()
        self._events: dict[str, _Event] = {}
        self._last_daily_date: Optional[str] = None
        self._last_daily_line: str = ""
        self._last_daily_count: int = 0
        self._daily_retry_after: Optional[float] = None
        # A failing/hanging command must not be spawned once per event in the
        # same collection: the first failure parks the whole channel briefly.
        self._channel_retry_after: Optional[float] = None
        self._sweep_last_at: Optional[int] = None
        self._sweep_last_error: Optional[str] = None
        command_error: Optional[str] = None
        try:
            self._command_argv = self._resolve_command(notify_command)
        except ValueError as exc:
            # A malformed WORKBENCH_NOTIFY_COMMAND must not prevent startup:
            # keep the engine usable with the desktop channel unavailable.
            self._command_argv = []
            command_error = f"invalid WORKBENCH_NOTIFY_COMMAND: {exc}"
        self._desktop = _DesktopState(
            available=self._command_available(self._command_argv),
            command=" ".join(self._command_argv) if self._command_argv else "",
            last_error=command_error,
        )

    # -- notification command --------------------------------------------

    @staticmethod
    def _resolve_command(command: Optional[Sequence[str] | str]) -> list[str]:
        if command is None:
            return ["notify-send"]
        if isinstance(command, str):
            return shlex.split(command)
        return [str(part) for part in command]

    @staticmethod
    def _command_available(argv: list[str]) -> bool:
        if not argv:
            return False
        # ``shutil.which`` also checks direct paths with a slash.
        return shutil.which(argv[0]) is not None

    def _notify_argv(self, summary: str, body: str, urgency: str) -> list[str]:
        return [
            *self._command_argv,
            f"--app-name={self._app_name}",
            f"--urgency={urgency}",
            summary,
            body,
        ]

    def _send(self, kind: str, summary: str, body: str, urgency: str) -> bool:
        """Best-effort desktop notification; failures land in the snapshot."""
        if not self._command_argv:
            if self._desktop.last_error is None:
                self._desktop.last_error = "notification command unavailable"
            return False
        now_float = float(self._clock())
        if (
            self._channel_retry_after is not None
            and now_float < self._channel_retry_after
        ):
            return False
        now = int(now_float)
        try:
            completed = subprocess.run(
                self._notify_argv(summary, body, urgency),
                capture_output=True,
                text=True,
                timeout=self._notify_timeout,
                check=False,
            )
        except Exception as exc:  # missing binary, timeout, permission, ...
            self._desktop.last_error = f"{exc.__class__.__name__}: {exc}"
            self._channel_retry_after = now_float + NOTIFY_RETRY_SECONDS
            return False
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            self._desktop.last_error = detail or f"exit {completed.returncode}"
            self._channel_retry_after = now_float + NOTIFY_RETRY_SECONDS
            return False
        self._desktop.last_error = None
        self._desktop.last_sent_at = now
        self._channel_retry_after = None
        self._desktop.last_kind = kind
        self._desktop.last_urgency = urgency
        return True

    # -- classification ----------------------------------------------------

    @staticmethod
    def _event_id(card: dict[str, Any]) -> str:
        task_id = card["id"]
        if card["status"] == REVIEW:
            stamp = _as_int(card.get("review_requested_at")) or 0
            return f"n:{task_id}:review:{stamp}"
        kind = card.get("block_kind") or "none"
        recurrences = _as_int(card.get("block_recurrences")) or 0
        return f"n:{task_id}:blocked:{kind}:{recurrences}"

    @staticmethod
    def _normalize_card(raw: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        task_id = raw.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            return None
        status = raw.get("status")
        if status not in (BLOCKED, REVIEW):
            return None
        block_kind = raw.get("block_kind")
        if not isinstance(block_kind, str) or not block_kind.strip():
            block_kind = None
        else:
            block_kind = block_kind.strip()
        parent_ids = raw.get("parent_ids")
        if not isinstance(parent_ids, list):
            links = raw.get("links")
            parent_ids = links.get("parents") if isinstance(links, dict) else None
        parents: list[str] = []
        if isinstance(parent_ids, list):
            for value in parent_ids:
                if isinstance(value, str) and value and value not in parents:
                    parents.append(value)
        title = raw.get("title")
        return {
            "id": task_id.strip(),
            "status": status,
            "title": title.strip() if isinstance(title, str) and title.strip() else task_id,
            "block_kind": block_kind,
            "block_recurrences": _as_int(raw.get("block_recurrences")) or 0,
            "review_requested_at": _as_int(raw.get("review_requested_at")),
            "parent_ids": tuple(parents),
            "unfinished_children": _unfinished_children(raw),
        }

    @staticmethod
    def _reason(event: _Event) -> str:
        if event.kind == REVIEW:
            return "待验收（有未完成子卡）" if event.unfinished_children else "待验收"
        if event.block_kind == "needs_input":
            return "worker 在等你回答"
        if event.block_kind == "capability":
            return "能力阻塞，等你处理"
        if event.unfinished_children:
            return "有未完成子卡，下游被堵"
        if event.block_kind:
            return f"阻塞（{event.block_kind}）"
        return "阻塞，等你处理"

    # -- collection --------------------------------------------------------

    def collect(self, board_snapshot: dict[str, Any]) -> dict[str, Any]:
        """Recompute state from a board snapshot and dispatch notifications.

        The caller supplies a successful ``board_snapshot`` only; an upstream
        failure must keep the previous snapshot and is handled in ``server.py``.
        """
        tasks = board_snapshot.get("tasks") if isinstance(board_snapshot, dict) else None
        if not isinstance(tasks, list):
            raise ValueError("board snapshot tasks must be a list")
        review_ttl = _as_int(board_snapshot.get("review_ttl_seconds"))
        if review_ttl is not None and review_ttl > 0:
            self._review_ttl_seconds = int(review_ttl)
        with self._lock:
            now = float(self._clock())
            candidates: dict[str, dict[str, Any]] = {}
            for raw in tasks:
                card = self._normalize_card(raw)
                if card is None:
                    continue
                event_id = self._event_id(card)
                if event_id not in candidates:
                    candidates[event_id] = card
            # Leaving the candidate set resolves the event; a later reappearance
            # creates a fresh event (and therefore re-notifies).
            for event_id in list(self._events):
                if event_id not in candidates:
                    del self._events[event_id]

            for event_id, card in candidates.items():
                level = _classify(
                    card["status"], card["block_kind"], card["unfinished_children"]
                )
                if level is None:
                    continue
                state = self._events.get(event_id)
                if state is None:
                    self._events[event_id] = _Event(
                        id=event_id,
                        task_id=card["id"],
                        kind=card["status"],
                        status=card["status"],
                        title=card["title"],
                        level=level,
                        block_kind=card["block_kind"],
                        block_recurrences=card["block_recurrences"],
                        review_requested_at=card["review_requested_at"],
                        parent_ids=card["parent_ids"],
                        unfinished_children=card["unfinished_children"],
                        first_seen_at=now,
                    )
                    continue
                state.title = card["title"]
                state.block_kind = card["block_kind"]
                state.block_recurrences = card["block_recurrences"]
                state.review_requested_at = card["review_requested_at"]
                state.parent_ids = card["parent_ids"]
                state.unfinished_children = card["unfinished_children"]
                # An escalated P1 stays P0 for the rest of this event's life.
                state.level = P0 if state.escalated and level == P1 else level

            self._dispatch(now)
            return self._build_snapshot(now)

    def _dispatch(self, now: float) -> None:
        events = list(self._events.values())
        for event in events:
            if event.snooze_until is not None and now >= event.snooze_until:
                event.snooze_until = None
                event.reminder_due = True

        def retrying(event: _Event) -> bool:
            return event.retry_after is not None and now < event.retry_after

        ordered = sorted(events, key=lambda event: (event.first_seen_at, event.id))

        # P1 that has waited out the escalation window is promoted to P0 in one
        # group push: this is the "30 minutes" half of the P1 batching rule and
        # the only escalation the event will ever get. Nothing is promoted and
        # no batch is sent unless the group push actually succeeded.
        due_p1 = [
            event
            for event in ordered
            if event.level == P1
            and event.snooze_until is None
            and not event.escalated
            and not retrying(event)
            and now - event.first_seen_at >= self._escalate_seconds
        ]
        batch_blocked = False
        if due_p1:
            if self._notify_escalation(due_p1):
                for event in due_p1:
                    event.level = P0
                    event.escalated = True
                    event.notified_at = now
                    event.reminder_due = False
                    event.retry_after = None
            else:
                for event in due_p1:
                    event.retry_after = now + NOTIFY_RETRY_SECONDS
                batch_blocked = True

        # P0: immediate first push, then at most one reminder/escalation.
        for event in ordered:
            if (
                event.level != P0
                or event.snooze_until is not None
                or retrying(event)
            ):
                continue
            if event.notified_at is None:
                if self._notify_event(event):
                    event.notified_at = now
                    event.retry_after = None
                else:
                    event.retry_after = now + NOTIFY_RETRY_SECONDS
                continue
            if event.reminder_due:
                # Snooze expiry: the human asked for this ping. If the
                # 30-minute window has also passed, this is the one
                # re-reminder instead of a second one.
                due = (
                    not event.escalated
                    and now - event.first_seen_at >= self._escalate_seconds
                )
                if self._notify_event(event, reminder=True):
                    if due:
                        event.escalated = True
                    event.notified_at = now
                    event.reminder_due = False
                    event.retry_after = None
                else:
                    event.retry_after = now + NOTIFY_RETRY_SECONDS
                continue
            if (
                not event.escalated
                and now - event.first_seen_at >= self._escalate_seconds
            ):
                # P0 has no higher level: the 30-minute escalation is one
                # re-reminder, never a second "升级" copy.
                if self._notify_event(event, reminder=True):
                    event.notified_at = now
                    event.escalated = True
                    event.retry_after = None
                else:
                    event.retry_after = now + NOTIFY_RETRY_SECONDS

        # P1: batch the rest (3 items or batch_seconds since the oldest).
        pending = [
            event
            for event in ordered
            if event.level == P1
            and event.snooze_until is None
            and event.notified_at is None
            and not event.reminder_due
            and not retrying(event)
        ]
        if pending and not batch_blocked:
            oldest = min(event.first_seen_at for event in pending)
            if len(pending) >= self._batch_size or now - oldest >= self._batch_seconds:
                if self._notify_batch(pending):
                    for event in pending:
                        event.notified_at = now
                        event.retry_after = None
                else:
                    for event in pending:
                        event.retry_after = now + NOTIFY_RETRY_SECONDS

        # P1 snooze expiry before the escalation window: one requested reminder
        # at the item's own level (normal urgency, never critical).
        for event in ordered:
            if (
                event.level == P1
                and event.snooze_until is None
                and event.reminder_due
                and not retrying(event)
            ):
                if self._notify_event(event, reminder=True):
                    event.notified_at = now
                    event.reminder_due = False
                    event.retry_after = None
                else:
                    event.retry_after = now + NOTIFY_RETRY_SECONDS

        # P2: one digest line per local day, no earlier than daily_hour.
        p2_events = [event for event in ordered if event.level == P2]
        if p2_events:
            local = datetime.fromtimestamp(now)
            today = local.date().isoformat()
            daily_retrying = (
                self._daily_retry_after is not None
                and now < self._daily_retry_after
            )
            if (
                local.hour >= self._daily_hour
                and self._last_daily_date != today
                and not daily_retrying
            ):
                line = self._daily_line(p2_events)
                if self._send(
                    P2,
                    f"P2 待验收 {len(p2_events)} 张",
                    line,
                    urgency="low",
                ):
                    self._last_daily_date = today
                    self._last_daily_line = line
                    self._last_daily_count = len(p2_events)
                    self._daily_retry_after = None
                else:
                    self._daily_retry_after = now + NOTIFY_RETRY_SECONDS

    def _notify_event(self, event: _Event, *, reminder: bool = False) -> bool:
        reason = self._reason(event)
        if event.level == P1:
            # A user-requested reminder must not borrow P0's critical copy.
            summary = f"P1 再提醒 · {event.title}" if reminder else f"P1 · {event.title}"
            body = f"{event.task_id} · {reason}"
            return self._send(
                P1, summary, _truncate(body, MAX_BODY_CHARS), urgency="normal"
            )
        if reminder:
            summary = f"P0 再提醒 · {event.title}"
            body = f"{event.task_id} · {reason}"
        else:
            summary = f"P0 需要你 · {event.title}"
            body = f"{event.task_id} · {reason}"
        return self._send(
            P0, summary, _truncate(body, MAX_BODY_CHARS), urgency="critical"
        )

    def _notify_escalation(self, events: list[_Event]) -> bool:
        """One group push for P1 events that hit the escalation window."""
        if len(events) == 1:
            event = events[0]
            return self._send(
                P0,
                f"P0 升级 · {event.title}",
                _truncate(
                    f"{event.task_id} · 30 分钟未处理，{self._reason(event)}",
                    MAX_BODY_CHARS,
                ),
                urgency="critical",
            )
        titles = "、".join(event.title for event in events)
        body = _truncate(f"{len(events)} 张卡：{titles}", MAX_BODY_CHARS)
        return self._send(
            P0,
            f"P0 升级 · {len(events)} 张卡",
            body,
            urgency="critical",
        )

    def _notify_batch(self, events: list[_Event]) -> bool:
        titles = "、".join(f"{event.title}" for event in events)
        body = _truncate(f"{len(events)} 张卡：{titles}", MAX_BODY_CHARS)
        return self._send(
            P1, f"P1 · {len(events)} 张卡在等你", body, urgency="normal"
        )

    @staticmethod
    def _daily_line(events: list[_Event]) -> str:
        titles = "、".join(event.title for event in events)
        return _truncate(f"P2 待验收 {len(events)} 张：{titles}", MAX_DAILY_LINE_CHARS)

    # -- snapshot ----------------------------------------------------------

    def _item(self, event: _Event, now: float) -> dict[str, Any]:
        auto_collect_in: Optional[int] = None
        if event.kind == REVIEW and event.review_requested_at is not None:
            review_age = max(0, int(now - event.review_requested_at))
            auto_collect_in = max(0, self._review_ttl_seconds - review_age)
        if event.kind == REVIEW:
            actions = ["accept", "reject"]
            if event.level == P0:
                actions.append("remind")
        else:
            actions = ["remind"] if event.level in (P0, P1) else []
        return {
            "id": event.id,
            "task_id": event.task_id,
            "kind": event.kind,
            "level": event.level,
            "title": event.title,
            "status": event.status,
            "reason": self._reason(event),
            "block_kind": event.block_kind,
            "block_recurrences": event.block_recurrences,
            "review_requested_at": event.review_requested_at,
            "auto_collect_in_seconds": auto_collect_in,
            "parent_ids": list(event.parent_ids),
            "auto_collect_due": auto_collect_in == 0,
            "first_seen_at": int(event.first_seen_at),
            "age_seconds": max(0, int(now - event.first_seen_at)),
            "notified_at": int(event.notified_at) if event.notified_at is not None else None,
            "escalated": event.escalated,
            "snooze_until": int(event.snooze_until) if event.snooze_until is not None else None,
            "actions": actions,
            "slot": None,
            "bundle_key": None,
        }

    def _bundles(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            if item["kind"] != REVIEW:
                continue
            parents = sorted(
                {
                    value
                    for value in (item.get("parent_ids") or [])
                    if isinstance(value, str) and value
                }
            )
            key = "parent:" + ",".join(parents) if parents else f"task:{item['task_id']}"
            groups.setdefault(key, []).append(item)
        bundles: list[dict[str, Any]] = []
        for key, cards in groups.items():
            cards.sort(
                key=lambda card: (
                    card["review_requested_at"] is None,
                    card["review_requested_at"] or card["first_seen_at"],
                    card["task_id"],
                )
            )
            parent_ids = sorted(
                {
                    value
                    for card in cards
                    for value in (card.get("parent_ids") or [])
                    if isinstance(value, str) and value
                }
            )
            bundle = {
                "key": key,
                "parent_ids": parent_ids,
                "count": len(cards),
                "task_ids": [card["task_id"] for card in cards],
                "cards": cards,
                "oldest_review_at": next(
                    (
                        card["review_requested_at"]
                        for card in cards
                        if card["review_requested_at"] is not None
                    ),
                    None,
                ),
            }
            bundles.append(bundle)
        bundles.sort(
            key=lambda bundle: (
                bundle["oldest_review_at"] is None,
                bundle["oldest_review_at"] or 0,
                bundle["key"],
            )
        )
        active = bundles[:SLOT_CAPACITY]
        queued = bundles[SLOT_CAPACITY:]
        for bundle in active:
            for card in bundle["cards"]:
                card["slot"] = "active"
                card["bundle_key"] = bundle["key"]
        for bundle in queued:
            for card in bundle["cards"]:
                card["slot"] = "queued"
                card["bundle_key"] = bundle["key"]
        return active, queued

    def _build_snapshot(self, now: float) -> dict[str, Any]:
        items = [self._item(event, now) for event in self._events.values()]
        review_items = [item for item in items if item["kind"] == REVIEW]
        active, queued = self._bundles(review_items)
        levels = {level: [] for level in LEVELS}
        for item in items:
            levels.setdefault(item["level"], []).append(item)
        for level_items in levels.values():
            level_items.sort(key=lambda item: (item["first_seen_at"], item["id"]))
        local = datetime.fromtimestamp(now)
        today = local.date().isoformat()
        p2_items = levels.get(P2, [])
        daily_sent = self._last_daily_date == today
        daily = {
            "date": today,
            "sent": daily_sent,
            "count": self._last_daily_count if daily_sent else len(p2_items),
            "line": (
                self._last_daily_line
                if daily_sent
                else self._daily_line(
                    [event for event in self._events.values() if event.level == P2]
                )
                if p2_items
                else ""
            ),
            "hour": self._daily_hour,
        }
        return {
            "ok": True,
            "generated_at": int(now),
            "now": int(now),
            "counts": {
                P0: len(levels[P0]),
                P1: len(levels[P1]),
                P2: len(levels[P2]),
                "total": len(items),
            },
            P0: levels[P0],
            P1: levels[P1],
            P2: levels[P2],
            "review_ttl_seconds": self._review_ttl_seconds,
            "slots": {
                "capacity": SLOT_CAPACITY,
                "active": active,
                "queued": queued,
                "queued_count": len(queued),
                "queued_cards": sum(bundle["count"] for bundle in queued),
            },
            "daily": daily,
            "desktop": {
                "available": self._desktop.available,
                "command": self._desktop.command,
                "last_error": self._desktop.last_error,
                "last_sent_at": self._desktop.last_sent_at,
                "last_kind": self._desktop.last_kind,
                "last_urgency": self._desktop.last_urgency,
            },
            "sweep": {
                "last_at": self._sweep_last_at,
                "last_error": self._sweep_last_error,
            },
            "channels": {
                "desktop": self._desktop.command,
                "mobile": "reserved",
            },
            "thresholds": {
                "batch_size": self._batch_size,
                "batch_seconds": int(self._batch_seconds),
                "escalate_seconds": int(self._escalate_seconds),
                "snooze_seconds": int(self._snooze_seconds),
                "daily_hour": self._daily_hour,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """Pure read; never talks to upstream and never notifies."""
        with self._lock:
            now = float(self._clock())
            return self._build_snapshot(now)

    def record_sweep(self, error: Optional[str] = None) -> None:
        """Record one collection attempt so the UI can show freshness/errors."""
        with self._lock:
            self._sweep_last_at = int(self._clock())
            self._sweep_last_error = error

    # -- actions -----------------------------------------------------------

    def action(
        self, event_id: str, action: Any, reason: Any = None
    ) -> tuple[int, dict[str, Any]]:
        """Apply a notification action.

        ``accept`` / ``reject`` delegate to the injected 2.1 ``verify``; a
        failure keeps the notification so the human can retry. ``remind``
        snoozes P0/P1 for the configured window.
        """
        if not isinstance(event_id, str) or not event_id:
            return 400, {"ok": False, "error": "invalid_request"}
        with self._lock:
            event = self._events.get(event_id)
            if event is None:
                return 404, {"ok": False, "error": "not_found"}
            if action == "remind":
                if event.level not in (P0, P1):
                    return 400, {"ok": False, "error": "invalid_action"}
                now = float(self._clock())
                event.snooze_until = now + self._snooze_seconds
                event.reminder_due = False
                return 200, {
                    "ok": True,
                    "id": event.id,
                    "action": "remind",
                    "snooze_until": int(event.snooze_until),
                }
            if action in ("accept", "reject"):
                if event.kind != REVIEW or self._verify is None:
                    return 400, {"ok": False, "error": "invalid_action"}
                status_code, body = self._verify(
                    event.task_id,
                    action,
                    reason,
                    event.review_requested_at,
                )
                if status_code == 200:
                    self._events.pop(event.id, None)
                return status_code, body
            return 400, {"ok": False, "error": "invalid_action"}


class NotificationPoller:
    """Daemon thread that runs a full sweep callable every ``interval_seconds``.

    The callable owns the board read + ``engine.collect`` pairing (and the
    server-side sweep/action lock), so the poller never applies a snapshot
    twice. Upstream failures leave the previous snapshot untouched.
    """

    def __init__(
        self,
        sweep: Callable[[], Any],
        interval_seconds: float,
        *,
        log: Callable[[str], None] = lambda message: None,
    ) -> None:
        self._sweep = sweep
        self._interval = max(0.05, float(interval_seconds))
        self._log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="workbench-notification-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def sweep_once(self) -> Any:
        result = self._sweep()
        if isinstance(result, tuple) and len(result) == 2:
            status, body = result
            if status != 200:
                error = body.get("error") if isinstance(body, dict) else body
                self._log(f"notification sweep failed: HTTP {status} ({error})")
        return result

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.sweep_once()
            except Exception as exc:  # a daemon thread must never die silently
                self._log(f"notification sweep failed: {exc}")
