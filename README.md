# Blue/Green Auto-Deployment System

A self-contained blue/green deployment platform written in Python and
**dockerized end-to-end**. It ships with a demo service, a traffic-shifting
router, and a pure-stdlib orchestrator (`bgdeploy`) that automates the whole
release lifecycle:

> **deploy → health check → automated smoke tests → gradual traffic shift →
> client-side verification → commit** ... and, when something goes wrong,
> **single-click rollback** or **automatic failover / self-healing**.

It runs against **docker or podman** interchangeably, has first-class
**testability** (a 92-test suite that fakes the engine and talks over real
HTTP sockets, plus opt-in real-engine and full-pipeline tests), and
implements **auto-recovery in code** at two levels.

Extras: an **HTTP control API with a web UI** (one-click rollback button),
**GitHub Actions CI + tag-triggered releases**, **multiple replicas per color**
with per-instance health, and **Prometheus metrics + recovery alert webhooks**.

---

## Features → where they live

| Feature | How | Code |
|---|---|---|
| Containerized service | FastAPI demo app + `Dockerfile`, image built & tagged per release | `service/` |
| Blue/green deployment | inactive color is replaced, validated, then activated; old color stays warm for rollback | `bgdeploy/deployer.py` |
| Traffic shifting | weighted router splits requests; orchestrator ramps `25 → 50 → 75 → 100%` and **verifies** from the client side | `router/app.py`, `bgdeploy/traffic.py` |
| Automated smoke tests | HTTP assertion suite run against the candidate *before* any traffic moves | `bgdeploy/smoke.py`, `smoke-tests.json` |
| Single-click rollback | `bgdeploy rollback` / `make rollback` | `bgdeploy/deployer.py` |
| Podman support | engine abstraction over the docker/podman CLI with auto-detection | `bgdeploy/engine.py` |
| Auto-recovery | router fails over per request; monitor durably flips traffic, restarts sick containers, restores when stable | `router/app.py`, `bgdeploy/recovery.py` |
| Replicas per color | `BG_REPLICAS` containers per color, per-instance health & load balancing | `router/app.py`, `bgdeploy/deployer.py` |
| Prometheus metrics | `/__router/metrics` text exposition (requests, failovers, instance up, weights) | `router/app.py` |
| Recovery alerts | `BG_ALERT_WEBHOOK_URL` receives JSON on failover/degraded/restore/self-heal failures | `bgdeploy/recovery.py` |
| Control API + web UI | `bgdeploy api`: deploy/rollback/status over HTTP, one-click rollback button | `bgdeploy/api.py` |
| CI / tag releases | `.github/workflows/ci.yml` + `release.yml`, GitHub webhook auto-deploy | `.github/` |
| Testing | unit + integration tests (fake engine, live HTTP backends, live router, live API) | `tests/` |

## Architecture

```
                        ┌─────────────────────────────────────────────┐
   users / curl ───────▶│  ROUTER  (bgdeploy-router, containerized)  │
                        │  reads weights.json on a poll loop          │
                        │  health-checks both backends                │
                        │  per-request auto-failover                  │
                        └──────────────┬───────────────┬──────────────┘
                                       │               │
                             ┌─────────▼─────┐   ┌─────▼─────────┐
                             │ app-blue  (v1)│   │ app-green (v2)│   ← demo service containers
                             │ color: blue   │   │ color: green  │      (published ports are
                             └─────────┬─────┘   └─────┬─────────┘      loopback-only, used by
                                       │               │               health + smoke checks)
                                       └───────────────┘
   bgdeploy orchestrator (host) ── docker/podman CLI, writes .bgdeploy/weights.json,
   state.json, deploy.lock; runs smoke tests over HTTP; RecoveryMonitor watches router health.
```

Three moving parts, all containers except the orchestrator:

1. **`service/`** – the deployable app. Exposes `/`, `/version`, `/healthz`,
   `/readyz`. Env-configurable: `APP_VERSION`, `APP_COLOR` and
   `UNHEALTHY_AFTER` (fault injection for recovery demos).
2. **`router/`** – the ingress. Proxies to `app-blue`/`app-green`, splits
   traffic by the weights file, and fails a request over to the healthy color
   when the weighted favorite is down. Admin surface: `/__router/health`,
   `/__router/ping`.
