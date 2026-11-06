#!/usr/bin/env python3
"""Repeatable integration tests for Story 3.3 (scheduled wake).

Covers the workbench-owned wake path end to end:

* due card with acceptance and no parent -> ``ready`` + ``contract_defaults``
* due card with an unfinished parent -> ``todo``
* due card without acceptance -> stays ``scheduled`` with ``wake_error`` and
  a five-minute ``wake_next_try_at`` backoff
* manual ``POST /api/workbench/wake/{id}`` (ignores the scheduler backoff)
* native Hermes ``scheduled`` cards without ``wake_at`` are never touched
* invalid ``wake_at`` values are rejected by metadata PATCH
* upstream 502 keeps ``wake_at`` and backs off for 60s
* the background scheduler converts due cards without a manual call

Like the other suites, the kanban + workbench DBs are throwaway temp files
(``test_dispatch`` owns the harness), so nothing touches the real databases.

Run with::

    python3 workbench-app/tests/test_wake.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from pathlib import Path
from urllib import parse as urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch as base  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as workbench_server  # noqa: E402


@contextlib.contextmanager
def upstream_with_failing_writes(get_payload: bytes):
    """Loopback upstream: reads succeed, every PATCH/POST returns 502."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, code: int, raw: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            self._respond(200, get_payload)

        def do_PATCH(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._respond(502, b"{}")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._respond(502, b"{}")

        def log_message(self, *args) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.1, label: str = "condition"):
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - report the last probe failure
            last_error = exc
        time.sleep(interval)
    detail = f" ({last_error})" if last_error is not None else ""
    raise AssertionError(f"timeout waiting for {label}{detail}")


