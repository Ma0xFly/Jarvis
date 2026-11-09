#!/usr/bin/env python3
"""Contract checks for the external coder profile (Story 3.5).

These files live outside the repository, so the test skips when they are not
present (CI / another machine). When present, the coder must resolve the
executor through ``bin/route``, run it through ``bin/executor-run`` and finish
with ``kanban_request_review`` — never with direct dsh dispatch or
``kanban_complete``.

Run with::

    python3 tests/test_worker_contract.py -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

CODER_DIR = Path.home() / ".hermes" / "profiles" / "coder"
SOUL = CODER_DIR / "SOUL.md"
DELEGATION_SKILL = CODER_DIR / "skills" / "dsh-delegation" / "SKILL.md"

REQUIRED_MARKERS = (
    "bin/route",
    "bin/executor-run",
    "kanban_request_review",
)
FORBIDDEN_MARKERS = (
    "dsh --profile headless",
    "kanban_complete",
)


class CoderWorkerContractTests(unittest.TestCase):
    def _read(self, path: Path) -> str:
        if not path.is_file():
            self.skipTest(f"external file not present: {path}")
        return path.read_text(encoding="utf-8")

    def _assert_contract(self, text: str) -> None:
        for marker in REQUIRED_MARKERS:
            self.assertIn(marker, text)
        for marker in FORBIDDEN_MARKERS:
            self.assertNotIn(marker, text)

    def test_soul_uses_route_executor_run_and_review(self) -> None:
        text = self._read(SOUL)
        self._assert_contract(text)
        self.assertIn("adapter_mode", text)
        self.assertIn("--source", text)
        self.assertIn("--title", text)
        self.assertIn("--type", text)
        self.assertIn("executor_session_id", text)
        self.assertIn("self_report", text)
        self.assertIn("solidified", text)

    def test_delegation_skill_uses_route_executor_run_and_review(self) -> None:
        text = self._read(DELEGATION_SKILL)
        self._assert_contract(text)
        self.assertIn("executor_session_id", text)
        self.assertIn("self_report", text)
        self.assertIn("solidified", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
