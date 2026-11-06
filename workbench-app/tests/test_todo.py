#!/usr/bin/env python3
"""Repeatable integration tests for Story 1.3 (todo three-zone backend).

Covers the workbench-owned write endpoints introduced for the todo page:

* ``POST /api/workbench/tasks`` — upstream store card (scheduled) + default metadata
* ``PATCH /api/workbench/metadata/{task_id}`` — bucket / contract merge
* ``PATCH /api/workbench/tasks/{task_id}`` — title + contract edit for the drawer

The kanban + workbench DBs are throwaway temp files (see ``test_dispatch``),
so nothing here touches the user's real kanban.db / workbench.db.

Run with::

    python3 workbench-app/tests/test_todo.py -v
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
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

_MISSING = object()


@contextlib.contextmanager
def fixed_upstream(status: int, payload: bytes, *, get_status: int | None = None, get_payload: bytes = b""):
    """A tiny loopback upstream with a fixed POST response.

    ``GET /tasks/<id>`` uses ``get_status``/``get_payload`` when supplied.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, code: int, raw: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._respond(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            if get_status is None:
                self._respond(200, b'{"task":{"id":"t_fake","status":"triage"}}')
            else:
                self._respond(get_status, get_payload)

        def do_PATCH(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._respond(200, b'{"task":{"id":"t_fake","status":"scheduled"}}')

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


def run_server(root: Path, defaults: Path, upstream: str) -> base.WorkbenchServer:
    return base.WorkbenchServer(
        root, defaults, env_extra={"WORKBENCH_UPSTREAM_BASE": upstream}
    ).start()


@contextlib.contextmanager
def parking_upstream(patches: list[bytes]):
    """Upstream whose scheduled PATCH returns 200 without landing there.

    The first PATCH (triage -> todo) and the second (todo -> scheduled) both
    return a task body; the second is a no-op ``todo``. The third PATCH is the
    best-effort restore to triage.
    """

    responses = [
        b'{"task":{"id":"t_fake","status":"todo"}}',
        b'{"task":{"id":"t_fake","status":"todo"}}',
        b'{"task":{"id":"t_fake","status":"triage"}}',
    ]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, code: int, raw: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._respond(200, b'{"task":{"id":"t_fake","status":"triage"}}')

        def do_GET(self) -> None:  # noqa: N802
            self._respond(200, b'{"task":{"id":"t_fake","status":"triage"}}')

        def do_PATCH(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            patches.append(self.rfile.read(length) if length else b"")
            self._respond(
                200,
                responses.pop(0)
                if responses
                else b'{"task":{"id":"t_fake","status":"triage"}}',
            )

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


class TodoBackendTestCase(unittest.TestCase):
    tmp: Path
    server: base.WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-todo-"))
        defaults = cls.tmp / "defaults.json"
        defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
        cls.server = base.WorkbenchServer(cls.tmp, defaults).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def create(self, title: str, bucket=_MISSING, idempotency_key=_MISSING):
        payload = {"title": title}
        if bucket is not _MISSING:
            payload["bucket"] = bucket
        if idempotency_key is not _MISSING:
            payload["idempotency_key"] = idempotency_key
        return base.http_json("POST", self.server.base + "/api/workbench/tasks", payload)

    def patch_metadata(self, task_id: str, patch):
        return base.http_json(
            "PATCH",
            self.server.base + "/api/workbench/metadata/" + urlparse.quote(task_id, safe=""),
            patch,
        )

    def patch_task(self, task_id: str, patch):
        return base.http_json(
            "PATCH",
            self.server.base + "/api/workbench/tasks/" + urlparse.quote(task_id, safe=""),
            patch,
        )

    def raw_request(self, method: str, path: str, data: bytes = b"", headers=None):
        request = urlrequest.Request(
            self.server.base + path,
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
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = raw.decode("utf-8", errors="replace")
                return int(exc.code), parsed
            finally:
                exc.close()

    def metadata(self, task_id: str) -> dict:
        code, rows = self.server.get_metadata([task_id])
        self.assertEqual(code, 200)
        return rows.get(task_id, {})

    def board_titles(self) -> set[str]:
        return {
            card["title"]
            for column in self.server.board()["columns"]
            for card in column["tasks"]
        }


class CreateTaskTests(TodoBackendTestCase):
    def test_create_defaults_to_today_and_empty_contract(self):
        code, body = self.create("第一张")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        task = body["task"]
        self.assertEqual(task["status"], "scheduled")
        self.assertTrue(task["id"])

        expected = {
            "bucket": "today",
            "contract": {"input": "", "accept": "", "ready": False},
        }
        self.assertEqual(body["metadata"], expected)
        self.assertEqual(self.metadata(task["id"]), expected)

        # The card is in the workbench store (scheduled), not triage/todo queues.
        self.assertEqual(self.server.status(task["id"]), "scheduled")
        self.assertIn("第一张", self.board_titles())

        # Empty acceptance still blocks dispatch: creating never auto-dispatches.
        code, dispatch = self.server.dispatch(task["id"])
        self.assertEqual(code, 400, dispatch)
        self.assertEqual(dispatch["error"], "missing_acceptance")

    def test_create_accepts_long_bucket_in_store(self):
        code, body = self.create("长期卡", bucket="long")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["metadata"]["bucket"], "long")
        self.assertEqual(self.server.status(body["task"]["id"]), "scheduled")

    def test_create_rejects_overlong_title(self):
        code, body = self.create("x" * 501)
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_title")

    def test_scheduled_noop_is_502_and_restores_triage(self):
        patches: list[bytes] = []
        root = self.tmp / "parking"
        root.mkdir()
        defaults = root / "defaults.json"
        defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
        with parking_upstream(patches) as upstream:
            server = run_server(root, defaults, upstream)
            try:
                code, body = base.http_json(
                    "POST",
                    server.base + "/api/workbench/tasks",
                    {"title": "停车失败", "bucket": "today"},
                )
            finally:
                server.stop()
        self.assertEqual(code, 502, body)
        self.assertEqual(body.get("task_id"), "t_fake")
        self.assertEqual(
            [json.loads(raw).get("status") for raw in patches],
            ["todo", "scheduled", "triage"],
        )

    def test_create_rejects_empty_title_and_bad_buckets(self):
        before = self.board_titles()
        invalid_titles = ["", "   ", "\n\t"]
        for title in invalid_titles:
            with self.subTest(title=repr(title)):
                code, body = self.create(title)
                self.assertEqual(code, 400, body)
                self.assertEqual(body, {"ok": False, "error": "invalid_title"})

        for bucket in ["todo", "TODAY", "", 1, None, ["today"]]:
            with self.subTest(bucket=repr(bucket)):
                code, body = self.create("坏 bucket " + repr(bucket), bucket=bucket)
                self.assertEqual(code, 400, body)
                self.assertEqual(body, {"ok": False, "error": "invalid_bucket"})

        after = self.board_titles()
        self.assertEqual(before, after)

    def test_create_requires_a_json_object_body(self):
        code, body = base.http_json("POST", self.server.base + "/api/workbench/tasks", [])
        self.assertEqual(code, 400, body)
        self.assertEqual(body, {"ok": False, "error": "invalid_request"})

        code, body = self.raw_request(
            "POST",
            "/api/workbench/tasks",
            b"{not json",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_request")

    def test_create_idempotency_key_returns_the_same_task(self):
        code, first = self.create("幂等卡", idempotency_key="probe-key-1")
        self.assertEqual(code, 200, first)
        code, second = self.create("幂等卡", idempotency_key="probe-key-1")
        self.assertEqual(code, 200, second)
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        self.assertEqual(
            sum(1 for title in self.board_titles() if title == "幂等卡"), 1
        )
        self.assertEqual(self.metadata(first["task"]["id"])["bucket"], "today")

    def test_create_retry_preserves_existing_contract_edits(self):
        code, first = self.create("重试保契约", idempotency_key="retry-contract-key")
        self.assertEqual(code, 200, first)
        task_id = first["task"]["id"]

        code, body = self.patch_metadata(
            task_id,
            {
                "contract": {
                    "accept": "用户补的验收",
                    "input": "需求单",
                    "budget": {"max_minutes": 30},
                }
            },
        )
        self.assertEqual(code, 200, body)

        # Same idempotency key: upstream returns the same card, and the
        # workbench must only fill missing metadata keys — never reset the
        # contract the user edited between attempts.
        code, second = self.create("重试保契约", idempotency_key="retry-contract-key")
        self.assertEqual(code, 200, second)
        self.assertEqual(second["task"]["id"], task_id)
        self.assertEqual(second["metadata"]["contract"]["accept"], "用户补的验收")
        self.assertEqual(second["metadata"]["contract"]["input"], "需求单")
        self.assertEqual(second["metadata"]["contract"]["budget"], {"max_minutes": 30})
        self.assertEqual(second["metadata"]["bucket"], "today")

        # An empty contract written by the first attempt is also left alone
        # (it already has every key).
        code, other = self.create("空契约重试", idempotency_key="retry-empty-key")
        self.assertEqual(code, 200, other)
        code, again = self.create("空契约重试", idempotency_key="retry-empty-key")
        self.assertEqual(code, 200, again)
        self.assertEqual(
            again["metadata"]["contract"], {"input": "", "accept": "", "ready": False}
        )

    def test_create_retry_fills_only_missing_contract_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            server = base.WorkbenchServer(root / "srv", defaults).start()
            try:
                # First attempt creates the card; then simulate a partial
                # contract written by another client before the retry.
                code, created = base.http_json(
                    "POST",
                    server.base + "/api/workbench/tasks",
                    {"title": "缺键重试", "idempotency_key": "fill-keys"},
                )
                self.assertEqual(code, 200, created)
                task_id = created["task"]["id"]

                # Simulate a pre-existing partial contract (e.g. written by a
                # future/older client): accept exists, input/ready do not.
                server.seed_metadata(task_id, {"contract": {"accept": "保留"}})
                code, retry = base.http_json(
                    "POST",
                    server.base + "/api/workbench/tasks",
                    {"title": "缺键重试", "idempotency_key": "fill-keys"},
                )
                self.assertEqual(code, 200, retry)
                self.assertEqual(retry["metadata"]["contract"]["accept"], "保留")
                self.assertEqual(retry["metadata"]["contract"]["input"], "")
                self.assertIs(retry["metadata"]["contract"]["ready"], False)
                self.assertEqual(retry["metadata"]["bucket"], "today")
            finally:
                server.stop()

    def test_create_rejects_invalid_idempotency_keys(self):
        before = self.board_titles()
        for key in ["", "   ", 1, ["k"], "x" * 129]:
            with self.subTest(key=repr(key)):
                code, body = self.create("坏幂等", idempotency_key=key)
                self.assertEqual(code, 400, body)
                self.assertEqual(body, {"ok": False, "error": "invalid_request"})
        self.assertEqual(self.board_titles(), before)

    def test_create_refuses_non_store_landing(self):
        with tempfile.TemporaryDirectory() as tmp, fixed_upstream(
            200,
            b'{"task":{"id":"t_fake","status":"triage"}}',
            get_status=200,
            get_payload=b'{"task":{"id":"t_fake","status":"ready"}}',
        ) as upstream:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            server = run_server(root / "srv", defaults, upstream)
            try:
                code, body = base.http_json(
                    "POST", server.base + "/api/workbench/tasks", {"title": "不该 ready"}
                )
            finally:
                server.stop()
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertEqual(body["status"], "ready")

    def test_create_maps_upstream_4xx_to_400_with_detail(self):
        with tempfile.TemporaryDirectory() as tmp, fixed_upstream(
            400, b'{"detail":"title too long"}'
        ) as upstream:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            server = run_server(root / "srv", defaults, upstream)
            try:
                code, body = base.http_json(
                    "POST", server.base + "/api/workbench/tasks", {"title": "x"}
                )
            finally:
                server.stop()
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_request")
        self.assertEqual(body["detail"], "title too long")
        self.assertEqual(body["upstream_status"], 400)

    def test_create_maps_upstream_5xx_to_502(self):
        with tempfile.TemporaryDirectory() as tmp, fixed_upstream(
            500, b'{"detail":"boom"}'
        ) as upstream:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            server = run_server(root / "srv", defaults, upstream)
            try:
                code, body = base.http_json(
                    "POST", server.base + "/api/workbench/tasks", {"title": "x"}
                )
            finally:
                server.stop()
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertEqual(body["upstream_status"], 500)

    def test_create_metadata_failure_returns_task_id_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            broken_db = root / "broken-db"
            broken_db.mkdir()
            server = base.WorkbenchServer(
                root / "srv", defaults, env_extra={"WORKBENCH_DB": str(broken_db)}
            ).start()
            try:
                code, first = base.http_json(
                    "POST",
                    server.base + "/api/workbench/tasks",
                    {"title": "不可见卡", "idempotency_key": "broken-db-key"},
                )
                self.assertEqual(code, 503, first)
                self.assertEqual(first["error"], "metadata_unavailable")
                self.assertTrue(first["task_id"])

                code, second = base.http_json(
                    "POST",
                    server.base + "/api/workbench/tasks",
                    {"title": "不可见卡", "idempotency_key": "broken-db-key"},
                )
                self.assertEqual(code, 503, second)
                self.assertEqual(second["task_id"], first["task_id"])

                titles = [
                    card["title"]
                    for column in server.board()["columns"]
                    for card in column["tasks"]
                ]
                self.assertEqual(titles.count("不可见卡"), 1)
            finally:
                server.stop()

    def test_create_502_when_upstream_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            closed_port = base.free_port()
            server = base.WorkbenchServer(
                root / "srv",
                defaults,
                env_extra={"WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{closed_port}"},
            ).start()
            try:
                code, body = base.http_json(
                    "POST", server.base + "/api/workbench/tasks", {"title": "x"}
                )
            finally:
                server.stop()
        self.assertEqual(code, 502, body)
        self.assertEqual(body, {"ok": False, "error": "upstream_unavailable"})

    def test_create_does_not_touch_shared_kanban_schema(self):
        self.create("schema-check")
        conn = sqlite3.connect(str(self.server.kanban_db), timeout=5)
        try:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'workbench%'"
                ).fetchall()
            ]
            columns = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(names, [])
        self.assertNotIn("metadata", columns)
        self.assertEqual(integrity, "ok")


class ShellVersionTests(TodoBackendTestCase):
    def test_version_endpoint_reports_shell_fingerprint(self):
        code, body = base.http_json(
            "GET", self.server.base + "/api/workbench/version"
        )
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"))
        self.assertIsInstance(body.get("version"), str)
        self.assertGreater(len(body["version"]), 0)

    def test_shell_assets_are_revalidated(self):
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        for path in ("/", "/static/index.js", "/sw.js"):
            request = urlrequest.Request(
                self.server.base + path, headers={"Accept": "text/html"}
            )
            with opener.open(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(
                    "no-cache",
                    (response.headers.get("Cache-Control") or ""),
                    path,
                )


class UpdateTaskTests(TodoBackendTestCase):
    def test_edit_title_and_contract_persist(self):
        code, created = self.create("原始标题", bucket="today")
        self.assertEqual(code, 200, created)
        task_id = created["task"]["id"]

        code, body = self.patch_task(
            task_id,
            {
                "title": "  改名后的卡  ",
                "contract": {"accept": "跑通测试", "input": "需求单"},
            },
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["task"]["title"], "改名后的卡")
        self.assertEqual(body["metadata"]["contract"]["accept"], "跑通测试")
        self.assertEqual(body["metadata"]["contract"]["input"], "需求单")
        # The title lives upstream, the contract stays workbench-owned.
        self.assertEqual(self.server.task(task_id)["title"], "改名后的卡")
        self.assertEqual(self.metadata(task_id)["contract"]["accept"], "跑通测试")

    def test_edit_rejects_card_outside_store(self):
        code, created = self.create("已派发卡")
        self.assertEqual(code, 200, created)
        task_id = created["task"]["id"]
        code, _ = self.patch_metadata(task_id, {"contract": {"accept": "完成"}})
        self.assertEqual(code, 200)
        code, body = self.server.dispatch(task_id)
        self.assertEqual(code, 200, body)

        code, body = self.patch_task(task_id, {"title": "不该改"})
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "not_in_store")
        self.assertNotEqual(self.server.task(task_id)["title"], "不该改")

    def test_title_only_keeps_bucket_and_contract(self):
        code, created = self.create("只改标题", bucket="long")
        self.assertEqual(code, 200, created)
        task_id = created["task"]["id"]
        code, _ = self.patch_metadata(task_id, {"contract": {"accept": "保留验收"}})
        self.assertEqual(code, 200)

        code, body = self.patch_task(task_id, {"title": "新标题"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["task"]["title"], "新标题")
        self.assertEqual(body["metadata"]["bucket"], "long")
        self.assertEqual(body["metadata"]["contract"]["accept"], "保留验收")
        # No contract keys were sent: the merge must not drop siblings.
        self.assertEqual(body["metadata"]["contract"]["input"], "")

    def test_contract_only_does_not_require_title(self):
        code, created = self.create("只改验收")
        self.assertEqual(code, 200, created)
        task_id = created["task"]["id"]

        code, body = self.patch_task(task_id, {"contract": {"accept": "新验收"}})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["task"]["title"], "只改验收")
        self.assertEqual(self.metadata(task_id)["contract"]["accept"], "新验收")

    def test_invalid_title_and_unknown_keys_are_400(self):
        code, created = self.create("校验标题")
        task_id = created["task"]["id"]

        for patch in (
            {"title": ""},
            {"title": "   "},
            {"title": "x" * 501},
            {"title": 123},
        ):
            code, body = self.patch_task(task_id, patch)
            self.assertEqual(code, 400, (patch, body))
            self.assertEqual(body["error"], "invalid_title")
        self.assertEqual(self.server.task(task_id)["title"], "校验标题")

        code, body = self.patch_task(task_id, {"body": "正文"})
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_request")
        code, body = self.patch_task(task_id, {})
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_request")

    def test_unknown_task_is_404_and_writes_nothing(self):
        before = self.metadata("t_missing")
        code, body = self.patch_task("t_missing", {"title": "不存在的卡"})
        self.assertEqual(code, 404, body)
        self.assertEqual(body["error"], "not_found")
        self.assertEqual(self.metadata("t_missing"), before)


class UpdateMetadataTests(TodoBackendTestCase):
    def test_patch_bucket_and_contract_merges_nested_keys(self):
        code, created = self.create("编辑验收", bucket="today")
        self.assertEqual(code, 200, created)
        task_id = created["task"]["id"]

        code, body = self.patch_metadata(
            task_id, {"contract": {"accept": "  跑通测试  "}}
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(
            body["metadata"],
            {
                "bucket": "today",
                "contract": {"input": "", "accept": "跑通测试", "ready": False},
            },
        )

        code, body = self.patch_metadata(
            task_id, {"bucket": "long", "contract": {"input": "需求单"}}
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["metadata"]["bucket"], "long")
        # Updating one contract key keeps the siblings written earlier.
        self.assertEqual(
            body["metadata"]["contract"],
            {"input": "需求单", "accept": "跑通测试", "ready": False},
        )
        self.assertEqual(self.metadata(task_id), body["metadata"])

    def test_acceptance_enables_dispatch_and_never_clobbers_snapshot(self):
        code, created = self.create("补验收")
        task_id = created["task"]["id"]

        code, body = self.server.dispatch(task_id)
        self.assertEqual(code, 400, body)

        code, body = self.patch_metadata(task_id, {"contract": {"accept": "完成标准"}})
        self.assertEqual(code, 200, body)
        code, body = self.server.dispatch(task_id)
        self.assertEqual(code, 200, body)

        snapshot_before = self.metadata(task_id)["contract_defaults"]

        # Editing bucket/contract after dispatch must preserve the snapshot.
        code, body = self.patch_metadata(
            task_id, {"bucket": "long", "contract": {"accept": "改过的标准"}}
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["metadata"]["contract_defaults"], snapshot_before)
        self.assertEqual(body["metadata"]["contract"]["accept"], "改过的标准")

        # Whitespace-only acceptance is stored empty and blocks dispatch again.
        other_code, other = self.create("清空验收")
        other_id = other["task"]["id"]
        code, body = self.patch_metadata(other_id, {"contract": {"accept": "有"}})
        self.assertEqual(code, 200, body)
        code, body = self.patch_metadata(other_id, {"contract": {"accept": "   "}})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["metadata"]["contract"]["accept"], "")
        code, body = self.server.dispatch(other_id)
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "missing_acceptance")

    def test_patch_rejects_invalid_metadata_without_writing(self):
        code, created = self.create("校验")
        task_id = created["task"]["id"]
        before = self.metadata(task_id)

        invalid = [
            ({}, "invalid_metadata"),
            ({"contract_defaults": {"max_minutes": 1}}, "invalid_metadata"),
            ({"unknown": 1}, "invalid_metadata"),
            ({"bucket": "todo"}, "invalid_bucket"),
            ({"bucket": "today", "contract_defaults": {}}, "invalid_metadata"),
            ({"contract": {}}, "invalid_contract"),
            ({"contract": {"accept": 1}}, "invalid_contract"),
            ({"contract": {"input": None}}, "invalid_contract"),
            ({"contract": {"ready": "false"}}, "invalid_contract"),
            ({"contract": {"unknown": "x"}}, "invalid_contract"),
            ({"contract": {"budget": {}}}, "invalid_contract"),
            ({"contract": {"accept": "x" * 2001}}, "invalid_contract"),
            ({"contract": {"input": "x" * 4001}}, "invalid_contract"),
            ({"contract": {"budget": {"max_minutes": 10081}}}, "invalid_contract"),
            ({"contract": {"budget": {"max_cost": -1}}}, "invalid_contract"),
            ({"contract": {"budget": {"deny_paths": []}}}, "invalid_contract"),
            ({"contract": {"budget": {"deny_paths": ["ok", 1]}}}, "invalid_contract"),
            (
                {"contract": {"budget": {"deny_paths": [f"/p{i}" for i in range(101)]}}},
                "invalid_contract",
            ),
            ({"contract": {"budget": {"unknown": 1}}}, "invalid_contract"),
        ]
        for patch, error in invalid:
            with self.subTest(patch=patch):
                code, body = self.patch_metadata(task_id, patch)
                self.assertEqual(code, 400, body)
                self.assertEqual(body, {"ok": False, "error": error})
                self.assertEqual(self.metadata(task_id), before)

    def test_patch_can_write_valid_budget_override(self):
        code, created = self.create("预算覆盖")
        task_id = created["task"]["id"]
        code, body = self.patch_metadata(
            task_id,
            {"contract": {"budget": {"max_minutes": 30, "deny_paths": [" /tmp/only "]}}},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(
            body["metadata"]["contract"]["budget"],
            {"max_minutes": 30, "deny_paths": ["/tmp/only"]},
        )

        # Dispatch snapshots the card override and keeps the rest global.
        code, body = self.patch_metadata(task_id, {"contract": {"accept": "ok"}})
        self.assertEqual(code, 200, body)
        code, body = self.server.dispatch(task_id)
        self.assertEqual(code, 200, body)
        defaults = self.metadata(task_id)["contract_defaults"]
        self.assertEqual(defaults["max_minutes"], 30)
        self.assertEqual(defaults["deny_paths"], ["/tmp/only"])
        self.assertEqual(defaults["max_cost"], base.VALID_DEFAULTS["budget"]["max_cost"])

    def test_patch_unknown_task_is_404_and_writes_nothing(self):
        code, body = self.patch_metadata("t_orphan", {"bucket": "long"})
        self.assertEqual(code, 404, body)
        self.assertEqual(body, {"ok": False, "error": "not_found"})
        code, rows = self.server.get_metadata(["t_orphan"])
        self.assertEqual(code, 200)
        self.assertEqual(rows, {})

    def test_patch_whitespace_task_id_is_400(self):
        code, body = self.patch_metadata("   ", {"bucket": "long"})
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "invalid_metadata")

    def test_patch_accepts_boundary_lengths_and_deny_paths(self):
        code, created = self.create("边界")
        task_id = created["task"]["id"]

        code, body = self.patch_metadata(
            task_id,
            {"contract": {"accept": "a" * 2000, "input": "b" * 4000}},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(len(body["metadata"]["contract"]["accept"]), 2000)
        self.assertEqual(len(body["metadata"]["contract"]["input"]), 4000)

        paths = [f"/p{i}" for i in range(100)]
        code, body = self.patch_metadata(
            task_id, {"contract": {"budget": {"deny_paths": paths}}}
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(len(body["metadata"]["contract"]["budget"]["deny_paths"]), 100)

    def test_patch_503_when_metadata_db_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            broken_db = root / "broken-db"
            broken_db.mkdir()
            server = base.WorkbenchServer(
                root / "srv", defaults, env_extra={"WORKBENCH_DB": str(broken_db)}
            ).start()
            try:
                code, created = base.http_json(
                    "POST",
                    server.base + "/api/plugins/kanban/tasks",
                    {"title": "metadata-down", "triage": True},
                )
                self.assertEqual(code, 200, created)
                task_id = created["task"]["id"]
                code, body = base.http_json(
                    "PATCH",
                    server.base + "/api/workbench/metadata/" + task_id,
                    {"bucket": "today"},
                )
            finally:
                server.stop()
        self.assertEqual(code, 503, body)
        self.assertEqual(body, {"ok": False, "error": "metadata_unavailable"})

    def test_patch_502_when_upstream_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            closed_port = base.free_port()
            server = base.WorkbenchServer(
                root / "srv",
                defaults,
                env_extra={"WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{closed_port}"},
            ).start()
            try:
                code, body = base.http_json(
                    "PATCH",
                    server.base + "/api/workbench/metadata/t_anything",
                    {"bucket": "today"},
                )
            finally:
                server.stop()
        self.assertEqual(code, 502, body)
        self.assertEqual(body, {"ok": False, "error": "upstream_unavailable"})


class RequestGuardTests(TodoBackendTestCase):
    def test_post_and_patch_require_json_content_type(self):
        code, body = self.raw_request(
            "POST",
            "/api/workbench/tasks",
            b'{"title":"x"}',
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(code, 415, body)
        self.assertEqual(body, {"ok": False, "error": "unsupported_media_type"})

        # urllib adds x-www-form-urlencoded when data has no explicit type.
        code, body = self.raw_request("POST", "/api/workbench/tasks", b'{"title":"x"}')
        self.assertEqual(code, 415, body)
        self.assertEqual(body["error"], "unsupported_media_type")

        code, body = self.raw_request(
            "PATCH",
            "/api/workbench/metadata/t_x",
            b'{"bucket":"today"}',
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(code, 415, body)
        self.assertEqual(body["error"], "unsupported_media_type")

    def test_cross_origin_is_403_and_same_origin_is_allowed(self):
        code, body = self.raw_request(
            "POST",
            "/api/workbench/tasks",
            json.dumps({"title": "跨域卡"}).encode("utf-8"),
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        self.assertEqual(code, 403, body)
        self.assertEqual(body, {"ok": False, "error": "forbidden_origin"})
        self.assertNotIn("跨域卡", self.board_titles())

        code, body = self.raw_request(
            "POST",
            "/api/workbench/tasks",
            json.dumps({"title": "同源卡"}).encode("utf-8"),
            {"Content-Type": "application/json", "Origin": self.server.base},
        )
        self.assertEqual(code, 200, body)
        self.assertIn("同源卡", self.board_titles())

    def test_dispatch_cross_origin_403_and_same_origin_forwarded(self):
        code, created = self.create("dispatch-origin")
        task_id = created["task"]["id"]
        path = "/api/workbench/dispatch/" + task_id

        code, body = self.raw_request(
            "POST",
            path,
            b"{}",
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        self.assertEqual(code, 403, body)
        self.assertEqual(body, {"ok": False, "error": "forbidden_origin"})
        self.assertEqual(self.server.status(task_id), "scheduled")

        # Same-origin passes the guard and reaches the acceptance check.
        code, body = self.raw_request(
            "POST",
            path,
            b"{}",
            {"Content-Type": "application/json", "Origin": self.server.base},
        )
        self.assertEqual(code, 400, body)
        self.assertEqual(body["error"], "missing_acceptance")

    def test_body_size_limit_is_413(self):
        big = json.dumps({"title": "x" * 70000}).encode("utf-8")
        code, body = self.raw_request(
            "POST",
            "/api/workbench/tasks",
            big,
            {"Content-Type": "application/json"},
        )
        self.assertEqual(code, 413, body)
        self.assertEqual(body, {"ok": False, "error": "payload_too_large"})

        big_patch = json.dumps({"contract": {"accept": "x" * 70000}}).encode("utf-8")
        code, body = self.raw_request(
            "PATCH",
            "/api/workbench/metadata/t_x",
            big_patch,
            {"Content-Type": "application/json"},
        )
        self.assertEqual(code, 413, body)
        self.assertEqual(body["error"], "payload_too_large")

    def test_malformed_json_is_bad_request(self):
        code, body = self.raw_request(
            "PATCH",
            "/api/workbench/metadata/t_x",
            b"{",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(code, 400, body)
        self.assertEqual(body, {"ok": False, "error": "invalid_request"})


class TaskLockTests(unittest.TestCase):
    """update_metadata must not clear acceptance inside a dispatch PATCH window."""

    def test_metadata_update_waits_for_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = root / "defaults.json"
            defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
            target = base.WorkbenchServer(root / "target", defaults).start()
            patch_seen = threading.Event()
            release_patch = threading.Event()
            target_base = target.base

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def _respond(self, status: int, raw: bytes) -> None:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def _handle(self) -> None:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length) if length else None
                    if self.command == "PATCH" and self.path.startswith(
                        "/api/plugins/kanban/tasks/"
                    ):
                        try:
                            payload = json.loads(body or b"{}")
                        except json.JSONDecodeError:
                            payload = {}
                        if payload.get("status") == "ready":
                            patch_seen.set()
                            release_patch.wait(timeout=10)
                    request = urlrequest.Request(
                        target_base + self.path,
                        data=body,
                        method=self.command,
                        headers={
                            "Content-Type": self.headers.get("Content-Type", "application/json"),
                            "Accept": "application/json",
                        },
                    )
                    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
                    try:
                        with opener.open(request, timeout=5) as response:
                            raw = response.read()
                            status = int(response.status)
                    except urlerror.HTTPError as exc:
                        try:
                            raw = exc.read()
                            status = int(exc.code)
                        finally:
                            exc.close()
                    self._respond(status, raw)

                do_GET = _handle
                do_PATCH = _handle
                do_POST = _handle

                def log_message(self, *args) -> None:
                    pass

            proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            server = base.WorkbenchServer(
                root / "dispatch",
                defaults,
                env_extra={
                    "WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{proxy.server_address[1]}",
                    "HERMES_KANBAN_HOME": str(target.kanban_home),
                    "WORKBENCH_DB": str(target.db_path),
                },
            ).start()
            try:
                card = target.create_task("lock-card", triage=True)
                target.seed_metadata(
                    card, {"bucket": "today", "contract": {"accept": "ok"}}
                )
                results: dict[str, tuple[int, dict]] = {}

                dispatch_thread = threading.Thread(
                    target=lambda: results.__setitem__("dispatch", server.dispatch(card))
                )
                dispatch_thread.start()
                self.assertTrue(
                    patch_seen.wait(timeout=5), "dispatch never reached the ready PATCH"
                )

                metadata_done = threading.Event()

                def run_metadata() -> None:
                    results["metadata"] = server.patch_metadata(
                        card, {"contract": {"accept": ""}}
                    )
                    metadata_done.set()

                metadata_thread = threading.Thread(target=run_metadata)
                metadata_thread.start()
                self.assertFalse(
                    metadata_done.wait(timeout=0.4),
                    "metadata update did not wait for the dispatch lock",
                )

                release_patch.set()
                dispatch_thread.join(timeout=10)
                metadata_thread.join(timeout=10)
                self.assertFalse(dispatch_thread.is_alive())
                self.assertFalse(metadata_thread.is_alive())
                self.assertEqual(results["dispatch"][0], 200, results["dispatch"])
                self.assertEqual(results["metadata"][0], 200, results["metadata"])
                self.assertEqual(target.status(card), "ready")
                final = target.metadata_row(card)
                self.assertEqual(final["contract"]["accept"], "")
                self.assertIn("contract_defaults", final)
            finally:
                release_patch.set()
                server.stop()
                proxy.shutdown()
                proxy.server_close()
                target.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)