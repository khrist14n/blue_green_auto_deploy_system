import shutil
import subprocess

import pytest

import bgdeploy.engine as engine_mod
from bgdeploy.engine import ContainerEngine, EngineError, parse_host_port, select_engine


def test_parse_host_port_extracts_host_port():
    data = {
        "NetworkSettings": {
            "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49153"}]}
        }
    }
    assert parse_host_port(data, 8000) == 49153


def test_parse_host_port_no_binding():
    data = {"NetworkSettings": {"Ports": {}}}
    assert parse_host_port(data, 8000) is None


def test_select_engine_unknown_preference():
    with pytest.raises(EngineError):
        select_engine("lxc")


def test_select_engine_auto_raises_when_nothing_installed(monkeypatch):
    def no_binary(name):
        return None

    monkeypatch.setattr(engine_mod.shutil, "which", no_binary)
    with pytest.raises(EngineError, match="no container engine"):
        select_engine("auto")


def test_select_engine_auto_prefers_docker(monkeypatch):
    def which(name):
        return {"docker": "/usr/bin/docker", "podman": "/usr/bin/podman"}.get(name)

    monkeypatch.setattr(engine_mod.shutil, "which", which)
    monkeypatch.setattr(ContainerEngine, "available", lambda self: True)
    assert select_engine("auto").binary == "docker"


def test_select_engine_auto_falls_back_to_podman(monkeypatch):
    def which(name):
        return "/usr/bin/podman" if name == "podman" else None

    monkeypatch.setattr(engine_mod.shutil, "which", which)
    monkeypatch.setattr(ContainerEngine, "available", lambda self: True)
    assert select_engine("auto").binary == "podman"


def test_select_engine_docker_rejected_when_unavailable(monkeypatch):
    monkeypatch.setattr(ContainerEngine, "available", lambda self: False)
    with pytest.raises(EngineError, match="not available"):
        select_engine("docker")


def test_select_engine_forces_podman(monkeypatch):
    def which(name):
        return "/usr/bin/podman" if name == "podman" else None

    monkeypatch.setattr(engine_mod.shutil, "which", which)
    monkeypatch.setattr(ContainerEngine, "available", lambda self: True)
    engine = select_engine("podman")
    assert engine.binary == "podman"


def test_select_engine_podman_rejected_when_unavailable(monkeypatch):
    def which(name):
        return "/usr/bin/podman" if name == "podman" else None

    monkeypatch.setattr(engine_mod.shutil, "which", which)
    monkeypatch.setattr(ContainerEngine, "available", lambda self: False)
    with pytest.raises(EngineError, match="not available"):
        select_engine("podman")


def _capture_run(captured):
    def fake_run(self, args, check=True):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    return fake_run


def test_available_probes_daemon_for_docker(monkeypatch):
    captured = {}
    monkeypatch.setattr(engine_mod.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(ContainerEngine, "run", _capture_run(captured))
    assert ContainerEngine("docker").available() is True
    assert captured["args"] == ["version", "--format", "{{.Client.Version}}"]


def test_available_probes_runtime_for_podman(monkeypatch):
    captured = {}
    monkeypatch.setattr(engine_mod.shutil, "which", lambda name: "/usr/bin/podman")
    monkeypatch.setattr(ContainerEngine, "run", _capture_run(captured))
    assert ContainerEngine("podman").available() is True
    # podman is daemonless: `version` always succeeds, so availability probes
    # the actual runtime with `podman info`.
    assert captured["args"] == ["info"]


def test_available_false_when_probe_fails(monkeypatch):
    def fake_run(self, args, check=True):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    monkeypatch.setattr(engine_mod.shutil, "which", lambda name: "/usr/bin/podman")
    monkeypatch.setattr(ContainerEngine, "run", fake_run)
    assert ContainerEngine("podman").available() is False
