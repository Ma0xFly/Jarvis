#!/usr/bin/env python3
"""Tests for Story 3.4: executor contract + ``claude_code`` adapter.

The harness puts a fake ``claude`` executable first on ``PATH``. It records
the argv it received and the task book it read from stdin, then emits one of
several canned JSON results. No real Claude session is started.

Covers:

* success mapping (session/result/cost/usage) and the final JSON envelope
* ``--mode write`` -> ``acceptEdits``, ``readonly`` -> ``plan``
* ``--max-usd`` -> ``--max-budget-usd`` and ``--resume`` forwarding
* exit codes 0 / 1 / 2 / 3 / 4 (including missing binary, unknown executor,
  missing registry and timeout)
* task book on stdin only; logs stay under ``.runs/`` and are redacted
* adapter unit behaviour (last-line JSON, permission/budget text, cost sums)

Run with::

    python3 tests/test_executor_run.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "executor-run"
REGISTRY = ROOT / "executors.json"

sys.path.insert(0, str(ROOT / "bin"))
from executors import claude_code, codex, dsh, hermes  # noqa: E402

TASK_BOOK_MARKER = "TASK_BOOK_MARKER_9f3c2b"

FAKE_CLAUDE = """#!__PYTHON__
import json
import os
import sys
import time

prompt = sys.stdin.read()
capture = os.environ.get("FAKE_CLAUDE_CAPTURE")
if capture:
    with open(capture, "w", encoding="utf-8") as handle:
        json.dump({"argv": sys.argv[1:], "stdin": prompt}, handle, ensure_ascii=False)

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")


def emit(payload, code=0):
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


if mode == "ok":
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "改完了 3 个文件，测试通过",
        "session_id": "sess-123",
        "total_cost_usd": 0.42,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
        },
        "permission_denials": [],
    })
elif mode == "permission":
    emit({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "permission denied: Bash",
        "session_id": "sess-perm",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "permission_denials": [{"tool_name": "Bash"}],
    }, code=1)
elif mode == "budget":
    emit({
        "type": "result",
        "subtype": "error_max_budget_usd",
        "is_error": True,
        "result": "Exceeded USD budget (2)",
        "session_id": "sess-budget",
        "total_cost_usd": 1.99,
        "usage": {"input_tokens": 5, "output_tokens": 5},
        "permission_denials": [],
    }, code=1)
elif mode == "badjson":
    print("this is not json")
    sys.exit(0)
elif mode == "nonzero":
    print("boom", file=sys.stderr)
    sys.exit(7)
elif mode == "nosession":
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "total_cost_usd": 0.0,
        "usage": {},
    })
elif mode == "echo":
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": prompt,
        "session_id": "sess-echo",
        "total_cost_usd": 0.0,
        "usage": {},
    })
elif mode == "nonresult":
    emit({"type": "system", "subtype": "init", "session_id": "sess-init"})
elif mode == "stderr_budget":
    print("Exceeded USD budget (2)", file=sys.stderr)
    sys.exit(1)
elif mode == "stderr_permission":
    print("permission denied: Bash", file=sys.stderr)
    sys.exit(1)
elif mode == "success_budget_text":
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Added a budget limit",
        "session_id": "sess-ok",
        "total_cost_usd": 0.0,
        "usage": {},
        "permission_denials": [],
    })
elif mode == "success_permission_text":
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "permission required for docs",
        "session_id": "sess-ok",
        "total_cost_usd": 0.0,
        "usage": {},
        "permission_denials": [],
    })
elif mode == "secret_echo":
    secret = os.environ.get("EXECUTOR_SECRET_TOKEN", "")
    print("secret=" + secret, file=sys.stderr)
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "used " + secret,
        "session_id": "sess-secret",
        "total_cost_usd": 0.0,
        "usage": {},
    })
elif mode == "echo_fail":
    print(prompt, file=sys.stderr)
    sys.exit(7)
elif mode == "marker":
    marker = os.environ.get("FAKE_CLAUDE_MARKER")
    if marker:
        with open(marker, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started": True}, handle)
    time.sleep(30)
    sys.exit(0)
elif mode == "sleep":
    time.sleep(30)
    sys.exit(0)
else:
    print("unknown fake mode", file=sys.stderr)
    sys.exit(9)
"""


FAKE_DSH = """#!__PYTHON__
import json
import os
import sys

prompt = sys.stdin.read()
capture = os.environ.get("FAKE_DSH_CAPTURE")
if capture:
    with open(capture, "w", encoding="utf-8") as handle:
        json.dump({"argv": sys.argv[1:], "stdin": prompt}, handle, ensure_ascii=False)

mode = os.environ.get("FAKE_DSH_MODE", "ok")


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


if mode == "ok":
    emit({"type": "session", "sessionId": "session-abc"})
    emit({"type": "status", "phase": "step_start"})
    emit({
        "type": "status",
        "phase": "step_end",
        "usage": {"inputTokens": 10, "outputTokens": 20},
    })
    emit({"type": "text", "text": "中间输出"})
    emit({
        "type": "status",
        "phase": "step_end",
        "usage": {"inputTokens": 1, "outputTokens": 2},
    })
    emit({"type": "final", "text": "dsh 改完 3 个文件"})
    print("diagnostic only", file=sys.stderr)
    sys.exit(0)
elif mode == "nosession":
    emit({
        "type": "status",
        "phase": "step_end",
        "usage": {"inputTokens": 5, "outputTokens": 5},
    })
    emit({"type": "final", "text": "ok"})
elif mode == "nofinal":
    emit({"type": "session", "sessionId": "session-nofinal"})
    emit({
        "type": "status",
        "phase": "step_end",
        "usage": {"inputTokens": 1, "outputTokens": 1},
    })
elif mode == "unrecognized":
    emit({"type": "unknown.event", "value": 1})
elif mode == "badjson":
    print("this is not json")
elif mode == "error_event":
    emit({"type": "error", "message": "dsh 内部错误"})
    sys.exit(7)
elif mode == "error_no_message":
    emit({"type": "error"})
    emit({"type": "final", "text": "看起来成功"})
elif mode == "nonzero_with_final":
    emit({"type": "final", "text": "看起来成功"})
    print("boom-dsh", file=sys.stderr)
    sys.exit(7)
elif mode == "nonzero":
    print("boom", file=sys.stderr)
    sys.exit(7)
else:
    print("unknown fake dsh mode", file=sys.stderr)
    sys.exit(9)
"""


FAKE_CODEX = """#!__PYTHON__
import json
import os
import sys

prompt = sys.stdin.read()
capture = os.environ.get("FAKE_CODEX_CAPTURE")
if capture:
    with open(capture, "w", encoding="utf-8") as handle:
        json.dump({"argv": sys.argv[1:], "stdin": prompt}, handle, ensure_ascii=False)

mode = os.environ.get("FAKE_CODEX_MODE", "ok")


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


if mode == "ok":
    emit({"type": "thread.started", "thread_id": "thread-abc"})
    emit({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "第一版"},
    })
    emit({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "最终答复"},
    })
    emit({
        "type": "turn.completed",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    })
