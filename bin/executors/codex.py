#!/usr/bin/env python3
"""``codex`` executor adapter.

Pure translation between the unified executor contract and the Codex CLI
(``codex exec --json``). This module deliberately imports nothing from Hermes,
never touches ``kanban.db``, and never logs (the runner owns logging).

Contract
--------
``build_command(ctx)``
    Map the unified context to::

        codex exec --json --sandbox read-only|workspace-write -
        codex exec resume --json <session-id> -

    ``-`` makes Codex read the task book from stdin. ``--sandbox`` is only
    available on the non-resume form (``codex exec resume`` has no sandbox
    flag in the installed version).

``translate_result(stdout, stderr, raw_exit)``
    Parse the JSONL event stream:

    * ``thread.started`` -> ``thread_id`` becomes ``session_id``;
    * the last ``item.completed`` with ``item.type == "agent_message"``
      supplies ``item.text`` -> ``summary``;
    * ``turn.completed.usage`` adds ``input_tokens + output_tokens`` to
      ``cost.tokens``.

    Output without recognized events maps to exit code 1. A process that dies
    before any parsable event with a loader/startup signature
    (``MODULE_NOT_FOUND``, a missing shared library, a bad interpreter, ...)
    maps to exit code 4 instead. On a failed run, budget/permission text maps
    to 3/2 as a heuristic; structured failure events are preferred when
    present. The runner owns the wall-clock timeout (3) and the missing-binary
    check (4).

Capabilities and limits
-----------------------
* **No per-command commit/push guard.** Codex is invoked with
  ``--sandbox workspace-write`` (``readonly`` -> ``read-only``) but has no
  denylist flag here, so ``git commit`` / ``git push`` are not blocked in
  write mode. The sandbox is the only containment; callers must not treat
  this adapter as a commit/push guard.
* **No USD budget.** Codex exposes no ``--max-usd`` equivalent here, so the
  runner's ``--max-usd`` is not forwarded and ``cost.usd`` stays ``null``.
  The runner still enforces ``--budget-minutes`` as a wall-clock timeout.
* **Tokens only.** Usage is summed from ``turn.completed`` events.
"""

from __future__ import annotations

import json
import math

from executors.text_signals import (
    EXIT_BUDGET,
    classify_failure,
    looks_like_startup_failure,
    startup_failure_line,
)

#: Unified mode -> Codex sandbox mode.
SANDBOX_MODES = {
    "write": "workspace-write",
    "readonly": "read-only",
}

_USAGE_TOKEN_KEYS = ("input_tokens", "output_tokens")


def build_command(ctx):
    """Return the ``codex`` argv for the unified context.

    ``ctx`` keys used here: ``binary``, ``mode`` and ``resume``.
    """
    ctx = ctx or {}
    binary = str(ctx.get("binary") or "codex")
    mode = str(ctx.get("mode") or "write")
    sandbox = SANDBOX_MODES.get(mode)
    if sandbox is None:
        raise ValueError(f"unsupported mode: {mode!r}")

    resume = ctx.get("resume")
    if resume:
        if mode == "readonly":
            raise ValueError(
                "codex exec resume 不能设置 --sandbox，拒绝 --mode readonly + --resume"
            )
        return [binary, "exec", "resume", "--json", str(resume), "-"]

    return [binary, "exec", "--json", "--sandbox", sandbox, "-"]


def translate_result(stdout, stderr="", raw_exit=0):
    """Translate codex JSONL events into the unified result dict."""
    events = _parse_events(stdout)
    if not events:
        if looks_like_startup_failure(_combined_output(stdout, stderr)):
            return _result(
                ok=False,
                session_id=None,
                summary=_startup_summary(stderr, stdout),
                cost=_empty_cost(),
                exit_code=4,
            )
        signal = classify_failure(stderr, stdout)
        if signal is not None:
            return _result(
                ok=False,
                session_id=None,
                summary=_signal_summary(signal, stderr, stdout),
                cost=_empty_cost(),
                exit_code=signal,
            )
        return _result(
            ok=False,
            session_id=None,
            summary=_malformed_summary(raw_exit, stderr),
            cost=_empty_cost(),
            exit_code=1,
        )

    session_id = None
    last_message = None
    error_text = ""
    failure_event = False
    tokens = 0
    recognized = False

    for event in events:
        event_type = event.get("type")
        if event_type == "thread.started":
            recognized = True
            if session_id is None:
                value = event.get("thread_id", event.get("threadId"))
                if isinstance(value, str) and value.strip():
                    session_id = value.strip()
        elif event_type == "item.completed":
            recognized = True
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    last_message = text
        elif event_type == "turn.completed":
            recognized = True
            usage = event.get("usage")
            if isinstance(usage, dict):
                tokens += _usage_tokens(usage)
        elif event_type in ("turn.failed", "error"):
            recognized = True
            failure_event = True
            message = event.get("message")
            if not isinstance(message, str) or not message.strip():
                error = event.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
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
        signal = classify_failure(stderr, error_text, last_message)
        if signal is not None:
            return _result(
                ok=False,
                session_id=session_id,
                summary=_signal_summary(signal, stderr, error_text or last_message),
                cost=cost,
                exit_code=signal,
            )
        return _result(
            ok=False,
            session_id=session_id,
            summary=_nonzero_summary(raw_exit, stderr, error_text, last_message),
            cost=cost,
            exit_code=1,
        )

    # A failure event is authoritative even when the process exits 0.
    if failure_event:
        signal = classify_failure(error_text, last_message, stderr)
        if signal is not None:
            return _result(
                ok=False,
                session_id=session_id,
                summary=_signal_summary(
                    signal, error_text, last_message or stderr
                ),
                cost=cost,
                exit_code=signal,
            )
        summary = error_text or "执行器报告失败（turn.failed/error），但没有错误信息"
        if last_message:
            summary += f"（agent 输出：{last_message[:300]}）"
        return _result(
            ok=False,
            session_id=session_id,
            summary=summary,
            cost=cost,
            exit_code=1,
        )

    # A session start / turn end alone does not prove the agent answered.
    if last_message is None:
        return _result(
            ok=False,
            session_id=session_id,
            summary="执行器输出缺少 item.completed(agent_message) 事件",
            cost=cost,
            exit_code=1,
        )

    return _result(
        ok=True,
        session_id=session_id,
        summary=last_message or "执行完成",
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


def _nonzero_summary(raw_exit, stderr, error_text, last_message):
    summary = f"执行器退出码 {raw_exit}"
    details = []
    first_line = _first_line(stderr)
    if first_line:
        details.append(first_line[:300])
    if error_text:
        details.append(f"错误事件：{error_text[:300]}")
    if last_message:
        details.append(f"agent 输出：{last_message[:300]}")
    if details:
        summary += "：" + "；".join(details)
    return summary


def _combined_output(stdout, stderr):
    parts = [
        part for part in (stdout, stderr) if isinstance(part, str) and part
    ]
    return "\n".join(parts)


def _startup_summary(stderr, stdout):
    summary = "执行器不可用：启动失败"
    detail = (
        startup_failure_line(stderr, stdout)
        or _first_line(stderr)
        or _first_line(stdout)
    )
    if detail:
        summary += f"：{detail[:300]}"
    return summary


def _signal_summary(exit_code, primary, fallback=""):
    if exit_code == EXIT_BUDGET:
        summary = "执行器预算中断"
    else:
        summary = "执行器需要人工授权"
    detail = _first_line(primary) or _first_line(fallback)
    if detail:
        summary += f"：{detail[:300]}"
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
