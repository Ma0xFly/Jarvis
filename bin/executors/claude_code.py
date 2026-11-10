#!/usr/bin/env python3
"""``claude_code`` executor adapter.

Pure translation between the unified executor contract and the Claude Code
CLI. This module deliberately imports nothing from Hermes, never touches
``kanban.db``, and never logs (the runner owns logging).

Contract
--------
``build_command(ctx)``
    Map the unified context to ``claude -p --output-format json`` plus
    ``--permission-mode`` / ``--max-budget-usd`` / ``--resume``.

``translate_result(stdout, stderr, raw_exit)``
    Read the **last non-empty stdout line** as Claude's JSON result and map
    it to the unified result dict with one of the five contract exit codes:
    0 success, 1 other error, 2 needs human, 3 over budget.

The command also denies ``git commit`` / ``git push`` best-effort. This is a
guardrail, not a sandbox: a determined executor can still commit or push by
other means, so callers must not treat it as containment.
"""

from __future__ import annotations

import json
import math

from executors.text_signals import looks_like_budget, looks_like_permission_text

#: Unified mode -> Claude Code permission mode.
PERMISSION_MODES = {
    "write": "acceptEdits",
    "readonly": "plan",
}

_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def build_command(ctx):
    """Return the ``claude`` argv for the unified context.

    ``ctx`` keys used here: ``binary``, ``mode``, ``max_usd``, ``resume``.
    The task book is *not* part of the command; the runner sends it on stdin.
    """
    ctx = ctx or {}
    binary = str(ctx.get("binary") or "claude")
    mode = str(ctx.get("mode") or "write")
    permission_mode = PERMISSION_MODES.get(mode)
    if permission_mode is None:
        raise ValueError(f"unsupported mode: {mode!r}")

    command = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        permission_mode,
    ]

    max_usd = ctx.get("max_usd")
    if max_usd is not None:
        command.extend(["--max-budget-usd", _format_amount(max_usd)])

    resume = ctx.get("resume")
    if resume:
        command.extend(["--resume", str(resume)])

    # Best-effort guardrail: the executor must not commit or push. Keep these
    # last because --disallowedTools is variadic.
    command.extend(
        [
            "--disallowedTools",
            "Bash(git commit *)",
            "Bash(git push *)",
        ]
    )

    return command


def translate_result(stdout, stderr="", raw_exit=0):
    """Translate Claude's JSON result into the unified result dict.

    The unified dict carries ``ok`` / ``session_id`` / ``summary`` /
    ``files_changed`` / ``cost`` / ``exit_code``. The runner adds
    ``executor`` and ``log_path``.
    """
    data = _parse_last_json(stdout)
    if data is None:
        return _translate_malformed(stderr, raw_exit)

    if data.get("type") != "result":
        message_type = data.get("type")
        label = message_type if isinstance(message_type, str) and message_type else "missing"
        return _result(
            ok=False,
            session_id=None,
            summary=f"执行器输出不是 result 消息（type={label}）",
            cost=_empty_cost(),
            exit_code=1,
        )

    result_text = data.get("result")
    if not isinstance(result_text, str):
        result_text = ""
    if not result_text:
        error_text = data.get("error")
        if isinstance(error_text, str):
            result_text = error_text

    session_id = data.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        session_id = str(session_id)
    if not session_id:
        session_id = None

    subtype = data.get("subtype")
    if not isinstance(subtype, str):
        subtype = ""

    cost = _cost(data)
    failed = (
        raw_exit != 0
        or data.get("is_error") is True
        or subtype.lower().startswith("error")
    )

    # Free-text heuristics only apply to failed runs. A successful result that
    # merely mentions "budget" or "permission" must not change the exit code.
    if failed and looks_like_budget(subtype, result_text):
        return _result(
            ok=False,
            session_id=session_id,
            summary=result_text or "执行器预算中断",
            cost=cost,
            exit_code=3,
        )

    # permission_denials is authoritative even when the result claims success.
    if data.get("permission_denials"):
        return _result(
            ok=False,
            session_id=session_id,
            summary=result_text or "执行器需要人工授权",
            cost=cost,
            exit_code=2,
        )

    if failed and looks_like_permission_text(result_text):
        return _result(
            ok=False,
            session_id=session_id,
            summary=result_text or "执行器需要人工授权",
            cost=cost,
            exit_code=2,
        )

    if raw_exit != 0:
        summary = _error_summary(raw_exit, stderr, subtype)
        if result_text:
            summary += f"：{result_text}"
        return _result(
            ok=False,
            session_id=session_id,
            summary=summary,
            cost=cost,
            exit_code=1,
        )

    if failed:
        summary = result_text or _error_summary(raw_exit, stderr, subtype)
        return _result(
            ok=False,
            session_id=session_id,
            summary=summary,
            cost=cost,
            exit_code=1,
        )

    return _result(
        ok=True,
        session_id=session_id,
        summary=result_text or "执行完成",
        cost=cost,
        exit_code=0,
    )


def _translate_malformed(stderr, raw_exit):
    """Classify an unparseable stdout line, including stderr signals."""
    first_line = _first_line(stderr)
    if looks_like_budget("", stderr):
        summary = "执行器输出无法解析，stderr 显示预算中断"
        if first_line:
            summary += f"：{first_line[:300]}"
        return _result(
            ok=False,
            session_id=None,
            summary=summary,
            cost=_empty_cost(),
            exit_code=3,
        )
    if looks_like_permission_text(stderr):
        summary = "执行器输出无法解析，stderr 显示需要人工授权"
        if first_line:
            summary += f"：{first_line[:300]}"
        return _result(
            ok=False,
            session_id=None,
            summary=summary,
            cost=_empty_cost(),
            exit_code=2,
        )
    return _result(
        ok=False,
        session_id=None,
        summary=_malformed_summary(raw_exit, stderr),
        cost=_empty_cost(),
        exit_code=1,
    )


def _format_amount(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid max_usd: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"invalid max_usd: {value!r}")
    return f"{number:g}"


def _parse_last_json(stdout):
    """Return the dict on the last non-empty stdout line, else ``None``."""
    if not isinstance(stdout, str):
        return None
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _finite_float(value):
    """Return a finite float, or ``None`` for bool/overflow/garbage."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cost(data):
    usage = data.get("usage")
    tokens = 0
    if isinstance(usage, dict):
        for key in _USAGE_TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                finite = math.isfinite(value)
            except (OverflowError, TypeError):
                finite = False
            if finite:
                tokens += int(value)

    usd = _finite_float(data.get("total_cost_usd"))
    return {"usd": usd, "tokens": max(0, tokens)}


def _empty_cost():
    return {"usd": None, "tokens": 0}


def _error_summary(raw_exit, stderr, subtype):
    detail = f"执行器退出码 {raw_exit}"
    if subtype:
        detail += f"，subtype={subtype}"
    first_line = _first_line(stderr)
    if first_line:
        detail += f"：{first_line[:300]}"
    return detail


def _malformed_summary(raw_exit, stderr):
    summary = "执行器输出无法解析：末行不是有效的 JSON"
    if raw_exit not in (0, None):
        summary += f"（退出码 {raw_exit}）"
    first_line = _first_line(stderr)
    if first_line:
        summary += f"：{first_line[:300]}"
    return summary


def _first_line(text):
    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _result(*, ok, session_id, summary, cost, exit_code):
    return {
        "ok": ok is True,
        "session_id": session_id,
        "summary": str(summary),
        "files_changed": [],
        "cost": cost,
        "exit_code": int(exit_code),
    }
