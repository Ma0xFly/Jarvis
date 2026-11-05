#!/usr/bin/env python3
"""Integration tests for Story 2.3 (session trace entry, hardened).

Covers ``GET /api/workbench/session/{task_id}``:

* resolution: newest run ``metadata.worker_session_id`` → next older run →
  ``tasks.session_id``; ``executor_session_id`` only from the resolved run;
* 404 ``no_session`` / 404 ``session_missing`` / 503 ``session_unavailable`` /
  404 ``not_found`` / 502 upstream unreachable;
* message window by ``messages.id`` (timestamps may regress), liveness
  ``(active = 1 OR compacted = 1)``, hidden rows, display-identity dedupe,
  ``_compressed_summary`` projection, internal user prefixes, system prompt;
* newest-N window/env override, filtered count vs ``sessions.message_count``,
  per-message 20KB cap, ~1MB response cap, tool-argument redaction;
* ``Cache-Control: no-store``; state.db and kanban.db stay read-only.

The workbench server runs as an isolated subprocess with throwaway kanban /
workbench / state DBs, reusing the harness from ``test_dispatch``.

Run with::

    python3 workbench-app/tests/test_session.py -v
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch as base  # noqa: E402

STATE_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    model TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    profile_name TEXT,
    title TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER,
    display_kind TEXT,
    display_identity BLOB,
    _compressed_summary INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_messages_session ON messages(session_id, id);
"""

_AUTO = object()


def make_defaults(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    defaults = root / "defaults.json"
    defaults.write_text(json.dumps(base.VALID_DEFAULTS), encoding="utf-8")
    return defaults


def create_state_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(STATE_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def add_session(
    path: Path,
    session_id: str,
    *,
    messages: list[dict] | None = None,
    message_count: int | None = None,
    system_prompt: str | None = None,
    started_at: float | None = None,
    ended_at=_AUTO,
    parent_session_id: str | None = None,
) -> None:
    conn = sqlite3.connect(str(path), timeout=5)
    try:
        base_ts = float(started_at if started_at is not None else int(time.time()) - 3600)
        resolved_end = base_ts + 60 if ended_at is _AUTO else ended_at
        conn.execute(
            "INSERT INTO sessions (id, source, model, system_prompt, parent_session_id, "
            "started_at, ended_at, message_count, input_tokens, output_tokens, "
            "profile_name, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "cli",
                "test-model",
                system_prompt,
                parent_session_id,
                base_ts,
                resolved_end,
                message_count if message_count is not None else len(messages or []),
                120,
                45,
                "probe",
                "会话 " + session_id,
            ),
        )
        for index, message in enumerate(messages or []):
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, (dict, list)):
                tool_calls = json.dumps(tool_calls)
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                "timestamp, active, compacted, display_order, display_kind, "
                "display_identity, _compressed_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    message.get("role") or "user",
                    message.get("content"),
                    tool_calls,
                    message.get("tool_name"),
                    message.get("timestamp", base_ts + index),
                    message.get("active", 1),
                    message.get("compacted", 0),
                    message.get("display_order"),
                    message.get("display_kind"),
                    message.get("display_identity"),
                    message.get("_compressed_summary", 0),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def add_run(
    server: base.WorkbenchServer,
    task_id: str,
    metadata: dict | None,
    *,
    started_at: int | None = None,
    status: str = "done",
) -> int:
    conn = sqlite3.connect(str(server.kanban_db), timeout=5)
    try:
        started = int(started_at if started_at is not None else time.time())
        cursor = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
            "outcome, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "probe",
                status,
                started,
                started + 30,
                "completed" if status == "done" else None,
                json.dumps(metadata) if metadata is not None else None,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.commit()
        return run_id
    finally:
        conn.close()


def set_task_session(server: base.WorkbenchServer, task_id: str, session_id: str) -> None:
    conn = sqlite3.connect(str(server.kanban_db), timeout=5)
    try:
        rows = conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id)
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert rows == 1


