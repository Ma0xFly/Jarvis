#!/usr/bin/env python3
"""Contract checks for the external worker profiles (Stories 3.1/3.2/3.5).

The profiles live outside the repository, so the whole external tree is
optional (CI / another machine): when ``~/.hermes/profiles`` is absent the
module skips. Once the tree exists, every configured profile must carry the
review contract — a missing profile or a missing contract is a failure, not a
skip.

Run with::

    python3 tests/test_worker_contract.py -v
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

PROFILES_DIR = Path.home() / ".hermes" / "profiles"
PROFILES = ("coder", "researcher", "housekeeper", "skeptic")
SHARED_SKILL_DIR = (
    Path.home()
    / ".config"
    / "magpie"
    / "library"
    / "skills"
    / "workbench-conventions"
)
SHARED_SKILL = SHARED_SKILL_DIR / "SKILL.md"
ROUTING = Path(__file__).resolve().parents[1] / "routing.json"

SOUL_REQUIRED_MARKERS = (
    "bin/route",
    "kanban_request_review",
    "self_report",
    "solidified",
    "executor",
    "executor_session_id",
)
SKILL_REQUIRED_MARKERS = (
    "kanban_request_review",
    "self_report",
    "solidified",
    "executor_session_id",
    "missing_solidified",
)
FORBIDDEN_MARKERS = ("kanban_complete",)
METADATA_FIELD_RES = (
    re.compile(r'"executor"\s*:'),
    re.compile(r'"executor_session_id"\s*:'),
    re.compile(r'"self_report"\s*:'),
    re.compile(r'"solidified"\s*:'),
)
SOLIDIFIED_TYPE_RE = re.compile(r'"type"\s*:\s*"(?:routing|skill_patch|script)"')
SOLIDIFIED_PATH_RE = re.compile(r'"path"\s*:\s*"[^"]+"')
SOLIDIFIED_STATUS_RE = re.compile(r'"status"\s*:\s*"pending"')


def setUpModule() -> None:
    if not PROFILES_DIR.is_dir():
        raise unittest.SkipTest(
            f"external profile tree not present: {PROFILES_DIR}"
        )


class WorkerContractTests(unittest.TestCase):
    maxDiff = None

    def _read(self, path: Path) -> str:
        self.assertTrue(path.is_file(), f"missing external worker contract file: {path}")
        return path.read_text(encoding="utf-8")

    def _assert_contract_markers(self, text: str, label: str) -> None:
        for marker in SKILL_REQUIRED_MARKERS:
            self.assertIn(marker, text, f"{label} missing {marker!r}")
        self._assert_metadata_fields(text, label)

    def _assert_metadata_fields(self, text: str, label: str) -> None:
        for pattern in METADATA_FIELD_RES:
            self.assertRegex(
                text,
                pattern,
                f"{label} missing metadata field matching {pattern.pattern!r}",
            )

    def _assert_no_forbidden(self, text: str, label: str) -> None:
        for marker in FORBIDDEN_MARKERS:
            self.assertNotIn(marker, text, f"{label} still instructs {marker!r}")

    def test_each_profile_soul_declares_the_review_contract(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile):
                profile_dir = PROFILES_DIR / profile
                self.assertTrue(
                    profile_dir.is_dir(),
                    f"configured profile is missing, not skipping: {profile_dir}",
                )
                text = self._read(profile_dir / "SOUL.md")
                label = f"{profile}/SOUL.md"
                for marker in SOUL_REQUIRED_MARKERS:
                    self.assertIn(marker, text, f"{label} missing {marker!r}")
                self._assert_no_forbidden(text, label)
                self._assert_metadata_fields(text, label)
                self.assertIn(
                    "missing_solidified",
                    text,
                    f"{label} must explain the empty-solidified path",
                )
                self.assertRegex(
                    text,
                    SOLIDIFIED_TYPE_RE,
                    f"{label} needs a valid solidified type example",
                )
                self.assertRegex(
                    text,
                    SOLIDIFIED_PATH_RE,
                    f"{label} needs a solidified path example",
                )
                self.assertRegex(
                    text,
                    SOLIDIFIED_STATUS_RE,
                    f"{label} needs status=pending",
                )
                if profile == "coder":
                    self.assertIn("bin/executor-run", text)
                    self.assertIn("adapter_mode", text)
                    for flag in ("--source", "--title", "--type"):
                        self.assertIn(flag, text)
                else:
                    self.assertIn("in-process", text)
                    self.assertIn("内置工具", text)
                    self.assertIn("null", text)

    def test_each_profile_can_load_workbench_conventions(self) -> None:
        shared_text = self._read(SHARED_SKILL)
        shared_label = "shared workbench-conventions/SKILL.md"
        self._assert_contract_markers(shared_text, shared_label)
        self._assert_no_forbidden(shared_text, shared_label)
        for profile in PROFILES:
            with self.subTest(profile=profile):
                skill_dir = PROFILES_DIR / profile / "skills" / "workbench-conventions"
                skill = skill_dir / "SKILL.md"
                self.assertTrue(
                    skill.is_file(),
                    f"{profile} cannot load workbench-conventions: {skill} is missing",
                )
                text = skill.read_text(encoding="utf-8")
                self.assertEqual(
                    text,
                    shared_text,
                    f"{profile} loads a stale or forked workbench-conventions copy",
                )
                label = f"{profile}/skills/workbench-conventions/SKILL.md"
                self._assert_contract_markers(text, label)
                self._assert_no_forbidden(text, label)
                self.assertTrue(
                    (skill_dir / "references" / "executor-contract.md").is_file(),
                    f"{profile} skill is missing its references/ directory",
                )

    def test_coder_delegation_skill_keeps_the_contract(self) -> None:
        skill = PROFILES_DIR / "coder" / "skills" / "dsh-delegation" / "SKILL.md"
        label = "coder/skills/dsh-delegation/SKILL.md"
        text = self._read(skill)
        for marker in (
            "bin/route",
            "bin/executor-run",
            "kanban_request_review",
            "self_report",
            "solidified",
            "executor_session_id",
        ):
            self.assertIn(marker, text, f"{label} missing {marker!r}")
        self._assert_no_forbidden(text, label)
        self.assertIn("missing_solidified", text)
        self.assertRegex(
            text,
            SOLIDIFIED_TYPE_RE,
            f"{label} needs a valid solidified type example",
        )
        self.assertRegex(
            text,
            SOLIDIFIED_STATUS_RE,
            f"{label} needs status=pending",
        )

    def test_shared_skill_has_no_direct_complete_exception(self) -> None:
        text = self._read(SHARED_SKILL)
        label = "shared workbench-conventions/SKILL.md"
        self._assert_contract_markers(text, label)
        self._assert_no_forbidden(text, label)
        self.assertNotIn(
            "才允许直接",
            text,
            "machine-checkable cards must also go through review",
        )
        self.assertIn("机器可判定", text)
        self.assertIn("review", text)
        self.assertIn("in-process", text)
        self.assertIn("null", text)

    def test_routing_skills_resolve_in_each_profile(self) -> None:
        routing = json.loads(ROUTING.read_text(encoding="utf-8"))
        rules = routing.get("rules")
        self.assertIsInstance(rules, list, "routing.json rules must be a list")
        covered: set[str] = set()
        for index, rule in enumerate(rules):
            self.assertIsInstance(rule, dict, f"rules[{index}] must be an object")
            when = rule.get("when")
            if not isinstance(when, dict) or "profiles" not in when:
                continue
            profiles = when["profiles"]
            self.assertIsInstance(
                profiles, list, f"rules[{index}].when.profiles must be a list"
            )
            self.assertTrue(profiles, f"rules[{index}].when.profiles must not be empty")
            skills = rule.get("skills", [])
            self.assertIsInstance(skills, list, f"rules[{index}].skills must be a list")
            self.assertTrue(skills, f"rules[{index}].skills must not be empty")
            self.assertIn(
                "workbench-conventions",
                skills,
                f"rules[{index}].skills must include the shared contract skill",
            )
            for profile in profiles:
                self.assertIn(
                    profile,
                    PROFILES,
                    f"rules[{index}] targets unknown profile {profile!r}",
                )
                covered.add(profile)
                for skill_name in skills:
                    self.assertIsInstance(skill_name, str)
                    skill = PROFILES_DIR / profile / "skills" / skill_name / "SKILL.md"
                    self.assertTrue(
                        skill.is_file(),
                        f"routing.json rules[{index}] lists {skill_name!r} "
                        f"but {profile} cannot load {skill}",
                    )
        self.assertEqual(
            covered,
            set(PROFILES),
            "routing.json must cover every configured worker profile",
        )

    def test_skill_prompt_snapshots_are_not_stale(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile):
                snapshot = PROFILES_DIR / profile / ".skills_prompt_snapshot.json"
                if not snapshot.is_file():
                    continue
                raw = snapshot.read_text(encoding="utf-8")
                self.assertNotIn(
                    "用 dsh headless 执行长编码任务",
                    raw,
                    f"{profile} snapshot still has the stale dsh-delegation "
                    "description; rename it so Hermes rebuilds",
                )
                self.assertIn(
                    "workbench-conventions",
                    raw,
                    f"{profile} snapshot does not include workbench-conventions; "
                    "rename it so Hermes rebuilds",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)