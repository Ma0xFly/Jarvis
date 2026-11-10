#!/usr/bin/env python3
"""Integration tests for full-screen verification (2.1 + 3.2).

Covers ``GET /api/workbench/inbox`` (pure read, parallel detail reads,
``failed_ids``), ``GET /api/workbench/inbox/count``,
``POST /api/workbench/reviews/sweep``, ``POST /api/workbench/verify/{id}``
(including the ``missing_solidified`` accept gate),
``POST /api/workbench/solidified/{id}/verify`` and the 48h review policy.
The workbench server runs as an isolated subprocess with throwaway kanban /
workbench DBs (see ``test_dispatch``).

Run with::

    python3 workbench-app/tests/test_verify.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# Importing the 1.2 harness keeps server startup / isolated env in one place.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch as base  # noqa: E402

REVIEW_TTL = 48 * 60 * 60
AUTO_PREFIX = "48h 自动收"
# Mirrors server.py; each self-report line is capped with a visible marker.
MAX_SELF_REPORT_CHARS = 2000
DEFAULT_SOLIDIFIED = {
    "type": "script",
    "path": "workbench-app/tests/test_verify.py",
    "status": "pending",
}


def make_defaults(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    defaults = root / "defaults.json"
    defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
    return defaults


def start_server(
    root: Path,
    *,
    env_extra=None,
    upstream: str | None = None,
) -> base.WorkbenchServer:
    env = dict(env_extra or {})
    # Keep the background sweeper off unless a test opts in, and disable the
    # 1s header cache so a card created within the same test is always visible.
    env.setdefault("WORKBENCH_REVIEW_SWEEP_SECONDS", "0")
    env.setdefault("WORKBENCH_INBOX_CACHE_SECONDS", "0")
    env.setdefault("WORKBENCH_NOTIFY_SWEEP_SECONDS", "0")
    if upstream is not None:
        env["WORKBENCH_UPSTREAM_BASE"] = upstream
    return base.WorkbenchServer(root, make_defaults(root), env_extra=env).start()


def fake_task(
    task_id: str,
    *,
    title: str | None = None,
    status: str = "review",
    summary: str | None = "完成",
    result: str | None = None,
    review_events: list[tuple[int, int]] | None = None,
    comments: list[dict] | None = None,
    block_kind: str | None = None,
    block_recurrences: int = 0,
    links: dict | None = None,
    progress: dict | None = None,
    runs: list[dict] | None = None,
) -> dict:
    now = int(time.time())
    events = [
        {
            "id": event_id,
            "task_id": task_id,
            "kind": "review_requested",
            "payload": {},
            "created_at": created_at,
            "run_id": None,
        }
        for event_id, created_at in (review_events or [(1, now - 600)])
    ]
    normalized_links = links or {"parents": [], "children": []}
    return {
        "id": task_id,
        "title": title or task_id,
        "status": status,
        "created_at": now - 7200,
        "result": result,
        "latest_summary": summary,
        "block_kind": block_kind,
        "block_recurrences": block_recurrences,
        "link_counts": {
            "parents": len(normalized_links.get("parents") or []),
            "children": len(normalized_links.get("children") or []),
        },
        "progress": progress,
        "links": normalized_links,
        "events": events,
        "comments": list(comments or []),
        "attachments": [],
        "runs": list(runs or []),
    }


def fake_run(
    run_id: int,
    *,
    metadata: object | None = None,
    started_at: int | None = None,
) -> dict:
    started = int(time.time()) if started_at is None else started_at
    return {
        "id": run_id,
        "profile": "probe",
        "status": "done",
        "outcome": "completed",
        "started_at": started,
        "ended_at": started + 30,
        "summary": "完成",
        "metadata": metadata,
        "error": None,
    }


class FakeKanban:
    """Small stateful loopback upstream for failure/parallelism scenarios."""

    def __init__(self, tasks: list[dict]) -> None:
        self.lock = threading.RLock()
        self.tasks = {task["id"]: dict(task) for task in tasks}
        self.calls: list[tuple[str, str]] = []
        self.patch_payloads: list[dict] = []
        self.patch_mode = "ok"  # ok | 404 | 409 | 500 | wrong_status | require_summary
        self.patch_failures = 0
        self.patch_moves_to: str | None = None
        self.comment_status = 200
        self.detail_fail_ids: set[str] = set()
        self._comment_id = 100

    # -- test-side mutations ---------------------------------------------
    def set_patch_mode(self, mode: str) -> None:
        with self.lock:
            self.patch_mode = mode

    def add_comment(self, task_id: str, author: str, body: str, created_at: int | None = None) -> None:
        with self.lock:
            self._comment_id += 1
            self.tasks[task_id]["comments"].append(
                {
                    "id": self._comment_id,
                    "task_id": task_id,
                    "author": author,
                    "body": body,
                    "created_at": int(created_at if created_at is not None else time.time()),
                }
            )

    def add_review_event(self, task_id: str, event_id: int, created_at: int) -> None:
        with self.lock:
            self.tasks[task_id]["events"].append(
                {
                    "id": event_id,
                    "task_id": task_id,
                    "kind": "review_requested",
                    "payload": {},
                    "created_at": created_at,
                    "run_id": None,
                }
            )

    def task(self, task_id: str) -> dict:
        with self.lock:
            return self.tasks[task_id]

    # -- HTTP -------------------------------------------------------------
    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    parsed = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    return {}
                return parsed if isinstance(parsed, dict) else {}

            def do_GET(self) -> None:  # noqa: N802
                with fake.lock:
                    fake.calls.append(("GET", self.path))
                    path = urlparse.urlsplit(self.path).path
                    if path == "/api/plugins/kanban/board":
                        columns: dict[str, list[dict]] = {}
                        for task in fake.tasks.values():
                            columns.setdefault(task["status"], []).append(
                                {
                                    "id": task["id"],
                                    "title": task["title"],
                                    "status": task["status"],
                                    "block_kind": task.get("block_kind"),
                                    "block_recurrences": task.get("block_recurrences", 0),
                                    "link_counts": task.get("link_counts")
                                    or {"parents": 0, "children": 0},
                                    "progress": task.get("progress"),
                                }
                            )
                        self._respond(
                            200,
                            {
                                "columns": [
                                    {"name": status, "tasks": cards}
                                    for status, cards in columns.items()
                                ]
                            },
                        )
                        return
                    prefix = "/api/plugins/kanban/tasks/"
                    if path.startswith(prefix):
                        task_id = urlparse.unquote(path[len(prefix):])
                        if task_id in fake.detail_fail_ids:
                            self._respond(500, {"detail": "detail failed"})
                            return
                        task = fake.tasks.get(task_id)
                        if task is None:
                            self._respond(404, {"detail": "not found"})
                            return
                        self._respond(
                            200,
                            {
                                "task": {
                                    "id": task["id"],
                                    "title": task["title"],
                                    "status": task["status"],
                                    "created_at": task["created_at"],
                                    "result": task["result"],
                                    "latest_summary": task["latest_summary"],
                                    "block_kind": task.get("block_kind"),
                                    "block_recurrences": task.get("block_recurrences", 0),
                                },
                                "events": list(task["events"]),
                                "comments": list(task["comments"]),
                                "attachments": list(task["attachments"]),
                                "links": task.get("links")
                                or {"parents": [], "children": []},
                                "runs": list(task["runs"]),
                            },
                        )
                        return
                    self._respond(404, {"detail": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse.urlsplit(self.path).path
                payload = self._body()
                with fake.lock:
                    fake.calls.append(("POST", self.path))
                    if path.endswith("/comments"):
                        if fake.comment_status != 200:
                            self._respond(fake.comment_status, {"detail": "comment failed"})
                            return
                        task_id = urlparse.unquote(
                            path.rsplit("/comments", 1)[0].rsplit("/", 1)[-1]
                        )
                        if task_id not in fake.tasks:
                            self._respond(404, {"detail": "not found"})
                            return
                        fake._comment_id += 1
                        fake.tasks[task_id]["comments"].append(
                            {
                                "id": fake._comment_id,
                                "task_id": task_id,
                                "author": payload.get("author") or "dashboard",
                                "body": payload.get("body") or "",
                                "created_at": int(time.time()),
                            }
                        )
                        self._respond(200, {"ok": True})
                        return
                    self._respond(404, {"detail": "not found"})

            def do_PATCH(self) -> None:  # noqa: N802
                path = urlparse.urlsplit(self.path).path
                payload = self._body()
                with fake.lock:
                    fake.calls.append(("PATCH", self.path))
                    fake.patch_payloads.append(payload)
                    if fake.patch_failures > 0:
                        fake.patch_failures -= 1
                        self._respond(500, {"detail": "patch failed"})
                        return
                    prefix = "/api/plugins/kanban/tasks/"
                    task_id = urlparse.unquote(path[len(prefix):])
                    task = fake.tasks.get(task_id)
                    if task is None:
                        self._respond(404, {"detail": "not found"})
                        return
                    mode = fake.patch_mode
                    if mode == "404":
                        self._respond(404, {"detail": "not found"})
                        return
                    if mode == "409":
                        if fake.patch_moves_to:
                            task["status"] = fake.patch_moves_to
                        self._respond(409, {"detail": "conflict"})
                        return
                    if mode == "500":
                        self._respond(500, {"detail": "boom"})
                        return
                    if mode == "wrong_status":
                        self._respond(200, {"task": {"id": task_id, "status": "triage"}})
                        return
                    if mode == "require_summary" and not payload.get("summary"):
                        self._respond(400, {"detail": "summary required"})
                        return
                    task["status"] = payload.get("status", task["status"])
                    if payload.get("summary"):
                        task["latest_summary"] = payload["summary"]
                    self._respond(
                        200, {"task": {"id": task_id, "status": task["status"]}}
                    )

            def log_message(self, *args) -> None:
                pass

        return Handler


@contextlib.contextmanager
def fake_kanban(tasks: list[dict] | None = None):
    fake = FakeKanban(tasks or [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        fake.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        yield fake
    finally:
        httpd.shutdown()
        httpd.server_close()


class VerifyBackendTestCase(unittest.TestCase):
    tmp: Path
    server: base.WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-verify-"))
        cls.server = start_server(cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def seed_review(
        self,
        title: str,
        *,
        review_age: int | None = None,
        accept: str = "跑通测试",
        input_text: str = "需求单",
        summary: str | None = None,
        solidified: object = DEFAULT_SOLIDIFIED,
    ) -> str:
        card = self.server.create_task(title, triage=True)
        self.server.seed_metadata(
            card,
            {
                "bucket": "today",
                "contract": {"accept": accept, "input": input_text, "ready": True},
            },
        )
        code, body = self.server.dispatch(card)
        self.assertEqual(code, 200, body)
        extra = {"metadata": {"solidified": solidified}} if solidified is not None else {}
        code, body = self.server.patch_status(
            card,
            "review",
            summary=summary if summary is not None else "完成 " + title,
            **extra,
        )
        self.assertEqual(code, 200, body)
        if review_age is not None:
            self.set_review_event_age(card, review_age)
        return card

    def set_review_event_age(self, task_id: str, age_seconds: int) -> None:
        conn = sqlite3.connect(str(self.server.kanban_db), timeout=5)
        try:
            cursor = conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'review_requested'",
                (int(time.time()) - age_seconds, task_id),
            )
            rows = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(rows, 1)

    def add_review_event(self, task_id: str, event_id: int, created_at: int) -> None:
        conn = sqlite3.connect(str(self.server.kanban_db), timeout=5)
        try:
            conn.execute(
                "INSERT INTO task_events (id, task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, NULL, 'review_requested', '{}', ?)",
                (event_id, task_id, created_at),
            )
            conn.commit()
        finally:
            conn.close()

    def inbox(self):
        return base.http_json("GET", self.server.base + "/api/workbench/inbox")

    def inbox_count(self):
        return base.http_json("GET", self.server.base + "/api/workbench/inbox/count")

    def sweep(self, server: base.WorkbenchServer | None = None):
        target = server or self.server
        return base.http_json("POST", target.base + "/api/workbench/reviews/sweep", {})

    def inbox_item(self, task_id: str):
        code, body = self.inbox()
        self.assertEqual(code, 200, body)
        for item in body["tasks"]:
            if item["id"] == task_id:
                return item
        return None

    def verify(
        self, task_id: str, action, reason=None, server=None, expected_review_at=None
    ):
        payload = {"action": action}
        if reason is not None:
            payload["reason"] = reason
        if expected_review_at is not None:
            payload["expected_review_at"] = expected_review_at
        target = server or self.server
        return base.http_json(
            "POST",
            target.base
            + "/api/workbench/verify/"
            + urlparse.quote(task_id, safe=""),
            payload,
        )

    def mark_verified(self, task_id: str, path, server=None):
        target = server or self.server
        return base.http_json(
            "POST",
            target.base
            + "/api/workbench/solidified/"
            + urlparse.quote(task_id, safe="")
            + "/verify",
            {"path": path},
        )

    def detail(self, task_id: str, server=None) -> dict:
        target = server or self.server
        code, body = base.http_json(
            "GET",
            target.base
            + "/api/plugins/kanban/tasks/"
            + urlparse.quote(task_id, safe=""),
        )
        self.assertEqual(code, 200, body)
        return body

    def comments(self, task_id: str, server=None) -> list[dict]:
        return self.detail(task_id, server).get("comments") or []

    def raw_request(self, method: str, path: str, data: bytes = b"", headers=None, server=None):
        target = server or self.server
        request = urlrequest.Request(
            target.base + path,
            data=data,
            headers={"Accept": "application/json", **(headers or {})},
            method=method,
        )
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                raw = response.read()
                return int(response.status), (json.loads(raw) if raw else None)
        except urlerror.HTTPError as exc:
            try:
                raw = exc.read()
                try:
                    parsed = json.loads(raw) if raw else None
                except (ValueError, UnicodeDecodeError):
                    parsed = raw.decode("utf-8", errors="replace")
                return int(exc.code), parsed
            finally:
                exc.close()


class InboxTests(VerifyBackendTestCase):
    def test_inbox_is_pure_read_with_metadata_age_and_order(self):
        older = self.seed_review("较早待收", review_age=3600)
        newer = self.seed_review("较晚待收", review_age=600)

        code, body = self.inbox()
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["review_ttl_seconds"], REVIEW_TTL)
        self.assertEqual(body["failed_ids"], [])

        ids = [item["id"] for item in body["tasks"]]
        self.assertLess(ids.index(older), ids.index(newer), ids)

        wide = next(item for item in body["tasks"] if item["id"] == older)
        self.assertEqual(wide["status"], "review")
        self.assertEqual(wide["metadata"]["contract"]["accept"], "跑通测试")
        self.assertEqual(wide["metadata"]["contract"]["input"], "需求单")
        self.assertIsNotNone(wide["review_requested_at"])
        self.assertGreaterEqual(wide["review_age_seconds"], 3600)
        self.assertLess(wide["review_age_seconds"], 3700)
        self.assertFalse(wide["auto_collect_due"])
        self.assertIsNone(wide["auto_collect_error"])

        narrow = next(item for item in body["tasks"] if item["id"] == newer)
        self.assertGreaterEqual(narrow["review_age_seconds"], 600)
        self.assertLess(narrow["review_age_seconds"], 700)

        # Pure read: reading never collects, even an overdue card.
        overdue = self.seed_review("只读不自动收", review_age=REVIEW_TTL + 60)
        item = self.inbox_item(overdue)
        self.assertIsNotNone(item)
        self.assertTrue(item["auto_collect_due"])
        self.assertEqual(self.server.status(overdue), "review")

    def test_inbox_only_lists_review_cards(self):
        review = self.seed_review("列表待收")
        triage = self.server.create_task("列表 triage", triage=True)
        ready = self.server.create_task("列表 ready", triage=True)
        self.server.seed_metadata(ready, {"contract": {"accept": "ok"}})
        code, body = self.server.dispatch(ready)
        self.assertEqual(code, 200, body)

        code, inbox = self.inbox()
        self.assertEqual(code, 200, inbox)
        ids = {item["id"] for item in inbox["tasks"]}
        self.assertIn(review, ids)
        self.assertNotIn(triage, ids)
        self.assertNotIn(ready, ids)

    def test_card_without_review_event_sorts_last(self):
        with_event = self.seed_review("有事件待收", review_age=100)
        no_event = self.seed_review("无事件待收", review_age=100)
        conn = sqlite3.connect(str(self.server.kanban_db), timeout=5)
        try:
            conn.execute(
                "DELETE FROM task_events WHERE task_id = ? AND kind = 'review_requested'",
                (no_event,),
            )
            conn.commit()
        finally:
            conn.close()

        code, body = self.inbox()
        self.assertEqual(code, 200, body)
        ids = [item["id"] for item in body["tasks"]]
        self.assertLess(ids.index(with_event), ids.index(no_event), ids)
        item = next(item for item in body["tasks"] if item["id"] == no_event)
        self.assertIsNone(item["review_requested_at"])
        self.assertIsNone(item["review_age_seconds"])
        self.assertFalse(item["auto_collect_due"])

    def test_newest_review_event_resets_the_clock(self):
        card = self.seed_review("二次 review")
        # Age the first round, then add a fresh review_requested event: the
        # newest event must reset the 48h clock.
        self.set_review_event_age(card, 5000)
        self.add_review_event(card, 502, int(time.time()) - 5)
        item = self.inbox_item(card)
        self.assertIsNotNone(item)
        self.assertGreaterEqual(item["review_age_seconds"], 5)
        self.assertLess(item["review_age_seconds"], 60)

    def test_self_report_lands_on_the_latest_run_via_review_handoff(self):
        """Dashboard PATCH review -> kanban_db.request_review lands metadata on the run.

        The worker tool wrapper (``kanban_request_review``) is not importable
        here; this covers the same ``request_review`` path the dashboard calls.
        """
        card = self.server.create_task("自述链路", triage=True)
        self.server.seed_metadata(card, {"contract": {"accept": "ok"}})
        code, body = self.server.dispatch(card)
        self.assertEqual(code, 200, body)
        report = {
            "delivered": "改了验收页",
            "evidence": "test_verify 全绿",
            "uncertain": "没跑真机",
        }
        code, body = self.server.patch_status(
            card, "review", summary="完成", metadata={"self_report": report}
        )
        self.assertEqual(code, 200, body)
        item = self.inbox_item(card)
        self.assertIsNotNone(item)
        self.assertEqual(item["self_report"], report)

    def test_solidified_lands_on_the_latest_run_via_review_handoff(self):
        """The same ``request_review`` path carries ``solidified`` to the run."""
        card = self.server.create_task("固化链路", triage=True)
        self.server.seed_metadata(card, {"contract": {"accept": "ok"}})
        code, body = self.server.dispatch(card)
        self.assertEqual(code, 200, body)
        solidified = [
            {"type": "script", "path": "scripts/x.sh", "status": "pending"}
        ]
        code, body = self.server.patch_status(
            card,
            "review",
            summary="完成",
            metadata={"solidified": solidified},
        )
        self.assertEqual(code, 200, body)
        item = self.inbox_item(card)
        self.assertIsNotNone(item)
        self.assertEqual(item["solidified"], solidified)

    def test_inbox_count_matches_inbox_and_never_reads_details(self):
        self.seed_review("计数卡")
        with fake_kanban(
            [
                fake_task("t1", review_events=[(1, int(time.time()) - 100)]),
                fake_task("t2", review_events=[(1, int(time.time()) - 50)]),
            ]
        ) as fake:
            fake.detail_fail_ids = {"t1", "t2"}
            with tempfile.TemporaryDirectory() as tmp:
                server = start_server(Path(tmp) / "srv", upstream=fake.base)
                try:
                    code, body = base.http_json(
                        "GET", server.base + "/api/workbench/inbox/count"
                    )
                    self.assertEqual(code, 200, body)
                    self.assertEqual(body["count"], 2)
                    self.assertEqual(body["review_ttl_seconds"], REVIEW_TTL)
                    task_reads = [
                        call for call in fake.calls
                        if call[0] == "GET" and "/tasks/" in call[1]
                    ]
                    self.assertEqual(task_reads, [])
                finally:
                    server.stop()

        code, body = self.inbox_count()
        self.assertEqual(code, 200, body)
        self.assertEqual(body["count"], self.inbox()[1]["count"])

    def test_one_failed_detail_is_skipped_and_reported(self):
        with fake_kanban(
            [
                fake_task("t_ok", title="可读详情", review_events=[(1, int(time.time()) - 100)]),
                fake_task("t_bad", title="详情失败", review_events=[(1, int(time.time()) - 50)]),
            ]
        ) as fake:
            fake.detail_fail_ids = {"t_bad"}
            with tempfile.TemporaryDirectory() as tmp:
                server = start_server(Path(tmp) / "srv", upstream=fake.base)
                try:
                    code, body = base.http_json(
                        "GET", server.base + "/api/workbench/inbox"
                    )
                finally:
                    server.stop()
        self.assertEqual(code, 200, body)
        self.assertEqual([item["id"] for item in body["tasks"]], ["t_ok"])
        self.assertEqual(body["failed_ids"], ["t_bad"])
        self.assertEqual(body["count"], 1)

    def test_inbox_503_when_metadata_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken_db = root / "broken-db"
            broken_db.mkdir()
            server = start_server(
                root / "srv", env_extra={"WORKBENCH_DB": str(broken_db)}
            )
            try:
                card = server.create_task("元数据不可用", triage=True)
                server.set_kanban_status(card, "review")
                server.add_kanban_event(card, "review_requested", {})
                code, body = base.http_json(
                    "GET", server.base + "/api/workbench/inbox"
                )
                self.assertEqual(code, 503, body)
                self.assertEqual(body, {"ok": False, "error": "metadata_unavailable"})
            finally:
                server.stop()

    def test_inbox_and_count_502_when_upstream_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            closed_port = base.free_port()
            server = start_server(
                root / "srv",
                upstream=f"http://127.0.0.1:{closed_port}",
            )
            try:
                for path in ("/api/workbench/inbox", "/api/workbench/inbox/count"):
                    code, body = base.http_json("GET", server.base + path)
                    self.assertEqual(code, 502, body)
                    self.assertEqual(body, {"ok": False, "error": "upstream_unavailable"})
                code, body = base.http_json(
                    "POST", server.base + "/api/workbench/reviews/sweep", {}
                )
                self.assertEqual(code, 502, body)
                self.assertEqual(body, {"ok": False, "error": "upstream_unavailable"})
            finally:
                server.stop()


class InboxSelfReportTests(unittest.TestCase):
    """The inbox surfaces only the newest run's normalized ``self_report``."""

    def _item(self, runs: list[dict]) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with fake_kanban(
            [fake_task("t1", review_events=[(1, int(time.time()) - 60)], runs=runs)]
        ) as fake:
            server = start_server(Path(tmp.name) / "srv", upstream=fake.base)
            try:
                code, body = base.http_json(
                    "GET", server.base + "/api/workbench/inbox"
                )
            finally:
                server.stop()
        self.assertEqual(code, 200, body)
        items = [item for item in body["tasks"] if item["id"] == "t1"]
        self.assertEqual(len(items), 1, body)
        return items[0]

    def test_complete_self_report_is_normalized(self):
        item = self._item(
            [
                fake_run(
                    1,
                    metadata={
                        "self_report": {
                            "delivered": "  改了验收页  ",
                            "evidence": "test_verify 全绿",
                            "uncertain": "没跑真机",
                        }
                    },
                )
            ]
        )
        self.assertEqual(
            item["self_report"],
            {
                "delivered": "改了验收页",
                "evidence": "test_verify 全绿",
                "uncertain": "没跑真机",
            },
        )
        self.assertFalse(item["self_report_truncated"])

    def test_oversized_field_is_capped_with_a_visible_marker(self):
        oversized = "x" * 50_000
        item = self._item(
            [
                fake_run(
                    1,
                    metadata={
                        "self_report": {
                            "delivered": oversized,
                            "evidence": "依据",
                            "uncertain": "",
                        }
                    },
                )
            ]
        )
        self.assertTrue(item["self_report_truncated"])
        delivered = item["self_report"]["delivered"]
        self.assertEqual(len(delivered), MAX_SELF_REPORT_CHARS)
        self.assertIn(f"原 {len(oversized)} 字符", delivered)
        self.assertTrue(delivered.endswith("]"))
        # Other fields are untouched by the cap.
        self.assertEqual(item["self_report"]["evidence"], "依据")
        self.assertEqual(item["self_report"]["uncertain"], "")

    def test_blank_uncertain_stays_an_empty_string(self):
        item = self._item(
            [
                fake_run(
                    1,
                    metadata={
                        "self_report": {
                            "delivered": "交付",
                            "evidence": "依据",
                            "uncertain": "   ",
                        }
                    },
                )
            ]
        )
        self.assertEqual(item["self_report"]["uncertain"], "")

    def test_missing_or_malformed_self_report_is_none(self):
        cases = {
            "no runs": [],
            "no metadata": [fake_run(1)],
            "metadata not dict": [fake_run(1, metadata="self_report")],
            "no self_report": [fake_run(1, metadata={"worker_session_id": "s"})],
            "self_report not dict": [
                fake_run(1, metadata={"self_report": "交付 / 依据 / 没底"})
            ],
            "field not string": [
                fake_run(
                    1,
                    metadata={
                        "self_report": {
                            "delivered": 1,
                            "evidence": "e",
                            "uncertain": "u",
                        }
                    },
                )
            ],
            "missing field": [
                fake_run(1, metadata={"self_report": {"delivered": "d", "evidence": "e"}})
            ],
            "blank delivered": [
                fake_run(
                    1,
                    metadata={
                        "self_report": {
                            "delivered": "   ",
                            "evidence": "e",
                            "uncertain": "",
                        }
                    },
                )
            ],
            "blank evidence": [
                fake_run(
                    1,
                    metadata={
                        "self_report": {
                            "delivered": "d",
                            "evidence": " ",
                            "uncertain": "",
                        }
                    },
                )
            ],
            "all blank": [
                fake_run(
                    1,
                    metadata={
                        "self_report": {
                            "delivered": " ",
                            "evidence": "",
                            "uncertain": "  ",
                        }
                    },
                )
            ],
        }
        for label, runs in cases.items():
            with self.subTest(label):
                self.assertIsNone(self._item(runs)["self_report"])

    def test_only_the_newest_run_counts(self):
        now = int(time.time())
        old = {
            "self_report": {
                "delivered": "旧轮",
                "evidence": "旧依据",
                "uncertain": "旧没底",
            }
        }
        # Older run has a report, newer run is missing it or malformed: no
        # backtracking to the older round.
        malformed = {
            "self_report": {"delivered": 7, "evidence": "e", "uncertain": "u"}
        }
        for label, newest in (
            ("missing", fake_run(2, started_at=now - 60)),
            ("malformed", fake_run(2, started_at=now - 60, metadata=malformed)),
        ):
            for runs in (
                [fake_run(1, started_at=now - 600, metadata=old), newest],
                [newest, fake_run(1, started_at=now - 600, metadata=old)],
            ):
                with self.subTest(label=label):
                    self.assertIsNone(self._item(runs)["self_report"])

        # Newest report wins even when an older one exists.
        item = self._item(
            [
                fake_run(1, started_at=now - 600, metadata=old),
                fake_run(
                    2,
                    started_at=now - 60,
                    metadata={
                        "self_report": {
                            "delivered": "新轮",
                            "evidence": "新依据",
                            "uncertain": "新没底",
                        }
                    },
                ),
            ]
        )
        self.assertEqual(item["self_report"]["delivered"], "新轮")

    def test_equal_timestamps_break_by_run_id(self):
        now = int(time.time())
        old = {
            "self_report": {
                "delivered": "旧轮",
                "evidence": "旧依据",
                "uncertain": "旧没底",
            }
        }
        item = self._item(
            [
                fake_run(1, started_at=now - 60, metadata={}),
                fake_run(2, started_at=now - 60, metadata=old),
            ]
        )
        self.assertEqual(item["self_report"]["delivered"], "旧轮")

        # Higher id without a report wins the tie: still no backtracking.
        item = self._item(
            [
                fake_run(1, started_at=now - 60, metadata=old),
                fake_run(2, started_at=now - 60, metadata={}),
            ]
        )
        self.assertIsNone(item["self_report"])


