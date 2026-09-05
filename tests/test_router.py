import asyncio
import json

from aiohttp import ClientSession, web

from router.app import create_app


def _backend_app(color: str, state: dict) -> web.Application:
    app = web.Application()

    async def health(_request):
        if state["ok"]:
            return web.json_response({"status": "ok"})
        return web.json_response({"status": "unhealthy"}, status=503)

    async def root(_request):
        return web.json_response({"color": color, "message": f"hello from {color}"})

    app.router.add_get("/healthz", health)
    app.router.add_get("/", root)
    return app


async def _serve(app: web.Application):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def _write_weights(path, blue, green):
    path.write_text(json.dumps({"blue": blue, "green": green}))


async def _wait_backend(session, base, color, healthy, timeout=4.0):
    """Poll the router's health view until `color` reaches the wanted state."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    url = base + "/__router/health"
    while loop.time() < deadline:
        async with session.get(url) as resp:
            data = await resp.json()
        if data["backends"][color]["healthy"] is healthy:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"router never saw {color} healthy={healthy}")


def test_router_shifts_traffic_by_weights(tmp_path):
    async def scenario():
        blue_state = {"ok": True}
        green_state = {"ok": True}
        blue_runner, blue_port = await _serve(_backend_app("blue", blue_state))
        green_runner, green_port = await _serve(_backend_app("green", green_state))
        weights_file = tmp_path / "weights.json"
        _write_weights(weights_file, blue=100, green=0)

        router_app = create_app({
            "backends": {"blue": f"http://127.0.0.1:{blue_port}",
                         "green": f"http://127.0.0.1:{green_port}"},
            "health_path": "/healthz",
            "health_interval": 0.05,
            "weights_poll": 0.05,
            "weights_file": str(weights_file),
            "initial_weights": {"blue": 100, "green": 0},
        })
        router_runner, router_port = await _serve(router_app)
        base = f"http://127.0.0.1:{router_port}"
        try:
            async with ClientSession() as session:
                await _wait_backend(session, base, "blue", True)
                await _wait_backend(session, base, "green", True)

                # 100% blue
                for _ in range(6):
                    async with session.get(base + "/") as resp:
                        assert resp.headers["X-Served-By"] == "blue"
                        assert resp.headers["X-Router-Failover"] == "false"
                        body = await resp.json()
                        assert body["color"] == "blue"

                # shift to 100% green by rewriting the weights file
                _write_weights(weights_file, blue=0, green=100)
                await asyncio.sleep(0.3)  # let the router poll the new weights
                for _ in range(6):
                    async with session.get(base + "/") as resp:
                        assert resp.headers["X-Served-By"] == "green"
                        body = await resp.json()
                        assert body["color"] == "green"
        finally:
            await router_runner.cleanup()
            await blue_runner.cleanup()
            await green_runner.cleanup()

    asyncio.run(scenario())


def test_router_fails_over_when_weighted_backend_unhealthy(tmp_path):
    async def scenario():
        blue_state = {"ok": True}
        green_state = {"ok": True}
        blue_runner, blue_port = await _serve(_backend_app("blue", blue_state))
        green_runner, green_port = await _serve(_backend_app("green", green_state))
        weights_file = tmp_path / "weights.json"
        _write_weights(weights_file, blue=100, green=0)

        router_app = create_app({
            "backends": {"blue": f"http://127.0.0.1:{blue_port}",
                         "green": f"http://127.0.0.1:{green_port}"},
            "health_path": "/healthz",
            "health_interval": 0.05,
            "weights_poll": 0.05,
            "weights_file": str(weights_file),
            "initial_weights": {"blue": 100, "green": 0},
        })
        router_runner, router_port = await _serve(router_app)
        base = f"http://127.0.0.1:{router_port}"
        try:
            async with ClientSession() as session:
                await _wait_backend(session, base, "blue", True)
                await _wait_backend(session, base, "green", True)

                # kill blue while weights still say 100% blue
                blue_state["ok"] = False
                await _wait_backend(session, base, "blue", False)

                async with session.get(base + "/") as resp:
                    assert resp.headers["X-Served-By"] == "green"
                    assert resp.headers["X-Router-Failover"] == "true"
                    body = await resp.json()
                    assert body["color"] == "green"

                # router health view reflects the failover state
                async with session.get(base + "/__router/health") as resp:
                    data = await resp.json()
                assert data["backends"]["green"]["failovers"] >= 1
        finally:
            await router_runner.cleanup()
            await blue_runner.cleanup()
            await green_runner.cleanup()

    asyncio.run(scenario())


def test_router_returns_503_when_all_backends_down(tmp_path):
    async def scenario():
        blue_state = {"ok": False}
        green_state = {"ok": False}
        blue_runner, blue_port = await _serve(_backend_app("blue", blue_state))
        green_runner, green_port = await _serve(_backend_app("green", green_state))
        weights_file = tmp_path / "weights.json"
        _write_weights(weights_file, blue=100, green=0)

        router_app = create_app({
            "backends": {"blue": f"http://127.0.0.1:{blue_port}",
                         "green": f"http://127.0.0.1:{green_port}"},
            "health_path": "/healthz",
            "health_interval": 0.05,
            "weights_poll": 0.05,
            "weights_file": str(weights_file),
            "initial_weights": {"blue": 100, "green": 0},
        })
        router_runner, router_port = await _serve(router_app)
        base = f"http://127.0.0.1:{router_port}"
        try:
            async with ClientSession() as session:
                await _wait_backend(session, base, "blue", False)
                await _wait_backend(session, base, "green", False)
                async with session.get(base + "/") as resp:
                    assert resp.status == 503
        finally:
            await router_runner.cleanup()
            await blue_runner.cleanup()
            await green_runner.cleanup()

    asyncio.run(scenario())


def test_router_load_balances_replicas_and_fails_over_between_instances(tmp_path):
    async def scenario():
        blue_state = {"ok": True}
        green_a = {"ok": True}
        green_b = {"ok": True}
        blue_runner, blue_port = await _serve(_backend_app("blue", blue_state))
        ga_runner, ga_port = await _serve(_backend_app("green", green_a))
        gb_runner, gb_port = await _serve(_backend_app("green", green_b))
        weights_file = tmp_path / "weights.json"
        _write_weights(weights_file, blue=0, green=100)
        green_urls = f"http://127.0.0.1:{ga_port},http://127.0.0.1:{gb_port}"

        router_app = create_app({
            "backends": {"blue": f"http://127.0.0.1:{blue_port}", "green": green_urls},
            "health_path": "/healthz",
            "health_interval": 0.05,
            "weights_poll": 0.05,
            "weights_file": str(weights_file),
            "initial_weights": {"blue": 0, "green": 100},
        })
        router_runner, router_port = await _serve(router_app)
        base = f"http://127.0.0.1:{router_port}"
        green_a_url = f"http://127.0.0.1:{ga_port}"
        green_b_url = f"http://127.0.0.1:{gb_port}"
        try:
            async with ClientSession() as session:
                await _wait_backend(session, base, "blue", True)
                await _wait_backend(session, base, "green", True)

                # 100% green: both replicas are healthy and get used
                seen = set()
                for _ in range(60):
                    async with session.get(base + "/") as resp:
                        assert resp.headers["X-Served-By"] == "green"
                        assert resp.headers["X-Router-Failover"] == "false"
                        seen.add(resp.headers["X-Served-Instance"])
                assert seen <= {green_a_url, green_b_url}
                assert len(seen) == 2  # load balances across replicas

                # kill replica A: green stays up via replica B
                green_a["ok"] = False
                await _wait_instance(session, base, "green", green_a_url, False)
                async with session.get(base + "/") as resp:
                    assert resp.headers["X-Served-By"] == "green"
                    assert resp.headers["X-Served-Instance"] == green_b_url

                # kill replica B too: whole green color down -> router fails over to blue
                green_b["ok"] = False
                await _wait_backend(session, base, "green", False)
                async with session.get(base + "/") as resp:
                    assert resp.headers["X-Served-By"] == "blue"
                    assert resp.headers["X-Router-Failover"] == "true"
        finally:
            await router_runner.cleanup()
            await blue_runner.cleanup()
            await ga_runner.cleanup()
            await gb_runner.cleanup()

    asyncio.run(scenario())


async def _wait_instance(session, base, color, url, healthy, timeout=4.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with session.get(base + "/__router/health") as resp:
            data = await resp.json()
        if data["backends"][color]["instances"][url]["healthy"] is healthy:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"instance {url} never reached healthy={healthy}")


def test_router_metrics_endpoint(tmp_path):
    async def scenario():
        green_state = {"ok": True}
        backend_runner, backend_port = await _serve(_backend_app("green", green_state))
        weights_file = tmp_path / "weights.json"
        _write_weights(weights_file, blue=0, green=100)
        router_app = create_app({
            "backends": {"blue": "http://127.0.0.1:9", "green": f"http://127.0.0.1:{backend_port}"},
            "health_path": "/healthz",
            "health_interval": 0.05,
            "weights_poll": 0.05,
            "weights_file": str(weights_file),
            "initial_weights": {"blue": 0, "green": 100},
        })
        router_runner, router_port = await _serve(router_app)
        base = f"http://127.0.0.1:{router_port}"
        try:
            async with ClientSession() as session:
                await _wait_backend(session, base, "green", True)
                for _ in range(3):
                    await session.get(base + "/")
                async with session.get(base + "/__router/metrics") as resp:
                    assert resp.status == 200
                    text = await resp.text()
                assert 'bgdeploy_router_requests_total{color="green"} 3' in text
                assert 'bgdeploy_router_requests_total{color="blue"} 0' in text
                assert 'bgdeploy_router_weights{color="green"} 100' in text
                assert (
                    f'bgdeploy_router_backend_up{{color="green",instance="http://127.0.0.1:{backend_port}"}} 1'
                    in text
                )
                assert 'bgdeploy_router_uptime_seconds' in text
        finally:
            await router_runner.cleanup()
            await backend_runner.cleanup()

    asyncio.run(scenario())


def test_router_admin_endpoints():
    async def scenario():
        blue_runner, _port = await _serve(_backend_app("blue", {"ok": True}))
        router_app = create_app({
            "backends": {"blue": "http://127.0.0.1:9", "green": "http://127.0.0.1:9"},
            "weights_file": "",
            "initial_weights": {"blue": 100, "green": 0},
        })
        router_runner, router_port = await _serve(router_app)
        base = f"http://127.0.0.1:{router_port}"
        try:
            async with ClientSession() as session:
                async with session.get(base + "/__router/ping") as resp:
                    assert resp.status == 200
                    assert await resp.text() == "pong"
                async with session.get(base + "/__router/health") as resp:
                    data = await resp.json()
                assert data["weights"] == {"blue": 100, "green": 0}
                assert set(data["backends"]) == {"blue", "green"}
        finally:
            await router_runner.cleanup()
            await blue_runner.cleanup()

    asyncio.run(scenario())
