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


class MetadataUnavailable(RuntimeError):
    """The workbench metadata DB cannot be opened or queried."""


class DefaultsError(ValueError):
    """``defaults.json`` is missing or has an invalid type/range."""


class UpstreamUnavailable(RuntimeError):
    """A loopback kanban REST call failed or exceeded the dispatch budget."""


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

    def __init__(self, base_url: str, store: MetadataStore, defaults_path: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.defaults_path = Path(defaults_path)
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
    def _find_on_board(board: dict[str, Any], task_id: str) -> Optional[dict[str, Any]]:
        for column in board.get("columns") or []:
            if not isinstance(column, dict):
                continue
            for index, card in enumerate(column.get("tasks") or []):
                if isinstance(card, dict) and card.get("id") == task_id:
                    return {
                        "status": card.get("status") or column.get("name"),
                        "column": column.get("name"),
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
    service = WorkbenchService(base_url, store, defaults_path)

    try:
        load_defaults(defaults_path)
    except DefaultsError as exc:
        print(f"workbench: defaults invalid at startup: {exc}", file=sys.stderr, flush=True)

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