#!/usr/bin/env python3
"""Tests for Story 3.5: routing.json + ``bin/route``.

Covers the I/O matrix and routing boundaries:

* repository routing: coder -> claude_code, other profiles -> hermes,
  no profile -> default.executor;
* rule matching: ``when.profiles`` / ``sources`` / ``types`` / ``keywords``,
  ``to`` as the profile fallback, first rule wins;
* a matched rule without ``executor`` falls back to coder -> dsh and every
  other profile -> hermes;
* unknown executors, malformed routing/registry JSON and missing files are
  fail-closed (exit 1, JSON on the last stdout line);
* ``--executor`` (card/event metadata override) is not accepted at all;
* budget / skills / tell are passed through unchanged.

Run with::

    python3 tests/test_routing.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "route"
REGISTRY = ROOT / "executors.json"
ROUTING = ROOT / "routing.json"

REGISTRY_DATA = {
    "version": 1,
    "executors": {
        "claude_code": {
            "module": "executors.claude_code",
            "binary": "claude",
            "mode": "cli",
        },
        "dsh": {"module": "executors.dsh", "binary": "dsh", "mode": "cli"},
        "codex": {"module": "executors.codex", "binary": "codex", "mode": "cli"},
        "hermes": {"module": "executors.hermes", "mode": "in-process"},
    },
}


class RoutingHarness(unittest.TestCase):
    def setUp(self) -> None:
        import shutil

        self.tmp = Path(tempfile.mkdtemp(prefix="route-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.registry = self.write_json("executors.json", REGISTRY_DATA)

    def write_json(self, name: str, data) -> Path:
        path = self.tmp / name
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def run_route(self, *args, routing=None, registry=None) -> subprocess.CompletedProcess:
        command = [sys.executable, str(CLI)]
        if routing is not None:
            command.extend(["--routing", str(routing)])
        if registry is not None:
            command.extend(["--registry", str(registry)])
        command.extend(args)
        return subprocess.run(command, text=True, capture_output=True, timeout=30)

    @staticmethod
    def payload(proc: subprocess.CompletedProcess) -> dict:
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        assert lines, f"no stdout; stderr={proc.stderr!r}"
        return json.loads(lines[-1])

    def route_with(self, routing_data, *args, registry=None):
        routing = self.write_json("routing.json", routing_data)
        return self.run_route(
            *args,
            routing=routing,
            registry=registry if registry is not None else self.registry,
        )


class RepositoryRoutingTests(unittest.TestCase):
    def run_repo(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            text=True,
            capture_output=True,
            timeout=30,
        )

    @staticmethod
    def payload(proc: subprocess.CompletedProcess) -> dict:
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        assert lines, f"no stdout; stderr={proc.stderr!r}"
        return json.loads(lines[-1])

    def test_coder_resolves_claude_code_cli(self) -> None:
        proc = self.run_repo("--profile", "coder", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["executor"], "claude_code")
        self.assertEqual(payload["mode"], "cli")
        self.assertEqual(payload["adapter_mode"], "cli")
        self.assertEqual(payload["to"], "coder")
        self.assertEqual(payload["rule"], 0)
        self.assertEqual(payload["rule_index"], 0)
        self.assertEqual(payload["rule_name"], "coder-claude-code")
        self.assertEqual(payload["budget"], {"max_minutes": 30, "max_cost": 2})

    def test_other_profiles_resolve_to_hermes_in_process(self) -> None:
        names = {
            "researcher": "researcher-hermes",
            "housekeeper": "housekeeper-hermes",
            "skeptic": "skeptic-hermes",
        }
        for profile, rule_name in names.items():
            with self.subTest(profile=profile):
                proc = self.run_repo("--profile", profile, "--json")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                payload = self.payload(proc)
                self.assertEqual(payload["executor"], "hermes")
                self.assertEqual(payload["mode"], "in-process")
                self.assertEqual(payload["adapter_mode"], "in-process")
                self.assertEqual(payload["to"], profile)
                self.assertEqual(payload["rule_name"], rule_name)

    def test_no_profile_uses_default_executor(self) -> None:
        proc = self.run_repo("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "hermes")
        self.assertIsNone(payload["rule"])
        self.assertIsNone(payload["rule_index"])
        self.assertIsNone(payload["rule_name"])
        self.assertIsNone(payload["to"])
        self.assertEqual(payload["adapter_mode"], payload["mode"])

    def test_repository_files_are_valid(self) -> None:
        routing = json.loads(ROUTING.read_text(encoding="utf-8"))
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(routing["default"]["executor"], "hermes")
        self.assertIn("claude_code", registry["executors"])
        self.assertIn("dsh", registry["executors"])
        self.assertIn("codex", registry["executors"])
        self.assertEqual(registry["executors"]["hermes"]["mode"], "in-process")
        names = [rule.get("name") for rule in routing["rules"]]
        self.assertTrue(all(isinstance(name, str) and name for name in names), names)
        self.assertEqual(len(names), len(set(names)), names)
        # `to` is only the fallback when when.profiles is absent.
        for rule in routing["rules"]:
            self.assertNotIn("to", rule)
            self.assertIn("profiles", rule.get("when", {}))


class RuleMatchingTests(RoutingHarness):
    def test_no_rule_match_uses_default(self) -> None:
        proc = self.route_with(
            {"default": {"executor": "hermes"}, "rules": [{"to": "coder"}]},
            "--profile",
            "reviewer",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "hermes")
        self.assertIsNone(payload["rule"])

    def test_rule_without_executor_coder_uses_dsh(self) -> None:
        proc = self.route_with(
            {"rules": [{"to": "coder"}]},
            "--profile",
            "coder",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "dsh")
        self.assertEqual(payload["mode"], "cli")

    def test_rule_without_executor_other_profile_uses_hermes(self) -> None:
        proc = self.route_with(
            {"rules": [{"to": "researcher"}]},
            "--profile",
            "researcher",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.payload(proc)["executor"], "hermes")

    def test_default_without_executor_uses_profile_default(self) -> None:
        proc = self.route_with(
            {"default": {}, "rules": []},
            "--profile",
            "coder",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.payload(proc)["executor"], "dsh")

        proc = self.route_with(
            {"default": {}, "rules": []},
            "--profile",
            "other",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.payload(proc)["executor"], "hermes")

    def test_to_matches_profile_when_when_is_absent(self) -> None:
        proc = self.route_with(
            {"rules": [{"to": "coder", "executor": "codex"}]},
            "--profile",
            "coder",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "codex")
        self.assertEqual(payload["rule"], 0)

        proc = self.route_with(
            {"default": {"executor": "hermes"}, "rules": [{"to": "coder", "executor": "codex"}]},
            "--profile",
            "researcher",
            "--json",
        )
        self.assertEqual(self.payload(proc)["executor"], "hermes")

    def test_profiles_list_wins_over_to(self) -> None:
        routing = {
            "default": {"executor": "hermes"},
            "rules": [
                {
                    "to": "coder",
                    "when": {"profiles": ["researcher"]},
                    "executor": "codex",
                }
            ],
        }
        proc = self.route_with(routing, "--profile", "researcher", "--json")
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "codex")
        self.assertEqual(payload["to"], "researcher")

        proc = self.route_with(routing, "--profile", "coder", "--json")
        self.assertEqual(self.payload(proc)["executor"], "hermes")

    def test_keyword_rule_matches_title(self) -> None:
        routing = {
            "default": {"executor": "hermes"},
            "rules": [
                {
                    "when": {"keywords": ["迁移", "migration"]},
                    "executor": "codex",
                }
            ],
        }
        proc = self.route_with(routing, "--title", "数据库迁移", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.payload(proc)["executor"], "codex")

        proc = self.route_with(routing, "--title", "DB MIGRATION plan", "--json")
        self.assertEqual(self.payload(proc)["executor"], "codex")

        proc = self.route_with(routing, "--title", "普通任务", "--json")
        self.assertEqual(self.payload(proc)["executor"], "hermes")

    def test_sources_and_types_are_and_conditions(self) -> None:
        routing = {
            "default": {"executor": "hermes"},
            "rules": [
                {
                    "when": {"sources": ["kanban"], "types": ["coding"]},
                    "executor": "claude_code",
                }
            ],
        }
        matched = self.route_with(
            routing,
            "--source",
            "kanban",
            "--type",
            "coding",
            "--json",
        )
        self.assertEqual(self.payload(matched)["executor"], "claude_code")

        for args in (
            ("--source", "kanban", "--type", "research"),
            ("--source", "cli", "--type", "coding"),
            ("--source", "kanban"),
        ):
            with self.subTest(args=args):
                proc = self.route_with(routing, *args, "--json")
                self.assertEqual(self.payload(proc)["executor"], "hermes")

    def test_first_matching_rule_wins(self) -> None:
        routing = {
            "default": {"executor": "hermes"},
            "rules": [
                {"to": "coder", "executor": "hermes"},
                {"to": "coder", "executor": "claude_code"},
            ],
        }
        proc = self.route_with(routing, "--profile", "coder", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "hermes")
        self.assertEqual(payload["rule"], 0)

    def test_budget_skills_and_tell_pass_through(self) -> None:
        routing = {
            "rules": [
                {
                    "to": "coder",
                    "executor": "dsh",
                    "skills": ["workbench-conventions", 7],
                    "budget": {"max_minutes": 12.5, "max_cost": 0.5},
                    "tell": "keep it short",
                }
            ]
        }
        proc = self.route_with(routing, "--profile", "coder", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["budget"], {"max_minutes": 12.5, "max_cost": 0.5})
        self.assertEqual(payload["skills"], ["workbench-conventions"])
        self.assertEqual(payload["tell"], "keep it short")

    def test_rule_can_select_in_process_hermes(self) -> None:
        routing = {"rules": [{"to": "researcher", "executor": "hermes"}]}
        proc = self.route_with(routing, "--profile", "researcher", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "hermes")
        self.assertEqual(payload["mode"], "in-process")


class FailClosedTests(RoutingHarness):
    def test_bad_routing_json_is_1(self) -> None:
        routing = self.write_json("routing.json", "{not json")
        proc = self.run_route(
            "--profile",
            "coder",
            "--json",
            routing=routing,
            registry=self.registry,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "routing_invalid")

    def test_non_object_routing_is_1(self) -> None:
        routing = self.write_json("routing.json", "[]")
        proc = self.run_route(
            "--json", routing=routing, registry=self.registry
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.payload(proc)["error"], "routing_invalid")

    def test_missing_routing_is_1(self) -> None:
        proc = self.run_route(
            "--json",
            routing=self.tmp / "missing.json",
            registry=self.registry,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "routing_unavailable")

    def test_bad_registry_json_is_1(self) -> None:
        registry = self.write_json("bad-registry.json", "{not json")
        proc = self.route_with({"rules": []}, "--json", registry=registry)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.payload(proc)["error"], "registry_invalid")

    def test_missing_registry_is_1(self) -> None:
        proc = self.route_with(
            {"rules": []},
            "--json",
            registry=self.tmp / "missing-registry.json",
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.payload(proc)["error"], "registry_unavailable")

    def test_unknown_executor_is_1(self) -> None:
        proc = self.route_with(
            {"rules": [{"to": "coder", "executor": "ghost"}]},
            "--profile",
            "coder",
            "--json",
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "unknown_executor")
        self.assertIn("ghost", payload["detail"])

    def test_malformed_rules_are_1(self) -> None:
        cases = [
            {"rules": {}},
            {"rules": ["coder"]},
            {"rules": [{"when": "coder"}]},
            {"rules": [{"when": {"profiles": "coder"}}]},
            {"rules": [{"when": {"profiles": [1]}}]},
            {"rules": [{"executor": 42}]},
            {"rules": [{"to": 42}]},
            {"default": []},
        ]
        for index, routing in enumerate(cases):
            with self.subTest(index=index, routing=routing):
                proc = self.route_with(routing, "--profile", "coder", "--json")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(self.payload(proc)["error"], "routing_invalid")


class HardeningTests(RoutingHarness):
    def test_non_utf8_routing_is_json_error(self) -> None:
        path = self.tmp / "bad-utf8.json"
        path.write_bytes(b'{"rules": [], "note": "\xff"}')
        proc = self.run_route(
            "--json", routing=path, registry=self.registry
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "routing_invalid")

    def test_unexpandable_home_is_json_error(self) -> None:
        proc = self.run_route(
            "--json",
            routing="~nonexistent-user-3-5/routing.json",
            registry=self.registry,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "path_invalid")

    def test_unknown_when_key_is_1(self) -> None:
        proc = self.route_with(
            {
                "rules": [
                    {
                        "name": "bad-when",
                        "when": {"mystery": ["x"]},
                        "executor": "dsh",
                    }
                ]
            },
            "--profile",
            "coder",
            "--json",
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["error"], "routing_invalid")
        self.assertIn("mystery", payload["detail"])

    def test_unknown_rule_level_keys_are_1(self) -> None:
        for key in ("exectuor", "budjet", "skils"):
            with self.subTest(key=key):
                rule = {"name": "typo", "to": "coder", "executor": "dsh"}
                rule[key] = "claude_code"
                proc = self.route_with(
                    {"rules": [rule]}, "--profile", "coder", "--json"
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                payload = self.payload(proc)
                self.assertEqual(payload["error"], "routing_invalid")
                self.assertIn(key, payload["detail"])

    def test_blank_executor_and_to_are_1(self) -> None:
        cases = [
            {"name": "blank-executor", "to": "coder", "executor": ""},
            {"name": "blank-executor", "to": "coder", "executor": "   "},
            {"name": "blank-to", "to": "", "executor": "dsh"},
            {"name": "blank-to", "to": "   ", "executor": "dsh"},
        ]
        for rule in cases:
            with self.subTest(rule=rule):
                proc = self.route_with(
                    {"rules": [rule]}, "--profile", "coder", "--json"
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(
                    self.payload(proc)["error"], "routing_invalid"
                )

    def test_non_object_budget_is_1(self) -> None:
        for budget in ("30", True, False, 0, [], ["max_minutes"], None):
            with self.subTest(budget=budget):
                routing = {
                    "rules": [
                        {
                            "name": "bad-budget",
                            "to": "coder",
                            "executor": "dsh",
                            "budget": budget,
                        }
                    ]
                }
                proc = self.route_with(routing, "--profile", "coder", "--json")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(
                    self.payload(proc)["error"], "routing_invalid"
                )

    def test_budget_values_must_be_finite_positive_numbers(self) -> None:
        cases = [
            {"max_minutes": "30"},
            {"max_cost": "2"},
            {"max_minutes": True},
            {"max_cost": False},
            {"max_minutes": 0},
            {"max_cost": -1},
            {"max_minutes": None},
            {"max_cost": [2]},
            {"max_minutes": {"value": 30}},
            {"max_tokens": 100},
        ]
        for budget in cases:
            with self.subTest(budget=budget):
                routing = {
                    "rules": [
                        {
                            "name": "bad-budget",
                            "to": "coder",
                            "executor": "dsh",
                            "budget": budget,
                        }
                    ]
                }
                proc = self.route_with(routing, "--profile", "coder", "--json")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(
                    self.payload(proc)["error"], "routing_invalid"
                )

    def test_registry_entry_requires_non_empty_module(self) -> None:
        cases = [
            {"binary": "dsh"},
            {"module": ""},
            {"module": "   "},
            {"module": 42},
            {},
            "",
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                registry = self.write_json(
                    "bad-module.json", {"executors": {"dsh": entry}}
                )
                proc = self.route_with(
                    {"rules": [{"name": "r", "to": "coder", "executor": "dsh"}]},
                    "--profile",
                    "coder",
                    "--json",
                    registry=registry,
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                payload = self.payload(proc)
                self.assertEqual(payload["error"], "registry_invalid")
                self.assertIn("module", payload["detail"])

    def test_singular_source_and_type_aliases_match(self) -> None:
        routing = {
            "default": {"executor": "hermes"},
            "rules": [
                {
                    "name": "kanban-coding",
                    "when": {"source": "kanban", "type": "coding"},
                    "executor": "codex",
                }
            ],
        }
        matched = self.route_with(
            routing,
            "--source",
            "kanban",
            "--type",
            "coding",
            "--json",
        )
        self.assertEqual(self.payload(matched)["executor"], "codex")

        for args in (
            ("--source", "kanban", "--type", "research"),
            ("--source", "cli", "--type", "coding"),
            ("--source", "kanban"),
        ):
            with self.subTest(args=args):
                proc = self.route_with(routing, *args, "--json")
                self.assertEqual(self.payload(proc)["executor"], "hermes")

    def test_empty_allowlists_match_nothing(self) -> None:
        cases = [
            {"profiles": []},
            {"sources": []},
            {"types": []},
            {"keywords": []},
        ]
        for when in cases:
            with self.subTest(when=when):
                proc = self.route_with(
                    {
                        "default": {"executor": "hermes"},
                        "rules": [
                            {"name": "empty", "when": when, "executor": "codex"}
                        ],
                    },
                    "--profile",
                    "coder",
                    "--source",
                    "kanban",
                    "--type",
                    "coding",
                    "--title",
                    "迁移",
                    "--json",
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.payload(proc)["executor"], "hermes")

    def test_non_finite_budget_is_1(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(value=value):
                raw = (
                    '{"rules":[{"name":"budget","to":"coder","executor":"dsh",'
                    '"budget":{"max_cost":' + value + "}}]}"
                )
                routing = self.write_json("routing.json", raw)
                proc = self.run_route(
                    "--profile",
                    "coder",
                    "--json",
                    routing=routing,
                    registry=self.registry,
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                payload = self.payload(proc)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"], "routing_invalid")

    def test_non_string_default_executor_is_1(self) -> None:
        for value in (42, "", "   ", None, ["hermes"]):
            with self.subTest(value=value):
                proc = self.route_with(
                    {"default": {"executor": value}, "rules": []},
                    "--profile",
                    "other",
                    "--json",
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(self.payload(proc)["error"], "routing_invalid")

    def test_invalid_registry_mode_is_1(self) -> None:
        for mode in (42, "", "banana", None):
            with self.subTest(mode=mode):
                registry = self.write_json(
                    "bad-mode.json",
                    {
                        "executors": {
                            "dsh": {"module": "executors.dsh", "mode": mode}
                        }
                    },
                )
                proc = self.route_with(
                    {"rules": [{"name": "r", "to": "coder", "executor": "dsh"}]},
                    "--profile",
                    "coder",
                    "--json",
                    registry=registry,
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(self.payload(proc)["error"], "registry_invalid")

    def test_to_returns_actual_profile_when_profiles_matched(self) -> None:
        proc = self.route_with(
            {
                "rules": [
                    {
                        "name": "stale-to",
                        "to": "someone-else",
                        "when": {"profiles": ["coder"]},
                        "executor": "dsh",
                    }
                ]
            },
            "--profile",
            "coder",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["executor"], "dsh")
        self.assertEqual(payload["to"], "coder")

    def test_rule_name_and_rule_index(self) -> None:
        routing = {
            "rules": [
                {"name": "first", "to": "researcher", "executor": "hermes"},
                {"name": "second", "to": "coder", "executor": "dsh"},
            ]
        }
        proc = self.route_with(routing, "--profile", "coder", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["rule"], 1)
        self.assertEqual(payload["rule_index"], 1)
        self.assertEqual(payload["rule_name"], "second")
        self.assertEqual(payload["adapter_mode"], payload["mode"])

    def test_rule_name_must_be_non_empty(self) -> None:
        for name in (42, "", "   "):
            with self.subTest(name=name):
                proc = self.route_with(
                    {
                        "rules": [
                            {"name": name, "to": "coder", "executor": "dsh"}
                        ]
                    },
                    "--profile",
                    "coder",
                    "--json",
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(self.payload(proc)["error"], "routing_invalid")

    def test_singular_aliases_must_be_non_empty_strings(self) -> None:
        for when in ({"source": ["kanban"]}, {"type": ""}, {"source": 42}):
            with self.subTest(when=when):
                proc = self.route_with(
                    {
                        "default": {"executor": "hermes"},
                        "rules": [{"name": "alias", "when": when, "executor": "dsh"}],
                    },
                    "--profile",
                    "coder",
                    "--json",
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(self.payload(proc)["error"], "routing_invalid")


class NoOverrideTests(RoutingHarness):
    def test_executor_flag_is_rejected(self) -> None:
        proc = self.route_with(
            {"rules": []},
            "--profile",
            "coder",
            "--executor",
            "dsh",
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("--executor", proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "invalid_args")

    def test_card_metadata_flag_is_rejected(self) -> None:
        proc = self.route_with(
            {"rules": []},
            "--profile",
            "coder",
            "--metadata",
            '{"executor":"dsh"}',
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("--metadata", proc.stderr)


class OutputShapeTests(RoutingHarness):
    def test_last_line_is_json_without_json_flag(self) -> None:
        proc = self.route_with(
            {"rules": [{"to": "coder", "executor": "dsh"}]},
            "--profile",
            "coder",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("executor="))
        payload = json.loads(lines[-1])
        self.assertEqual(payload["executor"], "dsh")

    def test_json_flag_prints_one_line(self) -> None:
        proc = self.route_with(
            {"rules": [{"to": "coder", "executor": "dsh"}]},
            "--profile",
            "coder",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["executor"], "dsh")

    def test_usage_error_still_ends_with_json(self) -> None:
        proc = self.run_route("--bogus")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = self.payload(proc)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "invalid_args")


if __name__ == "__main__":
    unittest.main(verbosity=2)