def run_metadata(server: base.WorkbenchServer, run_id: int) -> str | None:
    conn = sqlite3.connect(str(server.kanban_db), timeout=5)
    try:
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table_counts(path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    """Row counts via a read-only URI (WAL-safe stand-in for a byte hash)."""
    uri = "file:" + urlparse.quote(str(path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    finally:
        conn.close()


def http_json_headers(method: str, url: str, payload=None, timeout: float = 15.0):
    """Like ``base.http_json`` but also returns lower-cased response headers."""
    request_headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    req = urlrequest.Request(url, data=data, headers=request_headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            headers = {key.lower(): value for key, value in resp.headers.items()}
            return int(resp.status), (json.loads(raw) if raw else None), headers
    except urlerror.HTTPError as exc:
        try:
            raw = exc.read()
            parsed = json.loads(raw) if raw else None
            headers = {key.lower(): value for key, value in exc.headers.items()}
            return int(exc.code), parsed, headers
        finally:
            exc.close()


def start_session_server(
    root: Path,
    state_db: Path,
    *,
    env_extra: dict | None = None,
    upstream: str | None = None,
) -> base.WorkbenchServer:
    env = {
        "WORKBENCH_REVIEW_SWEEP_SECONDS": "0",
        "WORKBENCH_INBOX_CACHE_SECONDS": "0",
        "WORKBENCH_NOTIFY_SWEEP_SECONDS": "0",
        "WORKBENCH_STATE_DB": str(state_db),
    }
    env.update(env_extra or {})
    if upstream is not None:
        env["WORKBENCH_UPSTREAM_BASE"] = upstream
    return base.WorkbenchServer(root, make_defaults(root), env_extra=env).start()


class SessionServerTestCase(unittest.TestCase):
    """A shared server whose state DB lives in the class temp dir."""

    state_db: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-session-"))
        cls.state_db = cls.tmp / "state.db"
        create_state_db(cls.state_db)
        cls.server = start_session_server(cls.tmp, cls.state_db)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def new_task(self, title: str) -> str:
        return self.server.create_task(title, triage=True)

    def transcript(self, task_id: str, server: base.WorkbenchServer | None = None):
        target = server or self.server
        return base.http_json(
            "GET",
            target.base + "/api/workbench/session/" + urlparse.quote(task_id, safe=""),
        )

    def seed_session_task(
        self,
        title: str,
        session_id: str,
        *,
        messages: list[dict] | None = None,
        executor_session_id: str | None = None,
    ) -> str:
        task_id = self.new_task(title)
        add_run(
            self.server,
            task_id,
            {
                "worker_session_id": session_id,
                **(
                    {"executor_session_id": executor_session_id}
                    if executor_session_id
                    else {}
                ),
            },
        )
        add_session(self.state_db, session_id, messages=messages or [])
        return task_id


class ResolutionTests(SessionServerTestCase):
    def test_newest_run_worker_session_wins(self):
        task_id = self.new_task("最新 run 优先")
        add_run(
            self.server,
            task_id,
            {"worker_session_id": "sess-old"},
            started_at=int(time.time()) - 600,
        )
        new_id = add_run(
            self.server,
            task_id,
            {"worker_session_id": "sess-new"},
            started_at=int(time.time()) - 60,
        )
        add_session(self.state_db, "sess-old", messages=[{"content": "old"}])
        add_session(self.state_db, "sess-new", messages=[{"content": "new"}])

        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["session"]["id"], "sess-new")
        self.assertEqual(body["source"], "run")
        self.assertEqual(body["run_id"], new_id)
        self.assertEqual([m["content"] for m in body["messages"]], ["new"])

    def test_missing_newest_session_falls_back_to_older_run(self):
        task_id = self.new_task("回退到上一轮")
        add_run(
            self.server,
            task_id,
            {
                "worker_session_id": "sess-older",
                "executor_session_id": "exec-old",
            },
            started_at=int(time.time()) - 600,
        )
        add_run(
            self.server,
            task_id,
            {
                "worker_session_id": "sess-missing",
                "executor_session_id": "exec-new",
            },
            started_at=int(time.time()) - 60,
        )
        add_session(self.state_db, "sess-older", messages=[{"content": "older"}])

        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["session"]["id"], "sess-older")
        self.assertEqual(body["source"], "run")
        # Executor must come from the run that supplied the session.
        self.assertEqual(body["executor_session_id"], "exec-old")
        self.assertNotEqual(body["executor_session_id"], "exec-new")

    def test_falls_back_to_task_session_when_runs_missing(self):
        task_id = self.new_task("任务会话兜底")
        add_run(
            self.server,
            task_id,
            {
                "worker_session_id": "sess-missing",
                "executor_session_id": "exec-ignored",
            },
        )
        set_task_session(self.server, task_id, "sess-task")
        add_session(self.state_db, "sess-task", messages=[{"content": "task"}])

        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["session"]["id"], "sess-task")
        self.assertEqual(body["source"], "task")
        self.assertIsNone(body["run_id"])
        self.assertIsNone(body["executor_session_id"])

    def test_no_session_anywhere_is_404(self):
        task_id = self.new_task("无会话卡")
        code, body = self.transcript(task_id)
        self.assertEqual(code, 404, body)
        self.assertEqual(body, {"ok": False, "error": "no_session", "task_id": task_id})

    def test_unknown_task_is_404_not_found(self):
        code, body = self.transcript("t_missing")
        self.assertEqual(code, 404, body)
        self.assertEqual(body, {"ok": False, "error": "not_found"})

    def test_all_candidates_missing_is_404(self):
        task_id = self.new_task("state 无此行")
        add_run(self.server, task_id, {"worker_session_id": "sess-ghost"})
        code, body = self.transcript(task_id)
        self.assertEqual(code, 404, body)
        self.assertEqual(
            body,
            {"ok": False, "error": "session_missing", "session_id": "sess-ghost"},
        )

    def test_executor_session_is_displayed_without_writing(self):
        task_id = self.new_task("执行器 id")
        run_id = add_run(
            self.server,
            task_id,
            {
                "worker_session_id": "sess-exec",
                "executor_session_id": "exec-resume-id",
            },
        )
        add_session(self.state_db, "sess-exec", messages=[{"content": "exec"}])
        metadata_before = run_metadata(self.server, run_id)

        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["executor_session_id"], "exec-resume-id")
        self.assertEqual(run_metadata(self.server, run_id), metadata_before)

    def test_transcript_is_read_only_across_both_dbs(self):
        task_id = self.new_task("只读卡")
        run_id = add_run(self.server, task_id, {"worker_session_id": "sess-ro"})
        add_session(self.state_db, "sess-ro", messages=[{"content": "read only"}])
        state_before = digest(self.state_db)
        task_before = self.server.task(task_id)
        run_before = run_metadata(self.server, run_id)
        kanban_before = table_counts(
            self.server.kanban_db, ("tasks", "task_runs", "task_events")
        )

        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)

        self.assertEqual(digest(self.state_db), state_before)
        self.assertEqual(self.server.task(task_id), task_before)
        self.assertEqual(run_metadata(self.server, run_id), run_before)
        self.assertEqual(
            table_counts(self.server.kanban_db, ("tasks", "task_runs", "task_events")),
            kanban_before,
        )

    def test_transcript_is_no_store(self):
        task_id = self.seed_session_task("缓存头", "sess-cache", messages=[{"content": "x"}])
        url = self.server.base + "/api/workbench/session/" + urlparse.quote(task_id, safe="")
        code, body, headers = http_json_headers("GET", url)
        self.assertEqual(code, 200, body)
        self.assertEqual(headers.get("cache-control"), "no-store")

        code, body, headers = http_json_headers(
            "GET",
            self.server.base
            + "/api/workbench/session/"
            + urlparse.quote("t_missing", safe=""),
        )
        self.assertEqual(code, 404, body)
        self.assertEqual(headers.get("cache-control"), "no-store")


