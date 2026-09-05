"""Command line interface.

    bgdeploy deploy --version v1.2.0    # full blue/green rollout
    bgdeploy rollback                   # single-click undo to the previous release
    bgdeploy status                     # what is running and where traffic points
    bgdeploy shift --color green --percent 50   # manual canary knob
    bgdeploy smoke --url http://127.0.0.1:8080  # run the smoke suite anywhere
    bgdeploy recover                    # one auto-recovery pass
    bgdeploy watch                      # continuous auto-recovery loop
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import COLORS, Config
from .deployer import BlueGreenDeployer, DeploymentError
from .engine import EngineError, select_engine
from .recovery import RecoveryMonitor
from .smoke import SmokeCase, load_cases, run_smoke, substitute
from .state import DeploymentLockedError, FileLock, StateStore
from .traffic import TrafficWeightsStore, fetch_router_health


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def _build_config(args) -> Config:
    cfg = Config.from_env()
    overrides = {}
    if getattr(args, "engine", None):
        overrides["engine"] = args.engine
    if getattr(args, "data_dir", None):
        overrides["data_dir"] = args.data_dir
    if getattr(args, "image", None):
        overrides["image"] = args.image
    return cfg.with_overrides(**overrides)


def _make_deployer(cfg: Config, wait_for_backend: bool = False) -> BlueGreenDeployer:
    engine = select_engine(cfg.engine)
    router_checker = fetch_router_health if wait_for_backend else None
    return BlueGreenDeployer(
        cfg,
        engine=engine,
        router_checker=router_checker,
    )


def _print_status(snapshot: dict) -> None:
    state = snapshot["state"]
    print(f"engine            : {snapshot['engine']}")
    print(f"active color      : {state.get('active_color')}")
    print(f"active version    : {state.get('active_version')}")
    prev = state.get("previous")
    print(f"previous stable   : {prev.get('color')} ({prev.get('version')})" if prev else "previous stable   : (none)")
    print(f"weights (live)    : {snapshot['weights']}")
    for color in COLORS:
        info = snapshot["containers"][color]
        version = info.get("version") or "-"
        state_txt = "running" if info.get("running") else "stopped"
        port = info.get("host_port")
        port_txt = f"port {port}" if port else "no port"
        print(f"container {color:<5}: {state_txt:<7} {version:<12} {port_txt}")
    print(f"router            : {snapshot['router_url']}")


def cmd_deploy(args) -> int:
    cfg = _build_config(args)
    deployer = _make_deployer(cfg, wait_for_backend=True)
    summary = deployer.deploy(
        version=args.version,
        rebuild=not args.no_rebuild,
        skip_smoke=args.skip_smoke,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_rollback(args) -> int:
    cfg = _build_config(args)
    deployer = _make_deployer(cfg, wait_for_backend=True)
    summary = deployer.rollback(skip_smoke=args.skip_smoke)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_status(args) -> int:
    cfg = _build_config(args)
    deployer = _make_deployer(cfg)
    _print_status(deployer.snapshot())
    return 0


def cmd_shift(args) -> int:
    cfg = _build_config(args)
    weights = TrafficWeightsStore(cfg.weights_file)
    with FileLock(cfg.lock_file):
        new_weights = weights.set(args.color, args.percent)
    print(f"traffic weights now: {new_weights}")
    print(f"(router picks this up within its poll interval from {cfg.weights_file})")
    return 0


def cmd_smoke(args) -> int:
    if not args.url:
        print("error: --url is required", file=sys.stderr)
        return 2
    cases: list[SmokeCase] = load_cases(args.file or "smoke-tests.json")
    if args.version:
        cases = substitute(cases, args.version)
    print(f"running {len(cases)} smoke checks against {args.url}")
    report = run_smoke(args.url, cases)
    for check in report.checks:
        mark = "PASS" if check.ok else "FAIL"
        print(f"  {mark} {check.name} ({check.latency_ms:.0f}ms) {check.detail}")
    print(f"result: {report.summary()}")
    return 0 if report.passed else 1


def cmd_recover(args) -> int:
    cfg = _build_config(args)
    engine = select_engine(cfg.engine)
    monitor = RecoveryMonitor(
        cfg,
        engine=engine,
        state_store=StateStore(cfg.state_file),
        weights=TrafficWeightsStore(cfg.weights_file),
    )
    actions = monitor.poll_once()
    for action in actions:
        print(f"[recovery] {action}")
    return 0


def cmd_watch(args) -> int:
    cfg = _build_config(args)
    engine = select_engine(cfg.engine)
    monitor = RecoveryMonitor(
        cfg,
        engine=engine,
        state_store=StateStore(cfg.state_file),
        weights=TrafficWeightsStore(cfg.weights_file),
    )
    monitor.run_forever(interval=args.interval)
    return 0


def cmd_api(args) -> int:
    from .api import create_control_server

    cfg = _build_config(args)
    token = os.environ.get("BG_WEBHOOK_TOKEN")
    server = create_control_server(cfg, host=args.host, port=args.port,
                                   webhook_token=token)
    host, port = server.server_address
    print(f"bgdeploy control API + web UI on http://{host}:{port}")
    print("  - POST /api/jobs   {action: deploy|rollback, version: ...}")
    print("  - POST /api/webhook/github   (auto-deploys on tag pushes)")
    print("  - GET  /            web UI with one-click rollback")
    if cfg.api_password or cfg.api_token:
        print(f"  auth: ON (user {cfg.api_user!r} via BG_API_PASSWORD / BG_API_TOKEN)")
        print(f"  login guard: rate limit {cfg.login_rate_limit}/{cfg.login_rate_window:.0f}s "
              f"per IP, account lockout after {cfg.login_lockout_attempts} failures "
              f"for {cfg.login_lockout_seconds:.0f}s")
        print("  - POST /api/login  {username, password}  -> session cookie + token")
    else:
        print("  auth: off - set BG_API_PASSWORD and/or BG_API_TOKEN to require login")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bgdeploy",
        description="Blue/green auto-deployment with traffic shifting, smoke tests, "
                    "rollback and auto-recovery (docker & podman).",
    )
    parser.add_argument("--version", action="version", version=f"bgdeploy {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, engine=True, data_dir=True, image=False):
        if engine:
            p.add_argument("--engine", choices=["docker", "podman", "auto"], default=None,
                           help="container engine (default: auto-detect)")
        if data_dir:
            p.add_argument("--data-dir", default=None,
                           help="where state.json/weights.json live (default: .bgdeploy)")
        if image:
            p.add_argument("--image", default=None,
                           help="service image name (version becomes the tag)")

    p = sub.add_parser("deploy", help="blue/green deploy a new version")
    add_common(p, image=True)
    p.add_argument("--version", required=True, help="release version, e.g. v1.2.0")
    p.add_argument("--no-rebuild", action="store_true", help="reuse an already built image tag")
    p.add_argument("--skip-smoke", action="store_true", help="skip the automated smoke suite")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("rollback", help="shift traffic back to the previous release")
    add_common(p)
    p.add_argument("--skip-smoke", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("status", help="show deployment state, containers and weights")
    add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("shift", help="manually set traffic weights between colors")
    add_common(p, engine=False)
    p.add_argument("--color", choices=list(COLORS), required=True)
    p.add_argument("--percent", type=int, required=True, help="0-100 share for --color")
    p.set_defaults(func=cmd_shift)

    p = sub.add_parser("smoke", help="run the smoke suite against any URL")
    add_common(p, engine=False, data_dir=False)
    p.add_argument("--url", default=None, help="base URL to test")
    p.add_argument("--file", default=None, help="smoke test JSON file")
    p.add_argument("--version", dest="smoke_version", default=None,
                   help="version used to fill {version} placeholders")
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("recover", help="run one auto-recovery pass")
    add_common(p)
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("watch", help="continuously watch and auto-recover")
    add_common(p)
    p.add_argument("--interval", type=float, default=None, help="seconds between checks")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("api", help="run the HTTP control API + web UI")
    add_common(p)
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8090, help="bind port (default: 8090)")
    p.set_defaults(func=cmd_api)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (DeploymentError, EngineError, DeploymentLockedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
