#!/usr/bin/env python3
"""Repeatable integration tests for Story 1.2 (contract gate + dispatch).

The test process is stdlib-only; the workbench server itself is started as a
subprocess with the Hermes runtime Python in a throwaway ``HERMES_KANBAN_HOME``
and ``WORKBENCH_DB``. No test touches the user's real kanban.db / workbench.db.

Run with::

    python3 workbench-app/tests/test_dispatch.py -v
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "workbench-app"
SERVER = APP_DIR / "server.py"

# The repo defaults file is the single source of truth; tests assert the
# dispatch snapshot/ETA against it instead of duplicating the numbers.
VALID_DEFAULTS = json.loads((APP_DIR / "defaults.json").read_text(encoding="utf-8"))


def runtime_python() -> str:
    override = os.environ.get("WORKBENCH_PYTHON", "").strip()
    if override and Path(override).is_file():
        return override
    tools = sorted(
        glob.glob(str(Path.home() / ".hermes" / "tools" / "python-3.14*" / "bin" / "python3")),
        reverse=True,
    )
    for candidate in tools:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    venv = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return sys.executable


PYTHON = runtime_python()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(method: str, url: str, payload=None, timeout: float = 10.0, headers=None):
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    req = urlrequest.Request(url, data=data, headers=request_headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return int(resp.status), (json.loads(raw) if raw else None)
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
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise AssertionError(f"{method} {url} failed: {exc}") from exc


class WorkbenchServer:
    """One isolated workbench subprocess + helpers for its temp DBs."""

    def __init__(self, root: Path, defaults_path: Path, env_extra=None) -> None:
        self.root = Path(root)
        self.kanban_home = self.root / "kanban-home"
        self.db_path = self.root / "workbench.db"
        self.defaults_path = Path(defaults_path)
        self.env_extra = dict(env_extra or {})
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    @property
    def kanban_db(self) -> Path:
        return self.kanban_home / "kanban.db"

    def start(self) -> "WorkbenchServer":
        env = os.environ.copy()
        env["HERMES_KANBAN_HOME"] = str(self.kanban_home)
        env["WORKBENCH_DB"] = str(self.db_path)
        env["WORKBENCH_DEFAULTS"] = str(self.defaults_path)
        env.pop("WORKBENCH_UPSTREAM_BASE", None)
        env.update(self.env_extra)
        self.proc = subprocess.Popen(
            [PYTHON, "-I", str(SERVER), "--host", "127.0.0.1", "--port", str(self.port)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out, err = self.proc.communicate(timeout=2)
                raise AssertionError(f"server exited ({self.proc.returncode})\nstdout:\n{out}\nstderr:\n{err}")
            try:
                code, body = http_json("GET", self.base + "/healthz", timeout=1)
            except AssertionError:
                time.sleep(0.15)
                continue
            if code == 200 and body == {"ok": True}:
                return self
            time.sleep(0.15)
        self.stop()
        raise AssertionError("server did not become healthy in 30s")

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            self.proc.communicate(timeout=1)
        except Exception:
            pass
        self.proc = None

    def __enter__(self) -> "WorkbenchServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- kanban upstream --------------------------------------------------

    def create_task(self, title: str, *, triage: bool = True, parents=None) -> str:
        payload = {"title": title, "body": "test", "triage": triage}
        if parents:
            payload["parents"] = list(parents)
        code, data = http_json("POST", self.base + "/api/plugins/kanban/tasks", payload)
        if code != 200:
            raise AssertionError(f"create_task failed: {code} {data}")
        return data["task"]["id"]

    def task(self, task_id: str) -> dict:
        code, data = http_json(
            "GET", self.base + "/api/plugins/kanban/tasks/" + urlparse.quote(task_id, safe="")
        )
        if code != 200:
            raise AssertionError(f"get task failed: {code} {data}")
        return data["task"]

    def status(self, task_id: str) -> str:
        return self.task(task_id)["status"]

    def board(self, include_archived: bool = True) -> dict:
        suffix = "?include_archived=true" if include_archived else ""
        code, data = http_json("GET", self.base + "/api/plugins/kanban/board" + suffix)
        if code != 200:
            raise AssertionError(f"board failed: {code} {data}")
        return data

    def ready_ids(self) -> list[str]:
        for column in self.board()["columns"]:
            if column["name"] == "ready":
                return [card["id"] for card in column["tasks"]]
        return []

    def patch_status(self, task_id: str, status: str, **extra):
        return http_json(
            "PATCH",
            self.base + "/api/plugins/kanban/tasks/" + urlparse.quote(task_id, safe=""),
            {"status": status, **extra},
        )

    # -- workbench metadata / dispatch ------------------------------------

    def seed_metadata(self, task_id: str, metadata: dict) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS task_metadata ("
                " task_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO task_metadata (task_id, metadata, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET metadata = excluded.metadata, "
                "updated_at = excluded.updated_at",
                (task_id, json.dumps(metadata), int(time.time() * 1000)),
            )
            conn.commit()
        finally:
            conn.close()

    def metadata_row(self, task_id: str) -> dict | None:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            row = conn.execute(
                "SELECT metadata FROM task_metadata WHERE task_id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None

    def metadata_token(self, task_id: str) -> str | None:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            row = conn.execute(
                "SELECT write_token FROM task_metadata WHERE task_id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def set_metadata_token(self, task_id: str, token: str) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            conn.execute(
                "UPDATE task_metadata SET write_token = ? WHERE task_id = ?", (token, task_id)
            )
            conn.commit()
        finally:
            conn.close()

    def dispatch(self, task_id: str, timeout: float = 10.0):
        return http_json(
            "POST",
            self.base + "/api/workbench/dispatch/" + urlparse.quote(task_id, safe=""),
            {},
            timeout=timeout,
        )

    def get_metadata(self, ids: list[str]):
        query = urlparse.quote(",".join(ids), safe="")
        return http_json("GET", self.base + "/api/workbench/metadata?ids=" + query)

    def patch_metadata(self, task_id: str, patch: dict):
        return http_json(
            "PATCH",
            self.base + "/api/workbench/metadata/" + urlparse.quote(task_id, safe=""),
            patch,
        )

    # -- direct DB manipulation (only for states upstream REST cannot set) --

    def set_kanban_status(self, task_id: str, status: str) -> None:
        conn = sqlite3.connect(str(self.kanban_db), timeout=5)
        try:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            conn.commit()
        finally:
            conn.close()

    def add_kanban_event(self, task_id: str, kind: str, payload: dict) -> None:
        conn = sqlite3.connect(str(self.kanban_db), timeout=5)
        try:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, ?, ?, ?)",
                (task_id, kind, json.dumps(payload), int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()


class ServerTestCase(unittest.TestCase):
    """A class-level isolated server with valid defaults."""

    tmp: Path
    server: WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-test-"))
        defaults = cls.tmp / "defaults.json"
        defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")
        cls.server = WorkbenchServer(cls.tmp, defaults).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def seed_accept(self, task_id: str, accept: str = "done means tests pass") -> None:
        self.server.seed_metadata(
            task_id, {"contract": {"accept": accept}, "bucket": "today"}
        )

    def assert_no_snapshot(self, task_id: str) -> None:
        row = self.server.metadata_row(task_id)
        if row is not None:
            self.assertNotIn("contract_defaults", row)


class DispatchTests(ServerTestCase):
    def test_healthz_and_board(self):
        code, body = http_json("GET", self.server.base + "/healthz")
        self.assertEqual((code, body), (200, {"ok": True}))
        self.assertIsInstance(self.server.board()["columns"], list)

    def test_dispatch_missing_acceptance(self):
        card = self.server.create_task("missing-acceptance", triage=True)
        before = set(self.server.ready_ids())

        code, body = self.server.dispatch(card)
        self.assertEqual(code, 400)
        self.assertEqual(body, {"ok": False, "error": "missing_acceptance"})
        self.assertEqual(self.server.status(card), "triage")
        self.assertEqual(set(self.server.ready_ids()), before)
        self.assert_no_snapshot(card)

        # Whitespace-only acceptance is equally empty.
        self.server.seed_metadata(card, {"contract": {"accept": "   "}})
        code, body = self.server.dispatch(card)
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "missing_acceptance")
        self.assertEqual(self.server.status(card), "triage")

    def test_dispatch_success_snapshot_and_eta(self):
        card = self.server.create_task("dispatch-success", triage=True)
        self.seed_accept(card)

        code, body = self.server.dispatch(card)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "ready")
        self.assertRegex(body["eta"], r"^\d{2}:\d{2}$")

        ready = self.server.ready_ids()
        self.assertIn(card, ready)
        self.assertEqual(body["position"], ready.index(card) + 1)
        self.assertEqual(self.server.status(card), "ready")

        # ETA = now + minutes_per_task * position (HH:MM, minute precision).
        hour, minute = (int(part) for part in body["eta"].split(":"))
        now = datetime.now()
        candidates = [
            (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            for offset in (-1, 0, 1)
        ]
        eta = min(candidates, key=lambda candidate: abs((candidate - now).total_seconds()))
        expected = VALID_DEFAULTS["queue"]["minutes_per_task"] * body["position"] * 60
        self.assertLessEqual(abs((eta - now).total_seconds() - expected), 90)

        metadata = self.server.metadata_row(card)
        self.assertIsNotNone(metadata)
        defaults = metadata["contract_defaults"]
        self.assertEqual(
            set(defaults),
            {"max_minutes", "max_cost", "deny_paths"},
        )
        self.assertEqual(defaults, VALID_DEFAULTS["budget"])
        self.assertEqual(metadata["contract"]["accept"], "done means tests pass")

        # The same snapshot is visible through the read endpoint.
        code, rows = self.server.get_metadata([card])
        self.assertEqual(code, 200)
        self.assertEqual(rows[card]["contract_defaults"], VALID_DEFAULTS["budget"])

    def test_snapshot_uses_card_budget_override(self):
        card = self.server.create_task("budget-override", triage=True)
        self.server.seed_metadata(
            card,
            {
                "contract": {
                    "accept": "ok",
                    "budget": {"max_minutes": 30, "deny_paths": ["/tmp/only"]},
                }
            },
        )
        code, body = self.server.dispatch(card)
        self.assertEqual(code, 200, body)
        defaults = self.server.metadata_row(card)["contract_defaults"]
        self.assertEqual(defaults["max_minutes"], 30)
        self.assertEqual(defaults["max_cost"], VALID_DEFAULTS["budget"]["max_cost"])
        self.assertEqual(defaults["deny_paths"], ["/tmp/only"])

    def test_invalid_card_overrides_fall_back_to_global(self):
        variants = [
            {"max_minutes": 10081},
            {"max_minutes": -1},
            {"max_minutes": "30"},
            {"max_cost": 1001},
            {"max_cost": -0.5},
            {"deny_paths": "not-a-list"},
            {"deny_paths": [1, "valid"]},
        ]
        for index, override in enumerate(variants):
            with self.subTest(index=index):
                card = self.server.create_task(f"bad-override-{index}", triage=True)
                self.server.seed_metadata(
                    card,
                    {"contract": {"accept": "ok", "budget": override}},
                )
                code, body = self.server.dispatch(card)
                self.assertEqual(code, 200, body)
                defaults = self.server.metadata_row(card)["contract_defaults"]
                self.assertEqual(defaults, VALID_DEFAULTS["budget"])

    def test_metadata_read_omits_ids_without_rows(self):
        first = self.server.create_task("metadata-first", triage=True)
        missing = self.server.create_task("metadata-missing", triage=True)
        third = self.server.create_task("metadata-third", triage=True)
        self.server.seed_metadata(first, {"contract": {"accept": "a"}, "bucket": "today"})
        self.server.seed_metadata(third, {"bucket": "long"})

        code, rows = self.server.get_metadata([first, missing, third])
        self.assertEqual(code, 200)
        self.assertEqual(set(rows), {first, third})
        self.assertEqual(rows[first]["bucket"], "today")
        self.assertEqual(rows[third], {"bucket": "long"})

        code, rows = http_json("GET", self.server.base + "/api/workbench/metadata")
        self.assertEqual((code, rows), (200, {}))

    def test_metadata_chunking_201_ids(self):
        ids = [f"t_chunk_{index:03d}" for index in range(201)]
        for task_id in ids:
            self.server.seed_metadata(task_id, {"bucket": "today"})

        # Server-side single-request cap remains 200 (first 200 ids kept).
        code, rows = self.server.get_metadata(ids)
        self.assertEqual(code, 200)
        self.assertEqual(len(rows), 200)
        self.assertNotIn(ids[-1], rows)

        # The frontend sends 100-id chunks; merged result has all 201.
        merged: dict = {}
        for start in range(0, len(ids), 100):
            code, rows = self.server.get_metadata(ids[start:start + 100])
            self.assertEqual(code, 200)
            merged.update(rows)
        self.assertEqual(len(merged), 201)
        self.assertEqual(set(merged), set(ids))

    def test_direct_patch_ready_gate(self):
        card = self.server.create_task("gate-direct", triage=True)

        code, body = self.server.patch_status(card, "ready")
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "missing_acceptance")
        self.assertEqual(self.server.status(card), "triage")

        self.seed_accept(card)
        code, body = self.server.patch_status(card, "ready")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["task"]["status"], "ready")
        # A direct PATCH is a legitimate native transition, not a dispatch:
        # it must not fabricate a contract_defaults snapshot.
        self.assert_no_snapshot(card)

    def test_non_ready_patch_is_forwarded(self):
        card = self.server.create_task("gate-non-ready", triage=True)
        code, body = self.server.patch_status(card, "todo")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["task"]["status"], "todo")

        code, body = http_json(
            "PATCH",
            self.server.base + "/api/plugins/kanban/tasks/" + card,
            {"title": "renamed"},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["task"]["title"], "renamed")

    def test_direct_patch_ready_unknown_id_is_404_not_400(self):
        code, body = self.server.patch_status("t_does_not_exist", "ready")
        self.assertEqual(code, 404, body)

    def test_create_without_triage_is_gated(self):
        code, body = http_json(
            "POST",
            self.server.base + "/api/plugins/kanban/tasks",
            {"title": "would-land-ready", "body": "test"},
        )
        self.assertEqual(code, 400, body)
        self.assertEqual(body, {"ok": False, "error": "missing_acceptance"})

        board = self.server.board()
        titles = {
            card["title"]
            for column in board["columns"]
            for card in column["tasks"]
        }
        self.assertNotIn("would-land-ready", titles)

        # triage=True is the supported creation path through the gate.
        card = self.server.create_task("triage-created", triage=True)
        self.assertEqual(self.server.status(card), "triage")

        # String "true" is coerced the same way the upstream pydantic model
        # would coerce it.
        code, body = http_json(
            "POST",
            self.server.base + "/api/plugins/kanban/tasks",
            {"title": "triage-string-true", "body": "test", "triage": "true"},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["task"]["status"], "triage")

    def test_bulk_ready_gate_rejects_whole_batch(self):
        first = self.server.create_task("bulk-gate-first", triage=True)
        second = self.server.create_task("bulk-gate-second", triage=True)
        self.seed_accept(first)

        code, body = http_json(
            "POST",
            self.server.base + "/api/plugins/kanban/tasks/bulk",
            {"ids": [first, second], "status": "ready"},
        )
        self.assertEqual(code, 400, body)
        self.assertEqual(body, {"ok": False, "error": "missing_acceptance"})
        self.assertEqual(self.server.status(first), "triage")
        self.assertEqual(self.server.status(second), "triage")

        self.seed_accept(second)
        code, body = http_json(
            "POST",
            self.server.base + "/api/plugins/kanban/tasks/bulk",
            {"ids": [first, second], "status": "ready"},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(first), "ready")
        self.assertEqual(self.server.status(second), "ready")

        # Non-ready bulk patches are unaffected by the gate.
        third = self.server.create_task("bulk-gate-third", triage=True)
        code, body = http_json(
            "POST",
            self.server.base + "/api/plugins/kanban/tasks/bulk",
            {"ids": [third], "status": "todo"},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(third), "todo")

    def test_reclaim_gate(self):
        card = self.server.create_task("reclaim-gate", triage=True)
        self.server.set_kanban_status(card, "running")

        code, body = http_json(
            "POST",
            self.server.base + "/api/plugins/kanban/tasks/" + card + "/reclaim",
            {},
        )
        self.assertEqual(code, 400, body)
        self.assertEqual(body, {"ok": False, "error": "missing_acceptance"})
        self.assertEqual(self.server.status(card), "running")

        self.seed_accept(card)
        code, body = http_json(
            "POST",
            self.server.base + "/api/plugins/kanban/tasks/" + card + "/reclaim",
            {},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body, {"ok": True, "task_id": card})
        self.assertNotEqual(self.server.status(card), "running")

    def test_blocked_by_parent_lands_todo(self):
        parent = self.server.create_task("blocking-parent", triage=True)

        # Card already in todo: dispatch must report blocked_by_parent without
        # a second PATCH.
        todo_child = self.server.create_task("blocked-todo-child", parents=[parent])
        self.assertEqual(self.server.patch_status(todo_child, "todo")[0], 200)
        self.assertEqual(self.server.status(todo_child), "todo")
        self.seed_accept(todo_child)

        before = set(self.server.ready_ids())
        code, body = self.server.dispatch(todo_child)
        self.assertEqual(code, 409)
        self.assertEqual(body, {"ok": False, "error": "blocked_by_parent", "status": "todo"})
        self.assertEqual(self.server.status(todo_child), "todo")
        self.assertEqual(set(self.server.ready_ids()), before)
        self.assert_no_snapshot(todo_child)

        # A triage card with an unfinished parent must be moved to todo, not
        # silently left in triage.
        triage_child = self.server.create_task("blocked-triage-child", parents=[parent])
        self.assertEqual(self.server.status(triage_child), "triage")
        self.seed_accept(triage_child)
        code, body = self.server.dispatch(triage_child)
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "blocked_by_parent")
        self.assertEqual(body["status"], "todo")
        self.assertEqual(self.server.status(triage_child), "todo")
        self.assert_no_snapshot(triage_child)

    def test_blocked_card_that_resumes_review_is_not_reported_ready(self):
        card = self.server.create_task("review-resume", triage=True)
        self.seed_accept(card)
        code, body = self.server.patch_status(card, "ready")
        self.assertEqual(code, 200, body)
        code, body = self.server.patch_status(card, "review", summary="please review")
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(card), "review")

        # Upstream unblock_task restores ``review`` when the newest lifecycle
        # event says so; seed that exact state (REST cannot block from review).
        self.server.set_kanban_status(card, "blocked")
        self.server.add_kanban_event(card, "blocked", {"resume_status": "review"})

        code, body = self.server.dispatch(card)
        self.assertEqual(code, 409)
        self.assertEqual(body, {"ok": False, "error": "not_ready", "status": "review"})
        self.assertEqual(self.server.status(card), "review")
        self.assert_no_snapshot(card)

    def test_redispatch_is_idempotent_and_keeps_snapshot(self):
        first = self.server.create_task("idempotent-first", triage=True)
        self.seed_accept(first)
        code, body = self.server.dispatch(first)
        self.assertEqual(code, 200, body)

        # Replace the snapshot with a sentinel; a second dispatch must not
        # overwrite it.
        sentinel = {"max_minutes": 1, "max_cost": 0.5, "deny_paths": ["sentinel"]}
        self.server.seed_metadata(
            first,
            {
                "contract": {"accept": "done means tests pass"},
                "contract_defaults": sentinel,
                "bucket": "today",
            },
        )

        second = self.server.create_task("idempotent-second", triage=True)
        self.seed_accept(second)
        code, body = self.server.dispatch(second)
        self.assertEqual(code, 200, body)

        count_before = self.server.ready_ids().count(first)
        code, body = self.server.dispatch(first)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "ready")
        ready = self.server.ready_ids()
        self.assertEqual(ready.count(first), count_before)
        self.assertEqual(body["position"], ready.index(first) + 1)
        self.assertEqual(self.server.metadata_row(first)["contract_defaults"], sentinel)

        # created=False: the second snapshot write did not take ownership.
        token_before = self.server.metadata_token(first)
        self.assertTrue(token_before)
        self.assertEqual(self.server.dispatch(first)[0], 200)
        self.assertEqual(self.server.metadata_token(first), token_before)

        # The ownership token is a column, never part of the metadata payload.
        code, rows = self.server.get_metadata([first])
        self.assertEqual(code, 200)
        self.assertNotIn("write_token", rows[first])

    def test_done_and_archived_are_not_dispatchable(self):
        done = self.server.create_task("done-card", triage=True)
        self.seed_accept(done)
        self.assertEqual(self.server.patch_status(done, "ready")[0], 200)
        self.assertEqual(
            self.server.patch_status(done, "done", summary="finished")[0], 200
        )
        self.assertEqual(self.server.status(done), "done")
        code, body = self.server.dispatch(done)
        self.assertEqual((code, body["error"], body["status"]), (409, "not_dispatchable", "done"))
        self.assertEqual(self.server.status(done), "done")

        archived = self.server.create_task("archived-card", triage=True)
        self.assertEqual(self.server.patch_status(archived, "archived")[0], 200)
        self.assertEqual(self.server.status(archived), "archived")
        code, body = self.server.dispatch(archived)
        self.assertEqual((code, body["error"], body["status"]), (409, "not_dispatchable", "archived"))
        self.assertEqual(self.server.status(archived), "archived")

    def test_scheduled_and_blocked_cards_dispatch_successfully(self):
        scheduled = self.server.create_task("scheduled-card", triage=True)
        self.seed_accept(scheduled)
        self.assertEqual(self.server.patch_status(scheduled, "ready")[0], 200)
        code, body = self.server.patch_status(scheduled, "scheduled", block_reason="later")
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(scheduled), "scheduled")
        code, body = self.server.dispatch(scheduled)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(scheduled), "ready")

        blocked = self.server.create_task("blocked-card", triage=True)
        self.seed_accept(blocked)
        self.assertEqual(self.server.patch_status(blocked, "ready")[0], 200)
        code, body = self.server.patch_status(blocked, "blocked", block_reason="waiting")
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(blocked), "blocked")
        code, body = self.server.dispatch(blocked)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(blocked), "ready")

    def test_running_card_is_reported_without_patch(self):
        card = self.server.create_task("running-card", triage=True)
        self.seed_accept(card)
        self.server.set_kanban_status(card, "running")

        code, body = self.server.dispatch(card)
        self.assertEqual(code, 200, body)
        self.assertEqual(body, {"ok": True, "position": 0, "eta": None, "status": "running"})
        self.assertEqual(self.server.status(card), "running")
        self.assert_no_snapshot(card)

    def test_dispatch_unknown_task_is_404(self):
        code, body = self.server.dispatch("t_does_not_exist")
        self.assertEqual(code, 404)
        self.assertEqual(body, {"ok": False, "error": "not_found"})

    def test_concurrent_dispatch_keeps_other_metadata_keys(self):
        card = self.server.create_task("concurrent-card", triage=True)
        self.server.seed_metadata(
            card, {"contract": {"accept": "ok"}, "bucket": "today"}
        )

        results: list[tuple[int, dict]] = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            results.append(self.server.dispatch(card))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(len(results), 2)
        for code, body in results:
            self.assertEqual(code, 200, body)
            self.assertEqual(body["status"], "ready")

        metadata = self.server.metadata_row(card)
        self.assertEqual(metadata["bucket"], "today")
        self.assertEqual(set(metadata["contract_defaults"]), {"max_minutes", "max_cost", "deny_paths"})
        self.assertEqual(self.server.ready_ids().count(card), 1)

    def test_shared_kanban_db_has_no_workbench_objects(self):
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


class InvalidDefaultsTests(unittest.TestCase):
    tmp: Path
    defaults: Path
    server: WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-defaults-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")
        cls.server = WorkbenchServer(cls.tmp, cls.defaults).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _write(self, payload) -> None:
        self.defaults.write_text(json.dumps(payload), encoding="utf-8")

    def test_invalid_defaults_are_500_and_write_no_snapshot(self):
        invalid_variants = [
            {},
            {"budget": None, "queue": {"minutes_per_task": 7}},
            {
                "budget": {"max_minutes": "120", "max_cost": 5, "deny_paths": []},
                "queue": {"minutes_per_task": 7},
            },
            {
                "budget": {"max_minutes": 0, "max_cost": 5, "deny_paths": []},
                "queue": {"minutes_per_task": 7},
            },
            {
                "budget": {"max_minutes": 120, "max_cost": -1, "deny_paths": []},
                "queue": {"minutes_per_task": 7},
            },
            {
                "budget": {"max_minutes": 120, "max_cost": 5, "deny_paths": "nope"},
                "queue": {"minutes_per_task": 7},
            },
            {
                "budget": {"max_minutes": 120, "max_cost": 5, "deny_paths": []},
                "queue": {"minutes_per_task": 0},
            },
            {
                "budget": {"max_minutes": 10081, "max_cost": 5, "deny_paths": []},
                "queue": {"minutes_per_task": 7},
            },
            {
                "budget": {"max_minutes": 120, "max_cost": 1001, "deny_paths": []},
                "queue": {"minutes_per_task": 7},
            },
            {
                "budget": {"max_minutes": 120, "max_cost": 5, "deny_paths": ["ok", 1]},
                "queue": {"minutes_per_task": 7},
            },
            {
                "budget": {"max_minutes": 120, "max_cost": 5, "deny_paths": []},
                "queue": {"minutes_per_task": 1441},
            },
        ]
        card = self.server.create_task("invalid-defaults", triage=True)
        self.server.seed_metadata(card, {"contract": {"accept": "ok"}})

        for index, payload in enumerate(invalid_variants):
            with self.subTest(index=index):
                self._write(payload)
                code, body = self.server.dispatch(card)
                self.assertEqual(code, 500, body)
                self.assertEqual(body["error"], "invalid_defaults")
                self.assertEqual(self.server.status(card), "triage")
                metadata = self.server.metadata_row(card)
                self.assertNotIn("contract_defaults", metadata or {})

        self._write(VALID_DEFAULTS)
        code, body = self.server.dispatch(card)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.server.status(card), "ready")


class MetadataUnavailableTests(unittest.TestCase):
    tmp: Path
    defaults: Path
    server: WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-metadata-down-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")
        broken_db = cls.tmp / "broken-db"
        broken_db.mkdir()
        cls.server = WorkbenchServer(
            cls.tmp, cls.defaults, env_extra={"WORKBENCH_DB": str(broken_db)}
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_service_starts_but_metadata_endpoints_are_503(self):
        code, body = http_json("GET", self.server.base + "/healthz")
        self.assertEqual((code, body), (200, {"ok": True}))

        code, body = http_json("GET", self.server.base + "/api/workbench/metadata?ids=t_x")
        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "metadata_unavailable")

        card = self.server.create_task("metadata-down", triage=True)
        code, body = self.server.dispatch(card)
        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "metadata_unavailable")
        self.assertEqual(self.server.status(card), "triage")

        # The gate fails closed instead of forwarding a ready transition.
        code, body = self.server.patch_status(card, "ready")
        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "metadata_unavailable")
        self.assertEqual(self.server.status(card), "triage")


class UpstreamUnavailableTests(unittest.TestCase):
    tmp: Path
    defaults: Path
    server: WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-upstream-down-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")
        closed_port = free_port()
        cls.server = WorkbenchServer(
            cls.tmp,
            cls.defaults,
            env_extra={"WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{closed_port}"},
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_dispatch_reports_502_when_upstream_is_unreachable(self):
        code, body = self.server.dispatch("t_anything")
        self.assertEqual(code, 502)
        self.assertEqual(body, {"ok": False, "error": "upstream_unavailable"})


class BoardFailureAfterPatchTests(unittest.TestCase):
    """PATCH succeeds, but the follow-up /board read fails (VGO3/BH4)."""

    tmp: Path
    defaults: Path
    target: WorkbenchServer
    proxy: ThreadingHTTPServer
    server: WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-board-fail-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")

        cls.target = WorkbenchServer(cls.tmp / "target", cls.defaults).start()
        target_base = cls.target.base

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status: int, raw: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _proxy(self) -> None:
                if self.path.startswith("/api/plugins/kanban/board"):
                    self._respond(500, b'{"detail":"board boom"}')
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                req = urlrequest.Request(
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
                    with opener.open(req, timeout=5) as resp:
                        raw = resp.read()
                        status = int(resp.status)
                except urlerror.HTTPError as exc:
                    try:
                        raw = exc.read()
                        status = int(exc.code)
                    finally:
                        exc.close()
                self._respond(status, raw)

            do_GET = _proxy
            do_PATCH = _proxy
            do_POST = _proxy

            def log_message(self, *args) -> None:
                pass

        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        proxy_base = f"http://127.0.0.1:{cls.proxy.server_address[1]}"
        cls.server = WorkbenchServer(
            cls.tmp / "dispatch",
            cls.defaults,
            env_extra={
                "WORKBENCH_UPSTREAM_BASE": proxy_base,
                "HERMES_KANBAN_HOME": str(cls.target.kanban_home),
                "WORKBENCH_DB": str(cls.target.db_path),
            },
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.target.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_board_failure_returns_502_without_position_and_keeps_snapshot(self):
        card = self.target.create_task("board-fail", triage=True)
        self.target.seed_metadata(card, {"contract": {"accept": "ok"}})

        code, body = self.server.dispatch(card)
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertNotIn("position", body)
        self.assertEqual(self.target.status(card), "ready")
        # The PATCH succeeded, so the snapshot is the truthful record; it must
        # not be compensated away just because the position read failed.
        self.assertIn("contract_defaults", self.target.metadata_row(card))


class UpstreamPatchFailureTests(unittest.TestCase):
    """PATCH fails after the snapshot was written: 502 + compensation."""

    tmp: Path
    defaults: Path
    target: WorkbenchServer
    proxy: ThreadingHTTPServer
    server: WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-patch-fail-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")
        cls.target = WorkbenchServer(cls.tmp / "target", cls.defaults).start()
        target_base = cls.target.base

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status: int, raw: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _proxy(self) -> None:
                if self.command == "PATCH" and self.path.startswith("/api/plugins/kanban/tasks/"):
                    self._respond(500, b'{"detail":"patch boom"}')
                    return
                req = urlrequest.Request(
                    target_base + self.path,
                    method=self.command,
                    headers={"Accept": "application/json"},
                )
                opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
                try:
                    with opener.open(req, timeout=5) as resp:
                        raw = resp.read()
                        status = int(resp.status)
                except urlerror.HTTPError as exc:
                    try:
                        raw = exc.read()
                        status = int(exc.code)
                    finally:
                        exc.close()
                self._respond(status, raw)

            do_GET = _proxy
            do_PATCH = _proxy

            def log_message(self, *args) -> None:
                pass

        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        cls.server = WorkbenchServer(
            cls.tmp / "dispatch",
            cls.defaults,
            env_extra={
                "WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{cls.proxy.server_address[1]}",
                "HERMES_KANBAN_HOME": str(cls.target.kanban_home),
                "WORKBENCH_DB": str(cls.target.db_path),
            },
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.target.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_patch_5xx_returns_502_and_compensates_snapshot(self):
        card = self.target.create_task("patch-fail", triage=True)
        self.target.seed_metadata(card, {"contract": {"accept": "ok"}})

        code, body = self.server.dispatch(card)
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertEqual(self.target.status(card), "triage")
        metadata = self.target.metadata_row(card)
        self.assertNotIn("contract_defaults", metadata or {})


class TodoPatchFailureTests(unittest.TestCase):
    """The 409 blocked_by_parent flow must not fabricate a todo landing."""

    tmp: Path
    defaults: Path
    target: WorkbenchServer
    proxy: ThreadingHTTPServer
    server: WorkbenchServer
    todo_state: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-todo-fail-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")
        cls.target = WorkbenchServer(cls.tmp / "target", cls.defaults).start()
        target_base = cls.target.base
        cls.todo_state = {"mode": "500"}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status: int, raw: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _proxy(self, body=None) -> None:
                req = urlrequest.Request(
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
                    with opener.open(req, timeout=5) as resp:
                        raw = resp.read()
                        status = int(resp.status)
                except urlerror.HTTPError as exc:
                    try:
                        raw = exc.read()
                        status = int(exc.code)
                    finally:
                        exc.close()
                self._respond(status, raw)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                if self.command == "PATCH" and self.path.startswith("/api/plugins/kanban/tasks/"):
                    try:
                        payload = json.loads(body or b"{}")
                    except json.JSONDecodeError:
                        payload = {}
                    if payload.get("status") == "todo":
                        if cls.todo_state["mode"] == "500":
                            self._respond(500, b'{"detail":"todo boom"}')
                        else:
                            # 200 but no task.status: not proof of landing.
                            self._respond(200, b'{"task":{}}')
                        return
                self._proxy(body)

            do_GET = _handle
            do_PATCH = _handle

            def log_message(self, *args) -> None:
                pass

        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        cls.server = WorkbenchServer(
            cls.tmp / "dispatch",
            cls.defaults,
            env_extra={
                "WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{cls.proxy.server_address[1]}",
                "HERMES_KANBAN_HOME": str(cls.target.kanban_home),
                "WORKBENCH_DB": str(cls.target.db_path),
            },
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.target.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _dispatch_blocked_child(self, mode: str):
        self.todo_state["mode"] = mode
        parent = self.target.create_task(f"todo-parent-{mode}", triage=True)
        child = self.target.create_task(
            f"todo-child-{mode}", triage=True, parents=[parent]
        )
        self.target.seed_metadata(child, {"contract": {"accept": "ok"}})
        return child, self.server.dispatch(child)

    def test_todo_patch_500_returns_502_and_compensates(self):
        child, (code, body) = self._dispatch_blocked_child("500")
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertEqual(self.target.status(child), "triage")
        metadata = self.target.metadata_row(child)
        self.assertNotIn("contract_defaults", metadata or {})

    def test_todo_patch_200_without_task_status_returns_502(self):
        child, (code, body) = self._dispatch_blocked_child("no-status")
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertEqual(self.target.status(child), "triage")
        metadata = self.target.metadata_row(child)
        self.assertNotIn("contract_defaults", metadata or {})


class HangingUpstreamTests(unittest.TestCase):
    """A hanging loopback call must time out into a bounded 502."""

    tmp: Path
    defaults: Path
    proxy: ThreadingHTTPServer
    server: WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-hang-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                time.sleep(5)
                try:
                    raw = b'{"task": {"id": "t_slow", "status": "triage"}}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args) -> None:
                pass

        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        cls.server = WorkbenchServer(
            cls.tmp,
            cls.defaults,
            env_extra={
                "WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{cls.proxy.server_address[1]}"
            },
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.proxy.shutdown()
        cls.proxy.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_hanging_upstream_is_bounded_502(self):
        started = time.monotonic()
        code, body = self.server.dispatch("t_slow")
        elapsed = time.monotonic() - started
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "upstream_unavailable")
        self.assertLess(elapsed, 4.0)


class DeletedAfterGetTests(unittest.TestCase):
    """GET succeeds, but the subsequent PATCH returns 404."""

    tmp: Path
    defaults: Path
    target: WorkbenchServer
    proxy: ThreadingHTTPServer
    server: WorkbenchServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-404-after-get-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")
        cls.target = WorkbenchServer(cls.tmp / "target", cls.defaults).start()
        target_base = cls.target.base

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status: int, raw: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self) -> None:
                if self.command == "PATCH":
                    self._respond(404, b'{"detail":"task disappeared"}')
                    return
                req = urlrequest.Request(
                    target_base + self.path,
                    method=self.command,
                    headers={"Accept": "application/json"},
                )
                opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
                try:
                    with opener.open(req, timeout=5) as resp:
                        raw = resp.read()
                        status = int(resp.status)
                except urlerror.HTTPError as exc:
                    try:
                        raw = exc.read()
                        status = int(exc.code)
                    finally:
                        exc.close()
                self._respond(status, raw)

            do_GET = _handle
            do_PATCH = _handle

            def log_message(self, *args) -> None:
                pass

        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        cls.server = WorkbenchServer(
            cls.tmp / "dispatch",
            cls.defaults,
            env_extra={
                "WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{cls.proxy.server_address[1]}",
                "HERMES_KANBAN_HOME": str(cls.target.kanban_home),
                "WORKBENCH_DB": str(cls.target.db_path),
            },
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.target.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_patch_404_after_get_is_not_found(self):
        card = self.target.create_task("deleted-after-get", triage=True)
        self.target.seed_metadata(card, {"contract": {"accept": "ok"}})

        code, body = self.server.dispatch(card)
        self.assertEqual(code, 404, body)
        self.assertEqual(body, {"ok": False, "error": "not_found"})
        metadata = self.target.metadata_row(card)
        self.assertNotIn("contract_defaults", metadata or {})


class SnapshotOwnershipTests(unittest.TestCase):
    """Compensation must not delete a snapshot whose token changed."""

    tmp: Path
    defaults: Path
    target: WorkbenchServer
    proxy: ThreadingHTTPServer
    server: WorkbenchServer
    patch_seen: threading.Event
    release_patch: threading.Event

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-owner-"))
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")
        cls.target = WorkbenchServer(cls.tmp / "target", cls.defaults).start()
        target_base = cls.target.base
        cls.patch_seen = threading.Event()
        cls.release_patch = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status: int, raw: bytes) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _handle(self) -> None:
                if self.command == "PATCH":
                    cls.patch_seen.set()
                    cls.release_patch.wait(timeout=3)
                    self._respond(500, b'{"detail":"patch failed"}')
                    return
                req = urlrequest.Request(
                    target_base + self.path,
                    method=self.command,
                    headers={"Accept": "application/json"},
                )
                opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
                try:
                    with opener.open(req, timeout=5) as resp:
                        raw = resp.read()
                        status = int(resp.status)
                except urlerror.HTTPError as exc:
                    try:
                        raw = exc.read()
                        status = int(exc.code)
                    finally:
                        exc.close()
                self._respond(status, raw)

            do_GET = _handle
            do_PATCH = _handle

            def log_message(self, *args) -> None:
                pass

        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        cls.server = WorkbenchServer(
            cls.tmp / "dispatch",
            cls.defaults,
            env_extra={
                "WORKBENCH_UPSTREAM_BASE": f"http://127.0.0.1:{cls.proxy.server_address[1]}",
                "HERMES_KANBAN_HOME": str(cls.target.kanban_home),
                "WORKBENCH_DB": str(cls.target.db_path),
            },
        ).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.release_patch.set()
        cls.server.stop()
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.target.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_changed_token_blocks_compensation(self):
        card = self.target.create_task("owner-card", triage=True)
        self.target.seed_metadata(card, {"contract": {"accept": "ok"}})

        result: dict = {}

        def run_dispatch() -> None:
            result["value"] = self.server.dispatch(card)

        worker = threading.Thread(target=run_dispatch)
        worker.start()
        self.assertTrue(self.patch_seen.wait(timeout=5), "PATCH never reached the proxy")
        token = self.target.metadata_token(card)
        self.assertTrue(token, "snapshot was not written before PATCH")
        self.target.set_metadata_token(card, "foreign-token")
        self.release_patch.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())

        code, body = result["value"]
        self.assertEqual(code, 502, body)
        metadata = self.target.metadata_row(card)
        self.assertIn("contract_defaults", metadata or {})
        self.assertEqual(self.target.metadata_token(card), "foreign-token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