class InboxSolidifiedTests(unittest.TestCase):
    """The inbox surfaces only the newest run's normalized ``solidified``."""

    def _item(self, runs: list[dict]) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with fake_kanban(
            [fake_task("t1", review_events=[(1, int(time.time()) - 60)], runs=runs)]
        ) as fake:
            server = start_server(Path(tmp.name) / "srv", upstream=fake.base)
            try:
                code, body = base.http_json(
                    "GET", server.base + "/api/workbench/inbox"
                )
            finally:
                server.stop()
        self.assertEqual(code, 200, body)
        items = [item for item in body["tasks"] if item["id"] == "t1"]
        self.assertEqual(len(items), 1, body)
        return items[0]

    def test_single_object_is_normalized_to_a_list(self):
        item = self._item(
            [
                fake_run(
                    1,
                    metadata={
                        "solidified": {
                            "type": "script",
                            "path": "  scripts/a.sh  ",
                            "status": "pending",
                        }
                    },
                )
            ]
        )
        self.assertEqual(
            item["solidified"],
            [{"type": "script", "path": "scripts/a.sh", "status": "pending"}],
        )

    def test_list_keeps_valid_entries_and_drops_malformed_ones(self):
        item = self._item(
            [
                fake_run(
                    1,
                    metadata={
                        "solidified": [
                            {
                                "type": "routing",
                                "path": "routing.json",
                                "status": "verified",
                            },
                            "junk",
                            None,
                            {"type": "bogus", "path": "x", "status": "pending"},
                            {"type": "script", "path": "   ", "status": "pending"},
                            {"type": "script", "path": "scripts/b.sh", "status": "bogus"},
                            {
                                "type": "skill_patch",
                                "path": " skills/SKILL.md ",
                                "status": "pending",
                            },
                        ]
                    },
                )
            ]
        )
        self.assertEqual(
            item["solidified"],
            [
                {"type": "routing", "path": "routing.json", "status": "verified"},
                {
                    "type": "skill_patch",
                    "path": "skills/SKILL.md",
                    "status": "pending",
                },
            ],
        )

    def test_dedupes_by_type_and_path(self):
        item = self._item(
            [
                fake_run(
                    1,
                    metadata={
                        "solidified": [
                            {"type": "script", "path": "a.sh", "status": "pending"},
                            {"type": "script", "path": " a.sh ", "status": "verified"},
                            {"type": "routing", "path": "a.sh", "status": "pending"},
                        ]
                    },
                )
            ]
        )
        self.assertEqual(
            item["solidified"],
            [
                {"type": "script", "path": "a.sh", "status": "pending"},
                {"type": "routing", "path": "a.sh", "status": "pending"},
            ],
        )

    def test_caps_entries_at_fifty(self):
        entries = [
            {"type": "script", "path": f"scripts/{index}.sh", "status": "pending"}
            for index in range(60)
        ]
        item = self._item([fake_run(1, metadata={"solidified": entries})])
        self.assertEqual(len(item["solidified"]), 50)
        self.assertEqual(item["solidified"][-1]["path"], "scripts/49.sh")

    def test_path_length_cap(self):
        max_path = "a" * 500
        item = self._item(
            [
                fake_run(
                    1,
                    metadata={
                        "solidified": [
                            {"type": "script", "path": max_path, "status": "pending"},
                            {
                                "type": "script",
                                "path": "a" * 501,
                                "status": "pending",
                            },
                        ]
                    },
                )
            ]
        )
        self.assertEqual([entry["path"] for entry in item["solidified"]], [max_path])

    def test_missing_or_all_invalid_is_an_empty_list(self):
        cases = {
            "no runs": [],
            "no metadata": [fake_run(1)],
            "metadata not dict": [fake_run(1, metadata="solidified")],
            "no solidified": [fake_run(1, metadata={"self_report": {}})],
            "solidified not object/list": [fake_run(1, metadata={"solidified": "x"})],
            "empty list": [fake_run(1, metadata={"solidified": []})],
            "empty object": [fake_run(1, metadata={"solidified": {}})],
            "all invalid": [
                fake_run(
                    1,
                    metadata={
                        "solidified": [
                            {"type": "routing", "path": "", "status": "pending"},
                            {"type": "routing", "path": "r", "status": "done"},
                            {"type": "unknown", "path": "r", "status": "pending"},
                            42,
                        ]
                    },
                )
            ],
        }
        for label, runs in cases.items():
            with self.subTest(label):
                self.assertEqual(self._item(runs)["solidified"], [])

    def test_only_the_newest_run_counts(self):
        now = int(time.time())
        old = {
            "solidified": [
                {"type": "script", "path": "scripts/old.sh", "status": "pending"}
            ]
        }
        new = {
            "solidified": [
                {"type": "script", "path": "scripts/new.sh", "status": "verified"}
            ]
        }
        cases = {
            "newest missing": (
                [fake_run(1, started_at=now - 600, metadata=old), fake_run(2, started_at=now - 60)],
                [],
            ),
            "newest invalid": (
                [
                    fake_run(1, started_at=now - 600, metadata=old),
                    fake_run(2, started_at=now - 60, metadata={"solidified": "junk"}),
                ],
                [],
            ),
            "newest wins": (
                [
                    fake_run(1, started_at=now - 600, metadata=old),
                    fake_run(2, started_at=now - 60, metadata=new),
                ],
                new["solidified"],
            ),
        }
        for label, (runs, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self._item(runs)["solidified"], expected)

    def test_equal_timestamps_break_by_run_id(self):
        now = int(time.time())
        low = {
            "solidified": [
                {"type": "script", "path": "scripts/low.sh", "status": "pending"}
            ]
        }
        high = {
            "solidified": [
                {"type": "script", "path": "scripts/high.sh", "status": "pending"}
            ]
        }
        item = self._item(
            [
                fake_run(1, started_at=now - 60, metadata=low),
                fake_run(2, started_at=now - 60, metadata=high),
            ]
        )
        self.assertEqual(item["solidified"], high["solidified"])

        # Higher id without a record wins the tie: still no backtracking.
        item = self._item(
            [
                fake_run(1, started_at=now - 60, metadata=low),
                fake_run(2, started_at=now - 60, metadata={}),
            ]
        )
        self.assertEqual(item["solidified"], [])


