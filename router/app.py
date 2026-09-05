"""Weighted traffic-shifting router.

Sits in front of the blue and green deployments and decides, per request,
which color to serve based on the current weights:

    {"blue": 100, "green": 0}   -> all traffic to blue
    {"blue": 50,  "green": 50}  -> split 50/50
    {"blue": 0,   "green": 100} -> all traffic to green

Each color may have multiple replica containers (``app-blue-1..N``); the
router health-checks *every instance* and load-balances within the chosen
color.  If no instance of the weighted-favorite color is healthy the request
fails over to the other color automatically - the per-request half of the
auto-recovery story (the durable half is handled by bgdeploy's
RecoveryMonitor).

Weights live in a JSON file (WEIGHTS_FILE) updated by the bgdeploy orchestrator.

Control endpoints (never proxied):
    GET /__router/health   -> JSON status of backends, weights, counters
    GET /__router/ping     -> plain liveness of the router itself
    GET /__router/metrics  -> Prometheus text exposition format
"""
import argparse
import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

from aiohttp import ClientSession, web

COLORS = ("blue", "green")
LOG = logging.getLogger("router")
BG_STATE = web.AppKey("bgdeploy", dict)


def parse_args() -> dict:
    p = argparse.ArgumentParser(description="Blue/green traffic router")
    p.add_argument("--port", type=int, default=int(os.environ.get("ROUTER_PORT", "8080")))
    p.add_argument("--weights-file", default=os.environ.get("WEIGHTS_FILE", ""))
    p.add_argument("--health-path", default=os.environ.get("HEALTH_PATH", "/healthz"))
    p.add_argument("--health-interval", type=float, default=float(os.environ.get("HEALTH_INTERVAL", "2")))
    p.add_argument("--weights-poll", type=float, default=float(os.environ.get("WEIGHTS_POLL", "1")))
    p.add_argument("--initial-weights", default=os.environ.get("INITIAL_WEIGHTS", "blue:100,green:0"))
    for color in COLORS:
        p.add_argument(
            f"--backend-{color}",
            default=os.environ.get(f"BACKEND_{color.upper()}", f"http://app-{color}:8000"),
        )
    args = p.parse_args()
    return {
        "port": args.port,
        "weights_file": args.weights_file,
        "health_path": args.health_path,
        "health_interval": args.health_interval,
        "weights_poll": args.weights_poll,
        "backends": {c: split_urls(getattr(args, f"backend_{c}")) for c in COLORS},
        "initial_weights": parse_weights(args.initial_weights),
    }


def split_urls(value) -> list[str]:
    """Normalise a backend spec to a list of URLs (comma-separated string or list)."""
    if isinstance(value, (list, tuple)):
        return [str(u) for u in value if str(u).strip()]
    return [u.strip() for u in str(value).split(",") if u.strip()]


def parse_weights(raw: str) -> dict:
    """Parse 'blue:100,green:0' into {'blue': 100, 'green': 0}."""
    weights = {}
    for chunk in raw.split(","):
        if ":" in chunk:
            color, value = chunk.strip().split(":", 1)
            weights[color.strip()] = int(float(value))
    out = {c: weights.get(c, 0) for c in COLORS}
    _normalize(out)
    return out


def _normalize(weights: dict) -> None:
    total = sum(max(v, 0) for v in weights.values())
    if total <= 0:
        weights["blue"], weights["green"] = 100, 0
        return
    for c in COLORS:
        weights[c] = round(weights.get(c, 0) * 100 / total)


def pick_backend(weights: dict, health: dict, rng=None) -> tuple:
    """Pick the color to serve this request.

    Returns (serving_color, favorite_color). ``serving_color`` is chosen among
    the healthy colors using weighted randomness. ``favorite_color`` owns the
    majority of configured weight. When they differ the router is failing over
    an unhealthy color. Returns (None, favorite) when nothing is healthy.
    """
    rng = rng or random.random
    healthy = [c for c in COLORS if health.get(c)]
    favorite = max(COLORS, key=lambda c: (weights.get(c, 0), c == "green"))
    if not healthy:
        return None, favorite
    if len(healthy) == 1:
        return healthy[0], favorite
    total = sum(weights.get(c, 0) for c in healthy) or 100
    roll = rng() * total
    for c in healthy:
        roll -= weights.get(c, 0)
        if roll < 0:
            return c, favorite
    return healthy[-1], favorite


