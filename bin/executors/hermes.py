#!/usr/bin/env python3
"""``hermes`` executor marker: deliberately *not* a CLI adapter.

Hermes runs inside the worker process (built-in tools), so the registry marks
it ``"mode": "in-process"``. ``bin/executor-run`` refuses that mode before it
ever imports this module; the functions below exist only as a second line of
defence against a misconfigured registry entry and always refuse to build a
subprocess command.

Do not add subprocess logic here: a Hermes "executor" that shells out would
double-run the worker and break the session lineage.
"""

from __future__ import annotations

_REFUSAL = "hermes 在 worker 进程内运行，不是 CLI 执行器；请使用 worker 内置工具"


def build_command(ctx):
    """Always refuse: hermes is not a CLI executor."""
    raise RuntimeError(_REFUSAL)


def translate_result(stdout, stderr="", raw_exit=0):
    """Always refuse: hermes is not a CLI executor."""
    raise RuntimeError(_REFUSAL)