class SolidifiedGateUpstreamTests(unittest.TestCase):
    """Accept reads only the newest run, even when an older one is valid."""

    def _server(self, fake: FakeKanban) -> base.WorkbenchServer:
        self._tmp = tempfile.TemporaryDirectory()
        server = start_server(Path(self._tmp.name) / "srv", upstream=fake.base)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(server.stop)
        return server

    def test_accept_rejects_when_newest_run_drops_the_registration(self):
        now = int(time.time())
        older = fake_run(
            1, started_at=now - 600, metadata={"solidified": DEFAULT_SOLIDIFIED}
        )
        cases = {
            "missing": fake_run(2, started_at=now - 60, metadata={}),
            "invalid": fake_run(
                2,
                started_at=now - 60,
                metadata={
                    "solidified": [
                        {"type": "unknown", "path": "x", "status": "pending"}
                    ]
                },
            ),
        }
        for label, newest in cases.items():
            with self.subTest(label):
                task = fake_task(
                    "t1", review_events=[(1, now - 100)], runs=[older, newest]
                )
                with fake_kanban([task]) as fake:
                    server = self._server(fake)
                    code, body = base.http_json(
                        "POST",
                        server.base + "/api/workbench/verify/t1",
                        {"action": "accept"},
                    )
                self.assertEqual(code, 400, body)
                self.assertEqual(body["error"], "missing_solidified")
                self.assertEqual(fake.task("t1")["status"], "review")

    def test_older_valid_registration_does_not_leak_into_accept(self):
        now = int(time.time())
        newer = fake_run(
            2,
            started_at=now - 60,
            metadata={
                "solidified": [
                    {"type": "routing", "path": "routing.json", "status": "pending"}
                ]
            },
        )
        older = fake_run(
            1, started_at=now - 600, metadata={"solidified": DEFAULT_SOLIDIFIED}
        )
        task = fake_task("t1", review_events=[(1, now - 100)], runs=[older, newer])
        with fake_kanban([task]) as fake:
            server = self._server(fake)
            code, body = base.http_json(
                "GET", server.base + "/api/workbench/inbox"
            )
            self.assertEqual(code, 200, body)
            item = next(entry for entry in body["tasks"] if entry["id"] == "t1")
            self.assertEqual(
                item["solidified"],
                [{"type": "routing", "path": "routing.json", "status": "pending"}],
            )