def pick_instance(state: dict, color: str, rng=None):
    """Pick a healthy instance URL for a color, or None."""
    rng = rng or random.random
    healthy = [
        url for url, info in state["health"][color]["instances"].items()
        if info["healthy"]
    ]
    if not healthy:
        return None
    return healthy[int(rng() * len(healthy))]


def default_health(backends: dict) -> dict:
    health = {}
    for color, urls in backends.items():
        health[color] = {
            "healthy": False,
            "requests": 0,
            "failovers": 0,
            "instances": {
                url: {
                    "healthy": False,
                    "checked": None,
                    "consecutive_failures": 0,
                    "requests": 0,
                    "since": None,
                }
                for url in urls
            },
        }
    return health


def _aggregate(state: dict, color: str) -> None:
    info = state["health"][color]
    info["healthy"] = any(i["healthy"] for i in info["instances"].values())


def _metrics_text(state: dict) -> str:
    """Prometheus text exposition of router counters and gauges."""
    lines = [
        "# HELP bgdeploy_router_requests_total Requests proxied by the router.",
        "# TYPE bgdeploy_router_requests_total counter",
        "# HELP bgdeploy_router_failover_total Requests served by failover.",
        "# TYPE bgdeploy_router_failover_total counter",
        "# HELP bgdeploy_router_backend_up Whether an instance is healthy.",
        "# TYPE bgdeploy_router_backend_up gauge",
        "# HELP bgdeploy_router_weights Configured traffic share per color.",
        "# TYPE bgdeploy_router_weights gauge",
        "# HELP bgdeploy_router_uptime_seconds Seconds since the router started.",
        "# TYPE bgdeploy_router_uptime_seconds gauge",
    ]
    weights = state.get("weights", {})
    for color in COLORS:
        health = state["health"][color]
        lines.append(f'bgdeploy_router_requests_total{{color="{color}"}} {health["requests"]}')
        lines.append(f'bgdeploy_router_failover_total{{color="{color}"}} {health["failovers"]}')
        lines.append(f'bgdeploy_router_weights{{color="{color}"}} {weights.get(color, 0)}')
        for url, inst in health["instances"].items():
            label = f'color="{color}",instance="{url}"'
            lines.append(f"bgdeploy_router_backend_up{{{label}}} {1 if inst['healthy'] else 0}")
    lines.append(f"bgdeploy_router_uptime_seconds {round(time.time() - state['started'], 1)}")
    return "\n".join(lines) + "\n"