elif mode == "minimal":
    emit({"type": "thread.started", "thread_id": "thread-min"})
    emit({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "done"},
    })
elif mode == "unrecognized":
    emit({
        "type": "item.started",
        "item": {"type": "agent_message", "text": "ignored"},
    })
elif mode == "thread_only":
    emit({"type": "thread.started", "thread_id": "thread-only"})
elif mode == "turn_only":
    emit({
        "type": "turn.completed",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
elif mode == "error_no_message":
    emit({"type": "error"})
elif mode == "badjson":
    print("this is not json")
elif mode == "turn_failed":
    emit({
        "type": "turn.failed",
        "error": {"message": "codex 失败"},
    })
    sys.exit(7)
elif mode == "nonzero_with_agent":
    emit({"type": "thread.started", "thread_id": "thread-nonzero"})
    emit({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "看起来完成了"},
    })
    print("boom-codex", file=sys.stderr)
    sys.exit(7)
elif mode == "nonzero":
    print("boom", file=sys.stderr)
    sys.exit(7)
else:
    print("unknown fake codex mode", file=sys.stderr)
    sys.exit(9)
"""


class FakeClaudeHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="executor-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.fake_bin = self.tmp / "bin"
        self.fake_bin.mkdir()
        self._install_fake("claude", FAKE_CLAUDE)
        self._install_fake("dsh", FAKE_DSH)
        self._install_fake("codex", FAKE_CODEX)

        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        self.capture = self.tmp / "capture.json"
        self.dsh_capture = self.tmp / "dsh-capture.json"
        self.codex_capture = self.tmp / "codex-capture.json"
        self.marker = self.tmp / "marker.json"
        self.task = f"# 任务\n\n{TASK_BOOK_MARKER}\n\n只改 x.py。\n"

    def _install_fake(self, name: str, source: str) -> None:
        fake = self.fake_bin / name
        fake.write_text(source.replace("__PYTHON__", sys.executable), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_cli(
        self,
        *,
        mode: str = "ok",
        dsh_mode: str = "ok",
        codex_mode: str = "ok",
        args=None,
        task: str | None = None,
        path: str | None = None,
        registry: Path | None = None,
        env_overrides: dict | None = None,
        use_fake_path: bool = True,
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["FAKE_CLAUDE_MODE"] = mode
        env["FAKE_CLAUDE_CAPTURE"] = str(self.capture)
        env["FAKE_CLAUDE_MARKER"] = str(self.marker)
        env["FAKE_DSH_MODE"] = dsh_mode
        env["FAKE_DSH_CAPTURE"] = str(self.dsh_capture)
        env["FAKE_CODEX_MODE"] = codex_mode
        env["FAKE_CODEX_CAPTURE"] = str(self.codex_capture)
        if path is not None:
            env["PATH"] = path
        elif use_fake_path:
            env["PATH"] = str(self.fake_bin)
        if env_overrides:
            env.update(env_overrides)

        command = [sys.executable, str(CLI)]
        if registry is not None:
            command.extend(["--registry", str(registry)])
        if args is None:
            args = [
                "--executor",
                "claude_code",
                "--cwd",
                str(self.cwd),
                "--mode",
                "write",
                "--budget-minutes",
                "30",
                "--max-usd",
                "2",
            ]
        command.extend(args)
        return subprocess.run(
            command,
            input=self.task if task is None else task,
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )

    def capture_data(self) -> dict:
        self.assertTrue(self.capture.is_file(), "fake claude was not invoked")
        return json.loads(self.capture.read_text(encoding="utf-8"))

    def dsh_capture_data(self) -> dict:
        self.assertTrue(self.dsh_capture.is_file(), "fake dsh was not invoked")
        return json.loads(self.dsh_capture.read_text(encoding="utf-8"))

    def codex_capture_data(self) -> dict:
        self.assertTrue(self.codex_capture.is_file(), "fake codex was not invoked")
        return json.loads(self.codex_capture.read_text(encoding="utf-8"))

    def write_registry(self, data, name: str = "registry.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def write_module(self, name: str, source: str) -> Path:
        modules = self.tmp / "modules"
        modules.mkdir(exist_ok=True)
        (modules / f"{name}.py").write_text(source, encoding="utf-8")
        return modules

    @staticmethod
    def payload(proc: subprocess.CompletedProcess) -> dict:
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        assert lines, f"no stdout; stderr={proc.stderr!r}"
        return json.loads(lines[-1])


class SuccessContractTests(FakeClaudeHarness):
    def test_success_envelope_and_log(self) -> None:
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)

        self.assertEqual(
            set(payload),
            {
                "ok",
                "executor",
                "session_id",
                "summary",
                "files_changed",
                "cost",
                "log_path",
                "exit_code",
            },
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["executor"], "claude_code")
        self.assertEqual(payload["session_id"], "sess-123")
        self.assertEqual(payload["summary"], "改完了 3 个文件，测试通过")
        self.assertEqual(payload["files_changed"], [])
        self.assertEqual(payload["cost"], {"usd": 0.42, "tokens": 37})
        self.assertEqual(payload["exit_code"], 0)

        self.assertFalse(Path(payload["log_path"]).is_absolute())
        self.assertTrue(payload["log_path"].startswith(".runs/"))
        log = self.cwd / payload["log_path"]
        self.assertTrue(log.is_file())
        text = log.read_text(encoding="utf-8")
        self.assertIn("command: claude", text)
        self.assertIn("exit_code: 0", text)
        self.assertIn("改完了 3 个文件", text)

    def test_stdout_is_only_the_json_line(self) -> None:
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0)
        non_empty = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(non_empty), 1, proc.stdout)

    def test_task_book_goes_to_stdin_only(self) -> None:
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        captured = self.capture_data()
        self.assertEqual(captured["stdin"], self.task)
        self.assertNotIn(TASK_BOOK_MARKER, " ".join(captured["argv"]))
        self.assertNotIn(TASK_BOOK_MARKER, proc.stdout)
        log = self.cwd / self.payload(proc)["log_path"]
        self.assertNotIn(TASK_BOOK_MARKER, log.read_text(encoding="utf-8"))

    def test_write_flags_and_resume_forwarding(self) -> None:
        args = [
            "--executor",
            "claude_code",
            "--cwd",
            str(self.cwd),
            "--mode",
            "write",
            "--budget-minutes",
            "30",
            "--max-usd",
            "2",
            "--resume",
            "sess-old",
        ]
        proc = self.run_cli(args=args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.capture_data()["argv"]

        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2")
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-old")
        self.assertIn("--disallowedTools", argv)
        self.assertIn("Bash(git commit *)", argv)
        self.assertIn("Bash(git push *)", argv)

    def test_max_usd_omitted_forwards_no_flag(self) -> None:
        args = [
            "--executor",
            "claude_code",
            "--cwd",
            str(self.cwd),
            "--budget-minutes",
            "30",
        ]
        proc = self.run_cli(args=args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("--max-budget-usd", self.capture_data()["argv"])

    def test_default_budget_is_30_minutes(self) -> None:
        args = ["--executor", "claude_code", "--cwd", str(self.cwd)]
        proc = self.run_cli(args=args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = (self.cwd / self.payload(proc)["log_path"]).read_text(encoding="utf-8")
        self.assertIn("timeout_seconds: 1800.0", log)

    def test_readonly_maps_to_plan(self) -> None:
        args = [
            "--executor",
            "claude_code",
            "--cwd",
            str(self.cwd),
            "--mode",
            "readonly",
            "--budget-minutes",
            "30",
        ]
        proc = self.run_cli(args=args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.capture_data()["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertNotIn("acceptEdits", argv)

    def test_no_resume_flag_without_resume(self) -> None:
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("--resume", self.capture_data()["argv"])

    def test_no_session_id_is_null(self) -> None:
        proc = self.run_cli(mode="nosession")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertIsNone(payload["session_id"])

    def test_log_redacts_task_book_echo(self) -> None:
        proc = self.run_cli(mode="echo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertNotIn(TASK_BOOK_MARKER, proc.stdout)
        self.assertNotIn(TASK_BOOK_MARKER, payload["summary"])
        log = (self.cwd / payload["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn(TASK_BOOK_MARKER, log)
        self.assertIn("[task book redacted]", log)

    def test_log_does_not_contain_env_secrets(self) -> None:
        from unittest import mock

        secret = "sk-test-SECRET-do-not-log-8c1d"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}, clear=False):
            proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        log = (self.cwd / payload["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn(secret, log)
        self.assertNotIn(secret, proc.stdout)

    def test_short_task_book_is_redacted(self) -> None:
        proc = self.run_cli(mode="echo", task="abc")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertNotIn("abc", proc.stdout)
        log = (self.cwd / payload["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn("abc", log)
        self.assertIn("[task book redacted]", log)

    def test_failure_summary_redacts_task_book(self) -> None:
        proc = self.run_cli(mode="echo_fail")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertNotIn(TASK_BOOK_MARKER, payload["summary"])
        log = (self.cwd / payload["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn(TASK_BOOK_MARKER, log)
        self.assertIn("[task book redacted]", log)

    def test_executor_emitted_secret_is_redacted(self) -> None:
        secret = "EXECUTOR-TOKEN-value-do-not-log"
        proc = self.run_cli(
            mode="secret_echo",
            env_overrides={"EXECUTOR_SECRET_TOKEN": secret},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertNotIn(secret, proc.stdout)
        self.assertNotIn(secret, payload["summary"])
        log = (self.cwd / payload["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn(secret, log)
        self.assertIn("[redacted]", log)

    def test_hermes_kanban_db_is_untouched(self) -> None:
        home = self.tmp / "hermes-home"
        home.mkdir()
        kanban = home / "kanban.db"
        sentinel = b"sentinel-kanban-bytes"
        kanban.write_bytes(sentinel)
        proc = self.run_cli(env_overrides={"HERMES_KANBAN_HOME": str(home)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(kanban.read_bytes(), sentinel)


class ExitCodeTests(FakeClaudeHarness):
    def test_permission_denial_is_2(self) -> None:
        proc = self.run_cli(mode="permission")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 2)
        self.assertEqual(payload["session_id"], "sess-perm")
        self.assertEqual(payload["summary"], "permission denied: Bash")

    def test_budget_interruption_is_3(self) -> None:
        proc = self.run_cli(mode="budget")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 3)
        self.assertEqual(payload["session_id"], "sess-budget")
        self.assertEqual(payload["cost"], {"usd": 1.99, "tokens": 10})

    def test_timeout_is_3_and_keeps_log(self) -> None:
        args = [
            "--executor",
            "claude_code",
            "--cwd",
            str(self.cwd),
            "--mode",
            "write",
            "--budget-minutes",
            "0.01",
        ]
        proc = self.run_cli(mode="sleep", args=args)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 3)
        self.assertIsNotNone(payload["log_path"])
        log = self.cwd / payload["log_path"]
        self.assertTrue(log.is_file())
        self.assertIn("timed_out: true", log.read_text(encoding="utf-8"))

    def test_missing_binary_is_4(self) -> None:
        empty_bin = self.tmp / "empty-bin"
        empty_bin.mkdir()
        proc = self.run_cli(path=str(empty_bin))
        self.assertEqual(proc.returncode, 4, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 4)
        self.assertIn("找不到 claude", payload["summary"])

    def test_unknown_executor_is_4(self) -> None:
        args = [
            "--executor",
            "does_not_exist",
            "--cwd",
            str(self.cwd),
            "--budget-minutes",
            "30",
        ]
        proc = self.run_cli(args=args)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "does_not_exist")
        self.assertEqual(payload["exit_code"], 4)
        self.assertIn("未知执行器", payload["summary"])

    def test_missing_registry_is_4(self) -> None:
        proc = self.run_cli(registry=self.tmp / "missing-executors.json")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 4)
        self.assertIn("注册表不可用", payload["summary"])

    def test_bad_json_output_is_1(self) -> None:
        proc = self.run_cli(mode="badjson")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIsNone(payload["session_id"])
        self.assertIn("无法解析", payload["summary"])

    def test_nonzero_executor_exit_is_1(self) -> None:
        proc = self.run_cli(mode="nonzero")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("退出码 7", payload["summary"])
        self.assertIn("boom", payload["summary"])

    def test_empty_task_book_is_1(self) -> None:
        proc = self.run_cli(task="   \n\n")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("任务书为空", payload["summary"])
        self.assertFalse(self.capture.exists())

    def test_invalid_cwd_is_1(self) -> None:
        args = [
            "--executor",
            "claude_code",
            "--cwd",
            str(self.tmp / "does-not-exist"),
            "--budget-minutes",
            "30",
        ]
        proc = self.run_cli(args=args)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("cwd 不可用", payload["summary"])

    def test_invalid_budget_is_1(self) -> None:
        args = [
            "--executor",
            "claude_code",
            "--cwd",
            str(self.cwd),
            "--budget-minutes",
            "0",
        ]
        proc = self.run_cli(args=args)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("有限正数", payload["summary"])

    def test_argparse_error_uses_exit_1_not_2(self) -> None:
        proc = self.run_cli(args=["--executor", "claude_code"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("--cwd", proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["executor"], "-")
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("参数错误", payload["summary"])

    def test_nonresult_json_message_is_1(self) -> None:
        proc = self.run_cli(mode="nonresult")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("不是 result 消息", payload["summary"])

    def test_stderr_budget_is_3(self) -> None:
        proc = self.run_cli(mode="stderr_budget")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["exit_code"], 3)
        self.assertIn("预算中断", payload["summary"])

    def test_stderr_permission_is_2(self) -> None:
        proc = self.run_cli(mode="stderr_permission")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["exit_code"], 2)
        self.assertIn("人工授权", payload["summary"])

    def test_success_budget_text_stays_0(self) -> None:
        proc = self.run_cli(mode="success_budget_text")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["exit_code"], 0)

    def test_success_permission_text_stays_0(self) -> None:
        proc = self.run_cli(mode="success_permission_text")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["exit_code"], 0)

    def test_empty_or_whitespace_cwd_is_1(self) -> None:
        for cwd in ("", "   "):
            with self.subTest(cwd=cwd):
                proc = self.run_cli(args=["--executor", "claude_code", "--cwd", cwd])
                self.assertEqual(proc.returncode, 1, proc.stderr)
                payload = self.payload(proc)
                self.assertEqual(payload["exit_code"], 1)
                self.assertIn("cwd", payload["summary"])
                self.assertFalse(self.capture.exists())

    def test_help_exits_0(self) -> None:
        proc = subprocess.run(
            [str(CLI), "--help"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in (
            "--executor",
            "--cwd",
            "--mode",
            "--budget-minutes",
            "--max-usd",
            "--resume",
            "--registry",
        ):
            self.assertIn(flag, proc.stdout)
        self.assertFalse(proc.stdout.lstrip().startswith("{"))


class RegistryBehaviorTests(FakeClaudeHarness):
    def test_env_registry_is_used_without_flag(self) -> None:
        registry = self.write_registry(
            {
                "executors": {
                    "env_exec": {
                        "module": "executors.claude_code",
                        "binary": "claude",
                    }
                }
            }
        )
        args = ["--executor", "env_exec", "--cwd", str(self.cwd), "--budget-minutes", "30"]
        proc = self.run_cli(args=args, env_overrides={"EXECUTOR_REGISTRY": str(registry)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.payload(proc)["executor"], "env_exec")

    def test_cli_registry_overrides_env(self) -> None:
        args = ["--executor", "claude_code", "--cwd", str(self.cwd), "--budget-minutes", "30"]
        proc = self.run_cli(
            args=args,
            registry=REGISTRY,
            env_overrides={"EXECUTOR_REGISTRY": str(self.tmp / "missing.json")},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_env_registry_is_4(self) -> None:
        args = ["--executor", "claude_code", "--cwd", str(self.cwd), "--budget-minutes", "30"]
        proc = self.run_cli(
            args=args,
            env_overrides={"EXECUTOR_REGISTRY": str(self.tmp / "missing.json")},
        )
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("注册表不可用", self.payload(proc)["summary"])

    def test_flat_registry_ignores_metadata_keys(self) -> None:
        registry = self.write_registry(
            {
                "version": 1,
                "$schema": "https://example.invalid/schema.json",
                "description": "flat mapping",
                "claude_code": "executors.claude_code",
            }
        )
        proc = self.run_cli(registry=registry)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_bare_mapping_registry_is_accepted(self) -> None:
        registry = self.write_registry({"claude_code": "executors.claude_code"})
        proc = self.run_cli(registry=registry)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_malformed_registry_json_is_4(self) -> None:
        registry = self.tmp / "bad.json"
        registry.write_text("{not json", encoding="utf-8")
        proc = self.run_cli(registry=registry)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("合法 JSON", self.payload(proc)["summary"])

    def test_non_dict_registry_is_4(self) -> None:
        registry = self.tmp / "list.json"
        registry.write_text("[]", encoding="utf-8")
        proc = self.run_cli(registry=registry)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("格式错误", self.payload(proc)["summary"])

    def test_in_process_mode_is_4_end_to_end(self) -> None:
        registry = self.write_registry(
            {
                "executors": {
                    "local": {
                        "module": "executors.claude_code",
                        "mode": "in-process",
                    }
                }
            }
        )
        args = ["--executor", "local", "--cwd", str(self.cwd), "--budget-minutes", "30"]
        proc = self.run_cli(args=args, registry=registry)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("不支持 CLI 调用", self.payload(proc)["summary"])

    def test_missing_binary_key_uses_adapter_default(self) -> None:
        registry = self.write_registry(
            {"executors": {"claude_code": {"module": "executors.claude_code"}}}
        )
        proc = self.run_cli(registry=registry)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.payload(proc)["ok"])

    def test_relative_binary_resolves_against_cwd(self) -> None:
        relative_dir = self.cwd / "relbin"
        relative_dir.mkdir()
        script = relative_dir / "claude"
        script.write_text(
            FAKE_CLAUDE.replace("__PYTHON__", sys.executable),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        registry = self.write_registry(
            {
                "executors": {
                    "claude_code": {
                        "module": "executors.claude_code",
                        "binary": "./relbin/claude",
                    }
                }
            }
        )
        empty_path = self.tmp / "empty-path"
        empty_path.mkdir()
        proc = self.run_cli(registry=registry, path=str(empty_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.capture.is_file())


class AdapterFailureTests(FakeClaudeHarness):
    BUILD_FAIL = (
        "def build_command(ctx):\n"
        "    raise RuntimeError('boom-build')\n"
        "def translate_result(stdout, stderr, raw_exit):\n"
        "    return {}\n"
    )
    TRANSLATE_FAIL = (
        "def build_command(ctx):\n"
        "    return ['claude', '-p', '--output-format', 'json']\n"
        "def translate_result(stdout, stderr, raw_exit):\n"
        "    raise RuntimeError('boom-translate')\n"
    )

    def test_build_command_exception_is_1(self) -> None:
        modules = self.write_module("broken_build", self.BUILD_FAIL)
        registry = self.write_registry(
            {"executors": {"broken": {"module": "broken_build"}}}
        )
        args = ["--executor", "broken", "--cwd", str(self.cwd), "--budget-minutes", "30"]
        proc = self.run_cli(
            args=args,
            registry=registry,
            env_overrides={"PYTHONPATH": str(modules)},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("适配器构建命令失败", self.payload(proc)["summary"])

    def test_translate_result_exception_is_1(self) -> None:
        modules = self.write_module("broken_translate", self.TRANSLATE_FAIL)
        registry = self.write_registry(
            {"executors": {"broken": {"module": "broken_translate"}}}
        )
        args = ["--executor", "broken", "--cwd", str(self.cwd), "--budget-minutes", "30"]
        proc = self.run_cli(
            args=args,
            registry=registry,
            env_overrides={"PYTHONPATH": str(modules)},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("适配器解析结果失败", self.payload(proc)["summary"])


class TerminationTests(FakeClaudeHarness):
    def _args(self, budget: str = "30") -> list:
        return [
            "--executor",
            "claude_code",
            "--cwd",
            str(self.cwd),
            "--budget-minutes",
            budget,
        ]

    def _wait_marker(self, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.marker.is_file():
                try:
                    data = json.loads(self.marker.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = None
                if isinstance(data, dict) and data.get("pid"):
                    return data
            time.sleep(0.02)
        self.fail("fake claude never wrote its marker")

    def _wait_pid_gone(self, pid: int, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"executor child {pid} is still alive")

    def test_sigterm_kills_child_and_emits_json(self) -> None:
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin)
        env["FAKE_CLAUDE_MODE"] = "marker"
        env["FAKE_CLAUDE_CAPTURE"] = str(self.capture)
        env["FAKE_CLAUDE_MARKER"] = str(self.marker)
        proc = subprocess.Popen(
            [sys.executable, str(CLI), *self._args()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            proc.stdin.write(self.task)
            proc.stdin.close()
            info = self._wait_marker()
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

        self.assertEqual(proc.returncode, 1, stderr)
        payload = json.loads([line for line in stdout.splitlines() if line.strip()][-1])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("中断", payload["summary"])
        self._wait_pid_gone(info["pid"])

    def test_timeout_kills_child_process_group(self) -> None:
        proc = self.run_cli(mode="marker", args=self._args("0.05"))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        info = json.loads(self.marker.read_text(encoding="utf-8"))
        self._wait_pid_gone(info["pid"])


class DshContractTests(FakeClaudeHarness):
    def _args(self, *, mode: str = "write", resume=None) -> list:
        args = [
            "--executor",
            "dsh",
            "--cwd",
            str(self.cwd),
            "--mode",
            mode,
            "--budget-minutes",
            "30",
        ]
        if resume is not None:
            args.extend(["--resume", resume])
        return args

    def test_success_maps_ndjson_events(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["executor"], "dsh")
        self.assertEqual(payload["session_id"], "session-abc")
        self.assertEqual(payload["summary"], "dsh 改完 3 个文件")
        self.assertEqual(payload["cost"], {"usd": None, "tokens": 33})
        self.assertEqual(payload["exit_code"], 0)

        captured = self.dsh_capture_data()
        self.assertEqual(captured["argv"], ["--profile", "headless", "--json"])
        self.assertEqual(captured["stdin"], self.task)
        self.assertNotIn(TASK_BOOK_MARKER, " ".join(captured["argv"]))
        self.assertNotIn(TASK_BOOK_MARKER, proc.stdout)
        log = (self.cwd / payload["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn(TASK_BOOK_MARKER, log)
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.codex_capture.exists())

    def test_resume_maps_to_session_id(self) -> None:
        proc = self.run_cli(args=self._args(resume="session-old"), dsh_mode="ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        captured = self.dsh_capture_data()
        self.assertEqual(
            captured["argv"],
            ["--profile", "headless", "--json", "--session-id", "session-old"],
        )
        # The resumed id is not fabricated into the result; the event wins.
        self.assertEqual(self.payload(proc)["session_id"], "session-abc")

    def test_missing_session_event_is_null(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="nosession")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["session_id"])
        self.assertEqual(payload["cost"], {"usd": None, "tokens": 10})

    def test_missing_final_event_is_1(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="nofinal")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["session_id"], "session-nofinal")
        self.assertIn("final", payload["summary"])

    def test_unrecognized_events_are_1(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="unrecognized")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("没有可识别的事件", self.payload(proc)["summary"])

    def test_unparsable_output_is_1(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="badjson")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["session_id"])
        self.assertIn("无法解析", payload["summary"])

    def test_error_event_is_1(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="error_event")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIn("dsh 内部错误", payload["summary"])

    def test_error_without_message_is_not_success(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="error_no_message")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("错误信息", payload["summary"])
        self.assertIn("看起来成功", payload["summary"])

    def test_nonzero_with_final_prefers_exit_and_stderr(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="nonzero_with_final")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIn("退出码 7", payload["summary"])
        self.assertIn("boom-dsh", payload["summary"])
        self.assertIn("看起来成功", payload["summary"])

    def test_readonly_mode_is_rejected(self) -> None:
        proc = self.run_cli(args=self._args(mode="readonly"), dsh_mode="ok")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIn("只读", payload["summary"])
        self.assertFalse(self.dsh_capture.exists())

    def test_nonzero_exit_is_1(self) -> None:
        proc = self.run_cli(args=self._args(), dsh_mode="nonzero")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("boom", self.payload(proc)["summary"])

    def test_missing_binary_is_4_without_starting(self) -> None:
        empty_bin = self.tmp / "empty-bin"
        empty_bin.mkdir()
        proc = self.run_cli(args=self._args(), path=str(empty_bin))
        self.assertEqual(proc.returncode, 4, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "dsh")
        self.assertEqual(payload["exit_code"], 4)
        self.assertIn("找不到 dsh", payload["summary"])
        self.assertFalse(self.dsh_capture.exists())


class CodexContractTests(FakeClaudeHarness):
    def _args(self, *, mode: str = "write", resume=None) -> list:
        args = [
            "--executor",
            "codex",
            "--cwd",
            str(self.cwd),
            "--mode",
            mode,
            "--budget-minutes",
            "30",
        ]
        if resume is not None:
            args.extend(["--resume", resume])
        return args

    def test_success_maps_jsonl_events(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["executor"], "codex")
        self.assertEqual(payload["session_id"], "thread-abc")
        self.assertEqual(payload["summary"], "最终答复")
        self.assertEqual(payload["cost"], {"usd": None, "tokens": 30})
        self.assertEqual(payload["exit_code"], 0)

        captured = self.codex_capture_data()
        self.assertEqual(
            captured["argv"],
            ["exec", "--json", "--sandbox", "workspace-write", "-"],
        )
        self.assertEqual(captured["stdin"], self.task)
        self.assertNotIn(TASK_BOOK_MARKER, " ".join(captured["argv"]))
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.dsh_capture.exists())

    def test_minimal_thread_and_agent_message_is_ok(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="minimal")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["session_id"], "thread-min")
        self.assertEqual(payload["summary"], "done")
        self.assertEqual(payload["cost"], {"usd": None, "tokens": 0})

    def test_readonly_maps_to_read_only_sandbox(self) -> None:
        proc = self.run_cli(args=self._args(mode="readonly"), codex_mode="minimal")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.codex_capture_data()["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertNotIn("workspace-write", argv)

    def test_resume_uses_exec_resume(self) -> None:
        proc = self.run_cli(args=self._args(resume="thread-old"), codex_mode="ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        captured = self.codex_capture_data()
        self.assertEqual(
            captured["argv"],
            ["exec", "resume", "--json", "thread-old", "-"],
        )
        self.assertEqual(captured["stdin"], self.task)
        self.assertEqual(self.payload(proc)["session_id"], "thread-abc")

    def test_unrecognized_events_are_1(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="unrecognized")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("没有可识别的事件", self.payload(proc)["summary"])

    def test_unparsable_output_is_1(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="badjson")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["session_id"])
        self.assertIn("无法解析", payload["summary"])

    def test_turn_failed_is_1(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="turn_failed")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIn("codex 失败", payload["summary"])

    def test_thread_only_is_not_success(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="thread_only")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("agent_message", payload["summary"])

    def test_turn_only_is_not_success(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="turn_only")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIn("agent_message", payload["summary"])

    def test_error_without_message_is_not_success(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="error_no_message")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIn("错误信息", payload["summary"])

    def test_nonzero_with_agent_message_prefers_exit_and_stderr(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="nonzero_with_agent")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIn("退出码 7", payload["summary"])
        self.assertIn("boom-codex", payload["summary"])
        self.assertIn("看起来完成了", payload["summary"])

    def test_readonly_resume_is_rejected(self) -> None:
        args = self._args(mode="readonly", resume="thread-old")
        proc = self.run_cli(args=args, codex_mode="ok")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertIn("resume", payload["summary"])
        self.assertFalse(self.codex_capture.exists())

    def test_nonzero_exit_is_1(self) -> None:
        proc = self.run_cli(args=self._args(), codex_mode="nonzero")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("boom", self.payload(proc)["summary"])

    def test_missing_binary_is_4_without_starting(self) -> None:
        empty_bin = self.tmp / "empty-bin"
        empty_bin.mkdir()
        proc = self.run_cli(args=self._args(), path=str(empty_bin))
        self.assertEqual(proc.returncode, 4, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "codex")
        self.assertEqual(payload["exit_code"], 4)
        self.assertIn("找不到 codex", payload["summary"])
        self.assertFalse(self.codex_capture.exists())


class HermesInProcessTests(FakeClaudeHarness):
    def test_cli_returns_4_and_explains_in_process(self) -> None:
        args = [
            "--executor",
            "hermes",
            "--cwd",
            str(self.cwd),
            "--budget-minutes",
            "30",
        ]
        proc = self.run_cli(args=args)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        payload = self.payload(proc)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["executor"], "hermes")
        self.assertEqual(payload["exit_code"], 4)
        self.assertIn("worker 进程内运行", payload["summary"])
        self.assertIn("不支持 CLI 调用", payload["summary"])
        # Refused before import/exec: no fake binary was started.
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.dsh_capture.exists())
        self.assertFalse(self.codex_capture.exists())


class RunnerUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import runpy

        cls.runner = runpy.run_path(str(CLI))

    def test_non_bool_ok_is_rejected(self) -> None:
        payload = self.runner["_normalise_payload"](
            "claude_code",
            {"ok": "false", "exit_code": 0, "summary": "looks fine"},
            ".runs/test.log",
            "",
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)

    def test_huge_cost_falls_back_to_null_and_zero(self) -> None:
        payload = self.runner["_normalise_payload"](
            "claude_code",
            {
                "ok": True,
                "exit_code": 0,
                "summary": "done",
                "cost": {"usd": 10**400, "tokens": 10**400},
            },
            ".runs/test.log",
            "",
        )
        self.assertEqual(payload["cost"], {"usd": None, "tokens": 0})


class RegistryTests(unittest.TestCase):
    def test_registry_declares_claude_code(self) -> None:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        entry = data["executors"]["claude_code"]
        self.assertEqual(entry["module"], "executors.claude_code")
        self.assertEqual(entry["mode"], "cli")
        self.assertEqual(entry["binary"], "claude")

    def test_registry_declares_dsh_codex_and_hermes(self) -> None:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        executors = data["executors"]

        self.assertEqual(executors["dsh"]["module"], "executors.dsh")
        self.assertEqual(executors["dsh"]["mode"], "cli")
        self.assertEqual(executors["dsh"]["binary"], "dsh")

        self.assertEqual(executors["codex"]["module"], "executors.codex")
        self.assertEqual(executors["codex"]["mode"], "cli")
        self.assertEqual(executors["codex"]["binary"], "codex")

        self.assertEqual(executors["hermes"]["module"], "executors.hermes")
        self.assertEqual(executors["hermes"]["mode"], "in-process")
        self.assertNotIn("binary", executors["hermes"])

    def test_string_entry_form_is_accepted(self) -> None:
        import runpy

        module = runpy.run_path(str(CLI))
        entry = module["resolve_entry"]({"claude_code": "executors.claude_code"}, "claude_code")
        self.assertEqual(entry["module"], "executors.claude_code")
        self.assertEqual(entry["mode"], "cli")

    def test_in_process_entry_is_unavailable(self) -> None:
        import runpy

        module = runpy.run_path(str(CLI))
        with self.assertRaises(module["RegistryError"]):
            module["resolve_entry"](
                {"hermes": {"module": "executors.hermes", "mode": "in-process"}},
                "hermes",
            )


class DependencyBoundaryTests(unittest.TestCase):
    def test_executor_code_imports_no_hermes_or_kanban_modules(self) -> None:
        import ast

        for path in (
            ROOT / "bin" / "executor-run",
            ROOT / "bin" / "route",
            ROOT / "bin" / "executors" / "claude_code.py",
            ROOT / "bin" / "executors" / "dsh.py",
            ROOT / "bin" / "executors" / "codex.py",
            ROOT / "bin" / "executors" / "hermes.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    lowered = module.lower()
                    self.assertNotIn("hermes", lowered, f"{path} imports {module}")
                    self.assertNotIn("kanban", lowered, f"{path} imports {module}")


class ClaudeCodeAdapterTests(unittest.TestCase):
    def test_build_command_write(self) -> None:
        command = claude_code.build_command(
            {"mode": "write", "max_usd": 2, "resume": None, "binary": "claude"}
        )
        self.assertEqual(command[:6], [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
        ])
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2")
        self.assertNotIn("--resume", command)
        self.assertEqual(
            command[command.index("--disallowedTools") + 1 :],
            ["Bash(git commit *)", "Bash(git push *)"],
        )

    def test_build_command_omits_budget_when_max_usd_missing(self) -> None:
        command = claude_code.build_command({"mode": "write"})
        self.assertNotIn("--max-budget-usd", command)
        self.assertIn("--disallowedTools", command)

    def test_build_command_readonly_and_resume(self) -> None:
        command = claude_code.build_command(
            {"mode": "readonly", "max_usd": 1.5, "resume": "sess-9"}
        )
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "1.5")
        self.assertEqual(command[command.index("--resume") + 1], "sess-9")

    def test_build_command_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            claude_code.build_command({"mode": "yolo"})

    def test_translate_success_maps_fields(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "session_id": "s1",
                "total_cost_usd": 0.25,
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                },
                "permission_denials": [],
            }
        )
        result = claude_code.translate_result(stdout, "", 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["summary"], "done")
        self.assertEqual(result["cost"], {"usd": 0.25, "tokens": 10})
        self.assertEqual(result["files_changed"], [])
        self.assertEqual(result["exit_code"], 0)

    def test_translate_uses_last_non_empty_line(self) -> None:
        stdout = '{"type":"result","subtype":"success","is_error":false,"result":"old","session_id":"a"}\n' \
                 '{"type":"result","subtype":"success","is_error":false,"result":"new","session_id":"b"}\n\n'
        result = claude_code.translate_result(stdout, "", 0)
        self.assertEqual(result["summary"], "new")
        self.assertEqual(result["session_id"], "b")

    def test_translate_permission_denials_is_2(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "blocked",
                "session_id": "s2",
                "permission_denials": [{"tool_name": "Bash"}],
            }
        )
        result = claude_code.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 2)

    def test_translate_permission_denials_without_is_error_is_2(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "finished",
                "permission_denials": [{"tool_name": "Bash"}],
            }
        )
        result = claude_code.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 2)

    def test_translate_permission_text_is_2(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": (
                    "Claude requested permissions to use Bash, "
                    "but you haven't granted it yet."
                ),
            }
        )
        result = claude_code.translate_result(stdout, "", 1)
        self.assertEqual(result["exit_code"], 2)

    def test_translate_budget_subtype_is_3(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "result": "stopped",
            }
        )
        result = claude_code.translate_result(stdout, "", 1)
        self.assertEqual(result["exit_code"], 3)

    def test_translate_budget_text_is_3(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "Exceeded USD budget (2)",
            }
        )
        result = claude_code.translate_result(stdout, "", 1)
        self.assertEqual(result["exit_code"], 3)

    def test_translate_error_subtype_without_is_error_is_1(self) -> None:
        stdout = json.dumps(
            {"type": "result", "subtype": "error_max_turns", "result": "turn limit reached"}
        )
        result = claude_code.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)

    def test_translate_nonzero_is_1(self) -> None:
        stdout = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "looks fine"}
        )
        result = claude_code.translate_result(stdout, "boom", 7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("退出码 7", result["summary"])

    def test_translate_unparsable_is_1(self) -> None:
        result = claude_code.translate_result("not json\n", "oops", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("无法解析", result["summary"])
        self.assertIsNone(result["session_id"])

    def test_translate_missing_session_and_usage(self) -> None:
        result = claude_code.translate_result(
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}),
            "",
            0,
        )
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["cost"], {"usd": None, "tokens": 0})

    def test_translate_non_result_messages_are_1(self) -> None:
        for payload in ({}, {"type": "system", "subtype": "init"}, {"type": "assistant"}):
            with self.subTest(payload=payload):
                result = claude_code.translate_result(json.dumps(payload), "", 0)
                self.assertFalse(result["ok"])
                self.assertEqual(result["exit_code"], 1)
                self.assertIn("不是 result 消息", result["summary"])

    def test_translate_success_budget_text_stays_0(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Added a budget limit",
                "session_id": "s1",
            }
        )
        result = claude_code.translate_result(stdout, "", 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)

    def test_translate_success_permission_text_stays_0(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "permission required for docs",
                "session_id": "s1",
                "permission_denials": [],
            }
        )
        result = claude_code.translate_result(stdout, "", 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)

    def test_translate_stderr_budget_is_3(self) -> None:
        result = claude_code.translate_result("not json", "Exceeded USD budget (2)", 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("预算中断", result["summary"])

    def test_translate_stderr_permission_is_2(self) -> None:
        result = claude_code.translate_result("not json", "permission denied: Bash", 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("人工授权", result["summary"])

    def test_translate_huge_cost_falls_back(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "total_cost_usd": 10**400,
                "usage": {"input_tokens": 10**400, "output_tokens": 10**400},
            }
        )
        result = claude_code.translate_result(stdout, "", 0)
        self.assertEqual(result["cost"], {"usd": None, "tokens": 0})


class DshAdapterTests(unittest.TestCase):
    def test_build_command_default(self) -> None:
        command = dsh.build_command({"binary": "dsh"})
        self.assertEqual(command, ["dsh", "--profile", "headless", "--json"])

    def test_build_command_resume_maps_to_session_id(self) -> None:
        command = dsh.build_command({"resume": "session-9"})
        self.assertEqual(
            command,
            ["dsh", "--profile", "headless", "--json", "--session-id", "session-9"],
        )

    def test_build_command_rejects_readonly(self) -> None:
        with self.assertRaises(ValueError) as caught:
            dsh.build_command({"mode": "readonly"})
        self.assertIn("只读", str(caught.exception))

    def test_translate_success_maps_events(self) -> None:
        stdout = "\n".join(
            [
                'diagnostic before json',
                json.dumps({"type": "session", "sessionId": "session-1"}),
                json.dumps(
                    {
                        "type": "status",
                        "phase": "step_end",
                        "usage": {"inputTokens": 10, "outputTokens": 20},
                    }
                ),
                json.dumps({"type": "text", "text": "middle"}),
                json.dumps(
                    {
                        "type": "status",
                        "phase": "step_end",
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    }
                ),
                json.dumps({"type": "final", "text": "done"}),
                "diagnostic after json",
            ]
        )
        result = dsh.translate_result(stdout, "", 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "session-1")
        self.assertEqual(result["summary"], "done")
        self.assertEqual(result["cost"], {"usd": None, "tokens": 33})
        self.assertEqual(result["files_changed"], [])
        self.assertEqual(result["exit_code"], 0)

    def test_translate_missing_session_is_null(self) -> None:
        stdout = json.dumps({"type": "final", "text": "ok"})
        result = dsh.translate_result(stdout, "", 0)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["cost"], {"usd": None, "tokens": 0})

    def test_translate_missing_final_is_1(self) -> None:
        stdout = json.dumps({"type": "session", "sessionId": "session-x"})
        result = dsh.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["session_id"], "session-x")
        self.assertIn("final", result["summary"])

    def test_translate_error_event_is_1(self) -> None:
        stdout = json.dumps({"type": "error", "message": "boom"})
        result = dsh.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["summary"], "boom")

    def test_translate_error_without_message_is_1(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "error"}),
                json.dumps({"type": "final", "text": "looks fine"}),
            ]
        )
        result = dsh.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("错误信息", result["summary"])
        self.assertIn("looks fine", result["summary"])

    def test_translate_nonzero_exit_is_1(self) -> None:
        stdout = json.dumps({"type": "final", "text": "looks fine"})
        result = dsh.translate_result(stdout, "oops", 7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("退出码 7", result["summary"])
        self.assertIn("oops", result["summary"])
        self.assertIn("looks fine", result["summary"])

    def test_translate_unparsable_is_1(self) -> None:
        result = dsh.translate_result("not json\n", "oops", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("无法解析", result["summary"])
        self.assertIsNone(result["session_id"])

    def test_translate_huge_tokens_fall_back_to_zero(self) -> None:
        stdout = json.dumps(
            {
                "type": "status",
                "phase": "step_end",
                "usage": {"inputTokens": 10**400, "outputTokens": 10**400},
            }
        )
        result = dsh.translate_result(stdout, "", 0)
        self.assertEqual(result["cost"], {"usd": None, "tokens": 0})


class CodexAdapterTests(unittest.TestCase):
    def test_build_command_write_and_readonly(self) -> None:
        write = codex.build_command({"binary": "codex", "mode": "write"})
        readonly = codex.build_command({"mode": "readonly"})
        self.assertEqual(
            write,
            ["codex", "exec", "--json", "--sandbox", "workspace-write", "-"],
        )
        self.assertEqual(
            readonly,
            ["codex", "exec", "--json", "--sandbox", "read-only", "-"],
        )

    def test_build_command_resume(self) -> None:
        command = codex.build_command({"mode": "write", "resume": "thread-9"})
        self.assertEqual(
            command,
            ["codex", "exec", "resume", "--json", "thread-9", "-"],
        )

    def test_build_command_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            codex.build_command({"mode": "yolo"})

    def test_build_command_rejects_readonly_resume(self) -> None:
        with self.assertRaises(ValueError) as caught:
            codex.build_command({"mode": "readonly", "resume": "thread-9"})
        self.assertIn("resume", str(caught.exception))

    def test_translate_success_uses_last_agent_message(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "first"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "reasoning", "text": "ignored"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "last"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "output_tokens": 20},
                    }
                ),
            ]
        )
        result = codex.translate_result(stdout, "", 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "thread-1")
        self.assertEqual(result["summary"], "last")
        self.assertEqual(result["cost"], {"usd": None, "tokens": 30})
        self.assertEqual(result["exit_code"], 0)

    def test_translate_without_thread_still_ok_but_null_session(self) -> None:
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            }
        )
        result = codex.translate_result(stdout, "", 0)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["summary"], "done")

    def test_translate_unrecognized_events_is_1(self) -> None:
        stdout = json.dumps(
            {
                "type": "item.started",
                "item": {"type": "agent_message", "text": "ignored"},
            }
        )
        result = codex.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("没有可识别的事件", result["summary"])

    def test_translate_turn_failed_is_1(self) -> None:
        stdout = json.dumps(
            {"type": "turn.failed", "error": {"message": "codex exploded"}}
        )
        result = codex.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["summary"], "codex exploded")

    def test_translate_thread_only_is_1(self) -> None:
        stdout = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
        result = codex.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("agent_message", result["summary"])

    def test_translate_turn_only_is_1(self) -> None:
        stdout = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
        result = codex.translate_result(stdout, "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("agent_message", result["summary"])

    def test_translate_error_without_message_is_1(self) -> None:
        result = codex.translate_result(json.dumps({"type": "error"}), "", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("错误信息", result["summary"])

    def test_translate_nonzero_exit_is_1(self) -> None:
        stdout = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
        result = codex.translate_result(stdout, "oops", 7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("退出码 7", result["summary"])
        self.assertIn("oops", result["summary"])

    def test_translate_unparsable_is_1(self) -> None:
        result = codex.translate_result("not json\n", "oops", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("无法解析", result["summary"])

    def test_translate_huge_tokens_fall_back_to_zero(self) -> None:
        stdout = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10**400,
                    "output_tokens": 10**400,
                },
            }
        )
        result = codex.translate_result(stdout, "", 0)
        self.assertEqual(result["cost"], {"usd": None, "tokens": 0})


class HermesAdapterTests(unittest.TestCase):
    def test_build_command_refuses(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            hermes.build_command({})
        self.assertIn("worker 进程内运行", str(caught.exception))

    def test_translate_result_refuses(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            hermes.translate_result("{}", "", 0)
        self.assertIn("worker 进程内运行", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
