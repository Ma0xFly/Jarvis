#!/usr/bin/env python3
"""Shared failure-text classification for CLI executor adapters.

These are heuristics, not authoritative signals: adapters must prefer
structured output (``permission_denials`` / ``subtype`` / error events) when
it exists. Callers only apply them to runs that already failed, so a
successful result that merely mentions "budget" or "permission" keeps exit
code 0.
"""

from __future__ import annotations

import re

EXIT_NEEDS_HUMAN = 2
EXIT_BUDGET = 3

_BUDGET_TEXT_RE = re.compile(
    r"budget[^.\n]{0,80}(exceed|exhaust|limit|reache|interrupt|stop|spent)"
    r"|(exceed|exhaust|hit|reache)[^.\n]{0,80}budget",
    re.IGNORECASE,
)

_PERMISSION_TEXT_RE = re.compile(
    r"permission[^.\n]{0,80}(denied|denial|not granted|haven'?t granted"
    r"|has not been granted|required|requested|needs? (approval|grant))"
    r"|(not|no)[^.\n]{0,20}(allowed|permitted)"
    r"|tool[^.\n]{0,20}use[^.\n]{0,20}denied",
    re.IGNORECASE,
)

_PERMISSION_TEXT_ZH_RE = re.compile(
    r"(权限|授权)[^。\n]{0,30}(拒绝|不足|需要|申请)"
    r"|需要[^。\n]{0,30}(权限|授权)"
)

#: Signatures of a CLI/runtime that never started: Node/Python loader errors,
#: missing shared libraries, shell "command not found" and bad interpreters.
_STARTUP_FAILURE_RE = re.compile(
    r"MODULE_NOT_FOUND"
    r"|Cannot find (?:module|package)"
    r"|ERR_MODULE_NOT_FOUND"
    r"|ModuleNotFoundError"
    r"|No module named"
    r"|error while loading shared libraries"
    r"|Library not loaded"
    r"|command not found"
    r"|: not found\b"
    r"|ENOENT[^\n]{0,120}(?:spawn|exec)"
    r"|bad interpreter",
    re.IGNORECASE,
)


def looks_like_budget(subtype, text) -> bool:
    """Heuristic budget-interruption check; ``subtype`` is authoritative."""
    if "budget" in (subtype or "").lower():
        return True
    if not isinstance(text, str):
        return False
    if "预算" in text and any(
        word in text for word in ("超", "中断", "用尽", "上限", "不足")
    ):
        return True
    return bool(_BUDGET_TEXT_RE.search(text))


def looks_like_permission_text(text) -> bool:
    """Heuristic permission-denial check on free text (EN + ZH)."""
    if not isinstance(text, str) or not text:
        return False
    return bool(_PERMISSION_TEXT_RE.search(text) or _PERMISSION_TEXT_ZH_RE.search(text))


def classify_failure(*texts):
    """Return ``EXIT_BUDGET`` / ``EXIT_NEEDS_HUMAN`` for failed output, else None.

    Budget wins over permission when both appear. Callers must only pass text
    from runs that already failed.
    """
    candidates = [
        text for text in texts if isinstance(text, str) and text
    ]
    for text in candidates:
        if looks_like_budget("", text):
            return EXIT_BUDGET
    for text in candidates:
        if looks_like_permission_text(text):
            return EXIT_NEEDS_HUMAN
    return None


def looks_like_startup_failure(text) -> bool:
    """True when the text is a loader/startup failure (executor never ran)."""
    if not isinstance(text, str) or not text:
        return False
    return bool(_STARTUP_FAILURE_RE.search(text))


def startup_failure_line(*texts) -> str:
    """Return the first line that looks like a loader/startup failure."""
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and looks_like_startup_failure(line):
                return line
    return ""