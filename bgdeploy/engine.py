"""Container engine abstraction.

bgdeploy never talks to the daemon API directly - it shells out to the CLI
(`docker` or `podman`), which keeps the tool portable across rootful/rootless
setups, SELinux hosts and CI runners, and makes it trivial to fake in tests.

    engine = select_engine("auto")   # docker if present, else podman
    engine = select_engine("podman") # force podman
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


class EngineError(RuntimeError):
    pass


def parse_host_port(data: dict, container_port: int) -> int | None:
    """Extract the host port bound to a container port from `inspect` JSON."""
    ports = (data.get("NetworkSettings") or {}).get("Ports") or {}
    bindings = ports.get(f"{container_port}/tcp") or []
    if bindings and bindings[0].get("HostPort"):
        try:
            return int(bindings[0]["HostPort"])
        except (TypeError, ValueError):
            return None
    return None


@dataclass
class ContainerEngine:
    binary: str

    def run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a CLI command and return the completed process."""
        cmd = [self.binary] + args
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if check and proc.returncode != 0:
            raise EngineError(f"command failed: {' '.join(cmd)}\n{proc.stderr.strip()}")
        return proc

    def available(self) -> bool:
        if shutil.which(self.binary) is None:
            return False
        # Docker needs a reachable daemon -> `docker version`.
        # Podman is daemonless, so `podman version` always succeeds; probe the
        # actual runtime (storage/network/cgroups) with `podman info` instead.
        probe = (["info"] if self.binary == "podman"
                 else ["version", "--format", "{{.Client.Version}}"])
        try:
            return self.run(probe, check=False).returncode == 0
        except Exception:  # noqa: BLE001
            return False

    # ---- build / images -------------------------------------------------
    def build(self, tag: str, context: str, dockerfile: str = "Dockerfile") -> None:
        dockerfile_path = dockerfile if dockerfile.startswith("/") else f"{context}/{dockerfile}"
        self.run(["build", "-t", tag, "-f", dockerfile_path, context])

    def image_exists(self, tag: str) -> bool:
        return self.run(["image", "inspect", tag], check=False).returncode == 0

    # ---- networks --------------------------------------------------------
    def network_ensure(self, name: str) -> None:
        exists = self.run(["network", "inspect", name], check=False).returncode == 0
        if not exists:
            self.run(["network", "create", name])

    def network_remove(self, name: str) -> None:
        self.run(["network", "rm", name], check=False)  # missing network is fine

    # ---- containers ------------------------------------------------------
    def inspect(self, name: str) -> dict:
        proc = self.run(["inspect", "--format", "{{json .}}", name])
        try:
            data = json.loads(proc.stdout.strip())
        except json.JSONDecodeError as exc:
            raise EngineError(f"could not parse inspect output for {name}") from exc
        return data if isinstance(data, dict) else data[0]

    def container_exists(self, name: str) -> bool:
        return self.run(["inspect", name], check=False).returncode == 0

    def container_running(self, name: str) -> bool:
        try:
            return bool((self.inspect(name).get("State") or {}).get("Running"))
        except EngineError:
            return False

    def labels(self, name: str) -> dict:
        try:
            return (self.inspect(name).get("Config") or {}).get("Labels") or {}
        except EngineError:
            return {}

    def host_port(self, name: str, container_port: int) -> int | None:
        try:
            return parse_host_port(self.inspect(name), container_port)
        except EngineError:
            return None

    def ip(self, name: str, network: str) -> str | None:
        try:
            nets = (self.inspect(name).get("NetworkSettings") or {}).get("Networks") or {}
            return (nets.get(network) or {}).get("IPAddress") or None
        except EngineError:
            return None

    def run_container(
        self,
        name: str,
        image: str,
        network: str,
        env: dict | None = None,
        labels: dict | None = None,
        publish_bind: str | None = None,
        publish_host_port: int | None = None,
        container_port: int | None = None,
        extra_args: list[str] | None = None,
        command: list[str] | None = None,
    ) -> str:
        """Start a detached container with bgdeploy labels. Returns its name.
        `command` is appended after the image (IMAGE [COMMAND] [ARG...])."""
        args = ["run", "-d", "--name", name, "--network", network]
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        if publish_bind and container_port:
            if publish_host_port:
                args += ["-p", f"{publish_bind}:{publish_host_port}:{container_port}"]
            else:
                # Random ephemeral host port on the publish bind address.
                args += ["-p", f"{publish_bind}::{container_port}"]
        for key, value in (env or {}).items():
            args += ["--env", f"{key}={value}"]
        args += list(extra_args or [])
        args.append(image)
        args += list(command or [])
        self.run(args)
        return name

    def restart(self, name: str) -> None:
        self.run(["restart", "--time", "10", name])

    def stop(self, name: str) -> None:
        self.run(["stop", "--time", "10", name], check=False)

    def remove(self, name: str, force: bool = True) -> None:
        args = ["rm"]
        if force:
            args.append("-f")
        args.append(name)
        self.run(args, check=False)  # removing a missing container is fine

    def ps_managed(self) -> list[str]:
        proc = self.run(
            ["ps", "-a", "--filter", "label=bgdeploy.managed=true", "--format", "{{.Names}}"]
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def logs(self, name: str, tail: int = 50) -> str:
        proc = self.run(["logs", "--tail", str(tail), name], check=False)
        return (proc.stdout + proc.stderr).strip()


def _detect() -> str | None:
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return None


def select_engine(preference: str = "auto") -> ContainerEngine:
    """Pick a working engine. preference: docker | podman | auto."""
    if preference == "auto":
        binary = _detect()
        if binary is None:
            raise EngineError("no container engine found: install docker or podman")
        engine = ContainerEngine(binary)
        if not engine.available():
            raise EngineError(f"{binary} binary found but the daemon is not reachable")
        return engine
    if preference in ("docker", "podman"):
        engine = ContainerEngine(preference)
        if not engine.available():
            raise EngineError(
                f"engine '{preference}' is not available (binary missing or daemon unreachable)"
            )
        return engine
    raise EngineError(f"unknown engine preference '{preference}' (use docker, podman or auto)")
