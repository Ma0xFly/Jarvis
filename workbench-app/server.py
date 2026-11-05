#!/usr/bin/env python3
"""Jarvis Workbench — standalone kanban UI server.

Two distinct paths live here:

* ``/api/plugins/kanban/*`` — the Hermes dashboard kanban router is mounted
  unchanged (existing behaviour) so reads and edits keep using the upstream
  state machine.
* ``/api/workbench/*`` — workbench-owned endpoints (dispatch, metadata). These
  never import upstream internals and reach kanban only through the loopback
  public REST surface. Contract metadata (``contract`` / ``contract_defaults``
  / ``bucket``) lives in ``workbench-app/workbench.db``; ``kanban.db`` is never
  opened or written by this module.

Run with the Hermes runtime Python (see run.sh). Binds loopback by default.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", Path.home() / ".hermes" / "hermes-agent")).resolve()
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

# hermes_bootstrap wires the runtime dependency paths (fastapi, uvicorn, ...).
import hermes_bootstrap  # noqa: F401,E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402
from starlette.requests import ClientDisconnect  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
# The test harness starts this file with ``python -I`` (no script directory on
# sys.path); make the sibling notification module importable.
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from notifications import NotificationEngine, NotificationPoller  # noqa: E402

WEB_DIR = APP_DIR / "web"
DEFAULT_DB_PATH = APP_DIR / "workbench.db"
DEFAULT_DEFAULTS_PATH = APP_DIR / "defaults.json"

UPSTREAM_CALL_TIMEOUT = 1.0
DISPATCH_BUDGET_SECONDS = 2.9
BUDGET_KEYS = ("max_minutes", "max_cost", "deny_paths")
TASK_PATH_RE = re.compile(r"^/api/plugins/kanban/tasks/([^/]+)$")
RECLAIM_PATH_RE = re.compile(r"^/api/plugins/kanban/tasks/([^/]+)/reclaim$")
REASSIGN_PATH_RE = re.compile(r"^/api/plugins/kanban/tasks/([^/]+)/reassign$")
RUN_TERMINATE_PATH_RE = re.compile(r"^/api/plugins/kanban/runs/([0-9]+)/terminate$")
CREATE_TASK_PATH = "/api/plugins/kanban/tasks"
BULK_TASK_PATH = "/api/plugins/kanban/tasks/bulk"
TERMINAL_STATUSES = ("done", "archived")
MAX_METADATA_IDS = 200
MAX_MINUTES = 10080
MAX_COST = 1000
MAX_MINUTES_PER_TASK = 1440
GATE_UPSTREAM_TIMEOUT = 0.75
BUCKETS = ("today", "long")
METADATA_PATCH_KEYS = ("bucket", "contract")
MAX_ACCEPT_CHARS = 2000
MAX_INPUT_CHARS = 4000
MAX_DENY_PATHS = 100
MAX_REQUEST_BYTES = 64 * 1024
MAX_IDEMPOTENCY_KEY = 128

# Verification (Story 2.1). A review card is auto-accepted after 48h without a
# human decision so the review lane cannot stay clogged; the background sweeper
# runs every 5 minutes and every inbox read is also a fallback sweep.
REVIEW_STATUS = "review"
BLOCKED_STATUS = "blocked"
REVIEW_TTL_SECONDS = 48 * 60 * 60
REVIEW_SWEEP_SECONDS = 5 * 60
REVIEW_BACKOFF_SECONDS = 5 * 60
REVIEW_BUDGET_SECONDS = 5.0
INBOX_BUDGET_SECONDS = 3.0
INBOX_CACHE_SECONDS = 1.0
MAX_INBOX_WORKERS = 8
VERIFY_ACTIONS = ("accept", "reject", "skip")
ACCEPT_SUMMARY = "验收通过"
AUTO_COLLECT_COMMENT = "48h 自动收"
AUTO_COLLECT_AUDIT_SUMMARY = "48h 自动收（worker 自述保留在 review_requested 事件/run）"
MAX_REJECT_REASON_CHARS = 2000
VERIFY_AUTHOR = "workbench"

# Notifications (Story 2.2). The engine lives in memory; the background poller
# starts 5s after boot and re-reads the board. All thresholds are env-tunable
# so tests can shorten them without sleeping for half an hour.
NOTIFY_SWEEP_SECONDS = 5.0
NOTIFY_BATCH_SIZE = 3
NOTIFY_BATCH_SECONDS = 30 * 60
NOTIFY_ESCALATE_SECONDS = 30 * 60
NOTIFY_SNOOZE_SECONDS = 10 * 60
NOTIFY_DAILY_HOUR = 8


class MetadataUnavailable(RuntimeError):
    """The workbench metadata DB cannot be opened or queried."""


class DefaultsError(ValueError):
    """``defaults.json`` is missing or has an invalid type/range."""


class UpstreamUnavailable(RuntimeError):
    """A loopback kanban REST call failed or exceeded the dispatch budget."""


class TaskNotFound(UpstreamUnavailable):
    """``GET /tasks/{id}`` returned 404 for a card we expected to exist."""


# ---------------------------------------------------------------------------
# Defaults + contract snapshot
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_deny_paths(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _valid_budget_value(key: str, value: Any) -> bool:
    if key == "max_minutes":
        return _is_number(value) and 0 < value <= MAX_MINUTES
    if key == "max_cost":
        return _is_number(value) and 0 <= value <= MAX_COST
    if key == "deny_paths":
        return _valid_deny_paths(value)
    return False


def load_defaults(path: Path) -> dict[str, Any]:
    """Read and validate the single source of global budget/queue defaults."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DefaultsError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DefaultsError("defaults root must be an object")

    budget = raw.get("budget")
    if not isinstance(budget, dict):
        raise DefaultsError("budget must be an object")
    for key in BUDGET_KEYS:
        if key not in budget:
            raise DefaultsError(f"budget.{key} is required")
        if not _valid_budget_value(key, budget[key]):
            raise DefaultsError(f"budget.{key} has an invalid type or range")
    deny_paths = [item.strip() for item in budget["deny_paths"]]

    queue = raw.get("queue")
    if not isinstance(queue, dict):
        raise DefaultsError("queue must be an object")
    minutes = queue.get("minutes_per_task")
    if not _is_number(minutes) or not 0 < minutes <= MAX_MINUTES_PER_TASK:
        raise DefaultsError("queue.minutes_per_task must be a positive number <= 1440")

    return {
        "budget": {
            "max_minutes": budget["max_minutes"],
            "max_cost": budget["max_cost"],
            "deny_paths": deny_paths,
        },
        "minutes_per_task": minutes,
    }


def extract_accept(metadata: Any) -> Optional[str]:
    """Return the non-empty contract acceptance text, or None."""
    if not isinstance(metadata, dict):
        return None
    contract = metadata.get("contract")
    if not isinstance(contract, dict):
        return None
    accept = contract.get("accept")
    if isinstance(accept, str) and accept.strip():
        return accept.strip()
    return None


def build_contract_snapshot(defaults: dict[str, Any], metadata: Any) -> dict[str, Any]:
    """Merge the global budget with the card's ``contract.budget`` override.

    Invalid override values are ignored so a malformed card cannot poison the
    snapshot; the global default stays authoritative for that key.
    """
    budget = dict(defaults["budget"])
    override = None
    if isinstance(metadata, dict):
        contract = metadata.get("contract")
        if isinstance(contract, dict):
            override = contract.get("budget")
    if isinstance(override, dict):
        for key in BUDGET_KEYS:
            if key in override and _valid_budget_value(key, override[key]):
                value = override[key]
                budget[key] = [item.strip() for item in value] if key == "deny_paths" else value
    return {
        "max_minutes": budget["max_minutes"],
        "max_cost": budget["max_cost"],
        "deny_paths": list(budget["deny_paths"]),
    }