class TranscriptTests(SessionServerTestCase):
    def test_window_is_by_id_not_timestamp(self):
        task_id = self.new_task("时钟回退")
        add_run(self.server, task_id, {"worker_session_id": "sess-clock"})
        add_session(
            self.state_db,
            "sess-clock",
            messages=[
                {"content": "id1", "timestamp": 5000.0},
                {"content": "id2", "timestamp": 1000.0},
                {"content": "id3", "timestamp": 3000.0},
            ],
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual([m["content"] for m in body["messages"]], ["id1", "id2", "id3"])
        self.assertEqual(
            [m["id"] for m in body["messages"]],
            sorted(m["id"] for m in body["messages"]),
        )
        self.assertEqual(body["truncated"], False)
        self.assertEqual(body["total_count"], 3)

    def test_liveness_hidden_and_rewound_rows(self):
        task_id = self.new_task("活跃与重写")
        add_run(self.server, task_id, {"worker_session_id": "sess-live"})
        add_session(
            self.state_db,
            "sess-live",
            messages=[
                {"content": "visible-1"},
                {"content": "rewound", "active": 0, "compacted": 0},
                {"content": "compacted-visible", "active": 0, "compacted": 1},
                {"content": "hidden", "display_kind": "hidden"},
                {"content": "visible-2"},
            ],
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(
            [m["content"] for m in body["messages"]],
            ["visible-1", "compacted-visible", "visible-2"],
        )
        self.assertEqual(body["total_count"], 3)
        self.assertEqual(body["returned_count"], 3)

    def test_display_identity_keeps_latest_row(self):
        task_id = self.new_task("身份去重")
        add_run(self.server, task_id, {"worker_session_id": "sess-ident"})
        add_session(
            self.state_db,
            "sess-ident",
            messages=[
                {"content": "draft", "display_identity": b"turn-1"},
                {"content": "final", "display_identity": b"turn-1"},
                {"content": "other", "display_identity": b"turn-2"},
            ],
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual([m["content"] for m in body["messages"]], ["final", "other"])
        self.assertEqual(body["total_count"], 2)

    def test_compressed_summary_projection(self):
        task_id = self.new_task("压缩摘要")
        add_run(self.server, task_id, {"worker_session_id": "sess-compress"})
        add_session(
            self.state_db,
            "sess-compress",
            messages=[
                {
                    "role": "assistant",
                    "content": "pure handoff\n"
                    "--- END OF CONTEXT SUMMARY — respond to the message below, "
                    "not the summary above ---",
                    "_compressed_summary": 1,
                },
                {
                    "role": "assistant",
                    "content": "prior tail\n"
                    "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n"
                    "summary body",
                    "_compressed_summary": 1,
                },
                {
                    "role": "assistant",
                    "content": "summary body\n"
                    "--- END OF CONTEXT SUMMARY — respond to the message below, "
                    "not the summary above ---\nlatest live ask",
                    "_compressed_summary": 1,
                },
            ],
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(
            [m["content"] for m in body["messages"]],
            ["prior tail", "latest live ask"],
        )
        self.assertTrue(all(m["compressed"] for m in body["messages"]))
        self.assertEqual(body["total_count"], 2)

    def test_internal_user_prefixes_are_skipped(self):
        task_id = self.new_task("内部提示行")
        add_run(self.server, task_id, {"worker_session_id": "sess-internal"})
        add_session(
            self.state_db,
            "sess-internal",
            system_prompt="SECRET SYSTEM PROMPT",
            messages=[
                {"role": "system", "content": "SECRET SYSTEM MESSAGE"},
                {"role": "user", "content": "[System: internal scaffolding]"},
                {
                    "role": "user",
                    "content": "[CONTEXT COMPACTION — REFERENCE ONLY] handoff body",
                },
                {"role": "user", "content": "real user ask"},
            ],
            message_count=4,
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual([m["content"] for m in body["messages"]], ["real user ask"])
        self.assertNotIn("SECRET", json.dumps(body, ensure_ascii=False))
        self.assertNotIn("system", [m["role"] for m in body["messages"]])

    def test_tool_arguments_never_ship(self):
        prefix_secret = "SECRET_INSIDE_PREFIX"
        suffix_secret = "SECRET_AFTER_240_CHARS"
        arguments = json.dumps(
            {"command": prefix_secret, "padding": "x" * 600, "token": suffix_secret}
        )
        raw_tool_calls = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": arguments},
                },
                {"name": "kanban_comment", "arguments": prefix_secret},
            ]
        )
        task_id = self.new_task("工具参数脱敏")
        add_run(self.server, task_id, {"worker_session_id": "sess-tools"})
        add_session(
            self.state_db,
            "sess-tools",
            messages=[
                {"role": "assistant", "content": "", "tool_calls": raw_tool_calls},
                {
                    "role": "tool",
                    "content": "完成",
                    "tool_name": "kanban_complete",
                },
            ],
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        assistant, tool = body["messages"]
        self.assertEqual(
            assistant["tool_calls"],
            [
                {"name": "kanban_complete", "summary": "参数已折叠"},
                {"name": "kanban_comment", "summary": "参数已折叠"},
            ],
        )
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(prefix_secret, serialized)
        self.assertNotIn(suffix_secret, serialized)
        self.assertEqual(tool["tool_name"], "kanban_complete")

    def test_oversized_message_is_capped(self):
        task_id = self.new_task("超长消息")
        add_run(self.server, task_id, {"worker_session_id": "sess-big"})
        add_session(
            self.state_db,
            "sess-big",
            messages=[{"content": "x" * 25_000}],
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        message = body["messages"][0]
        self.assertTrue(message["content_truncated"])
        self.assertEqual(message["content_chars"], 25_000)
        self.assertIn("内容已截断", message["content"])
        self.assertLessEqual(len(message["content"]), 20_000)

    def test_total_response_content_cap(self):
        task_id = self.new_task("总大小上限")
        add_run(self.server, task_id, {"worker_session_id": "sess-total"})
        add_session(
            self.state_db,
            "sess-total",
            messages=[{"content": f"{index:02d}" + "y" * 19_998} for index in range(60)],
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertLess(body["returned_count"], 60)
        self.assertTrue(body["truncated"])
        self.assertEqual(body["total_count"], 60)
        self.assertLessEqual(
            sum(len(message["content"]) for message in body["messages"]),
            1_000_000,
        )
        # Newest messages survive the response budget.
        self.assertTrue(body["messages"][-1]["content"].startswith("59"))

    def test_messages_are_newest_200_in_id_order(self):
        task_id = self.new_task("超长会话")
        add_run(self.server, task_id, {"worker_session_id": "sess-long"})
        add_session(
            self.state_db,
            "sess-long",
            messages=[
                {"content": f"m{index}", "timestamp": 1000 + index}
                for index in range(205)
            ],
        )
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["truncated"])
        self.assertEqual(body["message_limit"], 200)
        self.assertEqual(body["returned_count"], 200)
        self.assertEqual(body["total_count"], 205)
        self.assertEqual(body["session"]["message_count"], 205)
        self.assertEqual(body["messages"][0]["content"], "m5")
        self.assertEqual(body["messages"][-1]["content"], "m204")
        ids = [message["id"] for message in body["messages"]]
        self.assertEqual(ids, sorted(ids))

    def test_count_mismatch_does_not_lie(self):
        inflated = self.new_task("count 偏大")
        add_run(self.server, inflated, {"worker_session_id": "sess-inflated"})
        add_session(
            self.state_db,
            "sess-inflated",
            messages=[{"content": "a"}, {"content": "b"}],
            message_count=99,
        )
        code, body = self.transcript(inflated)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["total_count"], 2)
        self.assertEqual(body["returned_count"], 2)
        self.assertFalse(body["truncated"])
        self.assertEqual(body["session"]["message_count"], 99)

        stale = self.new_task("count 偏小")
        add_run(self.server, stale, {"worker_session_id": "sess-stale"})
        add_session(
            self.state_db,
            "sess-stale",
            messages=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
            message_count=1,
        )
        code, body = self.transcript(stale)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["total_count"], 3)
        self.assertFalse(body["truncated"])

    def test_message_limit_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / "state.db"
            create_state_db(state_db)
            add_session(
                state_db,
                "sess-short",
                messages=[
                    {"content": f"m{index}", "timestamp": 100 + index}
                    for index in range(5)
                ],
            )
            server = start_session_server(
                root / "srv",
                state_db,
                env_extra={"WORKBENCH_SESSION_MESSAGE_LIMIT": "3"},
            )
            try:
                task_id = server.create_task("小窗口", triage=True)
                add_run(server, task_id, {"worker_session_id": "sess-short"})
                code, body = base.http_json(
                    "GET",
                    server.base
                    + "/api/workbench/session/"
                    + urlparse.quote(task_id, safe=""),
                )
            finally:
                server.stop()
        self.assertEqual(code, 200, body)
        self.assertEqual(body["message_limit"], 3)
        self.assertTrue(body["truncated"])
        self.assertEqual(body["returned_count"], 3)
        self.assertEqual(body["total_count"], 5)
        self.assertEqual([m["content"] for m in body["messages"]], ["m2", "m3", "m4"])

    def test_empty_session_returns_no_messages(self):
        task_id = self.seed_session_task("空会话", "sess-empty", messages=[])
        code, body = self.transcript(task_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["messages"], [])
        self.assertEqual(body["returned_count"], 0)
        self.assertEqual(body["total_count"], 0)
        self.assertFalse(body["truncated"])
        self.assertEqual(body["session"]["message_count"], 0)


class UnavailableTests(unittest.TestCase):
    def _seed_task(self, server: base.WorkbenchServer, session_id: str = "sess-x") -> str:
        task_id = server.create_task("状态库不可用", triage=True)
        add_run(server, task_id, {"worker_session_id": session_id})
        return task_id

    def _session(self, server: base.WorkbenchServer, task_id: str):
        return http_json_headers(
            "GET",
            server.base
            + "/api/workbench/session/"
            + urlparse.quote(task_id, safe=""),
        )

    def test_missing_state_db_is_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = start_session_server(root / "srv", root / "does-not-exist.db")
            try:
                task_id = self._seed_task(server)
                code, body, headers = self._session(server, task_id)
                self.assertEqual(server.status(task_id), "triage")
            finally:
                server.stop()
        self.assertEqual(code, 503, body)
        self.assertEqual(body, {"ok": False, "error": "session_unavailable"})
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_directory_state_db_is_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / "state-dir"
            broken.mkdir()
            server = start_session_server(root / "srv", broken)
            try:
                task_id = self._seed_task(server)
                code, body, _headers = self._session(server, task_id)
            finally:
                server.stop()
        self.assertEqual(code, 503, body)
        self.assertEqual(body["error"], "session_unavailable")

    def test_empty_state_db_is_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty.db"
            empty.write_bytes(b"")
            server = start_session_server(root / "srv", empty)
            try:
                task_id = self._seed_task(server)
                code, body, _headers = self._session(server, task_id)
            finally:
                server.stop()
        self.assertEqual(code, 503, body)
        self.assertEqual(body["error"], "session_unavailable")

    def test_corrupt_state_db_is_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corrupt = root / "corrupt.db"
            corrupt.write_bytes(b"this is not a sqlite database")
            server = start_session_server(root / "srv", corrupt)
            try:
                task_id = self._seed_task(server)
                code, body, _headers = self._session(server, task_id)
            finally:
                server.stop()
        self.assertEqual(code, 503, body)
        self.assertEqual(body["error"], "session_unavailable")

    def test_locked_state_db_is_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / "state.db"
            create_state_db(state_db)
            add_session(state_db, "sess-locked", messages=[{"content": "locked"}])
            locker = sqlite3.connect(str(state_db), timeout=5)
            locker.execute("BEGIN EXCLUSIVE")
            try:
                server = start_session_server(root / "srv", state_db)
                try:
                    task_id = self._seed_task(server, "sess-locked")
                    code, body, _headers = self._session(server, task_id)
                finally:
                    server.stop()
            finally:
                locker.execute("ROLLBACK")
                locker.close()
        self.assertEqual(code, 503, body)
        self.assertEqual(body["error"], "session_unavailable")

    def test_upstream_unreachable_is_502(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / "state.db"
            create_state_db(state_db)
            closed_port = base.free_port()
            server = start_session_server(
                root / "srv",
                state_db,
                upstream=f"http://127.0.0.1:{closed_port}",
            )
            try:
                code, body, _headers = http_json_headers(
                    "GET", server.base + "/api/workbench/session/any"
                )
            finally:
                server.stop()
        self.assertEqual(code, 502, body)
        self.assertEqual(body, {"ok": False, "error": "upstream_unavailable"})


if __name__ == "__main__":
    unittest.main(verbosity=2)