3. **`bgdeploy/`** – the orchestrator (stdlib only). `deploy`, `rollback`,
   `status`, `shift`, `smoke`, `recover`, `watch`.

Deployment state lives in `.bgdeploy/state.json` (trivially inspectable);
traffic weights in `.bgdeploy/weights.json`; a cross-process file lock keeps
deploys single-flight.

## Quickstart

Requires Python 3.10+ and a working **docker** or **podman** daemon.

```bash
make setup                          # create .venv, install deps + this package
make test                           # run the test suite (no daemon needed)
```

### Docker

```bash
make deploy VERSION=v1.0.0          # builds service + router, bootstraps blue
curl http://127.0.0.1:8080/version  # -> served by the active color

make deploy VERSION=v2.0.0          # green gets v2, traffic ramps 25→50→75→100%
make status                         # colors, versions, live weights, containers

# watch the shift happen in real time (second terminal):
while true; do curl -s http://127.0.0.1:8080/ | grep color; sleep 0.5; done
```

### Podman

Everything is identical – the engine is auto-detected, or forced explicitly:

```bash
export BG_ENGINE=podman             # or: make deploy ENGINE=podman
make deploy VERSION=v1.0.0
# or with the CLI directly:
bgdeploy deploy --engine podman --version v1.0.0
```

`BG_ENGINE` accepts `docker` | `podman` | `auto` (default). No other change is
required: bgdeploy shells out to the same CLI surface (`run`, `build`,
`inspect`, `network`, ...) for both engines.

Podman-specific notes:

- **No daemon, rootless by default** – `podman version` always succeeds, so
  bgdeploy probes the actual runtime with `podman info` when checking whether
  the engine is usable (covers storage/network/cgroups setup).
- **Rootless**: works out of the box for this stack (loopback port publishing,
  per-container networks, image builds via buildah). No `sudo` needed.
- **CI**: `.github/workflows/ci.yml` runs a full blue/green smoke deploy on
  podman (install → `deploy v1 → verify blue → deploy v2 → verify green →
  rollback`) in addition to the docker leg.
- **Inspect JSON**: podman returns inspect data in the same shape as docker;
  bgdeploy tolerates both single-object and array forms.
- The `Makefile` teardown target cleans up podman containers/network too
  (`make teardown ENGINE=podman`).

## Release lifecycle (what `deploy` does)

1. Pick the **inactive** color as the target (blue on a fresh stack).
2. Build `bgdeploy/service:<version>` and replace the target color's container.
3. Wait for `/healthz`, then run the **automated smoke suite** (from
   `smoke-tests.json`; `{version}` placeholders are substituted).
   → A failing release is **removed here**; live traffic never touched.
4. Shift traffic **gradually** toward the candidate (`BG_SHIFT_STEPS`).
5. **Verify** by sampling N requests through the router: every response must
   carry `X-Served-By: <candidate>`.
   → If verification fails, traffic is **automatically shifted back** and the
   candidate is cleaned up.
6. Commit: candidate becomes active, previous release is recorded, and the old
   color is kept running (warm) so rollback is instant.

## Multiple replicas per color

Set `BG_REPLICAS` to run N identical containers per color (e.g. `app-green-1`,
`app-green-2`, ...). The router health-checks **every instance**, load-balances
within the chosen color, and only treats a color as down when *all* its
instances are unhealthy. A mid-request failure to one replica moves to the
next. Changing `BG_REPLICAS` recreates the router with the new backend list
on the next deploy.

```bash
BG_REPLICAS=3 make deploy VERSION=v1.2.0
curl http://127.0.0.1:8080/__router/health   # see per-instance health
```

## Rollback

```bash
make rollback                       # the "single click"
```

Re-verifies the previous release with smoke tests, flips traffic to it, and
records the swap. State always remembers the last two environments
(`active_color`/`previous`), so `deploy → rollback → deploy` works as you'd
expect.

## Traffic shifting by hand (canary / A-B)

```bash
make shift COLOR=green PERCENT=10   # 10% to green, 90% stays on blue
make shift COLOR=green PERCENT=100  # commit fully
```

## Auto-recovery (in code, two layers)

1. **Router (per request, instant):** if the weighted-favorite backend fails a
   health check, requests are served by the other color with
   `X-Router-Failover: true`. No orchestrator round-trip needed.
