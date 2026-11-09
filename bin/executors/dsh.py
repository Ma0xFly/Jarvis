#!/usr/bin/env python3
"""``dsh`` executor adapter.

Pure translation between the unified executor contract and the dsh headless
CLI (``dsh --profile headless --json``). This module deliberately imports
nothing from Hermes, never touches ``kanban.db``, and never logs (the runner
owns logging).

Contract
--------
``build_command(ctx)``
    Map the unified context to ``dsh --profile headless --json`` plus
    ``--session-id <resume>``. The task book is not part of the command; dsh
    reads it from stdin when no task argument is given.

``translate_result(stdout, stderr, raw_exit)``
    Parse the NDJSON event stream:

    * the first ``session`` event supplies ``sessionId`` -> ``session_id``
      (missing event -> ``null``, never fabricated);
    * ``status`` events with ``phase == "step_end"`` accumulate
      ``usage.inputTokens + usage.outputTokens`` into ``cost.tokens``;
    * the last ``final`` event supplies ``text`` -> ``summary``.

    Output without any parsable JSON event, or without a ``final`` event on a
    zero exit, maps to exit code 1. Non-zero exits map to 1 as well; the
    runner owns codes 3 (timeout) and 4 (missing binary / unknown executor).
"""

from __future__ import annotations

import json
import math

#: Usage keys accepted on ``status.usage`` (camelCase is the dsh spelling).
_USAGE_TOKEN_KEYS = (
    "inputTokens",
    "outputTokens",
    "input_tokens",
    "output_tokens",
)


def build_command(ctx):
    """Return the ``dsh`` argv for the unified context.

    ``ctx`` keys used here: ``binary`` and ``resume``.
    """
    ctx = ctx or {}
    binary = str(ctx.get("binary") or "dsh")
    mode = str(ctx.get("mode") or "write")
    if mode != "write":
        raise ValueError(f"dsh headless 没有只读沙箱，不支持 mode={mode!r}")

    command = [binary, "--profile", "headless", "--json"]

    resume = ctx.get("resume")
    if resume:
        command.extend(["--session-id", str(resume)])

    return command


def translate_result(stdout, stderr="", raw_exit=0):
    """Translate dsh NDJSON events into the unified result dict."""
    events = _parse_events(stdout)
    if not events:
        return _result(
            ok=False,
            session_id=None,
            summary=_malformed_summary(raw_exit, stderr),
            cost=_empty_cost(),
            exit_code=1,
        )

    session_id = None
    final_text = None
    error_text = ""
    failure_event = False
    tokens = 0
    recognized = False

    for event in events:
        event_type = event.get("type")
        if event_type == "session":
            recognized = True
            if session_id is None:
                value = event.get("sessionId", event.get("session_id"))
                if isinstance(value, str) and value.strip():
                    session_id = value.strip()
        elif event_type == "status":
            recognized = True
            if event.get("phase") == "step_end":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    tokens += _usage_tokens(usage)
        elif event_type == "final":
            recognized = True
            text = event.get("text")
            if isinstance(text, str):
                final_text = text
        elif event_type in ("error", "fatal"):
            recognized = True
            failure_event = True
            message = event.get("message")
            if not isinstance(message, str) or not message.strip():
                message = event.get("text")
            if isinstance(message, str) and message.strip():
                error_text = message.strip()

    cost = {"usd": None, "tokens": max(0, tokens)}

    if not recognized:
        return _result(
            ok=False,
            session_id=session_id,
            summary="执行器输出没有可识别的事件：" + _first_line(stderr),
            cost=cost,
            exit_code=1,
        )

    if raw_exit != 0:
        return _result(
            ok=False,
            session_id=session_id,
            summary=_nonzero_summary(raw_exit, stderr, error_text, final_text),
            cost=cost,
            exit_code=1,
        )

    # An error/fatal event is authoritative even when the process exits 0.
    if failure_event:
        summary = error_text or "执行器报告失败（error/fatal），但没有错误信息"
        if final_text:
            summary += f"（final 输出：{final_text[:300]}）"
        return _result(
            ok=False,
            session_id=session_id,
            summary=summary,
            cost=cost,
            exit_code=1,
        )

    if final_text is None:
        return _result(
            ok=False,
            session_id=session_id,
            summary="执行器输出缺少 final 事件",
            cost=cost,
            exit_code=1,
        )

    return _result(
        ok=True,
        session_id=session_id,
        summary=final_text or "执行完成",
        cost=cost,
        exit_code=0,
    )


def _parse_events(stdout):
    """Return the dicts on stdout's non-empty JSON lines (other lines skipped)."""
    if not isinstance(stdout, str):
        return []
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _usage_tokens(usage):
    total = 0
    for key in _USAGE_TOKEN_KEYS:
        number = _finite_int(usage.get(key))
        if number is not None:
            total += number
    return total


def _finite_int(value):
    """Return a non-negative finite int, or ``None`` for bool/garbage/overflow."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    try:
        if not math.isfinite(number) or number < 0:
            return None
    except (OverflowError, TypeError):
        return None
    return int(number)


def _nonzero_summary(raw_exit, stderr, error_text, final_text):
    summary = f"执行器退出码 {raw_exit}"
    details = []
    first_line = _first_line(stderr)
    if first_line:
        details.append(first_line[:300])
    if error_text:
        details.append(f"错误事件：{error_text[:300]}")
    if final_text:
        details.append(f"final 输出：{final_text[:300]}")
    if details:
        summary += "：" + "；".join(details)
    return summary


def _malformed_summary(raw_exit, stderr):
    summary = "执行器输出无法解析：没有 JSON 事件行"
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


def _empty_cost():
    return {"usd": None, "tokens": 0}


def _result(*, ok, session_id, summary, cost, exit_code):
    return {
        "ok": ok is True,
        "session_id": session_id,
        "summary": str(summary),
        "files_changed": [],
        "cost": cost,
        "exit_code": int(exit_code),
    }