def hermes_venv_python() -> Path | None:
    """Hermes's own venv can import ``tools.kanban_tools`` (ruamel etc.)."""
    candidate = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    return candidate if candidate.is_file() else None


class ReviewHandoffWrapperTests(VerifyBackendTestCase):
    """Exercise the real ``kanban_request_review`` wrapper, not PATCH review.

    The wrapper adds ``_redact_metadata`` and ``_stamp_worker_session_metadata``
    on top of ``kanban_db.request_review``; the dashboard PATCH tests cannot see
    those. It runs in a subprocess with the throwaway kanban home, so nothing
    under ``~/.hermes/hermes-agent`` is modified.
    """

    def test_wrapper_lands_self_report_solidified_and_session_stamp(self):
        python = hermes_venv_python()
        if python is None:
            self.skipTest(
                "Hermes venv python unavailable; real kanban_request_review "
                "wrapper remains an untested gap"
            )

        card = self.server.create_task("真实 review wrapper", triage=True)
        self.server.seed_metadata(card, {"contract": {"accept": "ok"}})
        run_id = self.server.start_run(card)
        report = {
            "delivered": "改了真实 wrapper 链路",
            "evidence": "test_verify wrapper 用例",
            "uncertain": "没跑真机",
        }
        solidified = [
            {"type": "script", "path": "scripts/wrapper-probe.sh", "status": "pending"}
        ]
        script = (
            "import json, os, sys\n"
            "sys.path.insert(0, os.path.expanduser('~/.hermes/hermes-agent'))\n"
            "from tools import kanban_tools\n"
            "print(kanban_tools._handle_request_review("
            "json.loads(os.environ['WRAPPER_ARGS'])))\n"
        )
        env = os.environ.copy()
        # Never let a developer/CI pin redirect the wrapper at a real board.
        env.pop("HERMES_KANBAN_BOARD", None)
        env.update(
            {
                "HERMES_KANBAN_HOME": str(self.server.kanban_home),
                "HERMES_KANBAN_DB": str(self.server.kanban_db),
                "HERMES_KANBAN_TASK": card,
                "HERMES_KANBAN_RUN_ID": str(run_id),
                "HERMES_SESSION_ID": "sess-wrapper-review",
                "WRAPPER_ARGS": json.dumps(
                    {
                        "summary": "实现完成，转验收",
                        "metadata": {
                            "self_report": report,
                            "solidified": solidified,
                        },
                    }
                ),
            }
        )
        proc = subprocess.run(
            [str(python), "-I", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("status"), "review")
        self.assertEqual(result.get("task_id"), card)

        item = self.inbox_item(card)
        self.assertIsNotNone(item)
        self.assertEqual(item["self_report"], report)
        self.assertFalse(item["self_report_truncated"])
        self.assertEqual(item["solidified"], solidified)

        runs = self.detail(card)["runs"]
        metadata = next(run["metadata"] for run in runs if run["id"] == run_id)
        self.assertEqual(metadata["self_report"], report)
        self.assertEqual(metadata["solidified"], solidified)
        # Only the wrapper stamps this trusted identity; the PATCH path cannot.
        self.assertEqual(metadata["worker_session_id"], "sess-wrapper-review")


class VerifyActionTests(VerifyBackendTestCase):
    def test_accept_moves_to_done_and_leaves_inbox(self):
        card = self.seed_review("验收收下")
        code, body = self.verify(card, "accept")
        self.assertEqual(code, 200, body)
        self.assertEqual(
            body,
            {
                "ok": True,
                "action": "accept",
                "status": "done",
                "comment_written": False,
            },
        )

        detail = self.detail(card)
        self.assertEqual(detail["task"]["status"], "done")
        self.assertEqual(detail["task"]["latest_summary"], "验收通过")
        self.assertIsNone(self.inbox_item(card))

    def test_accept_requires_a_valid_solidified_entry(self):
        cases = {
            "missing": None,
            "empty": [],
            "all invalid": [
                {"type": "unknown", "path": "x", "status": "pending"},
                {"type": "script", "path": "  ", "status": "pending"},
            ],
        }
        for label, solidified in cases.items():
            with self.subTest(label):
                card = self.seed_review("缺固化 " + label, solidified=solidified)
                code, body = self.verify(card, "accept")
                self.assertEqual(code, 400, body)
                self.assertEqual(
                    body,
                    {
                        "ok": False,
                        "error": "missing_solidified",
                        "comment_written": False,
                    },
                )
                self.assertEqual(self.server.status(card), "review")
                self.assertIsNotNone(self.inbox_item(card))

    def test_mixed_solidified_with_one_valid_entry_accepts(self):
        card = self.seed_review(
            "混合固化",
            solidified=[
                {"type": "unknown", "path": "x", "status": "pending"},
                DEFAULT_SOLIDIFIED,
            ],
        )
        self.assertEqual(self.inbox_item(card)["solidified"], [DEFAULT_SOLIDIFIED])
        code, body = self.verify(card, "accept")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "done")

    def test_accept_accepts_upstream_verified_only(self):
        card = self.seed_review(
            "上游已验证",
            solidified={
                "type": "script",
                "path": "scripts/verified.sh",
                "status": "verified",
            },
        )
        self.assertEqual(
            self.inbox_item(card)["solidified"],
            [
                {
                    "type": "script",
                    "path": "scripts/verified.sh",
                    "status": "verified",
                }
            ],
        )
        code, body = self.verify(card, "accept")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "done")

    def test_reject_and_skip_do_not_require_solidified(self):
        skipped = self.seed_review("缺固化跳过", solidified=None)
        code, body = self.verify(skipped, "skip")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "review")
        self.assertIsNotNone(self.inbox_item(skipped))

        rejected = self.seed_review("缺固化打回", solidified=None)
        code, body = self.verify(rejected, "reject", "没有登记可复用件")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "triage")

    def test_reject_requires_reason_and_caps_length(self):
        card = self.seed_review("验收缺理由")
        before = len(self.comments(card))

        code, body = self.verify(card, "reject")
        self.assertEqual(code, 400, body)
        self.assertEqual(body, {"ok": False, "error": "reason_required"})
        self.assertEqual(self.server.status(card), "review")

        code, body = self.verify(card, "reject", "   ")
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "reason_required")

        code, body = self.verify(card, "reject", "x" * 2001)
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "reason_too_long")
        self.assertEqual(self.server.status(card), "review")
        self.assertEqual(len(self.comments(card)), before)

        code, body = self.verify(card, "later")
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_action")
        self.assertEqual(self.server.status(card), "review")

    def test_reject_cap_boundary_is_accepted(self):
        card = self.seed_review("验收边界理由")
        code, body = self.verify(card, "reject", "x" * 2000)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["comment_written"])
        self.assertEqual(self.server.status(card), "triage")

    def test_reject_writes_comment_then_moves_to_triage(self):
        card = self.seed_review("验收打回")
        code, body = self.verify(card, "reject", "  没跑测试，请补上  ")
        self.assertEqual(code, 200, body)
        self.assertEqual(
            body,
            {
                "ok": True,
                "action": "reject",
                "status": "triage",
                "comment_written": True,
            },
        )
        self.assertEqual(self.server.status(card), "triage")

        comments = self.comments(card)
        self.assertEqual(comments[-1]["author"], "workbench")
        self.assertEqual(comments[-1]["body"], "没跑测试，请补上")
        self.assertIsNone(self.inbox_item(card))

    def test_skip_keeps_review_and_card_stays_in_inbox(self):
        card = self.seed_review("验收跳过")
        before = len(self.comments(card))
        code, body = self.verify(card, "skip")
        self.assertEqual(code, 200, body)
        self.assertEqual(
            body,
            {
                "ok": True,
                "action": "skip",
                "status": "review",
                "comment_written": False,
            },
        )
        self.assertEqual(self.server.status(card), "review")
        self.assertEqual(len(self.comments(card)), before)
        self.assertIsNotNone(self.inbox_item(card))

    def test_expected_review_at_guards_the_rendered_round(self):
        card = self.seed_review("跨轮守卫")
        item = self.inbox_item(card)
        self.assertIsNotNone(item)
        first_at = item["review_requested_at"]
        self.assertIsNotNone(first_at)

        # Matching round: the action is allowed.
        code, body = self.verify(card, "skip", expected_review_at=first_at)
        self.assertEqual(code, 200, body)

        # A newer round landed: the old round's evidence must not accept it.
        second_at = int(time.time()) + 5
        self.add_review_event(card, int(time.time() * 1000), second_at)
        code, body = self.verify(card, "accept", expected_review_at=first_at)
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "stale_review")
        self.assertEqual(body["review_requested_at"], second_at)
        self.assertFalse(body["comment_written"])
        self.assertEqual(self.server.status(card), "review")

        # Re-reviewing the new round is allowed once the UI refreshes.
        code, body = self.verify(card, "accept", expected_review_at=second_at)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "done")

    def test_verify_non_review_card_is_409(self):
        triage = self.server.create_task("不是 review", triage=True)
        code, body = self.verify(triage, "accept")
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "not_in_review")
        self.assertEqual(body["status"], "triage")
        self.assertFalse(body["comment_written"])
        self.assertEqual(self.server.status(triage), "triage")

        ready = self.server.create_task("ready 卡", triage=True)
        self.server.seed_metadata(ready, {"contract": {"accept": "ok"}})
        code, body = self.server.dispatch(ready)
        self.assertEqual(code, 200, body)
        code, body = self.verify(ready, "skip")
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "not_in_review")
        self.assertEqual(self.server.status(ready), "ready")

    def test_verify_missing_task_is_404(self):
        code, body = self.verify("t_missing", "accept")
        self.assertEqual(code, 404, body)
        self.assertEqual(
            body, {"ok": False, "error": "not_found", "comment_written": False}
        )

    def test_verify_requires_json_and_same_origin(self):
        card = self.seed_review("验收请求守卫")
        path = "/api/workbench/verify/" + urlparse.quote(card, safe="")
        code, body = self.raw_request(
            "POST", path, b'{"action":"accept"}', {"Content-Type": "text/plain"}
        )
        self.assertEqual(code, 415, body)
        self.assertEqual(body["error"], "unsupported_media_type")

        code, body = self.raw_request(
            "POST",
            path,
            json.dumps({"action": "accept"}).encode("utf-8"),
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        self.assertEqual(code, 403, body)
        self.assertEqual(body["error"], "forbidden_origin")
        self.assertEqual(self.server.status(card), "review")

    def test_sweep_endpoint_requires_json_and_same_origin(self):
        for headers, expected in (
            ({"Content-Type": "text/plain"}, 415),
            (
                {"Content-Type": "application/json", "Origin": "http://evil.example"},
                403,
            ),
        ):
            with self.subTest(headers=headers):
                code, body = self.raw_request(
                    "POST",
                    "/api/workbench/reviews/sweep",
                    b"{}",
                    headers,
                )
                self.assertEqual(code, expected, body)


class SolidifiedVerifiedTests(VerifyBackendTestCase):
    """Manual ``pending -> verified`` is a workbench.db overlay keyed by path."""

    def test_manual_verify_writes_overlay_and_stays_idempotent(self):
        card = self.seed_review("手动已验证")
        path = DEFAULT_SOLIDIFIED["path"]
        code, body = self.mark_verified(card, path)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["path"], path)
        overlay = body["solidified_verified"]
        self.assertEqual(len(overlay), 1)
        self.assertEqual(overlay[0]["path"], path)
        self.assertIsInstance(overlay[0]["verified_at"], int)
        self.assertEqual(self.server.metadata_row(card)["solidified_verified"], overlay)
        self.assertEqual(
            self.inbox_item(card)["metadata"]["solidified_verified"], overlay
        )

        # Repeat click: one entry, same timestamp (strict idempotency).
        code, again = self.mark_verified(card, path)
        self.assertEqual(code, 200, again)
        self.assertEqual(again["solidified_verified"], overlay)
        self.assertEqual(
            len(self.server.metadata_row(card)["solidified_verified"]), 1
        )

        # Upstream stays the worker's original registration: still pending.
        runs = self.detail(card)["runs"]
        registered = [
            run.get("metadata", {}).get("solidified")
            for run in runs
            if isinstance(run.get("metadata"), dict)
        ]
        self.assertIn(DEFAULT_SOLIDIFIED, registered)

    def test_multiple_paths_are_appended_and_verified_independently(self):
        first = {"type": "routing", "path": "routing.json", "status": "pending"}
        second = {"type": "script", "path": "scripts/b.sh", "status": "pending"}
        card = self.seed_review("两条固化", solidified=[first, second])
        self.assertEqual(self.inbox_item(card)["solidified"], [first, second])

        code, body = self.mark_verified(card, second["path"])
        self.assertEqual(code, 200, body)
        code, body = self.mark_verified(card, first["path"])
        self.assertEqual(code, 200, body)
        paths = [entry["path"] for entry in body["solidified_verified"]]
        self.assertEqual(paths, [second["path"], first["path"]])

        code, again = self.mark_verified(card, second["path"])
        self.assertEqual(code, 200, again)
        self.assertEqual(len(again["solidified_verified"]), 2)

    def test_unknown_path_is_missing_solidified(self):
        card = self.seed_review("固化路径不在 run")
        code, body = self.mark_verified(card, "scripts/not-registered.sh")
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "missing_solidified")
        self.assertNotIn("solidified_verified", self.server.metadata_row(card))

    def test_blank_or_non_string_path_is_invalid_request(self):
        card = self.seed_review("固化路径为空")
        for path in ("", "   ", None, 123, [], {}):
            with self.subTest(path=path):
                code, body = self.mark_verified(card, path)
                self.assertEqual(code, 400, body)
                self.assertEqual(body["error"], "invalid_request")
        self.assertNotIn("solidified_verified", self.server.metadata_row(card))

    def test_mark_requires_review_status(self):
        card = self.server.create_task("非 review 标记", triage=True)
        code, body = self.mark_verified(card, DEFAULT_SOLIDIFIED["path"])
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "not_in_review")
        self.assertEqual(body["status"], "triage")
        self.assertNotIn(
            "solidified_verified", self.server.metadata_row(card) or {}
        )

    def test_corrupt_overlay_degrades_and_is_repaired(self):
        card = self.seed_review("坏 overlay")
        path = DEFAULT_SOLIDIFIED["path"]
        self.server.seed_metadata(
            card,
            {
                "bucket": "today",
                "contract": {"accept": "x"},
                "solidified_verified": [
                    {"path": "scripts/nan.sh", "verified_at": float("nan")},
                    {"path": "scripts/inf.sh", "verified_at": float("inf")},
                    {"path": "scripts/huge.sh", "verified_at": 10**400},
                    {"path": "", "verified_at": 1},
                    {"path": "scripts/no-timestamp.sh"},
                    "junk",
                ],
            },
        )
        # A read must not 500 on the corrupt row.
        code, body = self.inbox()
        self.assertEqual(code, 200, body)
        # Marking normalizes the overlay and drops every invalid entry.
        code, body = self.mark_verified(card, path)
        self.assertEqual(code, 200, body)
        self.assertEqual(
            [entry["path"] for entry in body["solidified_verified"]], [path]
        )
        self.assertEqual(
            self.server.metadata_row(card)["solidified_verified"],
            body["solidified_verified"],
        )

    def test_metadata_batch_sanitizes_corrupt_overlay(self):
        """One NaN/corrupt row must not 500 the whole metadata batch."""
        bad = self.server.create_task("坏 metadata 批次", triage=True)
        good = self.server.create_task("好 metadata 批次", triage=True)
        self.server.seed_metadata(
            bad,
            {
                "solidified_verified": [
                    {"path": "scripts/nan.sh", "verified_at": float("nan")},
                    {"path": "scripts/inf.sh", "verified_at": float("inf")},
                    {"path": "scripts/huge.sh", "verified_at": 10**400},
                    {"path": "scripts/ok.sh", "verified_at": 123},
                ]
            },
        )
        self.server.seed_metadata(
            good,
            {
                "solidified_verified": [
                    {"path": "scripts/good.sh", "verified_at": 456}
                ]
            },
        )

        code, rows = self.server.get_metadata([bad, good])
        self.assertEqual(code, 200, rows)
        self.assertEqual(
            rows[bad]["solidified_verified"],
            [{"path": "scripts/ok.sh", "verified_at": 123}],
        )
        self.assertEqual(
            rows[good]["solidified_verified"],
            [{"path": "scripts/good.sh", "verified_at": 456}],
        )

    def test_upstream_failure_is_502(self):
        task = fake_task(
            "t1",
            runs=[fake_run(1, metadata={"solidified": DEFAULT_SOLIDIFIED})],
        )
        with fake_kanban([task]) as fake:
            fake.detail_fail_ids = {"t1"}
            with tempfile.TemporaryDirectory() as tmp:
                server = start_server(Path(tmp) / "srv", upstream=fake.base)
                try:
                    code, body = self.mark_verified(
                        "t1", DEFAULT_SOLIDIFIED["path"], server=server
                    )
                finally:
                    server.stop()
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")

    def test_metadata_unavailable_is_503(self):
        task = fake_task(
            "t1",
            runs=[fake_run(1, metadata={"solidified": DEFAULT_SOLIDIFIED})],
        )
        with fake_kanban([task]) as fake:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                broken_db = root / "broken-db"
                broken_db.mkdir()
                server = start_server(
                    root / "srv",
                    upstream=fake.base,
                    env_extra={"WORKBENCH_DB": str(broken_db)},
                )
                try:
                    code, body = self.mark_verified(
                        "t1", DEFAULT_SOLIDIFIED["path"], server=server
                    )
                finally:
                    server.stop()
        self.assertEqual(code, 503, body)
        self.assertEqual(body["error"], "metadata_unavailable")

    def test_missing_task_is_404(self):
        code, body = self.mark_verified("t_missing", "scripts/x.sh")
        self.assertEqual(code, 404, body)
        self.assertEqual(body["error"], "not_found")

    def test_verify_endpoint_guards_json_and_origin(self):
        card = self.seed_review("固化请求守卫")
        path = (
            "/api/workbench/solidified/"
            + urlparse.quote(card, safe="")
            + "/verify"
        )
        code, body = self.raw_request(
            "POST",
            path,
            json.dumps({"path": DEFAULT_SOLIDIFIED["path"]}).encode("utf-8"),
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(code, 415, body)
        self.assertEqual(body["error"], "unsupported_media_type")

        code, body = self.raw_request(
            "POST",
            path,
            json.dumps({"path": DEFAULT_SOLIDIFIED["path"]}).encode("utf-8"),
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        self.assertEqual(code, 403, body)
        self.assertEqual(body["error"], "forbidden_origin")
        self.assertNotIn("solidified_verified", self.server.metadata_row(card))


class FakeUpstreamFailureTests(unittest.TestCase):
    """Verify mapping and reject dedupe against a controllable upstream."""

    def _server(self, fake: FakeKanban) -> base.WorkbenchServer:
        self._tmp = tempfile.TemporaryDirectory()
        server = start_server(Path(self._tmp.name) / "srv", upstream=fake.base)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(server.stop)
        return server

    @staticmethod
    def _accept_task() -> dict:
        """Review card with a valid solidified record so accept reaches PATCH."""
        return fake_task(
            "t1",
            runs=[fake_run(1, metadata={"solidified": DEFAULT_SOLIDIFIED})],
        )

    def test_patch_409_while_still_review_is_502(self):
        with fake_kanban([self._accept_task()]) as fake:
            fake.patch_mode = "409"
            server = self._server(fake)
            code, body = base.http_json(
                "POST", server.base + "/api/workbench/verify/t1", {"action": "accept"}
            )
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertEqual(body["status"], "review")
        self.assertFalse(body["comment_written"])
        self.assertEqual(fake.task("t1")["status"], "review")

    def test_patch_409_after_another_actor_moved_it_is_409(self):
        with fake_kanban([self._accept_task()]) as fake:
            fake.patch_mode = "409"
            fake.patch_moves_to = "triage"
            server = self._server(fake)
            code, body = base.http_json(
                "POST", server.base + "/api/workbench/verify/t1", {"action": "accept"}
            )
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "not_in_review")
        self.assertEqual(body["status"], "triage")
        self.assertFalse(body["comment_written"])

    def test_patch_200_with_wrong_landed_status_is_502(self):
        with fake_kanban([self._accept_task()]) as fake:
            fake.patch_mode = "wrong_status"
            server = self._server(fake)
            code, body = base.http_json(
                "POST", server.base + "/api/workbench/verify/t1", {"action": "accept"}
            )
        self.assertEqual(code, 502, body)
        self.assertEqual(body["status"], "review")
        self.assertEqual(body["upstream_status"], 200)

    def test_patch_500_is_502(self):
        with fake_kanban([self._accept_task()]) as fake:
            fake.patch_mode = "500"
            server = self._server(fake)
            code, body = base.http_json(
                "POST", server.base + "/api/workbench/verify/t1", {"action": "accept"}
            )
        self.assertEqual(code, 502, body)
        self.assertFalse(body["comment_written"])

    def test_comment_written_is_present_on_404_and_409(self):
        with fake_kanban([fake_task("t1")]) as fake:
            server = self._server(fake)
            fake.patch_mode = "404"
            code, body = base.http_json(
                "POST",
                server.base + "/api/workbench/verify/t1",
                {"action": "reject", "reason": "打回"},
            )
            self.assertEqual(code, 404, body)
            self.assertTrue(body["comment_written"])

            fake.patch_mode = "409"
            code, body = base.http_json(
                "POST",
                server.base + "/api/workbench/verify/t1",
                {"action": "reject", "reason": "再打回"},
            )
            self.assertEqual(code, 502, body)
            self.assertTrue(body["comment_written"])

            fake.patch_mode = "409"
            fake.patch_moves_to = "triage"
            code, body = base.http_json(
                "POST",
                server.base + "/api/workbench/verify/t1",
                {"action": "reject", "reason": "第三次"},
            )
            self.assertEqual(code, 409, body)
            self.assertTrue(body["comment_written"])

    def test_failed_reject_comment_never_patches_status(self):
        with fake_kanban([fake_task("t1")]) as fake:
            fake.comment_status = 500
            server = self._server(fake)
            code, body = base.http_json(
                "POST",
                server.base + "/api/workbench/verify/t1",
                {"action": "reject", "reason": "没达标"},
            )
        self.assertEqual(code, 502, body)
        self.assertEqual(body["comment_written"], False)
        self.assertNotIn(("PATCH", "/api/plugins/kanban/tasks/t1"), fake.calls)

    def test_comment_404_on_reject_is_404(self):
        with fake_kanban([fake_task("t1")]) as fake:
            fake.comment_status = 404
            server = self._server(fake)
            code, body = base.http_json(
                "POST",
                server.base + "/api/workbench/verify/t1",
                {"action": "reject", "reason": "没达标"},
            )
        self.assertEqual(code, 404, body)
        self.assertFalse(body["comment_written"])

    def test_reject_dedupes_comment_inside_the_same_round(self):
        now = int(time.time())
        with fake_kanban([fake_task("t1", review_events=[(7, now - 600)])]) as fake:
            fake.add_comment("t1", "workbench", "同一个理由", now - 100)
            server = self._server(fake)
            before = len(fake.task("t1")["comments"])
            code, body = base.http_json(
                "POST",
                server.base + "/api/workbench/verify/t1",
                {"action": "reject", "reason": "同一个理由"},
            )
            after = len(fake.task("t1")["comments"])
        self.assertEqual(code, 200, body)
        self.assertTrue(body["comment_written"])
        self.assertEqual(after, before)
        self.assertEqual(fake.task("t1")["status"], "triage")

    def test_same_reason_from_a_previous_round_is_not_deduped(self):
        now = int(time.time())
        with fake_kanban([fake_task("t1", review_events=[(8, now - 100)])]) as fake:
            fake.add_comment("t1", "workbench", "同一个理由", now - 600)
            server = self._server(fake)
            before = len(fake.task("t1")["comments"])
            code, body = base.http_json(
                "POST",
                server.base + "/api/workbench/verify/t1",
                {"action": "reject", "reason": "同一个理由"},
            )
            after = len(fake.task("t1")["comments"])
        self.assertEqual(code, 200, body)
        self.assertTrue(body["comment_written"])
        self.assertEqual(after, before + 1)


class AutoCollectTests(unittest.TestCase):
    def _server(self, *, env_extra=None, upstream=None) -> base.WorkbenchServer:
        self._tmp = tempfile.TemporaryDirectory()
        server = start_server(
            Path(self._tmp.name) / "srv", env_extra=env_extra, upstream=upstream
        )
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(server.stop)
        return server

    def _seed_review(
        self, server: base.WorkbenchServer, title: str, *, age: int, summary: str
    ) -> str:
        card = server.create_task(title, triage=True)
        server.seed_metadata(card, {"contract": {"accept": "ok"}})
        code, body = server.dispatch(card)
        self.assertEqual(code, 200, body)
        code, body = server.patch_status(card, "review", summary=summary)
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

    def test_sweep_collects_and_preserves_worker_summary(self):
        server = self._server()
        card = self._seed_review(
            server, "保留自述", age=int(REVIEW_TTL) + 60, summary="worker 真正的自述"
        )

        # Pure inbox read leaves it alone; the explicit sweep collects it.
        code, inbox = base.http_json("GET", server.base + "/api/workbench/inbox")
        self.assertEqual(code, 200, inbox)
        item = next(i for i in inbox["tasks"] if i["id"] == card)
        self.assertTrue(item["auto_collect_due"])
        self.assertEqual(server.status(card), "review")

        code, body = base.http_json(
            "POST", server.base + "/api/workbench/reviews/sweep", {}
        )
        self.assertEqual(code, 200, body)
        self.assertGreaterEqual(body["collected"], 1)
        self.assertIn(card, body["collected_ids"])

        detail = base.http_json(
            "GET",
            server.base + "/api/plugins/kanban/tasks/" + urlparse.quote(card, safe=""),
        )[1]
        self.assertEqual(detail["task"]["status"], "done")
        # The worker handoff summary must survive the auto-collect.
        self.assertEqual(detail["task"]["latest_summary"], "worker 真正的自述")
        auto_comments = [
            c for c in detail["comments"] if c["body"].startswith(AUTO_PREFIX)
        ]
        self.assertEqual(len(auto_comments), 1)
        self.assertIn("review:", auto_comments[0]["body"])

        # Second sweep is a no-op and cannot duplicate the record.
        code, body = base.http_json(
            "POST", server.base + "/api/workbench/reviews/sweep", {}
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["collected"], 0)
        detail = base.http_json(
            "GET",
            server.base + "/api/plugins/kanban/tasks/" + urlparse.quote(card, safe=""),
        )[1]
        self.assertEqual(
            len([c for c in detail["comments"] if c["body"].startswith(AUTO_PREFIX)]),
            1,
        )

    def test_re_review_round_writes_a_new_record(self):
        server = self._server()
        card = self._seed_review(
            server, "二次超时", age=int(REVIEW_TTL) + 60, summary="第一轮自述"
        )
        code, _ = base.http_json(
            "POST", server.base + "/api/workbench/reviews/sweep", {}
        )
        self.assertEqual(code, 200)

        # Put the same card back into review with a fresh review round.
        conn = sqlite3.connect(str(server.kanban_db), timeout=5)
        try:
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (card,))
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'review_requested', '{}', ?)",
                (card, int(time.time()) - int(REVIEW_TTL) - 60),
            )
            conn.commit()
        finally:
            conn.close()

        code, body = base.http_json(
            "POST", server.base + "/api/workbench/reviews/sweep", {}
        )
        self.assertEqual(code, 200, body)
        detail = base.http_json(
            "GET",
            server.base + "/api/plugins/kanban/tasks/" + urlparse.quote(card, safe=""),
        )[1]
        auto_comments = [
            c for c in detail["comments"] if c["body"].startswith(AUTO_PREFIX)
        ]
        self.assertEqual(len(auto_comments), 2)
        self.assertNotEqual(auto_comments[0]["body"], auto_comments[1]["body"])
        self.assertEqual(detail["task"]["latest_summary"], "第一轮自述")

    def test_audit_summary_fallback_when_upstream_requires_a_summary(self):
        now = int(time.time())
        task = fake_task("t1", summary=None, review_events=[(1, now - REVIEW_TTL - 60)])
        with fake_kanban([task]) as fake:
            fake.patch_mode = "require_summary"
            server = self._server(upstream=fake.base)
            code, body = base.http_json(
                "POST", server.base + "/api/workbench/reviews/sweep", {}
            )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["collected"], 1)
        self.assertEqual(len(fake.patch_payloads), 2)
        self.assertNotIn("summary", fake.patch_payloads[0])
        self.assertIn("worker 自述保留", fake.patch_payloads[1]["summary"])
        self.assertIn("worker 自述保留", fake.task("t1")["latest_summary"])
        auto_comments = [
            c for c in fake.task("t1")["comments"] if c["body"].startswith(AUTO_PREFIX)
        ]
        self.assertEqual(len(auto_comments), 1)

    def test_failure_keeps_card_and_backs_off(self):
        now = int(time.time())
        task = fake_task("t1", summary="原自述", review_events=[(1, now - REVIEW_TTL - 60)])
        with fake_kanban([task]) as fake:
            fake.patch_mode = "ok"
            fake.patch_failures = 1
            server = self._server(
                env_extra={"WORKBENCH_REVIEW_BACKOFF_SECONDS": "0.05"},
                upstream=fake.base,
            )
            code, body = base.http_json(
                "POST", server.base + "/api/workbench/reviews/sweep", {}
            )
            self.assertEqual(code, 200, body)
            self.assertEqual(body["failed"], 1)
            self.assertEqual(body["failed_ids"], ["t1"])
            self.assertEqual(fake.task("t1")["status"], "review")
            self.assertEqual(fake.task("t1")["comments"], [])

            code, inbox = base.http_json("GET", server.base + "/api/workbench/inbox")
            self.assertEqual(code, 200, inbox)
            item = next(i for i in inbox["tasks"] if i["id"] == "t1")
            self.assertTrue(item["auto_collect_due"])
            self.assertIsNotNone(item["auto_collect_error"])
            self.assertGreaterEqual(item["auto_collect_error"]["attempts"], 1)

            # Inside the backoff window: no second PATCH.
            patch_calls = len([c for c in fake.calls if c[0] == "PATCH"])
            code, body = base.http_json(
                "POST", server.base + "/api/workbench/reviews/sweep", {}
            )
            self.assertEqual(code, 200, body)
            self.assertEqual(body["skipped"], 1)
            self.assertEqual(len([c for c in fake.calls if c[0] == "PATCH"]), patch_calls)

            time.sleep(0.08)
            code, body = base.http_json(
                "POST", server.base + "/api/workbench/reviews/sweep", {}
            )
            self.assertEqual(code, 200, body)
            self.assertEqual(body["collected"], 1)
        self.assertEqual(fake.task("t1")["status"], "done")
        self.assertEqual(fake.task("t1")["latest_summary"], "原自述")
        auto_comments = [
            c for c in fake.task("t1")["comments"] if c["body"].startswith(AUTO_PREFIX)
        ]
        self.assertEqual(len(auto_comments), 1)

    def test_ttl_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = start_server(
                Path(tmp) / "srv",
                env_extra={"WORKBENCH_REVIEW_TTL_SECONDS": "120"},
            )
            try:
                card = server.create_task("短 TTL", triage=True)
                server.seed_metadata(card, {"contract": {"accept": "ok"}})
                code, body = server.dispatch(card)
                self.assertEqual(code, 200, body)
                code, body = server.patch_status(card, "review", summary="完成")
                self.assertEqual(code, 200, body)
                conn = sqlite3.connect(str(server.kanban_db), timeout=5)
                try:
                    conn.execute(
                        "UPDATE task_events SET created_at = ? "
                        "WHERE task_id = ? AND kind = 'review_requested'",
                        (int(time.time()) - 180, card),
                    )
                    conn.commit()
                finally:
                    conn.close()

                code, inbox = base.http_json(
                    "GET", server.base + "/api/workbench/inbox"
                )
                self.assertEqual(code, 200, inbox)
                self.assertEqual(inbox["review_ttl_seconds"], 120)
                item = next(i for i in inbox["tasks"] if i["id"] == card)
                self.assertTrue(item["auto_collect_due"])

                code, body = base.http_json(
                    "POST", server.base + "/api/workbench/reviews/sweep", {}
                )
                self.assertEqual(code, 200, body)
                self.assertEqual(server.status(card), "done")
            finally:
                server.stop()

    def test_invalid_ttl_env_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = start_server(
                Path(tmp) / "srv",
                env_extra={"WORKBENCH_REVIEW_TTL_SECONDS": "not-a-number"},
            )
            try:
                code, body = base.http_json(
                    "GET", server.base + "/api/workbench/inbox/count"
                )
                self.assertEqual(code, 200, body)
                self.assertEqual(body["review_ttl_seconds"], REVIEW_TTL)
            finally:
                server.stop()

    def test_background_sweeper_collects_without_inbox_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = start_server(
                root / "srv",
                env_extra={"WORKBENCH_REVIEW_SWEEP_SECONDS": "0.4"},
            )
            try:
                card = server.create_task("后台自动收", triage=True)
                server.seed_metadata(card, {"contract": {"accept": "ok"}})
                code, body = server.dispatch(card)
                self.assertEqual(code, 200, body)
                code, body = server.patch_status(card, "review", summary="worker 自述")
                self.assertEqual(code, 200, body)
                conn = sqlite3.connect(str(server.kanban_db), timeout=5)
                try:
                    conn.execute(
                        "UPDATE task_events SET created_at = ? "
                        "WHERE task_id = ? AND kind = 'review_requested'",
                        (int(time.time()) - REVIEW_TTL - 120, card),
                    )
                    conn.commit()
                finally:
                    conn.close()

                deadline = time.time() + 10
                while time.time() < deadline and server.status(card) != "done":
                    time.sleep(0.2)
                self.assertEqual(server.status(card), "done")
                # The PATCH lands before the record comment; wait for it.
                bodies: list[str] = []
                while time.time() < deadline:
                    code, detail = base.http_json(
                        "GET",
                        server.base
                        + "/api/plugins/kanban/tasks/"
                        + urlparse.quote(card, safe=""),
                    )
                    self.assertEqual(code, 200, detail)
                    bodies = [c["body"] for c in detail.get("comments") or []]
                    if any(b.startswith(AUTO_PREFIX) for b in bodies):
                        break
                    time.sleep(0.1)
                self.assertEqual(detail["task"]["latest_summary"], "worker 自述")
                self.assertEqual(
                    len([b for b in bodies if b.startswith(AUTO_PREFIX)]), 1
                )
            finally:
                server.stop()

    def test_auto_collect_does_not_require_solidified(self):
        """48h auto-collect is the queue backstop: no solidified gate."""
        server = self._server()
        card = self._seed_review(
            server, "无固化自动收", age=int(REVIEW_TTL) + 60, summary="完成"
        )
        code, body = base.http_json(
            "POST", server.base + "/api/workbench/reviews/sweep", {}
        )
        self.assertEqual(code, 200, body)
        self.assertIn(card, body["collected_ids"])
        self.assertEqual(server.status(card), "done")

    def test_sweep_ignores_card_under_ttl(self):
        server = self._server()
        card = self._seed_review(
            server, "未超时", age=int(REVIEW_TTL) - 60, summary="完成"
        )
        code, body = base.http_json(
            "POST", server.base + "/api/workbench/reviews/sweep", {}
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["collected"], 0)
        self.assertEqual(server.status(card), "review")


if __name__ == "__main__":
    unittest.main(verbosity=2)