2. **RecoveryMonitor (durable):** run continuously with `bgdeploy watch`
   (or one-off via `bgdeploy recover`, e.g. from a systemd timer). The
   failover decision is persisted in `state.json`, so recovery context survives
   monitor restarts and `recover` + `watch` can be separate processes. It:
   - **FAILOVER** – when the active color is unhealthy and the other is
     healthy, shifts 100% of traffic durably and records an incident;
   - **SELF-HEAL** – restarts the sick container (rate-limited by
     `BG_RESTART_COOLDOWN`) so it can recover;
   - **RESTORE** – when the original color has been healthy for N consecutive
     checks (`BG_RESTORE_STEADY`, opt-out with `BG_AUTO_RESTORE=false`),
     traffic shifts back and the marker is cleared.

A manual `deploy`/`rollback` commit clears the failover marker, so a new
release always starts from a clean slate.

### Live recovery demo

Deploy a release that "gets sick" 20 seconds after boot, then watch the
monitor fail over, restart it, and restore traffic once it is healthy again:

```bash
make deploy VERSION=v3.0.0          # regular release on the current inactive color
BG_APP_EXTRA_ENV=UNHEALTHY_AFTER=20 bgdeploy deploy --version v3.1.0   # or: BG_APP_EXTRA_ENV=UNHEALTHY_AFTER=20 make deploy VERSION=v3.1.0
make watch                          # failover -> self-heal -> restore in the logs
curl http://127.0.0.1:8080          # still answered the whole time
```

`BG_APP_EXTRA_ENV` injects extra `KEY=VALUE` pairs into the app container
(comma-separated); the demo service interprets `UNHEALTHY_AFTER` as "start
failing /healthz after N seconds".

## Testing

```bash
make test                 # or: .venv/bin/python -m pytest
```

The default suite (92 tests) never needs a container daemon (except the
real-engine tests, which **auto-skip** when docker/podman is absent):

- `tests/test_deployer.py` – the full pipeline against a **FakeEngine** and
  real threaded HTTP backends: bootstrap, ramp order, smoke-failure abort,
  shift-failure auto-rollback, rollback, replica sets.
- `tests/test_api.py` – the **control API over HTTP**: deploy/rollback jobs,
  status, web UI, GitHub webhook (incl. token auth), smoke-failure jobs, and
  authentication (login/session, bearer token, CSRF guard, logout, login
  rate limiting + account lockout).
- `tests/test_router.py` – a **live aiohttp router**: weighted shifting via
  the weights file, per-request failover, full outage → 503, replica load
  balancing + per-instance failover, Prometheus metrics.
- `tests/test_recovery.py` – failover, degraded, self-heal + cooldown,
  automatic restore, sticky failover when auto-restore is off.
- `tests/test_smoke.py`, `test_traffic.py`, `test_state.py`, `test_engine.py`
  – smoke runner, sampling/verification, state + locking, engine selection
  and `inspect` parsing.

### Real-engine tests (docker + podman)

`tests/test_real_engine.py` drives the **actual `ContainerEngine` CLI**
against whichever engine is installed and reachable — parametrized over
**docker and podman** — and skips automatically when none is:

- full lifecycle: `network_ensure` (idempotent) → `run_container` with
  labels/env/port publish → discover the host port via `inspect` → actually
  serve HTTP through it → container IP + labels → `restart` (host port is
  re-read, since daemons may rebind random ports) → `stop` → `remove`
- image build parity (`docker build` vs podman buildah) + `image_exists`
- managed-container listing (`ps_managed`) + log capture

These run automatically in CI against both engines (the `test` job on
Docker, the `podman-smoke-deploy` job after installing podman). Disable
locally with `BG_TEST_REAL_ENGINE=0`.

### Opt-in full-pipeline test (`-m realdeploy`)

`tests/test_real_deploy.py` builds the real service + router images and runs
the entire blue/green lifecycle through the real router — `deploy v1 →
verify blue → deploy v2 → verify green → rollback → verify blue` — against
each real engine. It is excluded from the default run (rebuilds images,
takes minutes) and opt-in per engine:

```bash
pytest -m realdeploy                    # both docker and podman
BG_ENGINE=podman pytest -m realdeploy   # pin one engine
```

Every piece of state is namespaced per run (container prefix, router name,
network, port, data dir), so it is safe to run next to a live stack.

