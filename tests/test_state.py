import json
import sys

import pytest

from bgdeploy.state import (
    DeploymentLockedError,
    FileLock,
    StateStore,
    default_state,
)


def test_default_state_shape():
    state = default_state()
    assert state["active_color"] is None
    assert state["previous"] is None
    assert state["colors"] == {}
    assert state["history"] == []
    assert state["incidents"] == []


def test_save_and_load_roundtrip(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    state = default_state()
    state["active_color"] = "blue"
    state["active_version"] = "v1"
    store.save(state)

    reloaded = StateStore(str(tmp_path / "state.json")).load()
    assert reloaded["active_color"] == "blue"
    assert reloaded["active_version"] == "v1"
    assert reloaded["colors"] == {}


def test_corrupt_state_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")
    state = StateStore(str(path)).load()
    assert state["active_color"] is None


def test_events_prepend_and_cap(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    state = default_state()
    store.add_event(state, "deploy", color="blue", version="v1", result="ok")
    store.add_event(state, "deploy", color="green", version="v2", result="ok")
    history = StateStore(str(tmp_path / "state.json")).load()["history"]
    assert len(history) == 2
    assert history[0]["version"] == "v2"
    assert history[0]["action"] == "deploy"
    assert history[1]["version"] == "v1"


def test_incidents_are_recorded(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    state = default_state()
    state["active_color"] = "green"
    store.add_incident(state, "failover", "green unhealthy -> traffic to blue")
    incidents = StateStore(str(tmp_path / "state.json")).load()["incidents"]
    assert incidents[0]["kind"] == "failover"
    assert "green" in incidents[0]["detail"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses fcntl")
def test_file_lock_is_exclusive(tmp_path):
    lock_path = str(tmp_path / "deploy.lock")
    with FileLock(lock_path):
        with pytest.raises(DeploymentLockedError):
            with FileLock(lock_path):
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="uses fcntl")
def test_file_lock_released(tmp_path):
    lock_path = str(tmp_path / "deploy.lock")
    with FileLock(lock_path):
        pass
    # Re-acquiring after release must succeed.
    with FileLock(lock_path):
        pass