class WakeBackendTestCase(unittest.TestCase):
    """Class-level server with the background scheduler disabled."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-wake-"))
        defaults = cls.tmp / "defaults.json"
        defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
        cls.server = base.WorkbenchServer(
            cls.tmp,
            defaults,
            env_extra={"WORKBENCH_WAKE_SWEEP_SECONDS": "0"},
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def create_store_card(self, title: str, accept: str | None = None) -> str:
        code, body = base.http_json(
            "POST",
            self.server.base + "/api/workbench/tasks",
            {"title": title, "bucket": "today"},
        )
        self.assertEqual(code, 200, body)
        task_id = body["task"]["id"]
        if accept is not None:
            code, body = self.patch_metadata(task_id, {"contract": {"accept": accept}})
            self.assertEqual(code, 200, body)
        return task_id

    def patch_metadata(self, task_id: str, patch: dict):
        return base.http_json(
            "PATCH",
            self.server.base
            + "/api/workbench/metadata/"
            + urlparse.quote(task_id, safe=""),
            patch,
        )

    def wake(self, task_id: str, timeout: float = 10.0):
        return base.http_json(
            "POST",
            self.server.base
            + "/api/workbench/wake/"
            + urlparse.quote(task_id, safe=""),
            {},
            timeout=timeout,
        )

    def metadata(self, task_id: str) -> dict:
        code, rows = self.server.get_metadata([task_id])
        self.assertEqual(code, 200)
        return rows.get(task_id, {})

    def status(self, task_id: str) -> str:
        return self.server.task(task_id)["status"]

    # -- due / manual semantics -------------------------------------------

    def test_manual_wake_ready_clears_fields_and_snapshots(self):
        task_id = self.create_store_card("wake-ready", accept="跑通测试")
        previous = int(time.time())
        code, body = self.patch_metadata(task_id, {"wake_at": previous + 3600})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["metadata"]["wake_at"], previous + 3600)

        code, body = self.wake(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "ready")
        self.assertIsInstance(body.get("position"), int)
        self.assertEqual(self.status(task_id), "ready")

        metadata = self.metadata(task_id)
        self.assertNotIn("wake_at", metadata)
        self.assertNotIn("wake_error", metadata)
        self.assertNotIn("wake_next_try_at", metadata)
        self.assertIn("contract_defaults", metadata)
        self.assertEqual(metadata["contract"]["accept"], "跑通测试")

    def test_manual_wake_with_open_parent_lands_todo(self):
        parent = self.server.create_task("wake-parent", triage=True)
        child = self.server.create_task("wake-child", triage=True, parents=[parent])
        self.server.set_kanban_status(child, "scheduled")

        code, body = self.patch_metadata(
            child,
            {
                "bucket": "today",
                "contract": {"accept": "parent 完成后执行"},
                "wake_at": int(time.time()) + 3600,
            },
        )
        self.assertEqual(code, 200, body)

        code, body = self.wake(child)
        self.assertEqual(code, 200, body)
        self.assertEqual(body, {"ok": True, "status": "todo"})
        self.assertEqual(self.status(child), "todo")

        metadata = self.metadata(child)
        self.assertNotIn("wake_at", metadata)
        self.assertNotIn("wake_error", metadata)
        self.assertNotIn("wake_next_try_at", metadata)
        # A parent-blocked card is not dispatched, so it has no snapshot.
        self.assertNotIn("contract_defaults", metadata)

    def test_missing_acceptance_stays_scheduled_and_backs_off(self):
        task_id = self.create_store_card("wake-no-accept")
        now = int(time.time())
        code, body = self.patch_metadata(task_id, {"wake_at": now - 1})
        self.assertEqual(code, 200, body)

        code, body = self.wake(task_id)
        self.assertEqual(code, 400, body)
        self.assertEqual(body, {"ok": False, "error": "missing_acceptance"})
        self.assertEqual(self.status(task_id), "scheduled")

        metadata = self.metadata(task_id)
        self.assertEqual(metadata["wake_at"], now - 1)
        self.assertEqual(metadata["wake_error"], "missing_acceptance")
        self.assertGreaterEqual(metadata["wake_next_try_at"], now + 290)
        self.assertLessEqual(metadata["wake_next_try_at"], int(time.time()) + 310)
        self.assertNotIn("contract_defaults", metadata)

        # Manual wake is not limited by wake_next_try_at: adding acceptance
        # makes the same endpoint succeed immediately.
        code, body = self.patch_metadata(
            task_id, {"contract": {"accept": "补上验收后可以走"}}
        )
        self.assertEqual(code, 200, body)
        code, body = self.wake(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.status(task_id), "ready")
        self.assertNotIn("wake_at", self.metadata(task_id))

    def test_new_wake_at_clears_previous_failure(self):
        task_id = self.create_store_card("wake-reset-error")
        code, body = self.patch_metadata(task_id, {"wake_at": int(time.time()) - 1})
        self.assertEqual(code, 200, body)
        code, body = self.wake(task_id)
        self.assertEqual(code, 400, body)
        self.assertEqual(self.metadata(task_id)["wake_error"], "missing_acceptance")

        code, body = self.patch_metadata(
            task_id, {"wake_at": int(time.time()) + 3600}
        )
        self.assertEqual(code, 200, body)
        metadata = body["metadata"]
        self.assertNotIn("wake_error", metadata)
        self.assertNotIn("wake_next_try_at", metadata)
        self.assertGreater(metadata["wake_at"], int(time.time()))

    def test_wake_outside_store_409_and_clears_stale_fields(self):
        task_id = self.create_store_card("wake-left-store", accept="已经派发")
        code, body = self.server.dispatch(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.status(task_id), "ready")

        # A late metadata write can still schedule a card that already left the
        # repository; wake must report not_in_store and drop the stale keys.
        code, body = self.patch_metadata(
            task_id, {"wake_at": int(time.time()) + 3600}
        )
        self.assertEqual(code, 200, body)

        code, body = self.wake(task_id)
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "not_in_store")
        self.assertEqual(body["status"], "ready")
        self.assertNotIn("wake_at", self.metadata(task_id))

    def test_wake_missing_task_is_404(self):
        code, body = self.wake("t_orphan")
        self.assertEqual(code, 404, body)
        self.assertEqual(body, {"ok": False, "error": "not_found"})

    def test_manual_wake_without_wake_at_still_dispatches(self):
        # The endpoint is also the "wake now" superset: a scheduled card that
        # never had a schedule can be pushed back into the pipeline manually.
        task_id = self.create_store_card("wake-now-no-schedule", accept="立即执行")
        self.assertEqual(self.status(task_id), "scheduled")
        code, body = self.wake(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(self.status(task_id), "ready")
        self.assertNotIn("wake_at", self.metadata(task_id))

    def test_manual_wake_from_todo_without_wake_at(self):
        # Legacy repository cards can sit in todo without a wake_at; manual
        # wake must still run the normal dispatch gate from there.
        task_id = self.server.create_task("wake-now-todo", triage=True)
        self.server.set_kanban_status(task_id, "todo")
        code, body = self.patch_metadata(
            task_id,
            {"bucket": "today", "contract": {"accept": "从 todo 手动唤醒"}},
        )
        self.assertEqual(code, 200, body)

        code, body = self.wake(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(self.status(task_id), "ready")

    def test_dispatch_success_clears_wake_and_409_keeps_it(self):
        # A manual drag-dispatch that lands in ready drops the schedule...
        ready_card = self.create_store_card("wake-dispatch-ok", accept="手动派发")
        wake_at = int(time.time()) + 3600
        code, body = self.patch_metadata(ready_card, {"wake_at": wake_at})
        self.assertEqual(code, 200, body)
        code, body = self.server.dispatch(ready_card)
        self.assertEqual(code, 200, body)
        metadata = self.metadata(ready_card)
        self.assertNotIn("wake_at", metadata)
        self.assertNotIn("wake_error", metadata)

        # ...while a refused dispatch (here: a terminal card) keeps it, so the
        # scheduler can still act once the card is valid again.
        done_card = self.create_store_card("wake-dispatch-409", accept="终态不派发")
        self.server.set_kanban_status(done_card, "done")
        code, body = self.patch_metadata(done_card, {"wake_at": wake_at})
        self.assertEqual(code, 200, body)
        code, body = self.server.dispatch(done_card)
        self.assertEqual(code, 409, body)
        self.assertEqual(self.metadata(done_card)["wake_at"], wake_at)

    def test_invalid_wake_at_is_400_and_not_written(self):
        task_id = self.create_store_card("wake-invalid", accept="ok")
        before = self.metadata(task_id)
        invalid = [
            {"wake_at": "tomorrow"},
            {"wake_at": True},
            {"wake_at": -1},
            {"wake_at": workbench_server.MAX_WAKE_AT + 1},
            {"wake_at": 1.5},
            {"wake_at": [1]},
            {"wake_error": "client-owned"},
            {"wake_next_try_at": 123},
        ]
        for patch in invalid:
            with self.subTest(patch=patch):
                code, body = self.patch_metadata(task_id, patch)
                self.assertEqual(code, 400, body)
                self.assertEqual(body, {"ok": False, "error": "invalid_metadata"})
                self.assertEqual(self.metadata(task_id), before)

        # null is the explicit clear form, not an invalid number.
        code, body = self.patch_metadata(task_id, {"wake_at": None})
        self.assertEqual(code, 200, body)
        self.assertNotIn("wake_at", body["metadata"])


class WakeSchedulerTests(unittest.TestCase):
    """A fast background scheduler (0.2s) exercises the due scan."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-wake-sched-"))
        defaults = cls.tmp / "defaults.json"
        defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
        cls.server = base.WorkbenchServer(
            cls.tmp,
            defaults,
            env_extra={"WORKBENCH_WAKE_SWEEP_SECONDS": "0.2"},
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def create_store_card(self, title: str, accept: str | None = None) -> str:
        code, body = base.http_json(
            "POST",
            self.server.base + "/api/workbench/tasks",
            {"title": title, "bucket": "today"},
        )
        self.assertEqual(code, 200, body)
        task_id = body["task"]["id"]
        if accept is not None:
            code, body = base.http_json(
                "PATCH",
                self.server.base
                + "/api/workbench/metadata/"
                + urlparse.quote(task_id, safe=""),
                {"contract": {"accept": accept}},
            )
            self.assertEqual(code, 200, body)
        return task_id

    def patch_wake(self, task_id: str, wake_at: int) -> dict:
        code, body = base.http_json(
            "PATCH",
            self.server.base
            + "/api/workbench/metadata/"
            + urlparse.quote(task_id, safe=""),
            {"wake_at": wake_at},
        )
        self.assertEqual(code, 200, body)
        return body["metadata"]

    def metadata(self, task_id: str) -> dict:
        code, rows = self.server.get_metadata([task_id])
        self.assertEqual(code, 200)
        return rows.get(task_id, {})

    def test_due_card_wakes_without_manual_call(self):
        task_id = self.create_store_card("sched-due", accept="到点自动跑")
        self.patch_wake(task_id, int(time.time()) - 1)

        wait_until(
            lambda: self.server.status(task_id) == "ready",
            timeout=5.0,
            label="scheduler dispatch to ready",
        )
        # Ready becomes visible inside _dispatch_locked; the scheduler clears
        # the schedule immediately after, so wait for that follow-up write.
        wait_until(
            lambda: "wake_at" not in self.metadata(task_id),
            timeout=5.0,
            label="scheduler clears wake fields",
        )
        metadata = self.metadata(task_id)
        self.assertNotIn("wake_error", metadata)
        self.assertNotIn("wake_next_try_at", metadata)
        self.assertIn("contract_defaults", metadata)

    def test_native_scheduled_without_wake_at_is_untouched(self):
        task_id = self.create_store_card("sched-native", accept="不应被扫描")
        before = self.metadata(task_id)
        time.sleep(0.8)
        self.assertEqual(self.server.status(task_id), "scheduled")
        self.assertEqual(self.metadata(task_id), before)
        self.assertNotIn("wake_error", self.metadata(task_id))

    def test_future_wake_at_is_not_woken_early(self):
        task_id = self.create_store_card("sched-future", accept="还没到点")
        wake_at = int(time.time()) + 3600
        self.patch_wake(task_id, wake_at)
        time.sleep(0.8)
        self.assertEqual(self.server.status(task_id), "scheduled")
        self.assertEqual(self.metadata(task_id)["wake_at"], wake_at)
        self.assertNotIn("wake_error", self.metadata(task_id))

    def test_acceptance_added_during_backoff_is_picked_up(self):
        # A missing-acceptance card stays scheduled with a 5-minute backoff;
        # adding acceptance clears the backoff keys, so the next 0.2s tick can
        # wake it without a manual call.
        task_id = self.create_store_card("sched-retry")
        self.patch_wake(task_id, int(time.time()) - 1)
        wait_until(
            lambda: self.metadata(task_id).get("wake_error") == "missing_acceptance",
            timeout=5.0,
            label="missing_acceptance before retry",
        )

        code, body = base.http_json(
            "PATCH",
            self.server.base
            + "/api/workbench/metadata/"
            + urlparse.quote(task_id, safe=""),
            {"contract": {"accept": "补验收后自动重试"}},
        )
        self.assertEqual(code, 200, body)
        wait_until(
            lambda: self.server.status(task_id) == "ready",
            timeout=5.0,
            label="scheduler retry after acceptance",
        )
        wait_until(
            lambda: "wake_at" not in self.metadata(task_id),
            timeout=5.0,
            label="scheduler retry clears wake fields",
        )

    def test_due_missing_acceptance_backs_off_and_does_not_dispatch(self):
        task_id = self.create_store_card("sched-no-accept")
        self.patch_wake(task_id, int(time.time()) - 1)

        metadata = wait_until(
            lambda: (
                self.metadata(task_id)
                if self.metadata(task_id).get("wake_error") == "missing_acceptance"
                else None
            ),
            timeout=5.0,
            label="missing_acceptance backoff",
        )
        next_try = metadata["wake_next_try_at"]
        self.assertEqual(self.server.status(task_id), "scheduled")
        self.assertNotIn("contract_defaults", metadata)

        # Inside the five-minute backoff the scheduler must not rewrite the
        # retry stamp (a second attempt would move it forward).
        time.sleep(0.7)
        self.assertEqual(self.metadata(task_id)["wake_next_try_at"], next_try)

        # Adding acceptance + a manual wake is the immediate recovery path.
        code, body = base.http_json(
            "PATCH",
            self.server.base
            + "/api/workbench/metadata/"
            + urlparse.quote(task_id, safe=""),
            {"contract": {"accept": "补上后手动唤醒"}},
        )
        self.assertEqual(code, 200, body)
        code, body = base.http_json(
            "POST",
            self.server.base
            + "/api/workbench/wake/"
            + urlparse.quote(task_id, safe=""),
            {},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(task_id), "ready")
        self.assertNotIn("wake_at", self.metadata(task_id))


class WakeUpstreamFailureTests(unittest.TestCase):
    """A loopback fake upstream returns fresh reads but fails writes with 502."""

    def _run_failure(self, task_payload: dict) -> tuple[int, dict, dict]:
        payload = json.dumps({"task": task_payload}).encode("utf-8")
        with upstream_with_failing_writes(payload) as upstream:
            root = Path(tempfile.mkdtemp(prefix="wb-wake-502-"))
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            server = base.WorkbenchServer(
                root,
                defaults,
                env_extra={
                    "WORKBENCH_UPSTREAM_BASE": upstream,
                    "WORKBENCH_WAKE_SWEEP_SECONDS": "0",
                },
            ).start()
            try:
                server.seed_metadata(
                    "t_fake",
                    {
                        "bucket": "today",
                        "contract": {"accept": "跑通测试"},
                        "wake_at": int(time.time()) - 1,
                    },
                )
                code, body = base.http_json(
                    "POST", server.base + "/api/workbench/wake/t_fake", {}
                )
                return code, body, server.metadata_row("t_fake") or {}
            finally:
                server.stop()
                shutil.rmtree(root, ignore_errors=True)

    def test_dispatch_502_keeps_wake_at_and_sets_60s_backoff(self):
        code, body, metadata = self._run_failure(
            {"id": "t_fake", "status": "scheduled"}
        )
        now = int(time.time())
        self.assertEqual(code, 502, body)
        self.assertEqual(body, {"ok": False, "error": "upstream_unavailable"})
        self.assertIsInstance(metadata["wake_at"], int)
        self.assertLessEqual(metadata["wake_at"], now)
        self.assertEqual(metadata["wake_error"], "upstream_unavailable")
        self.assertGreaterEqual(metadata["wake_next_try_at"], now + 50)
        self.assertLessEqual(metadata["wake_next_try_at"], now + 70)
        self.assertNotIn("contract_defaults", metadata)

    def test_parent_patch_502_keeps_wake_at_and_sets_backoff(self):
        payload = {
            "id": "t_fake",
            "status": "scheduled",
            "links": {"parents": ["p_open"]},
            "link_tasks": [{"id": "p_open", "status": "triage"}],
        }
        code, body, metadata = self._run_failure(payload)
        now = int(time.time())
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertIsInstance(metadata["wake_at"], int)
        self.assertLessEqual(metadata["wake_at"], now)
        self.assertEqual(metadata["wake_error"], "upstream_unavailable")
        self.assertGreaterEqual(metadata["wake_next_try_at"], now + 50)
        self.assertLessEqual(metadata["wake_next_try_at"], now + 70)


class _FakeMetadataStore:
    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata

    def get(self, task_id: str) -> dict:
        return self.metadata


class _FakeDueStore:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    def due_wake_tasks(self, now: int) -> list[str]:
        return list(self.ids)


def _wake_service(dispatch_result: tuple[int, dict]) -> tuple[object, list, list]:
    """A WorkbenchService with only the collaborators ``wake_task`` touches."""
    service = object.__new__(workbench_server.WorkbenchService)
    service.store = _FakeMetadataStore(
        {"wake_at": 1, "contract": {"accept": "单测验收"}}
    )
    service._task_lock = lambda task_id: contextlib.nullcontext()
    service._task_detail = lambda task_id, deadline: {
        "task": {"id": task_id, "status": "scheduled"}
    }
    failures: list[tuple] = []
    cleared: list[str] = []

    def record_failure(task_id: str, error: str, delay: int, now: float) -> None:
        failures.append((task_id, error, delay))

    def clear(task_id: str) -> bool:
        cleared.append(task_id)
        return True

    service._set_wake_failure = record_failure
    service._clear_wake_fields = clear
    service._dispatch_locked = lambda task_id: dispatch_result
    return service, failures, cleared


class WakeTaskUnitTests(unittest.TestCase):
    """Narrow units for branches the subprocess tests cannot trigger cheaply."""

    def test_clear_failure_after_success_still_returns_200(self):
        service, failures, cleared = _wake_service(
            (200, {"ok": True, "status": "ready", "position": 1})
        )
        service._clear_wake_fields = lambda task_id: cleared.append(task_id) or False

        code, body = service.wake_task("t_unit")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(cleared, ["t_unit"])
        self.assertEqual(failures, [])

    def test_invalid_defaults_is_passed_through_and_backed_off(self):
        service, failures, _ = _wake_service(
            (500, {"ok": False, "error": "invalid_defaults"})
        )
        code, body = service.wake_task("t_unit")
        self.assertEqual(code, 500)
        self.assertEqual(body["error"], "invalid_defaults")
        self.assertEqual(
            failures,
            [("t_unit", "invalid_defaults", workbench_server.WAKE_RETRY_SECONDS)],
        )

    def test_metadata_unavailable_is_passed_through_without_backoff(self):
        service, failures, _ = _wake_service(
            (503, {"ok": False, "error": "metadata_unavailable"})
        )
        code, body = service.wake_task("t_unit")
        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "metadata_unavailable")
        self.assertEqual(failures, [])

    def test_require_due_rechecks_clear_postpone_and_backoff(self):
        due = workbench_server.WorkbenchService._wake_is_due
        self.assertTrue(due({"wake_at": 100}, 100))
        self.assertFalse(due({}, 100))
        self.assertFalse(due({"wake_at": 101}, 100))
        self.assertFalse(due({"wake_at": 99, "wake_next_try_at": 101}, 100))
        self.assertTrue(due({"wake_at": 99, "wake_next_try_at": 100}, 100))

    def test_one_bad_card_does_not_stop_the_sweep(self):
        service = object.__new__(workbench_server.WorkbenchService)
        service.store = _FakeDueStore(["bad", "good"])
        calls: list[tuple[str, bool]] = []

        def fake_wake(task_id: str, *, require_due: bool = False):
            calls.append((task_id, require_due))
            if task_id == "bad":
                raise RuntimeError("boom")
            return 200, {"ok": True}

        service.wake_task = fake_wake
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            count = service.wake_due()
        self.assertEqual(count, 1)
        self.assertEqual(calls, [("bad", True), ("good", True)])
        self.assertIn("wake failed for bad", stderr.getvalue())

    def test_skipped_due_card_is_not_counted(self):
        service = object.__new__(workbench_server.WorkbenchService)
        service.store = _FakeDueStore(["moved"])
        service.wake_task = lambda task_id, *, require_due=False: (
            200,
            {"ok": True, "skipped": "not_due"},
        )
        self.assertEqual(service.wake_due(), 0)


class WakeEnvTests(unittest.TestCase):
    def test_wake_sweep_env_parsing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                workbench_server._env_wake_sweep_seconds(),
                float(workbench_server.WAKE_SWEEP_SECONDS),
            )
        with mock.patch.dict(
            os.environ, {"WORKBENCH_WAKE_SWEEP_SECONDS": "0"}, clear=True
        ):
            self.assertEqual(workbench_server._env_wake_sweep_seconds(), 0.0)
        with mock.patch.dict(
            os.environ, {"WORKBENCH_WAKE_SWEEP_SECONDS": "0.2"}, clear=True
        ):
            self.assertEqual(workbench_server._env_wake_sweep_seconds(), 0.2)

    def test_invalid_wake_sweep_env_logs_and_disables(self):
        for raw in ("nope", "-1", "inf"):
            with self.subTest(raw=raw):
                stderr = io.StringIO()
                with mock.patch.dict(
                    os.environ,
                    {"WORKBENCH_WAKE_SWEEP_SECONDS": raw},
                    clear=True,
                ), contextlib.redirect_stderr(stderr):
                    self.assertEqual(workbench_server._env_wake_sweep_seconds(), 0.0)
                self.assertIn("invalid WORKBENCH_WAKE_SWEEP_SECONDS", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)