## Observability: Prometheus metrics + recovery alerts

The router exposes Prometheus text metrics at `/__router/metrics`:
`bgdeploy_router_requests_total{color}`, `bgdeploy_router_failover_total{color}`,
`bgdeploy_router_backend_up{color,instance}` (per replica), `bgdeploy_router_weights{color}`
and `bgdeploy_router_uptime_seconds` - scrape it with your Prometheus of choice.

The recovery monitor can POST JSON alerts when it acts. Set
`BG_ALERT_WEBHOOK_URL` (any HTTP endpoint; e.g. a Slack/Teams/ntfy hook);
events `failover`, `degraded`, `restore`, `self-heal-failed` and `monitor` are
delivered with `{kind, detail, active_color, active_version, weights}`, rate
limited by `BG_ALERT_MIN_INTERVAL` (default 60s).

```bash
BG_ALERT_WEBHOOK_URL=https://hooks.example.com/deploy .venv/bin/bgdeploy watch
```

## Control API + web UI (`make api`)

Expose deploy/rollback/status over HTTP with a single self-contained page that
has a **one-click rollback button**:

```bash
make api              # http://127.0.0.1:8090
# or:  .venv/bin/bgdeploy api --port 8090
```

| Endpoint | Meaning |
|---|---|
| `GET  /` | embedded web UI (status, deploy form, rollback button, job log) |
| `GET  /api/status` | same as `bgdeploy status` |
| `POST /api/jobs` | `{"action": "deploy"\|"rollback", "version": ...}` → `202` + job id |
| `GET  /api/jobs` , `GET /api/jobs/{id}` | job queue incl. live log lines |
| `POST /api/webhook/github` | GitHub push webhook – tag pushes auto-queue a deploy |
| `GET  /api/healthz` | liveness (always open) |
| `GET  /api/session` | auth state: `{authenticated, auth_enabled, username}` |
| `POST /api/login` | `{"username", "password"}` → session token + HttpOnly cookie |
| `POST /api/logout` | invalidate the session |

Deploys run as background jobs (builds + smoke + ramp take minutes); the UI
polls the job and streams its log. The deployer file lock keeps concurrent
deploys/rollbacks single-flight, and job failures (e.g. smoke tests) surface
as `failed` jobs with the reason in `error`. Protect the webhook with
`BG_WEBHOOK_TOKEN` – when set, requests need the matching `X-Bg-Token` header.

### Authentication (`BG_API_PASSWORD` / `BG_API_TOKEN`)

Auth is **off by default** (the API binds to `127.0.0.1`). Set a password to
require login, a static API token for programmatic access, or both:

```bash
BG_API_PASSWORD='change-me' .venv/bin/bgdeploy api
```

- **Web UI:** `GET /` serves a sign-in page; after login the HttpOnly
  `bgdeploy_session` cookie unlocks the panel. Logout button in the header.
- **API clients:** `Authorization: Bearer <token>` where the token is the
  login response token or the static `BG_API_TOKEN`.
- `GET /api/healthz`, `GET /api/session` and `POST /api/login` stay open;
  everything else returns `401` without a valid credential.
- Cookie-authenticated `POST`s must be same-origin (CSRF-guarded via
  `Origin`/`Referer`); use a bearer token from scripts/curl.
- `BG_API_USER` (default `admin`) and `BG_API_SESSION_TTL` (default 12h)
  tune the login. Sessions are held in memory, so restarting the API logs
  everyone out.
- **Brute-force protection:** `/api/login` is rate-limited per client IP
  (`BG_LOGIN_RATE_LIMIT`, default 5 attempts per `BG_LOGIN_RATE_WINDOW`,
  default 60s) and accounts lock after `BG_LOGIN_LOCKOUT_ATTEMPTS`
  consecutive failures (default 5) for `BG_LOGIN_LOCKOUT_SECONDS` (default
  15 min). Blocked attempts return `429` with a `Retry-After` header; a
  successful login resets the counters. Set `BG_API_TRUST_PROXY=true` when
  behind a reverse proxy so `X-Forwarded-For` identifies the real client.
  Guard state (attempt counters + lockouts) is persisted atomically to
  `.bgdeploy/auth.json`, so lockouts survive API restarts; sessions stay
  in-memory and reset on restart.

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8090/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"change-me"}' | .venv/bin/python -c \
  'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s http://127.0.0.1:8090/api/status -H "Authorization: Bearer $TOKEN"
