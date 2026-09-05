"""Persistent deployment state + single-flight file lock.

state.json holds everything bgdeploy needs to reason about blue/green state:

    {
      "active_color": "blue",
      "active_version": "v1.2.0",
      "previous": {"color": "blue", "version": "v1.1.0"},   # last stable env
      "colors": {"blue": {"version": "v1.1.0", "deployed_at": "..."},
                 "green": {"version": "v1.2.0", "deployed_at": "..."}},
      "history":   [...],   # deployment / rollback / shift events
      "incidents": [...]    # auto-recovery incidents
    }

Everything is plain JSON so it is trivially inspectable and portable.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

HISTORY_LIMIT = 200
INCIDENTS_LIMIT = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_state() -> dict:
    return {
        "active_color": None,
        "active_version": None,
        "previous": None,
        "colors": {},
        "history": [],
        "incidents": [],
    }


class StateStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        state = default_state()
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    state.update(loaded)
            except (json.JSONDecodeError, OSError):
                pass  # corrupted file -> start from defaults, never crash the CLI
        return state

    def save(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def add_event(self, state: dict, action: str, color=None, version=None, result="ok", detail="") -> dict:
        event = {
            "ts": _now_iso(),
            "action": action,
            "color": color,
            "version": version,
            "result": result,
            "detail": detail,
        }
        state.setdefault("history", []).insert(0, event)
        state["history"] = state["history"][:HISTORY_LIMIT]
        self.save(state)
        return state

    def add_incident(self, state: dict, kind: str, detail: str) -> dict:
        incident = {"ts": _now_iso(), "kind": kind, "detail": detail}
        state.setdefault("incidents", []).insert(0, incident)
        state["incidents"] = state["incidents"][:INCIDENTS_LIMIT]
        self.save(state)
        return state


class DeploymentLockedError(RuntimeError):
    pass


class FileLock:
    """Cross-process exclusive lock. Deploys are single-flight."""

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl

            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self._fh.close()
                self._fh = None
                raise DeploymentLockedError(
                    f"another deployment/rollback is in progress (lock: {self.path})"
                ) from exc
        except ImportError:  # pragma: no cover - non-POSIX fallback
            pass  # best effort on platforms without fcntl

    def release(self) -> None:
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