def validate_metadata_patch(raw: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Validate a client metadata patch for ``task_metadata``.

    Only ``bucket`` and ``contract`` are writable. ``contract_defaults`` is a
    server-owned dispatch snapshot and must never be accepted from a client.
    Returns ``(patch, None)`` on success or ``(None, error_code)``.
    """
    if not isinstance(raw, dict) or not raw:
        return None, "invalid_metadata"
    if set(raw) - set(METADATA_PATCH_KEYS):
        return None, "invalid_metadata"

    patch: dict[str, Any] = {}
    if "bucket" in raw:
        bucket = raw["bucket"]
        if bucket not in BUCKETS:
            return None, "invalid_bucket"
        patch["bucket"] = bucket

    if "contract" in raw:
        contract = raw["contract"]
        if not isinstance(contract, dict) or not contract:
            return None, "invalid_contract"
        contract_patch: dict[str, Any] = {}
        for key, value in contract.items():
            if key == "accept":
                if not isinstance(value, str) or len(value) > MAX_ACCEPT_CHARS:
                    return None, "invalid_contract"
                # Acceptance is compared trimmed by the dispatch gate; store
                # the same normalised value the gate will see.
                contract_patch[key] = value.strip()
            elif key == "input":
                if not isinstance(value, str) or len(value) > MAX_INPUT_CHARS:
                    return None, "invalid_contract"
                contract_patch[key] = value
            elif key == "ready":
                if not isinstance(value, bool):
                    return None, "invalid_contract"
                contract_patch[key] = value
            elif key == "budget":
                if not isinstance(value, dict) or not value:
                    return None, "invalid_contract"
                budget_patch: dict[str, Any] = {}
                for budget_key, budget_value in value.items():
                    if budget_key == "deny_paths":
                        if (
                            not isinstance(budget_value, list)
                            or not budget_value
                            or len(budget_value) > MAX_DENY_PATHS
                            or not _valid_deny_paths(budget_value)
                        ):
                            return None, "invalid_contract"
                        budget_patch[budget_key] = [item.strip() for item in budget_value]
                    elif budget_key not in BUDGET_KEYS or not _valid_budget_value(budget_key, budget_value):
                        return None, "invalid_contract"
                    else:
                        budget_patch[budget_key] = budget_value
                contract_patch["budget"] = budget_patch
            else:
                return None, "invalid_contract"
        patch["contract"] = contract_patch

    if not patch:
        return None, "invalid_metadata"
    return patch, None


# ---------------------------------------------------------------------------
# Workbench-owned metadata store
# ---------------------------------------------------------------------------


@dataclass
class SnapshotWrite:
    task_id: str
    token: str
    created: bool
    value: dict[str, Any]
    # Set when this write replaced an existing snapshot (rework re-dispatch);
    # compensation restores it instead of deleting the history.
    previous: Optional[dict[str, Any]] = None
    previous_token: Optional[str] = None


class MetadataStore:
    """``workbench.db`` task_metadata with JSON1 per-key updates.

    The DB is opened lazily and idempotently initialised; a broken/unavailable
    DB only disables the workbench endpoints (503), never the whole service.
    Snapshots are written with ``json_set`` and removed with ``json_remove`` so
    other keys written concurrently (e.g. by Story 1.3) survive. A per-write
    ``write_token`` column records ownership; compensation only removes the
    snapshot when the token still matches.
    """

    CREATE_SQL = (
        "CREATE TABLE IF NOT EXISTS task_metadata ("
        " task_id TEXT PRIMARY KEY,"
        " metadata TEXT NOT NULL,"
        " updated_at INTEGER NOT NULL,"
        " write_token TEXT"
        ")"
    )

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ready = False

    def start(self) -> None:
        """Best-effort startup init; failures are reported, not raised."""
        try:
            self._ensure()
        except MetadataUnavailable as exc:
            print(f"workbench: metadata DB unavailable at startup: {exc}", file=sys.stderr, flush=True)

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=1.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(self.CREATE_SQL)
            # Additive migration for DBs created before snapshot ownership.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(task_metadata)").fetchall()}
            if "write_token" not in columns:
                conn.execute("ALTER TABLE task_metadata ADD COLUMN write_token TEXT")
            conn.commit()
        finally:
            conn.close()

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            try:
                self._init()
            except Exception as exc:  # sqlite3.OperationalError, OSError, ...
                raise MetadataUnavailable(str(exc)) from exc
            self._ready = True

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        try:
            return sqlite3.connect(str(self.path), timeout=1.0)
        except Exception as exc:
            self._ready = False
            raise MetadataUnavailable(str(exc)) from exc

    @staticmethod
    def _decode(raw: Any) -> Optional[dict[str, Any]]:
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT metadata FROM task_metadata WHERE task_id = ?", (task_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            self._ready = False
            raise MetadataUnavailable(str(exc)) from exc
        finally:
            conn.close()
        return self._decode(row[0]) if row is not None else None

    def get_many(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not task_ids:
            return {}
        placeholders = ",".join("?" for _ in task_ids)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT task_id, metadata FROM task_metadata WHERE task_id IN ({placeholders})",
                tuple(task_ids),
            ).fetchall()
        except sqlite3.Error as exc:
            self._ready = False
            raise MetadataUnavailable(str(exc)) from exc
        finally:
            conn.close()
        out: dict[str, dict[str, Any]] = {}
        for task_id, raw in rows:
            decoded = self._decode(raw)
            if decoded is not None:
                out[task_id] = decoded
        return out

    def merge(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge a validated patch with ``json_patch`` (JSON1).

        Nested objects merge instead of replacing, so writing
        ``contract.accept`` keeps sibling contract keys. Keys not named in the
        patch — notably the server-owned ``contract_defaults`` snapshot — are
        left untouched. The write runs in one ``BEGIN IMMEDIATE`` transaction
        so it serialises with dispatch snapshot writes.
        """
        encoded = json.dumps(patch, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        now_ms = int(time.time() * 1000)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT metadata FROM task_metadata WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO task_metadata (task_id, metadata, updated_at) VALUES (?, ?, ?)",
                        (task_id, encoded, now_ms),
                    )
                else:
                    current = self._decode(row[0])
                    if current is None:
                        conn.execute("ROLLBACK")
                        raise MetadataUnavailable(f"corrupt metadata row for {task_id}")
                    conn.execute(
                        "UPDATE task_metadata SET metadata = json_patch(metadata, json(?)), "
                        "updated_at = ? WHERE task_id = ?",
                        (encoded, now_ms, task_id),
                    )
                row = conn.execute(
                    "SELECT metadata FROM task_metadata WHERE task_id = ?", (task_id,)
                ).fetchone()
                merged = self._decode(row[0]) if row is not None else None
                if merged is None:
                    conn.execute("ROLLBACK")
                    raise MetadataUnavailable(f"metadata merge failed for {task_id}")
                conn.execute("COMMIT")
                return merged
            except MetadataUnavailable:
                raise
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise MetadataUnavailable(str(exc)) from exc
            finally:
                conn.close()

    def write_snapshot(
        self, task_id: str, value: dict[str, Any], *, overwrite: bool = False
    ) -> SnapshotWrite:
        """Write ``contract_defaults`` for a dispatch.

        ``overwrite=False`` (ready re-dispatch) keeps an existing snapshot and
        only inserts one when the card has none. ``overwrite=True`` is used when
        a dispatch starts from another status (triage/todo/blocked/scheduled/
        review): the card is being dispatched afresh, so the current defaults
        replace an older snapshot and take a new ``write_token``.

        The stored ``write_token`` identifies the request that wrote the
        snapshot so compensation cannot delete a foreign/replaced snapshot. A
        replaced snapshot is remembered so a failed re-dispatch can restore it.
        """
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        now_ms = int(time.time() * 1000)
        token = uuid.uuid4().hex
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT metadata, write_token FROM task_metadata WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is not None:
                    current = self._decode(row[0])
                    if current is None:
                        conn.execute("ROLLBACK")
                        raise MetadataUnavailable(f"corrupt metadata row for {task_id}")
                    if "contract_defaults" in current and not overwrite:
                        conn.execute("COMMIT")
                        return SnapshotWrite(
                            task_id=task_id, token=token, created=False, value=value
                        )
                    previous = current.get("contract_defaults")
                    previous_token = row[1] if "contract_defaults" in current else None
                    conn.execute(
                        "UPDATE task_metadata SET "
                        "metadata = json_set(metadata, '$.contract_defaults', json(?)), "
                        "updated_at = ?, write_token = ? WHERE task_id = ?",
                        (encoded, now_ms, token, task_id),
                    )
                    conn.execute("COMMIT")
                    return SnapshotWrite(
                        task_id=task_id,
                        token=token,
                        created=True,
                        value=value,
                        previous=previous if isinstance(previous, dict) else None,
                        previous_token=previous_token,
                    )
                conn.execute(
                    "INSERT INTO task_metadata (task_id, metadata, updated_at, write_token) "
                    "VALUES (?, json_object('contract_defaults', json(?)), ?, ?)",
                    (task_id, encoded, now_ms, token),
                )
                conn.execute("COMMIT")
                return SnapshotWrite(task_id=task_id, token=token, created=True, value=value)
            except MetadataUnavailable:
                raise
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise MetadataUnavailable(str(exc)) from exc
            finally:
                conn.close()

    def remove_snapshot(self, write: SnapshotWrite) -> bool:
        """Compensate only when this request still owns the snapshot."""
        if not write.created:
            return False
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                task_id = write.task_id
                row = conn.execute(
                    "SELECT metadata, write_token FROM task_metadata WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None or row[1] != write.token:
                    conn.execute("COMMIT")
                    return False
                current = self._decode(row[0])
                if current is None or current.get("contract_defaults") != write.value:
                    conn.execute("COMMIT")
                    return False
                if write.previous is None:
                    conn.execute(
                        "UPDATE task_metadata SET metadata = json_remove(metadata, '$.contract_defaults'), "
                        "write_token = NULL, updated_at = ? WHERE task_id = ?",
                        (int(time.time() * 1000), task_id),
                    )
                else:
                    # Re-dispatch replaced an older snapshot; a failed PATCH
                    # restores it rather than erasing the last known contract.
                    conn.execute(
                        "UPDATE task_metadata SET "
                        "metadata = json_set(metadata, '$.contract_defaults', json(?)), "
                        "write_token = ?, updated_at = ? WHERE task_id = ?",
                        (
                            json.dumps(
                                write.previous,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            write.previous_token,
                            int(time.time() * 1000),
                            task_id,
                        ),
                    )
                row = conn.execute(
                    "SELECT metadata FROM task_metadata WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is not None and self._decode(row[0]) == {}:
                    conn.execute("DELETE FROM task_metadata WHERE task_id = ?", (task_id,))
                conn.execute("COMMIT")
                return True
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise MetadataUnavailable(str(exc)) from exc
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Loopback kanban REST client
# ---------------------------------------------------------------------------


@dataclass
class UpstreamResult:
    status: int
    data: Any


class WorkbenchService:
    """Dispatch/gate/metadata logic on top of the public loopback REST API."""

    def __init__(
        self,
        base_url: str,
        store: MetadataStore,
        defaults_path: Path,
        *,
        review_ttl_seconds: int = REVIEW_TTL_SECONDS,
        auto_collect_backoff_seconds: float = REVIEW_BACKOFF_SECONDS,
        inbox_cache_seconds: float = INBOX_CACHE_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.defaults_path = Path(defaults_path)
        self.review_ttl_seconds = int(review_ttl_seconds)
        self.auto_collect_backoff_seconds = float(auto_collect_backoff_seconds)
        self.inbox_cache_seconds = float(inbox_cache_seconds)
        # Header counts share one board read: a short cache plus a lock that
        # also acts as the in-flight guard for concurrent count/inbox calls.
        self._review_ids_lock = threading.Lock()
        self._review_ids_cache: Optional[tuple[float, list[str]]] = None
        # Auto-collect failures are kept per task so an inbox read can expose
        # them and the sweeper backs off instead of hammering a broken upstream.
        self._auto_collect_failures: dict[str, dict[str, Any]] = {}
        self._auto_collect_guard = threading.Lock()
        # Loopback calls made by this service carry an in-process token. The
        # middleware trusts it and skips re-gating: dispatch's own ready PATCH
        # already passed the gate before it was sent, and re-entering the gate
        # would deadlock on the per-task lock dispatch is holding.
        self.internal_token = uuid.uuid4().hex
        # One lock per task: dispatch/gates hold it from the acceptance read
        # through the forwarded state change, so update_metadata cannot clear
        # acceptance in between. These are plain Locks (not RLocks) because a
        # gate acquires them in a threadpool worker but must release them from
        # the event loop after the ASGI forward completes; plain Lock.release()
        # is the only primitive that may be called from another thread.
        self._task_locks: dict[str, threading.Lock] = {}
        self._task_locks_guard = threading.Lock()

    def _task_lock(self, task_id: str) -> threading.Lock:
        with self._task_locks_guard:
            lock = self._task_locks.get(task_id)
            if lock is None:
                lock = threading.Lock()
                self._task_locks[task_id] = lock
            return lock

    def acquire_task_locks(self, task_ids: list[str]) -> list[threading.Lock]:
        """Acquire per-task locks in a deterministic order (off the event loop).

        Sorted order prevents two multi-id bulk gates from deadlocking. On any
        failure the already-acquired locks are released.
        """
        locks: list[threading.Lock] = []
        try:
            for task_id in sorted({item for item in task_ids if isinstance(item, str) and item}):
                lock = self._task_lock(task_id)
                lock.acquire()
                locks.append(lock)
        except BaseException:
            self.release_task_locks(locks)
            raise
        return locks

    @staticmethod
    def release_task_locks(locks: list[threading.Lock]) -> None:
        for lock in reversed(locks):
            lock.release()

    # -- upstream ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        deadline: Optional[float] = None,
    ) -> UpstreamResult:
        timeout = UPSTREAM_CALL_TIMEOUT
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpstreamUnavailable("dispatch budget exhausted")
            timeout = min(UPSTREAM_CALL_TIMEOUT, remaining)
        headers = {
            "Accept": "application/json",
            "X-Workbench-Internal": self.internal_token,
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urlrequest.Request(
            self.base_url + path, data=body, headers=headers, method=method
        )
        # A fresh opener per call: loopback must never use an ambient proxy, and
        # the dispatch path can run concurrently in FastAPI's thread pool.
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                status = int(resp.status)
        except urlerror.HTTPError as exc:
            try:
                raw = exc.read()
                status = int(exc.code)
            finally:
                exc.close()
        except (urlerror.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
            raise UpstreamUnavailable(str(exc)) from exc
        data: Any = None
        if raw:
            try:
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = None
        return UpstreamResult(status=status, data=data)

    def _get_task(self, task_id: str, deadline: float) -> UpstreamResult:
        return self._request(
            "GET", "/api/plugins/kanban/tasks/" + urlparse.quote(task_id, safe=""), deadline=deadline
        )

    def _patch_status(self, task_id: str, status: str, deadline: float) -> UpstreamResult:
        return self._request(
            "PATCH",
            "/api/plugins/kanban/tasks/" + urlparse.quote(task_id, safe=""),
            payload={"status": status},
            deadline=deadline,
        )

    def _patch_task(
        self, task_id: str, payload: dict[str, Any], deadline: float
    ) -> UpstreamResult:
        return self._request(
            "PATCH",
            "/api/plugins/kanban/tasks/" + urlparse.quote(task_id, safe=""),
            payload=payload,
            deadline=deadline,
        )

    def _post_comment(self, task_id: str, body: str, deadline: float) -> UpstreamResult:
        return self._request(
            "POST",
            "/api/plugins/kanban/tasks/"
            + urlparse.quote(task_id, safe="")
            + "/comments",
            payload={"body": body, "author": VERIFY_AUTHOR},
            deadline=deadline,
        )

    def _task_detail(self, task_id: str, deadline: float) -> dict[str, Any]:
        """``GET /tasks/{id}`` with the event/comment/attachment/run detail.

        The verify page and the 48h sweeper both read the review handoff from
        here; the inbox only needs the events for the review clock.
        """
        res = self._request(
            "GET",
            "/api/plugins/kanban/tasks/" + urlparse.quote(task_id, safe=""),
            deadline=deadline,
        )
        if res.status == 404:
            raise TaskNotFound("task not found")
        if res.status != 200 or not isinstance(res.data, dict):
            raise UpstreamUnavailable("task detail unavailable")
        task = res.data.get("task")
        if not isinstance(task, dict):
            raise UpstreamUnavailable("task detail unavailable")
        return res.data

    @staticmethod
    def _latest_review_requested_event(detail: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Newest ``review_requested`` event, or None.

        The newest event starts the current review round: it resets the 48h
        clock and scopes the auto-collect record. Cards without that event have
        no trustworthy review clock, so they are never auto-collected.
        """
        events = detail.get("events")
        if not isinstance(events, list):
            return None
        latest: Optional[dict[str, Any]] = None
        latest_stamp: Optional[int] = None
        for event in events:
            if not isinstance(event, dict) or event.get("kind") != "review_requested":
                continue
            created = event.get("created_at")
            if not isinstance(created, (int, float)) or isinstance(created, bool):
                continue
            stamp = int(created)
            if latest is None or latest_stamp is None or stamp >= latest_stamp:
                latest = event
                latest_stamp = stamp
        return latest

    @classmethod
    def _latest_review_requested_at(cls, detail: dict[str, Any]) -> Optional[int]:
        event = cls._latest_review_requested_event(detail)
        if event is None:
            return None
        created = event.get("created_at")
        return int(created) if isinstance(created, (int, float)) else None

    @staticmethod
    def _review_round_marker(event: dict[str, Any]) -> str:
        """Stable marker for one review round: event id, else its timestamp."""
        event_id = event.get("id")
        if isinstance(event_id, int) and not isinstance(event_id, bool):
            return f"review:{event_id}"
        created = event.get("created_at")
        return f"review:at:{int(created) if isinstance(created, (int, float)) else 0}"

    @classmethod
    def _worker_summary(cls, detail: dict[str, Any]) -> Optional[str]:
        """The worker's handoff summary that auto-collect must not overwrite."""
        task = cls._detail_task(detail)
        summary = task.get("latest_summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        event = cls._latest_review_requested_event(detail)
        payload = event.get("payload") if isinstance(event, dict) else None
        if isinstance(payload, dict):
            value = payload.get("summary")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _detail_task(detail: dict[str, Any]) -> dict[str, Any]:
        task = detail.get("task")
        return task if isinstance(task, dict) else {}

    @staticmethod
    def _review_age_seconds(review_at: Optional[int], now: float) -> Optional[int]:
        if review_at is None:
            return None
        return max(0, int(now - review_at))

    def _board(self, deadline: float) -> dict[str, Any]:
        # ``include_archived=true`` exposes archived cards and gives this read a
        # different coalescing key from the UI's 5s polling (which omits it).
        path = "/api/plugins/kanban/board?include_archived=true"
        res = self._request("GET", path, deadline=deadline)
        if res.status != 200 or not isinstance(res.data, dict) or not isinstance(res.data.get("columns"), list):
            raise UpstreamUnavailable("board unavailable")
        return res.data

    def _ready_position(self, task_id: str, deadline: float) -> Optional[int]:
        """Ready-lane 1-based position; retry the board once when absent/stale."""
        for _attempt in range(2):
            board = self._board(deadline)
            found = self._find_on_board(board, task_id)
            if found is not None and found["status"] == "ready":
                return int(found["position"])
        return None

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _iter_board_cards(board: dict[str, Any]):
        """Yield ``(card, column_status, index)`` for dict cards on a board."""
        for column in board.get("columns") or []:
            if not isinstance(column, dict):
                continue
            column_status = column.get("name")
            for index, card in enumerate(column.get("tasks") or []):
                if isinstance(card, dict):
                    yield card, column_status, index

    @classmethod
    def _find_on_board(cls, board: dict[str, Any], task_id: str) -> Optional[dict[str, Any]]:
        for card, column_status, index in cls._iter_board_cards(board):
            if card.get("id") == task_id:
                return {
                    "status": card.get("status") or column_status,
                    "column": column_status,
                    "position": index + 1,
                }
        return None

    @staticmethod
    def _has_open_parents(task: dict[str, Any]) -> bool:
        links = task.get("links")
        if not isinstance(links, dict):
            return False
        parent_ids = links.get("parents")
        if not isinstance(parent_ids, list) or not parent_ids:
            return False
        statuses: dict[str, Any] = {}
        for linked in task.get("link_tasks") or []:
            if isinstance(linked, dict) and linked.get("id"):
                statuses[linked["id"]] = linked.get("status")
        return any(
            statuses.get(parent_id) is not None and statuses.get(parent_id) not in TERMINAL_STATUSES
            for parent_id in parent_ids
        )

    @staticmethod
    def _error_detail(result: UpstreamResult) -> str:
        if isinstance(result.data, dict):
            detail = result.data.get("detail")
            if isinstance(detail, str):
                return detail.lower()
        return ""

    def _rollback(self, write: SnapshotWrite) -> None:
        try:
            self.store.remove_snapshot(write)
        except MetadataUnavailable as exc:
            print(f"workbench: snapshot rollback failed: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def _queued(position: int, minutes_per_task: float) -> tuple[int, dict[str, Any]]:
        # Epics formula: ETA = now + minutes_per_task * position. Position 1 is
        # the first 7-minute slot (the card currently at the head), position 2
        # starts one slot later, and so on.
        try:
            eta = datetime.now() + timedelta(minutes=minutes_per_task * position)
        except (OverflowError, ValueError):
            return 500, {"ok": False, "error": "dispatch_failed"}
        return 200, {
            "ok": True,
            "position": position,
            "eta": eta.strftime("%H:%M"),
            "status": "ready",
        }

    @staticmethod
    def _fail(status: int, error: str, task_status: Optional[str] = None) -> tuple[int, dict[str, Any]]:
        body: dict[str, Any] = {"ok": False, "error": error}
        if task_status is not None:
            body["status"] = task_status
        return status, body

    def _land_in_todo(
        self, task_id: str, current_status: Optional[str], write: SnapshotWrite, deadline: float
    ) -> tuple[int, dict[str, Any]]:
        """A parent-gated card must land in ``todo`` (never ``ready``)."""
        if current_status == "todo":
            self._rollback(write)
            return self._fail(409, "blocked_by_parent", "todo")
        try:
            patched = self._patch_status(task_id, "todo", deadline)
        except UpstreamUnavailable:
            self._rollback(write)
            return self._fail(502, "upstream_unavailable")
        self._rollback(write)
        if patched.status == 404:
            return self._fail(404, "not_found")
        if patched.status != 200:
            # Do not claim a landing we did not observe.
            return self._fail(502, "upstream_unavailable")
        landed = None
        if isinstance(patched.data, dict) and isinstance(patched.data.get("task"), dict):
            landed = patched.data["task"].get("status")
        if not isinstance(landed, str) or not landed:
            # A 200 without task.status is not proof of a todo landing.
            return self._fail(502, "upstream_unavailable")
        return self._fail(409, "blocked_by_parent", landed)

    # -- endpoints --------------------------------------------------------

    def _ready_gate(self, task_id: str) -> Optional[tuple[int, dict[str, Any]]]:
        """Gate transitions that could put a card into ``ready``.

        Order: GET the card first. A nonexistent id is forwarded to upstream
        (client sees 404), not masked as 400. Existing cards then need a
        non-empty workbench ``contract.accept``. ``None`` means forward.
        """
        try:
            fetched = self._get_task(task_id, time.monotonic() + GATE_UPSTREAM_TIMEOUT)
        except UpstreamUnavailable:
            return self._fail(502, "upstream_unavailable")
        if fetched.status == 404:
            return None  # forward so the client gets the upstream 404
        if fetched.status != 200:
            return self._fail(502, "upstream_unavailable")
        try:
            metadata = self.store.get(task_id)
        except MetadataUnavailable:
            return self._fail(503, "metadata_unavailable")
        if extract_accept(metadata):
            return None
        return self._fail(400, "missing_acceptance")

    patch_ready_gate = _ready_gate
    reclaim_gate = _ready_gate

    def resolve_run_task(
        self, run_id: str
    ) -> tuple[Optional[str], Optional[tuple[int, dict[str, Any]]]]:
        """Map a run id to its task through the public ``GET /runs/{run_id}``.

        ``POST /runs/{run_id}/terminate`` reclaims the task and can therefore
        put it back into ``ready``. The workbench cannot gate that request
        without the mapping, so an unavailable/unreadable mapping fails closed
        with 403 ``gate_unavailable`` instead of forwarding an ungated reclaim.
        """
        try:
            fetched = self._request(
                "GET",
                "/api/plugins/kanban/runs/" + urlparse.quote(str(run_id), safe=""),
                deadline=time.monotonic() + GATE_UPSTREAM_TIMEOUT,
            )
        except UpstreamUnavailable:
            return None, (
                403,
                {
                    "ok": False,
                    "error": "gate_unavailable",
                    "detail": "cannot resolve run's task; terminate blocked",
                },
            )
        if fetched.status == 404:
            return None, (
                403,
                {
                    "ok": False,
                    "error": "gate_unavailable",
                    "detail": "run not found; terminate blocked",
                },
            )
        run = fetched.data.get("run") if isinstance(fetched.data, dict) else None
        task_id = run.get("task_id") if isinstance(run, dict) else None
        if (
            fetched.status != 200
            or not isinstance(task_id, str)
            or not task_id.strip()
        ):
            return None, (
                403,
                {
                    "ok": False,
                    "error": "gate_unavailable",
                    "detail": "cannot resolve run's task; terminate blocked",
                },
            )
        return task_id.strip(), None

    def bulk_ready_gate(self, task_ids: list[str]) -> Optional[tuple[int, dict[str, Any]]]:
        """Reject the whole bulk request when any id lacks acceptance."""
        unique = [task_id for task_id in dict.fromkeys(task_ids) if task_id]
        if not unique:
            return None
        try:
            rows = self.store.get_many(unique)
        except MetadataUnavailable:
            return self._fail(503, "metadata_unavailable")
        for task_id in unique:
            if not extract_accept(rows.get(task_id)):
                return self._fail(400, "missing_acceptance")
        return None

    @staticmethod
    def create_ready_gate() -> tuple[int, dict[str, Any]]:
        """``POST /tasks`` without ``triage`` could land ready; reject it."""
        return 400, {"ok": False, "error": "missing_acceptance"}

    def metadata_for(self, task_ids: list[str]) -> tuple[int, dict[str, Any]]:
        try:
            return 200, self.store.get_many(task_ids)
        except MetadataUnavailable:
            return self._fail(503, "metadata_unavailable")

    # -- verification inbox / 48h sweeper ---------------------------------

    def _review_ids_on_board(self, deadline: float) -> list[str]:
        board = self._board(deadline)
        ids: list[str] = []
        for card, column_status, _index in self._iter_board_cards(board):
            task_id = card.get("id")
            status = card.get("status") or column_status
            if status == REVIEW_STATUS and isinstance(task_id, str) and task_id:
                ids.append(task_id)
        return ids

    def _invalidate_review_cache(self) -> None:
        with self._review_ids_lock:
            self._review_ids_cache = None

    def _review_ids_cached(self) -> list[str]:
        """Short cache + in-flight guard for header counts and inbox reads.

        Holding the lock across the board call means concurrent 5s polls from
        the App and the verify page share one upstream read instead of stacking
        up. The cache is intentionally tiny (1s) so a new review card shows up
        on the next poll.
        """
        with self._review_ids_lock:
            now = time.monotonic()
            cached = self._review_ids_cache
            if (
                cached is not None
                and self.inbox_cache_seconds > 0
                and now - cached[0] <= self.inbox_cache_seconds
            ):
                return list(cached[1])
            ids = self._review_ids_on_board(now + INBOX_BUDGET_SECONDS)
            if self.inbox_cache_seconds > 0:
                self._review_ids_cache = (now, list(ids))
            return ids

    def _auto_collect_error_for(self, task_id: str) -> Optional[dict[str, Any]]:
        """Active auto-collect failure/backoff for an inbox item, if any."""
        now = time.monotonic()
        with self._auto_collect_guard:
            entry = self._auto_collect_failures.get(task_id)
            if entry is None:
                return None
            remaining = float(entry["until"]) - now
            if remaining <= 0:
                return None
            return {
                "message": entry["message"],
                "attempts": entry["attempts"],
                "retry_in_seconds": max(0, int(math.ceil(remaining))),
            }

    def _in_auto_collect_backoff(self, task_id: str) -> bool:
        now = time.monotonic()
        with self._auto_collect_guard:
            entry = self._auto_collect_failures.get(task_id)
            if entry is None:
                return False
            if float(entry["until"]) <= now:
                self._auto_collect_failures.pop(task_id, None)
                return False
            return True

    def _record_auto_collect_failure(self, task_id: str, message: str) -> None:
        with self._auto_collect_guard:
            previous = self._auto_collect_failures.get(task_id)
            attempts = int(previous["attempts"]) + 1 if previous else 1
            self._auto_collect_failures[task_id] = {
                "message": str(message)[:500],
                "attempts": attempts,
                "until": time.monotonic() + max(0.0, self.auto_collect_backoff_seconds),
            }

    def _clear_auto_collect_failure(self, task_id: str) -> None:
        with self._auto_collect_guard:
            self._auto_collect_failures.pop(task_id, None)

    @staticmethod
    def _parent_ids(detail: dict[str, Any]) -> list[str]:
        links = detail.get("links")
        parents = links.get("parents") if isinstance(links, dict) else None
        if not isinstance(parents, list):
            return []
        return [value for value in parents if isinstance(value, str) and value]

    def _inbox_item(
        self, task_id: str, detail: dict[str, Any], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        task = self._detail_task(detail)
        review_at = self._latest_review_requested_at(detail)
        age = self._review_age_seconds(review_at, time.time())
        return {
            "id": task_id,
            "title": task.get("title") if isinstance(task.get("title"), str) else "",
            "status": task.get("status"),
            "assignee": task.get("assignee"),
            "priority": task.get("priority"),
            "created_at": task.get("created_at"),
            "completed_at": task.get("completed_at"),
            "result": task.get("result"),
            "latest_summary": task.get("latest_summary"),
            "metadata": metadata,
            "review_requested_at": review_at,
            "review_age_seconds": age,
            "auto_collect_due": age is not None and age >= self.review_ttl_seconds,
            "auto_collect_error": self._auto_collect_error_for(task_id),
        }

    # -- notification board slice (Story 2.2) ------------------------------

    @staticmethod
    def _int_field(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value)

    def board_snapshot(self) -> tuple[int, dict[str, Any]]:
        """Read-only board + detail slice for the notification engine.

        Only ``blocked`` / ``review`` cards are returned. Blocked cards need
        nothing beyond the board (block_kind, block_recurrences, progress);
        review cards get ``GET /tasks/{id}`` for the review clock and parent
        links used by acceptance-slot bundling. A board or detail failure is a
        502: the engine then keeps its previous snapshot and sends nothing.
        """
        deadline = time.monotonic() + INBOX_BUDGET_SECONDS
        try:
            board = self._board(deadline)
        except UpstreamUnavailable:
            return self._fail(502, "upstream_unavailable")

        cards: dict[str, dict[str, Any]] = {}
        review_ids: list[str] = []
        for card, column_status, _index in self._iter_board_cards(board):
            task_id = card.get("id")
            status = card.get("status") or column_status
            if status not in (BLOCKED_STATUS, REVIEW_STATUS):
                continue
            if not isinstance(task_id, str) or not task_id:
                continue
            block_kind = card.get("block_kind")
            progress = card.get("progress")
            entry: dict[str, Any] = {
                "id": task_id,
                "title": card.get("title") if isinstance(card.get("title"), str) else "",
                "status": status,
                "block_kind": block_kind if isinstance(block_kind, str) and block_kind else None,
                "block_recurrences": self._int_field(card.get("block_recurrences")),
                "progress": progress if isinstance(progress, dict) else None,
                "review_requested_at": None,
                "parent_ids": [],
            }
            cards[task_id] = entry
            if status == REVIEW_STATUS:
                review_ids.append(task_id)

        if review_ids:
            details: dict[str, dict[str, Any]] = {}
            workers = min(MAX_INBOX_WORKERS, len(review_ids))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._task_detail, task_id, deadline): task_id
                    for task_id in review_ids
                }
                for future in concurrent.futures.as_completed(futures):
                    task_id = futures[future]
                    try:
                        details[task_id] = future.result()
                    except TaskNotFound:
                        # Gone between /board and the detail read: resolved.
                        continue
                    except Exception:
                        return self._fail(502, "upstream_unavailable")
            for task_id in review_ids:
                detail = details.get(task_id)
                if detail is None:
                    cards.pop(task_id, None)
                    continue
                task = self._detail_task(detail)
                if task.get("status") != REVIEW_STATUS:
                    # Moved between the board read and now; next round sees it.
                    cards.pop(task_id, None)
                    continue
                entry = cards[task_id]
                entry["review_requested_at"] = self._latest_review_requested_at(detail)
                entry["parent_ids"] = self._parent_ids(detail)

        return 200, {
            "ok": True,
            "generated_at": int(time.time()),
            "review_ttl_seconds": self.review_ttl_seconds,
            "tasks": list(cards.values()),
        }

    def _read_status(
        self, task_id: str
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        """Fresh status read used after a failed auto-collect PATCH."""
        try:
            detail = self._task_detail(task_id, time.monotonic() + REVIEW_BUDGET_SECONDS)
        except TaskNotFound:
            return "missing", None
        except UpstreamUnavailable:
            return None, None
        task = self._detail_task(detail)
        status = task.get("status")
        return (status if isinstance(status, str) else None), detail

    @staticmethod
    def _has_auto_record(detail: dict[str, Any], marker: str) -> bool:
        comments = detail.get("comments")
        return isinstance(comments, list) and any(
            isinstance(comment, dict)
            and comment.get("author") == VERIFY_AUTHOR
            and isinstance(comment.get("body"), str)
            and comment["body"].startswith(AUTO_COLLECT_COMMENT)
            and marker in comment["body"]
            for comment in comments
        )

    def _auto_collect_comment(
        self, task_id: str, detail: dict[str, Any], event: dict[str, Any]
    ) -> bool:
        """Write the 48h record after the card landed ``done``.

        The marker scopes idempotency to this review round, so a re-review that
        becomes overdue again writes a new record instead of being suppressed
        by the previous round's comment.
        """
        marker = self._review_round_marker(event)
        body = f"{AUTO_COLLECT_COMMENT}（{marker}）"
        if self._has_auto_record(detail, marker):
            return True
        for attempt in range(2):
            try:
                posted = self._post_comment(
                    task_id, body, time.monotonic() + REVIEW_BUDGET_SECONDS
                )
            except UpstreamUnavailable as exc:
                if attempt == 1:
                    print(
                        f"workbench: auto-collect comment failed for {task_id}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return False
                # A lost response may already have written the comment; re-read
                # before retrying so the record cannot be duplicated.
                try:
                    fresh = self._task_detail(
                        task_id, time.monotonic() + REVIEW_BUDGET_SECONDS
                    )
                    if self._has_auto_record(fresh, marker):
                        return True
                except UpstreamUnavailable:
                    pass
                continue
            if posted.status == 200:
                return True
            if attempt == 1:
                print(
                    f"workbench: auto-collect comment failed for {task_id}: {posted.status}",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            try:
                fresh = self._task_detail(
                    task_id, time.monotonic() + REVIEW_BUDGET_SECONDS
                )
                if self._has_auto_record(fresh, marker):
                    return True
            except UpstreamUnavailable:
                pass
        return False

    def _auto_collect_locked(
        self, task_id: str, detail: dict[str, Any]
    ) -> tuple[str, Optional[str]]:
        """Auto-accept one overdue review card. Caller holds the task lock.

        Returns ``(outcome, error)`` where outcome is ``collected``,
        ``skipped`` (not due / moved elsewhere / no review event) or
        ``failed`` (PATCH refused/failed; a backoff is recorded). The record
        comment is written only after the PATCH actually lands ``done``.
        """
        task = self._detail_task(detail)
        if task.get("status") != REVIEW_STATUS:
            return "skipped", None
        event = self._latest_review_requested_event(detail)
        if event is None:
            return "skipped", None
        review_at = self._latest_review_requested_at(detail)
        if review_at is None or time.time() - review_at < self.review_ttl_seconds:
            return "skipped", None

        def landed_done(result: UpstreamResult) -> bool:
            if result.status != 200:
                return False
            landed = self._detail_task(result.data) if isinstance(result.data, dict) else {}
            return landed.get("status") == "done"

        # Try to keep the worker's handoff as the latest summary. This upstream
        # synthesizes "review approved" when a review completion omits the
        # summary, so the worker summary is re-sent when one exists; with no
        # worker summary the completion is summary-less.
        worker_summary = self._worker_summary(detail)
        first_payload: dict[str, Any] = {"status": "done"}
        if worker_summary:
            first_payload["summary"] = worker_summary
        try:
            patched = self._patch_task(
                task_id, first_payload, time.monotonic() + REVIEW_BUDGET_SECONDS
            )
        except UpstreamUnavailable as exc:
            self._record_auto_collect_failure(task_id, f"PATCH failed: {exc}")
            return "failed", str(exc)

        if landed_done(patched):
            self._clear_auto_collect_failure(task_id)
            self._auto_collect_comment(task_id, detail, event)
            return "collected", None

        status_after, detail_after = self._read_status(task_id)
        if status_after in (None, "done", "missing"):
            # Unknown read, someone else completed it, or it disappeared: do
            # not fabricate a collection record.
            if status_after == "done" or status_after == "missing":
                self._clear_auto_collect_failure(task_id)
                return "skipped", None
            self._record_auto_collect_failure(task_id, "status re-read failed")
            return "failed", "status re-read failed"
        if status_after != REVIEW_STATUS:
            self._clear_auto_collect_failure(task_id)
            return "skipped", None

        # Still in review. An upstream that refuses a summary-less completion
        # gets one retry with a distinct audit summary (the worker summary
        # remains in the review_requested event/run).
        retryable = patched.status in (400, 422) or (
            patched.status == 200 and not landed_done(patched)
        )
        if retryable or patched.status == 409:
            try:
                patched_retry = self._patch_task(
                    task_id,
                    {"status": "done", "summary": AUTO_COLLECT_AUDIT_SUMMARY},
                    time.monotonic() + REVIEW_BUDGET_SECONDS,
                )
            except UpstreamUnavailable as exc:
                self._record_auto_collect_failure(task_id, f"PATCH retry failed: {exc}")
                return "failed", str(exc)
            if landed_done(patched_retry):
                self._clear_auto_collect_failure(task_id)
                self._auto_collect_comment(
                    task_id, detail_after or detail, event
                )
                return "collected", None
            self._record_auto_collect_failure(
                task_id, f"PATCH refused ({patched.status}/{patched_retry.status})"
            )
            return "failed", "PATCH refused"

        self._record_auto_collect_failure(task_id, f"PATCH failed ({patched.status})")
        return "failed", f"PATCH failed ({patched.status})"

    def _auto_collect(self, task_id: str) -> tuple[str, Optional[str]]:
        """Lock, re-read, and auto-accept a card whose review clock expired."""
        if self._in_auto_collect_backoff(task_id):
            return "skipped", None
        with self._task_lock(task_id):
            if self._in_auto_collect_backoff(task_id):
                return "skipped", None
            try:
                detail = self._task_detail(task_id, time.monotonic() + REVIEW_BUDGET_SECONDS)
            except TaskNotFound:
                self._clear_auto_collect_failure(task_id)
                return "skipped", None
            except UpstreamUnavailable as exc:
                self._record_auto_collect_failure(task_id, f"read failed: {exc}")
                return "failed", str(exc)
            return self._auto_collect_locked(task_id, detail)

    def sweep_reviews(self) -> tuple[int, dict[str, Any]]:
        """Auto-accept every review card older than the TTL.

        Shared by the guarded sweep endpoint and the daemon thread. A failed
        card keeps its review status, gets a backoff window, and is reported in
        ``failed_ids``; one failure never aborts the rest of the sweep.
        """
        try:
            review_ids = self._review_ids_on_board(
                time.monotonic() + INBOX_BUDGET_SECONDS
            )
        except UpstreamUnavailable:
            return 502, {"ok": False, "error": "upstream_unavailable"}
        collected: list[str] = []
        failed: list[str] = []
        skipped = 0
        for task_id in review_ids:
            outcome, _error = self._auto_collect(task_id)
            if outcome == "collected":
                collected.append(task_id)
            elif outcome == "failed":
                failed.append(task_id)
            else:
                skipped += 1
        self._invalidate_review_cache()
        return 200, {
            "ok": True,
            "collected": len(collected),
            "failed": len(failed),
            "skipped": skipped,
            "collected_ids": collected,
            "failed_ids": failed,
        }

    def inbox_count(self) -> tuple[int, dict[str, Any]]:
        """Header count: one cached board read, no per-card detail calls."""
        try:
            review_ids = self._review_ids_cached()
        except UpstreamUnavailable:
            return self._fail(502, "upstream_unavailable")
        return 200, {
            "ok": True,
            "count": len(review_ids),
            "review_ttl_seconds": self.review_ttl_seconds,
        }

    def inbox(self) -> tuple[int, dict[str, Any]]:
        """Review queue sorted by the earliest ``review_requested`` event.

        Pure read: expired cards are left for ``POST /reviews/sweep`` (called
        by the UI) or the background sweeper. Card details are fetched in
        parallel under one overall deadline; one failed detail is skipped and
        reported in ``failed_ids`` instead of blanking the whole queue.
        """
        try:
            review_ids = self._review_ids_cached()
        except UpstreamUnavailable:
            return self._fail(502, "upstream_unavailable")
        try:
            metadata_by_id = self.store.get_many(review_ids)
        except MetadataUnavailable:
            return self._fail(503, "metadata_unavailable")

        details: dict[str, dict[str, Any]] = {}
        failed_ids: list[str] = []
        if review_ids:
            deadline = time.monotonic() + INBOX_BUDGET_SECONDS
            workers = min(MAX_INBOX_WORKERS, len(review_ids))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._task_detail, task_id, deadline): task_id
                    for task_id in review_ids
                }
                for future in concurrent.futures.as_completed(futures):
                    task_id = futures[future]
                    try:
                        details[task_id] = future.result()
                    except TaskNotFound:
                        # Already gone; not a failure worth surfacing.
                        continue
                    except UpstreamUnavailable:
                        failed_ids.append(task_id)
                    except Exception:  # a bug in one read must not blank the queue
                        failed_ids.append(task_id)

        items: list[dict[str, Any]] = []
        for task_id in review_ids:
            detail = details.get(task_id)
            if detail is None:
                continue
            if self._detail_task(detail).get("status") != REVIEW_STATUS:
                continue
            items.append(self._inbox_item(task_id, detail, metadata_by_id.get(task_id, {})))

        # Oldest review first (clear the backlog first); cards without a
        # review_requested event sort last because their age is unknown.
        items.sort(
            key=lambda item: (
                item["review_requested_at"] is None,
                item["review_requested_at"] or item["created_at"] or 0,
                item["id"],
            )
        )
        failed_ids.sort()
        return 200, {
            "ok": True,
            "count": len(items),
            "review_ttl_seconds": self.review_ttl_seconds,
            "tasks": items,
            "failed_ids": failed_ids,
        }

    @staticmethod
    def _has_round_comment(detail: dict[str, Any], reason: str) -> bool:
        """True when this review round already has the same workbench comment.

        Re-sending the identical rejection for the same round would duplicate
        the comment after a retry; the check is scoped to comments created at
        or after the latest ``review_requested`` event.
        """
        review_at = WorkbenchService._latest_review_requested_at(detail)
        if review_at is None:
            return False
        comments = detail.get("comments")
        if not isinstance(comments, list):
            return False
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            if comment.get("author") != VERIFY_AUTHOR or comment.get("body") != reason:
                continue
            created = comment.get("created_at")
            if isinstance(created, (int, float)) and not isinstance(created, bool):
                if int(created) >= review_at:
                    return True
        return False

    def _verify_patch_response(
        self,
        task_id: str,
        patched: UpstreamResult,
        action: str,
        expected: str,
        comment_written: bool,
    ) -> tuple[int, dict[str, Any]]:
        """Map a PATCH result, re-reading the card on anything unexpected.

        If the card is still ``review`` after a failed/odd PATCH, report 502
        (the action did not land) rather than pretending it left the queue.
        ``comment_written`` is present on every outcome.
        """
        if patched.status == 404:
            return 404, {
                "ok": False,
                "error": "not_found",
                "comment_written": comment_written,
            }
        landed = self._detail_task(patched.data) if isinstance(patched.data, dict) else {}
        actual = landed.get("status")
        if patched.status == 200 and actual == expected:
            return 200, {
                "ok": True,
                "action": action,
                "status": actual,
                "comment_written": comment_written,
            }

        status_after, _detail = self._read_status(task_id)
        if status_after == "missing":
            return 404, {
                "ok": False,
                "error": "not_found",
                "comment_written": comment_written,
            }
        if status_after is None:
            return 502, {
                "ok": False,
                "error": "upstream_unavailable",
                "comment_written": comment_written,
            }
        if status_after == REVIEW_STATUS:
            body: dict[str, Any] = {
                "ok": False,
                "error": "upstream_unavailable",
                "status": REVIEW_STATUS,
                "comment_written": comment_written,
            }
            if patched.status:
                body["upstream_status"] = patched.status
            return 502, body
        return 409, {
            "ok": False,
            "error": "not_in_review",
            "status": status_after,
            "comment_written": comment_written,
        }

    def verify(
        self,
        task_id: str,
        action: Any,
        reason: Any = None,
        expected_review_at: Any = None,
    ) -> tuple[int, dict[str, Any]]:
        """``accept`` / ``reject`` / ``skip`` one review card.

        ``accept`` = PATCH done + summary; ``reject`` = comment first, then
        PATCH triage (a failed comment never changes the status); ``skip``
        leaves the upstream card untouched. The frontend and this endpoint
        both require a non-empty reason (<= 2000 chars) for reject.

        ``expected_review_at`` scopes a notification action to the review
        round it was rendered from; a newer ``review_requested`` rejects the
        action with 409 ``stale_review`` instead of acting on the new round.
        """
        normalized = task_id.strip() if isinstance(task_id, str) else ""
        if not normalized:
            return self._fail(400, "invalid_request")
        if action not in VERIFY_ACTIONS:
            return self._fail(400, "invalid_action")
        expected: Optional[int] = None
        if expected_review_at is not None:
            if isinstance(expected_review_at, bool) or not isinstance(
                expected_review_at, (int, float)
            ):
                return self._fail(400, "invalid_request")
            expected = int(expected_review_at)
        rejection_reason: Optional[str] = None
        if action == "reject":
            if not isinstance(reason, str) or not reason.strip():
                return self._fail(400, "reason_required")
            rejection_reason = reason.strip()
            if len(rejection_reason) > MAX_REJECT_REASON_CHARS:
                return self._fail(400, "reason_too_long")

        with self._task_lock(normalized):
            deadline = time.monotonic() + REVIEW_BUDGET_SECONDS
            try:
                detail = self._task_detail(normalized, deadline)
            except TaskNotFound:
                return 404, {
                    "ok": False,
                    "error": "not_found",
                    "comment_written": False,
                }
            except UpstreamUnavailable:
                return self._fail(502, "upstream_unavailable")
            current = self._detail_task(detail).get("status")
            if current != REVIEW_STATUS:
                return 409, {
                    "ok": False,
                    "error": "not_in_review",
                    "status": current if isinstance(current, str) else None,
                    "comment_written": False,
                }
            latest_review_at = self._latest_review_requested_at(detail)
            if expected is not None and latest_review_at != expected:
                return 409, {
                    "ok": False,
                    "error": "stale_review",
                    "status": REVIEW_STATUS,
                    "comment_written": False,
                    "review_requested_at": latest_review_at,
                }
            if action == "skip":
                return 200, {
                    "ok": True,
                    "action": "skip",
                    "status": REVIEW_STATUS,
                    "comment_written": False,
                }

            if action == "accept":
                try:
                    patched = self._patch_task(
                        normalized,
                        {"status": "done", "summary": ACCEPT_SUMMARY},
                        deadline,
                    )
                except UpstreamUnavailable:
                    return self._fail(502, "upstream_unavailable")
                return self._verify_patch_response(
                    normalized, patched, "accept", "done", False
                )

            # reject: write the reason comment first; only then move the card
            # back to triage. A comment failure leaves it in review. A retry
            # of the same round with the same reason reuses the comment.
            comment_written = self._has_round_comment(detail, rejection_reason or "")
            if not comment_written:
                try:
                    commented = self._post_comment(
                        normalized, rejection_reason or "", deadline
                    )
                except UpstreamUnavailable:
                    return self._fail(502, "upstream_unavailable")
                if commented.status == 404:
                    return 404, {
                        "ok": False,
                        "error": "not_found",
                        "comment_written": False,
                    }
                if commented.status != 200:
                    return 502, {
                        "ok": False,
                        "error": "upstream_unavailable",
                        "comment_written": False,
                    }
                comment_written = True
            try:
                patched = self._patch_status(normalized, "triage", deadline)
            except UpstreamUnavailable:
                return 502, {
                    "ok": False,
                    "error": "upstream_unavailable",
                    "comment_written": comment_written,
                }
            return self._verify_patch_response(
                normalized, patched, "reject", "triage", comment_written
            )

    @staticmethod
    def _upstream_detail(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is None:
            return ""
        try:
            return json.dumps(detail, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(detail)

    def create_task(
        self, title: Any, bucket: Any = "today", idempotency_key: Any = None
    ) -> tuple[int, dict[str, Any]]:
        """Create a triage card upstream, then store its todo metadata.

        ``idempotency_key`` is forwarded to upstream, which returns the
        existing non-archived task for a repeated key. The follow-up GET
        verifies the idempotent hit is still a triage card. The two writes are
        not atomic: a metadata failure leaves a triage card with no bucket and
        returns 503 plus the task id, so an idempotent retry can finish the
        metadata without creating a duplicate.
        """
        if not isinstance(title, str) or not title.strip():
            return self._fail(400, "invalid_title")
        if bucket not in BUCKETS:
            return self._fail(400, "invalid_bucket")
        key: Optional[str] = None
        if idempotency_key is not None:
            if (
                not isinstance(idempotency_key, str)
                or not idempotency_key.strip()
                or len(idempotency_key) > MAX_IDEMPOTENCY_KEY
            ):
                return self._fail(400, "invalid_request")
            key = idempotency_key.strip()

        payload: dict[str, Any] = {"title": title.strip(), "triage": True}
        if key is not None:
            payload["idempotency_key"] = key
        try:
            created = self._request("POST", CREATE_TASK_PATH, payload=payload)
        except UpstreamUnavailable:
            return self._fail(502, "upstream_unavailable")

        if 400 <= created.status < 500:
            body: dict[str, Any] = {
                "ok": False,
                "error": "invalid_request",
                "upstream_status": created.status,
            }
            detail = self._upstream_detail(created.data)
            if detail:
                body["detail"] = detail
            return 400, body
        if created.status != 200 or not isinstance(created.data, dict):
            body = {"ok": False, "error": "upstream_unavailable"}
            if created.status:
                body["upstream_status"] = created.status
            return 502, body
        task = created.data.get("task")
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
            return self._fail(502, "upstream_unavailable")
        task_id = task["id"]

        # A repeated idempotency key can return a card that has since moved on;
        # only report success when the card is still the promised triage card.
        try:
            fetched = self._get_task(task_id, time.monotonic() + GATE_UPSTREAM_TIMEOUT)
        except UpstreamUnavailable:
            return self._fail(502, "upstream_unavailable")
        if fetched.status == 404:
            return self._fail(404, "not_found")
        if fetched.status != 200 or not isinstance(fetched.data, dict):
            return self._fail(502, "upstream_unavailable")
        fresh = fetched.data.get("task")
        if not isinstance(fresh, dict):
            return self._fail(502, "upstream_unavailable")
        actual = fresh.get("status")
        if actual != "triage":
            return self._fail(502, "upstream_unavailable", actual if isinstance(actual, str) else None)

        # An idempotent retry may hit a card whose requirements were already
        # edited. Only fill *missing* keys: never overwrite an existing bucket
        # or a non-empty contract (accept/input/budget).
        with self._task_lock(task_id):
            try:
                existing = self.store.get(task_id)
            except MetadataUnavailable:
                return 503, {"ok": False, "error": "metadata_unavailable", "task_id": task_id}

            fill: dict[str, Any] = {}
            if not isinstance(existing, dict) or "bucket" not in existing:
                fill["bucket"] = bucket
            existing_contract = existing.get("contract") if isinstance(existing, dict) else None
            if not isinstance(existing_contract, dict):
                fill["contract"] = {"input": "", "accept": "", "ready": False}
            else:
                contract_fill = {
                    key: default
                    for key, default in (("input", ""), ("accept", ""), ("ready", False))
                    if key not in existing_contract
                }
                if contract_fill:
                    fill["contract"] = contract_fill

            if not fill and isinstance(existing, dict):
                return 200, {"ok": True, "task": fresh, "metadata": existing}
            try:
                merged = self.store.merge(task_id, fill)
            except MetadataUnavailable:
                return 503, {"ok": False, "error": "metadata_unavailable", "task_id": task_id}
        return 200, {"ok": True, "task": fresh, "metadata": merged}

    def update_metadata(self, task_id: str, raw: Any) -> tuple[int, dict[str, Any]]:
        """Validate and merge a client metadata patch (bucket / contract).

        The upstream task must exist (no arbitrary-id upsert). The per-task
        lock serialises this write with dispatch for the same card, so
        acceptance cannot be cleared between the dispatch gate and its PATCH.
        """
        normalized = task_id.strip() if isinstance(task_id, str) else ""
        if not normalized:
            return self._fail(400, "invalid_metadata")
        with self._task_lock(normalized):
            try:
                fetched = self._get_task(normalized, time.monotonic() + GATE_UPSTREAM_TIMEOUT)
            except UpstreamUnavailable:
                return self._fail(502, "upstream_unavailable")
            if fetched.status == 404:
                return self._fail(404, "not_found")
            if (
                fetched.status != 200
                or not isinstance(fetched.data, dict)
                or not isinstance(fetched.data.get("task"), dict)
            ):
                return self._fail(502, "upstream_unavailable")
            patch, error = validate_metadata_patch(raw)
            if patch is None:
                return self._fail(400, error or "invalid_metadata")
            try:
                merged = self.store.merge(normalized, patch)
            except MetadataUnavailable:
                return self._fail(503, "metadata_unavailable")
            return 200, {"ok": True, "metadata": merged}

    def _landing_error(
        self, actual: str, task: dict[str, Any], detail: str = ""
    ) -> tuple[int, dict[str, Any]]:
        if actual in TERMINAL_STATUSES:
            return self._fail(409, "not_dispatchable", actual)
        if actual == "todo" and (
            self._has_open_parents(task)
            or "blocked by parent" in detail
            or "unsatisfied parent" in detail
        ):
            return self._fail(409, "blocked_by_parent", actual)
        return self._fail(409, "not_ready", actual)

    def dispatch(self, task_id: str) -> tuple[int, dict[str, Any]]:
        # The lock spans the whole gate-read -> PATCH -> landing window, so a
        # concurrent update_metadata for this card cannot clear acceptance
        # after the gate has approved it.
        with self._task_lock(task_id):
            return self._dispatch_locked(task_id)

    def _acceptance_after_landing(
        self, task_id: str, write: SnapshotWrite
    ) -> Optional[tuple[int, dict[str, Any]]]:
        """Defense-in-depth re-check after a ready landing.

        The per-task lock covers our own writes; this catches an out-of-band
        writer (direct DB edit / another process). A metadata read failure is
        not grounds to fail a dispatch that already landed.
        """
        try:
            metadata = self.store.get(task_id)
        except MetadataUnavailable:
            return None
        if extract_accept(metadata):
            return None
        self._rollback(write)
        return self._fail(400, "missing_acceptance", "ready")

    def _dispatch_locked(self, task_id: str) -> tuple[int, dict[str, Any]]:
        deadline = time.monotonic() + DISPATCH_BUDGET_SECONDS

        # 1. Card first: running and terminal cards do not need acceptance.
        try:
            fetched = self._get_task(task_id, deadline)
        except UpstreamUnavailable:
            return self._fail(502, "upstream_unavailable")
        if fetched.status == 404:
            return self._fail(404, "not_found")
        if fetched.status != 200 or not isinstance(fetched.data, dict):
            return self._fail(502, "upstream_unavailable")
        task = fetched.data.get("task")
        if not isinstance(task, dict):
            return self._fail(502, "upstream_unavailable")

        status = task.get("status")
        if status == "running":
            return 200, {"ok": True, "position": 0, "eta": None, "status": "running"}
        if status in TERMINAL_STATUSES:
            return self._fail(409, "not_dispatchable", status)

        # 2. Acceptance, defaults, snapshot.
        try:
            metadata = self.store.get(task_id)
        except MetadataUnavailable:
            return self._fail(503, "metadata_unavailable")
        if not extract_accept(metadata):
            return self._fail(400, "missing_acceptance")

        try:
            defaults = load_defaults(self.defaults_path)
        except DefaultsError:
            return self._fail(500, "invalid_defaults")

        snapshot = build_contract_snapshot(defaults, metadata)
        minutes = defaults["minutes_per_task"]
        # Lifecycle: a re-dispatch from a non-ready/non-running status is a fresh
        # dispatch and re-snapshots the effective defaults (new write_token). A
        # ready re-dispatch keeps the existing snapshot and only fills a missing
        # one (e.g. a card put into ready by a legacy direct PATCH).
        try:
            write = self.store.write_snapshot(
                task_id, snapshot, overwrite=status != "ready"
            )
        except MetadataUnavailable:
            return self._fail(503, "metadata_unavailable")

        # Already ready: never PATCH again and never overwrite the snapshot;
        # only recompute the current queue position.
        if status == "ready":
            gate = self._acceptance_after_landing(task_id, write)
            if gate is not None:
                return gate
            try:
                position = self._ready_position(task_id, deadline)
            except UpstreamUnavailable:
                return self._fail(502, "upstream_unavailable")
            if position is None:
                return self._fail(502, "upstream_unavailable")
            return self._queued(position, minutes)

        try:
            patched = self._patch_status(task_id, "ready", deadline)
        except UpstreamUnavailable:
            self._rollback(write)
            return self._fail(502, "upstream_unavailable")

        if patched.status == 404:
            self._rollback(write)
            return self._fail(404, "not_found")
        if patched.status >= 500:
            self._rollback(write)
            return self._fail(502, "upstream_unavailable")

        if patched.status != 200:
            detail = self._error_detail(patched)
            if self._has_open_parents(task) or "blocked by parent" in detail or "unsatisfied parent" in detail:
                return self._land_in_todo(task_id, status, write, deadline)
            # The PATCH was refused; use a fresh task read (not /board) to
            # report the actual status without inventing a landing.
            try:
                landed = self._get_task(task_id, deadline)
            except UpstreamUnavailable:
                self._rollback(write)
                return self._fail(502, "upstream_unavailable")
            if landed.status == 404:
                self._rollback(write)
                return self._fail(404, "not_found")
            if landed.status != 200 or not isinstance(landed.data, dict):
                self._rollback(write)
                return self._fail(502, "upstream_unavailable")
            landed_task = landed.data.get("task")
            actual = landed_task.get("status") if isinstance(landed_task, dict) else None
            if not isinstance(actual, str) or not actual:
                self._rollback(write)
                return self._fail(502, "upstream_unavailable")
            if actual == "ready":
                gate = self._acceptance_after_landing(task_id, write)
                if gate is not None:
                    return gate
                try:
                    position = self._ready_position(task_id, deadline)
                except UpstreamUnavailable:
                    return self._fail(502, "upstream_unavailable")
                if position is None:
                    return self._fail(502, "upstream_unavailable")
                return self._queued(position, minutes)
            self._rollback(write)
            return self._landing_error(actual, task, detail)

        # 3. PATCH succeeded; trust a fresh GET /tasks/{id} as landing truth.
        try:
            landed = self._get_task(task_id, deadline)
        except UpstreamUnavailable:
            # PATCH succeeded but landing is unobservable; keep the snapshot.
            return self._fail(502, "upstream_unavailable")
        if landed.status == 404:
            self._rollback(write)
            return self._fail(404, "not_found")
        if landed.status != 200 or not isinstance(landed.data, dict):
            return self._fail(502, "upstream_unavailable")
        landed_task = landed.data.get("task")
        actual = landed_task.get("status") if isinstance(landed_task, dict) else None
        if not isinstance(actual, str) or not actual:
            return self._fail(502, "upstream_unavailable")

        if actual == "ready":
            gate = self._acceptance_after_landing(task_id, write)
            if gate is not None:
                return gate
            try:
                position = self._ready_position(task_id, deadline)
            except UpstreamUnavailable:
                return self._fail(502, "upstream_unavailable")
            if position is None:
                # Ready per the task read but absent/stale on two board reads:
                # report failure rather than fabricate a position.
                return self._fail(502, "upstream_unavailable")
            return self._queued(position, minutes)

        self._rollback(write)
        return self._landing_error(actual, task)


class ReviewSweeper:
    """Daemon thread that auto-accepts review cards older than 48h.

    Every inbox read also performs the same fallback sweep, so the thread is
    only needed to unclog the queue when nobody has the verify page open.
    """

    def __init__(self, service: WorkbenchService, interval_seconds: float) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="workbench-review-sweeper", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # First pass after one interval: startup must not block on the board,
        # and the inbox read covers cards that are already overdue.
        while not self._stop.wait(self.interval_seconds):
            try:
                self.service.sweep_reviews()
            except Exception as exc:  # a daemon thread must never die silently
                print(f"workbench: review sweep failed: {exc}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# PATCH status=ready gate (pure ASGI middleware)
# ---------------------------------------------------------------------------


async def _read_http_body(receive: Callable[[], Any]) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        chunk = message.get("body", b"")
        if chunk:
            chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay_receive(body: bytes, receive: Callable[[], Any]) -> Callable[[], Any]:
    state = {"sent": False}

    async def replay() -> dict[str, Any]:
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


def _body_object(body: bytes) -> Optional[dict[str, Any]]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _requested_status(body: bytes) -> Any:
    payload = _body_object(body)
    return payload.get("status") if payload is not None else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


class ReadyGateMiddleware:
    """Keep cards without a workbench acceptance contract out of ``ready``.

    Covered entry points:

    * ``PATCH /tasks/{id}`` with ``status=ready`` (404 is forwarded upstream)
    * ``POST /tasks`` when no ``triage`` flag would create a ready card
    * ``POST /tasks/bulk`` with ``status=ready``, or with
      ``assignee`` + ``reclaim_first`` (the upstream bulk path then calls
      ``reclaim_task``); any id missing acceptance rejects the whole batch
    * ``POST /tasks/{id}/reclaim`` (restores a ready/review phase)
    * ``POST /tasks/{id}/reassign`` with ``reclaim_first``
    * ``POST /runs/{run_id}/terminate`` — the run is mapped to its task via
      ``GET /runs/{run_id}`` and gated. If the mapping cannot be resolved the
      request is refused with 403 ``gate_unavailable`` (fail closed), never
      forwarded ungated.

    The per-task lock is acquired before the gate's metadata read and held
    until the forwarded state change finishes, so ``update_metadata`` cannot
    clear acceptance in between. Acquisition runs off the event loop; release
    happens in ``finally``.

    Upstream ``recompute_ready`` (parent completion) and the native 9119
    dashboard do not pass through this service and are outside the gate.
    """

    def __init__(self, app: Callable[..., Any], service: WorkbenchService) -> None:
        self.app = app
        self.service = service

    async def _reject(
        self, scope: dict[str, Any], receive: Callable[[], Any], send: Callable[[], Any],
        gate: tuple[int, dict[str, Any]],
    ) -> None:
        status_code, payload = gate
        await JSONResponse(payload, status_code=status_code)(scope, receive, send)

    async def _gated_forward(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[], Any],
        task_ids: list[str],
        gate_fn: Callable[[], Optional[tuple[int, dict[str, Any]]]],
    ) -> None:
        """Hold the per-task lock(s) across gate read + ASGI forward."""
        locks = await run_in_threadpool(self.service.acquire_task_locks, task_ids)
        try:
            gate = await run_in_threadpool(gate_fn)
            if gate is not None:
                await self._reject(scope, receive, send, gate)
                return
            await self.app(scope, receive, send)
        finally:
            self.service.release_task_locks(locks)

    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Any], send: Callable[[], Any]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # Trusted loopback call from WorkbenchService itself (dispatch's PATCH,
        # gate reads). It already passed the gate before being sent.
        headers = dict(scope.get("headers") or [])
        if headers.get(b"x-workbench-internal") == self.service.internal_token.encode():
            await self.app(scope, receive, send)
            return
        method = scope.get("method")
        path = scope.get("path", "")

        if method == "PATCH":
            match = TASK_PATH_RE.match(path)
            if match is None:
                await self.app(scope, receive, send)
                return
            body = await _read_http_body(receive)
            replay = _replay_receive(body, receive)
            if _requested_status(body) != "ready":
                await self.app(scope, replay, send)
                return
            task_id = match.group(1)
            await self._gated_forward(
                scope,
                replay,
                send,
                [task_id],
                lambda: self.service.patch_ready_gate(task_id),
            )
            return

        if method != "POST":
            await self.app(scope, receive, send)
            return

        if path == CREATE_TASK_PATH:
            body = await _read_http_body(receive)
            replay = _replay_receive(body, receive)
            payload = _body_object(body)
            if payload is None or _truthy(payload.get("triage")):
                await self.app(scope, replay, send)
                return
            await self._reject(scope, receive, send, self.service.create_ready_gate())
            return

        if path == BULK_TASK_PATH:
            body = await _read_http_body(receive)
            replay = _replay_receive(body, receive)
            payload = _body_object(body)
            if payload is None:
                await self.app(scope, replay, send)
                return
            ids = payload.get("ids")
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                await self.app(scope, replay, send)
                return
            # ``reclaim_first`` only reclaims when an assignee is carried
            # (upstream: ``payload.assignee is not None``).
            reclaiming = (
                "assignee" in payload
                and payload.get("assignee") is not None
                and _truthy(payload.get("reclaim_first"))
            )
            if payload.get("status") != "ready" and not reclaiming:
                await self.app(scope, replay, send)
                return
            await self._gated_forward(
                scope,
                replay,
                send,
                ids,
                lambda: self.service.bulk_ready_gate(ids),
            )
            return

        match = REASSIGN_PATH_RE.match(path)
        if match is not None:
            body = await _read_http_body(receive)
            replay = _replay_receive(body, receive)
            payload = _body_object(body)
            if payload is None or not _truthy(payload.get("reclaim_first")):
                await self.app(scope, replay, send)
                return
            task_id = match.group(1)
            await self._gated_forward(
                scope,
                replay,
                send,
                [task_id],
                lambda: self.service.reclaim_gate(task_id),
            )
            return

        match = RECLAIM_PATH_RE.match(path)
        if match is not None:
            task_id = match.group(1)
            await self._gated_forward(
                scope,
                receive,
                send,
                [task_id],
                lambda: self.service.reclaim_gate(task_id),
            )
            return

        match = RUN_TERMINATE_PATH_RE.match(path)
        if match is not None:
            task_id, gate_error = await run_in_threadpool(
                self.service.resolve_run_task, match.group(1)
            )
            if gate_error is not None or task_id is None:
                await self._reject(
                    scope,
                    receive,
                    send,
                    gate_error
                    or (
                        403,
                        {
                            "ok": False,
                            "error": "gate_unavailable",
                            "detail": "cannot resolve run's task; terminate blocked",
                        },
                    ),
                )
                return
            await self._gated_forward(
                scope,
                receive,
                send,
                [task_id],
                lambda: self.service.reclaim_gate(task_id),
            )
            return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------


def load_kanban_router():
    """Load the dashboard plugin's router without importing the dashboard app."""
    path = HERMES_ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    if not path.is_file():
        raise SystemExit(f"kanban plugin API not found: {path}")
    spec = importlib.util.spec_from_file_location("workbench_kanban_plugin_api", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.router


def _self_host(host: str) -> str:
    host = (host or "").strip()
    if host in ("", "0.0.0.0", "::", "::0"):
        return "127.0.0.1"
    return host


def _self_base(host: str, port: int) -> str:
    resolved = _self_host(host)
    if ":" in resolved and not resolved.startswith("["):
        resolved = f"[{resolved}]"
    return f"http://{resolved}:{int(port)}"


def _parse_ids(raw: str) -> tuple[list[str], bool]:
    """Return ``(ids, too_many)``; never silently truncate the caller's list."""
    seen: list[str] = []
    for part in raw.split(","):
        task_id = part.strip()
        if task_id and task_id not in seen:
            if len(seen) >= MAX_METADATA_IDS:
                return seen, True
            seen.append(task_id)
    return seen, False


def _env_positive_float(name: str, default: float) -> float:
    """Finite positive float from the environment, else the given default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    if not math.isfinite(value) or value <= 0:
        return float(default)
    return value


def _env_nonnegative_float(name: str, default: float) -> float:
    """Finite non-negative float from the environment, else the default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    if not math.isfinite(value) or value < 0:
        return float(default)
    return value


def _env_sweep_seconds() -> float:
    """Sweep interval: finite >0 (capped at a day), otherwise disabled."""
    raw = os.environ.get("WORKBENCH_REVIEW_SWEEP_SECONDS", "").strip()
    if not raw:
        return float(REVIEW_SWEEP_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return min(value, 86400.0)


def _env_positive_int(name: str, default: int, *, minimum: int = 1, maximum: int = 1000) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        return int(default)
    if value < minimum or value > maximum:
        return int(default)
    return value


def _env_notify_sweep_seconds() -> float:
    """Notification poll interval: finite >0 (capped at a day), else disabled."""
    raw = os.environ.get("WORKBENCH_NOTIFY_SWEEP_SECONDS", "").strip()
    if not raw:
        return float(NOTIFY_SWEEP_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return min(value, 86400.0)


def _json_error(status_code: int, error: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error": error}
    body.update(extra)
    return JSONResponse(body, status_code=status_code)


def _request_guard(request: Request) -> Optional[JSONResponse]:
    """Require JSON bodies and same-origin browser writes for POST/PATCH.

    A missing Origin (curl, tests, same-origin GET-less clients) is allowed;
    when present it must match the request Host. Cross-origin writes get 403
    before any upstream call.
    """
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        return _json_error(415, "unsupported_media_type")
    origin = request.headers.get("origin")
    if origin is not None:
        host = (request.headers.get("host") or "").strip()
        parsed = None
        try:
            parsed = urlparse.urlsplit(origin)
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme not in ("http", "https")
            or not host
            or parsed.netloc != host
        ):
            return _json_error(403, "forbidden_origin")
    return None


async def _request_json_object(
    request: Request,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Read a JSON object body with a hard size cap.

    Returns ``(payload, None)`` or ``(None, reason)`` where ``reason`` is
    ``payload_too_large``, ``disconnected`` or ``invalid``.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                return None, "payload_too_large"
            chunks.append(chunk)
    except ClientDisconnect:
        return None, "disconnected"
    try:
        payload = json.loads(b"".join(chunks)) if chunks else None
    except (ValueError, UnicodeDecodeError):
        return None, "invalid"
    if not isinstance(payload, dict):
        return None, "invalid"
    return payload, None


def create_app(host: str = "127.0.0.1", port: int = 8787) -> FastAPI:
    app = FastAPI(
        title="Jarvis Workbench",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(load_kanban_router(), prefix="/api/plugins/kanban")

    db_path = Path(os.environ.get("WORKBENCH_DB", "").strip() or DEFAULT_DB_PATH)
    defaults_path = Path(os.environ.get("WORKBENCH_DEFAULTS", "").strip() or DEFAULT_DEFAULTS_PATH)
    store = MetadataStore(db_path)
    store.start()

    base_url = os.environ.get("WORKBENCH_UPSTREAM_BASE", "").strip() or _self_base(host, port)
    review_ttl_seconds = int(
        _env_positive_float("WORKBENCH_REVIEW_TTL_SECONDS", REVIEW_TTL_SECONDS)
    )
    auto_collect_backoff_seconds = _env_positive_float(
        "WORKBENCH_REVIEW_BACKOFF_SECONDS", REVIEW_BACKOFF_SECONDS
    )
    inbox_cache_seconds = _env_nonnegative_float(
        "WORKBENCH_INBOX_CACHE_SECONDS", INBOX_CACHE_SECONDS
    )
    service = WorkbenchService(
        base_url,
        store,
        defaults_path,
        review_ttl_seconds=review_ttl_seconds,
        auto_collect_backoff_seconds=auto_collect_backoff_seconds,
        inbox_cache_seconds=inbox_cache_seconds,
    )

    try:
        load_defaults(defaults_path)
    except DefaultsError as exc:
        print(f"workbench: defaults invalid at startup: {exc}", file=sys.stderr, flush=True)

    sweep_seconds = _env_sweep_seconds()
    sweeper: Optional[ReviewSweeper] = None
    if sweep_seconds > 0:
        sweeper = ReviewSweeper(service, sweep_seconds)
        sweeper.start()
        app.router.add_event_handler("shutdown", sweeper.stop)

    notification_engine = NotificationEngine(
        verify=service.verify,
        review_ttl_seconds=review_ttl_seconds,
        notify_command=os.environ.get("WORKBENCH_NOTIFY_COMMAND", "").strip() or None,
        batch_size=_env_positive_int(
            "WORKBENCH_NOTIFY_BATCH_SIZE", NOTIFY_BATCH_SIZE, minimum=1, maximum=1000
        ),
        batch_seconds=_env_positive_float(
            "WORKBENCH_NOTIFY_BATCH_SECONDS", NOTIFY_BATCH_SECONDS
        ),
        escalate_seconds=_env_positive_float(
            "WORKBENCH_NOTIFY_ESCALATE_SECONDS", NOTIFY_ESCALATE_SECONDS
        ),
        snooze_seconds=_env_positive_float(
            "WORKBENCH_NOTIFY_SNOOZE_SECONDS", NOTIFY_SNOOZE_SECONDS
        ),
        daily_hour=_env_positive_int(
            "WORKBENCH_NOTIFY_DAILY_HOUR", NOTIFY_DAILY_HOUR, minimum=0, maximum=23
        ),
    )

    # Serialise board-read+collect against actions: otherwise a sweep that
    # fetched the board just before an accept could re-add the review card as
    # a new event (and notify again) after the action removed it.
    notification_lock = threading.Lock()

    def notification_sweep() -> tuple[int, dict[str, Any]]:
        """One collection round; a failed upstream keeps the old snapshot."""
        with notification_lock:
            status_code, payload = service.board_snapshot()
            if status_code != 200:
                error = (
                    payload.get("error")
                    if isinstance(payload, dict)
                    else "upstream_unavailable"
                )
                notification_engine.record_sweep(error=error)
                return status_code, payload
            try:
                notification_engine.collect(payload)
            except Exception as exc:
                notification_engine.record_sweep(error=str(exc))
                raise
            notification_engine.record_sweep()
            # Rebuild so the sweep response itself carries the freshness just
            # recorded (the UIs then GET a consistent snapshot).
            return 200, notification_engine.snapshot()

    def notification_action(
        event_id: str, action: Any, reason: Any
    ) -> tuple[int, dict[str, Any]]:
        with notification_lock:
            return notification_engine.action(event_id, action, reason)

    notify_sweep_seconds = _env_notify_sweep_seconds()
    notification_poller: Optional[NotificationPoller] = None
    if notify_sweep_seconds > 0:
        notification_poller = NotificationPoller(
            notification_sweep,
            notify_sweep_seconds,
            log=lambda message: print(
                f"workbench: {message}", file=sys.stderr, flush=True
            ),
        )
        notification_poller.start()
        app.router.add_event_handler("shutdown", notification_poller.stop)

    app.add_middleware(ReadyGateMiddleware, service=service)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    @app.get("/workbench")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse(
            {
                "name": "Jarvis 工作台",
                "short_name": "工作台",
                "description": "待办 / 看板 / 已完成：任务读写本机 kanban.db，契约存工作台自有库",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0b1110",
                "theme_color": "#0b1110",
                "icons": [
                    {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}
                ],
            }
        )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post("/api/workbench/dispatch/{task_id}")
    def dispatch(task_id: str, request: Request) -> JSONResponse:
        guard = _request_guard(request)
        if guard is not None:
            return guard
        status_code, payload = service.dispatch(task_id)
        return JSONResponse(payload, status_code=status_code)

    @app.get("/api/workbench/metadata")
    def metadata(ids: str = "") -> JSONResponse:
        task_ids, too_many = _parse_ids(ids)
        if too_many:
            return _json_error(400, "too_many_ids")
        status_code, payload = service.metadata_for(task_ids)
        return JSONResponse(payload, status_code=status_code)

    @app.get("/api/workbench/inbox")
    async def inbox() -> JSONResponse:
        # Pure read; the 48h sweep is a guarded POST /reviews/sweep call made
        # by the UI, so polling cannot mutate cards.
        status_code, payload = await run_in_threadpool(service.inbox)
        return JSONResponse(payload, status_code=status_code)

    @app.get("/api/workbench/inbox/count")
    async def inbox_count() -> JSONResponse:
        # Header poll: cached board read, no per-card detail calls.
        status_code, payload = await run_in_threadpool(service.inbox_count)
        return JSONResponse(payload, status_code=status_code)

    @app.post("/api/workbench/reviews/sweep")
    async def reviews_sweep(request: Request) -> JSONResponse:
        guard = _request_guard(request)
        if guard is not None:
            return guard
        payload, body_error = await _request_json_object(request)
        if body_error is not None:
            if body_error == "payload_too_large":
                return _json_error(413, "payload_too_large")
            return _json_error(400, "invalid_request")
        status_code, body = await run_in_threadpool(service.sweep_reviews)
        return JSONResponse(body, status_code=status_code)

    @app.post("/api/workbench/verify/{task_id}")
    async def verify(task_id: str, request: Request) -> JSONResponse:
        guard = _request_guard(request)
        if guard is not None:
            return guard
        payload, body_error = await _request_json_object(request)
        if body_error is not None:
            if body_error == "payload_too_large":
                return _json_error(413, "payload_too_large")
            return _json_error(400, "invalid_request")
        status_code, body = await run_in_threadpool(
            service.verify,
            task_id,
            payload.get("action"),
            payload.get("reason"),
            payload.get("expected_review_at"),
        )
        return JSONResponse(body, status_code=status_code)

    @app.post("/api/workbench/tasks")
    async def create_task(request: Request) -> JSONResponse:
        guard = _request_guard(request)
        if guard is not None:
            return guard
        payload, body_error = await _request_json_object(request)
        if body_error is not None:
            if body_error == "payload_too_large":
                return _json_error(413, "payload_too_large")
            return _json_error(400, "invalid_request")
        # The service calls the loopback upstream with blocking urllib; run it
        # in the threadpool so this same event loop can serve that request.
        status_code, body = await run_in_threadpool(
            service.create_task,
            payload.get("title"),
            # Missing bucket defaults to today; an explicit null is invalid.
            payload.get("bucket", "today"),
            payload.get("idempotency_key"),
        )
        return JSONResponse(body, status_code=status_code)

    @app.get("/api/workbench/notifications")
    async def notifications() -> JSONResponse:
        # Pure read: the engine's in-memory snapshot; never talks to upstream.
        return JSONResponse(await run_in_threadpool(notification_engine.snapshot))

    @app.post("/api/workbench/notifications/sweep")
    async def notifications_sweep(request: Request) -> JSONResponse:
        guard = _request_guard(request)
        if guard is not None:
            return guard
        payload, body_error = await _request_json_object(request)
        if body_error is not None:
            if body_error == "payload_too_large":
                return _json_error(413, "payload_too_large")
            return _json_error(400, "invalid_request")
        status_code, body = await run_in_threadpool(notification_sweep)
        return JSONResponse(body, status_code=status_code)

    @app.post("/api/workbench/notifications/{event_id}/action")
    async def notifications_action(event_id: str, request: Request) -> JSONResponse:
        guard = _request_guard(request)
        if guard is not None:
            return guard
        payload, body_error = await _request_json_object(request)
        if body_error is not None:
            if body_error == "payload_too_large":
                return _json_error(413, "payload_too_large")
            return _json_error(400, "invalid_request")
        status_code, body = await run_in_threadpool(
            notification_action,
            event_id,
            payload.get("action"),
            payload.get("reason"),
        )
        return JSONResponse(body, status_code=status_code)

    @app.patch("/api/workbench/metadata/{task_id}")
    async def update_metadata(task_id: str, request: Request) -> JSONResponse:
        guard = _request_guard(request)
        if guard is not None:
            return guard
        payload, body_error = await _request_json_object(request)
        if body_error is not None:
            if body_error == "payload_too_large":
                return _json_error(413, "payload_too_large")
            return _json_error(400, "invalid_request")
        status_code, body = await run_in_threadpool(service.update_metadata, task_id, payload)
        return JSONResponse(body, status_code=status_code)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis Workbench standalone server")
    parser.add_argument("--host", default=os.environ.get("WORKBENCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORKBENCH_PORT", "8787")))
    args = parser.parse_args()

    import uvicorn

    print(f"Jarvis Workbench → http://{args.host}:{args.port}", flush=True)
    uvicorn.run(
        create_app(host=args.host, port=args.port),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()