```

For a remote deployment, put the API behind TLS (reverse proxy) since
credentials travel over plain HTTP otherwise.

## Git tags & webhooks (automated releases)

Two ways to get automated deploys on release:

1. **GitHub Actions release workflow** (`.github/workflows/release.yml`) – on a
   `v*` tag it runs the test suite and then either pushes the service image
   and triggers `bgdeploy` on a deployment host (self-hosted runner) or POSTs
   to the control API webhook (`CONTROL_API_URL` + `BG_WEBHOOK_TOKEN` secrets)
   to auto-deploy the tagged version.
2. **GitHub webhook** – point a repository webhook at
   `http://<host>:8090/api/webhook/github` (Content-Type `application/json`);
   every tag push (`refs/tags/v*`) automatically starts a deploy job for that
   version. Rollbacks stay manual and single-click from the UI.

## CI workflow (`.github/workflows/ci.yml`)

Every push runs three jobs: the unit/integration suite (which also picks up
the real-engine tests against Docker), a **containerized smoke deploy** on
Docker (`bgdeploy deploy v1 → deploy v2 → rollback`, traffic verified
through the router at each step), and the **same smoke deploy on podman**
(installs podman, then runs the identical sequence with `--engine podman`).
The `BG_SHIFT_DWELL=2.0` CI setting is above the router's 1s weights poll to
keep the shift verification deterministic.

## CLI reference

```
bgdeploy deploy --version v1.2.0        blue/green deploy
bgdeploy rollback                       back to the previous release
bgdeploy status                         deployment state + containers + weights
bgdeploy shift --color green --percent 50
bgdeploy smoke --url http://127.0.0.1:8080
bgdeploy recover                        single auto-recovery pass
bgdeploy watch                          continuous auto-recovery
bgdeploy api                            HTTP control API + web UI
```

Configuration is env-driven with `BG_` prefixes (see `bgdeploy/config.py`):
`BG_ENGINE`, `BG_DATA_DIR`, `BG_NETWORK`, `BG_IMAGE`, `BG_ROUTER_PORT`,
`BG_SMOKE_FILE`, `BG_SHIFT_STEPS`, `BG_SHIFT_DWELL`, `BG_VERIFY_SAMPLES`,
`BG_KEEP_OLD`, `BG_REPLICAS`, `BG_APP_EXTRA_ENV`, `BG_RESTART_COOLDOWN`,
`BG_AUTO_RESTORE`, `BG_ALERT_WEBHOOK_URL`, `BG_WEBHOOK_TOKEN`,
`BG_API_USER`, `BG_API_PASSWORD`, `BG_API_TOKEN`, `BG_API_SESSION_TTL`,
`BG_LOGIN_RATE_LIMIT`, `BG_LOGIN_RATE_WINDOW`, `BG_LOGIN_LOCKOUT_ATTEMPTS`,
`BG_LOGIN_LOCKOUT_SECONDS`, `BG_API_TRUST_PROXY`, ... most also have
corresponding CLI flags.

## Layout

```
bgdeploy/            orchestrator (stdlib-only Python package)
  config.py          BG_* env configuration
  engine.py          docker/podman CLI abstraction + auto-detection
  state.py           state.json store + single-flight file lock
  smoke.py           automated smoke-test runner (HTTP)
  traffic.py         weights file, gradual shift, client-side verification
  deployer.py        blue/green pipeline: deploy / rollback / status
  recovery.py        auto-recovery monitor + alert webhooks
  api.py             HTTP control API + web UI + GitHub webhook
  cli.py             bgdeploy command line
router/              containerized traffic router (aiohttp): weights, replicas,
                     failover, Prometheus /__router/metrics
service/             containerized demo service (FastAPI)
.github/workflows/   ci.yml (tests + containerized smoke deploy), release.yml
                     (tag-triggered deploys)
tests/               92-test default suite (fake engine + live HTTP + live API)
                     + real-engine tests (docker/podman, auto-skip) + opt-in
                     `-m realdeploy` full pipeline test
smoke-tests.json     default smoke suite used on every deploy
Makefile             setup / test / deploy / rollback / api / teardown
```