def create_app(cfg: dict) -> web.Application:
    """Create the router application.

    ``cfg`` may be built by parse_args() or handed over directly in tests.
    Backend values may be single URLs, comma-separated strings or lists.
    """
    weights_file = cfg.get("weights_file")
    health_path = cfg.get("health_path", "/healthz")
    health_interval = cfg.get("health_interval", 2.0)
    weights_poll = cfg.get("weights_poll", 1.0)
    backends = {c: split_urls(cfg["backends"][c]) for c in COLORS}

    app = web.Application()
    app[BG_STATE] = {
        "backends": backends,
        "weights": dict(cfg.get("initial_weights", {"blue": 100, "green": 0})),
        "weights_file": weights_file,
        "health": default_health(backends),
        "session": None,
        "started": time.time(),
    }

    async def read_weights() -> None:
        state = app[BG_STATE]
        if not weights_file:
            return
        try:
            data = json.loads(Path(weights_file).read_text())
            weights = {c: int(data.get(c, 0)) for c in COLORS}
            _normalize(weights)
            state["weights"] = weights
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - a bad file must not kill the router
            LOG.warning("could not read weights file %s", weights_file, exc_info=True)

    async def check_backend(color: str, url: str) -> None:
        state = app[BG_STATE]
        session = state["session"]
        inst = state["health"][color]["instances"][url]
        ok = False
        try:
            async with session.get(url + health_path, timeout=2) as resp:
                ok = resp.status < 500 and resp.status != 404
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            if inst["healthy"] is False:
                inst["since"] = time.time()
            inst["consecutive_failures"] = 0
        else:
            inst["consecutive_failures"] += 1
        inst["healthy"] = ok
        inst["checked"] = time.time()

    async def health_loop() -> None:
        state = app[BG_STATE]
        while True:
            for color, urls in state["backends"].items():
                for url in urls:
                    try:
                        await check_backend(color, url)
                    except Exception:  # noqa: BLE001
                        LOG.exception("health check failed for %s", url)
                _aggregate(state, color)
            await asyncio.sleep(health_interval)

    async def weights_loop() -> None:
        while True:
            try:
                await read_weights()
            except Exception:  # noqa: BLE001
                LOG.exception("weights refresh failed")
            await asyncio.sleep(weights_poll)

    async def proxy(request: web.Request) -> web.StreamResponse:
        state = app[BG_STATE]
        session = state["session"]
        aggregate = {c: state["health"][c]["healthy"] for c in COLORS}
        color, favorite = pick_backend(state["weights"], aggregate)
        if color is None:
            return web.json_response(
                {"error": "no healthy backend available"},
                status=503,
                headers={"X-Router-Failover": "true", "X-Served-By": "none"},
            )
        failed_over = color != favorite
        path = request.rel_url.raw_path
        query = request.rel_url.query_string
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length")}
        body = await request.read() if request.can_read_body else None

        # Try every healthy instance of the chosen color before giving up; a
        # mid-request failure just moves to the next replica.
        urls = list(state["health"][color]["instances"].keys())
        random.shuffle(urls)
        for url in urls:
            inst = state["health"][color]["instances"][url]
            if not inst["healthy"]:
                continue
            try:
                async with session.request(request.method, url + path + (f"?{query}" if query else ""),
                                           headers=headers, data=body, timeout=15) as upstream:
                    upstream_body = await upstream.read()
                inst["requests"] += 1
                state["health"][color]["requests"] += 1
                if failed_over:
                    state["health"][color]["failovers"] += 1
                return web.Response(
                    body=upstream_body,
                    status=upstream.status,
                    headers={
                        "X-Served-By": color,
                        "X-Served-Instance": url,
                        "X-Router-Failover": "true" if failed_over else "false",
                        "Content-Type": upstream.headers.get("Content-Type", "application/json"),
                    },
                )
            except Exception:  # noqa: BLE001 - try the next replica
                LOG.warning("request to %s failed", url)
                inst["healthy"] = False
                inst["consecutive_failures"] += 1
        _aggregate(state, color)
        return web.json_response({"error": "backend unreachable"}, status=502)

    async def router_health(request: web.Request) -> web.Response:
        state = app[BG_STATE]
        return web.json_response(
            {
                "status": "ok",
                "weights": dict(state["weights"]),
                "uptime": round(time.time() - state["started"], 1),
                "backends": state["health"],
            }
        )

    async def router_ping(request: web.Request) -> web.Response:
        return web.Response(text="pong")

    async def router_metrics(request: web.Request) -> web.Response:
        state = app[BG_STATE]
        return web.Response(
            text=_metrics_text(state),
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        )

    async def on_startup(app_: web.Application) -> None:
        state = app_[BG_STATE]
        state["session"] = ClientSession()
        await read_weights()
        state["tasks"] = []
        if cfg.get("start_background", True):
            state["tasks"].append(asyncio.create_task(health_loop()))
            state["tasks"].append(asyncio.create_task(weights_loop()))

    async def on_cleanup(app_: web.Application) -> None:
        state = app_[BG_STATE]
        session = state.get("session")
        if session:
            await session.close()
        for task in state.get("tasks", []):
            task.cancel()
        if state.get("tasks"):
            await asyncio.gather(*state["tasks"], return_exceptions=True)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/__router/health", router_health)
    app.router.add_get("/__router/ping", router_ping)
    app.router.add_get("/__router/metrics", router_metrics)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s router %(levelname)s %(message)s")
    cfg = parse_args()
    web.run_app(create_app(cfg), host="0.0.0.0", port=cfg["port"])


if __name__ == "__main__":
    main()
