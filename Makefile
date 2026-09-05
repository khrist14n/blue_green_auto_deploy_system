# Blue/green auto-deployment system - developer convenience targets.
# Container engine is auto-detected (docker first, podman second); override
# with:  make deploy ENGINE=podman   or   export BG_ENGINE=podman

PY     ?= .venv/bin/python
BG     ?= .venv/bin/bgdeploy
VERSION ?= dev
ENGINE ?= auto
ENGINE_ARG := $(if $(filter auto,$(ENGINE)),,--engine $(ENGINE))
PORT   ?= 8090

.PHONY: setup test deploy rollback status shift watch recover smoke api demo teardown help

help:
	@echo "targets:"
	@echo "  make setup                 create venv + install deps"
	@echo "  make test                  run the unit/integration test suite"
	@echo "  make deploy VERSION=v1.2   blue/green deploy (engine: docker|podman|auto)"
	@echo "  make rollback              single-click rollback to the previous release"
	@echo "  make status                show colors, versions, weights, containers"
	@echo "  make shift COLOR=green PERCENT=50   manual traffic split"
	@echo "  make watch                 continuous auto-recovery loop"
	@echo "  make recover               one auto-recovery pass"  @echo "  make api PORT=8090       HTTP control API + web UI (deploy/rollback buttons;"
  @echo "                            set BG_API_PASSWORD/BG_API_TOKEN to require login)"
	@echo "  make smoke URL=http://127.0.0.1:8080   run smoke suite against a URL"
	@echo "  make demo VERSION1=v1.0 VERSION2=v2.0  deploy twice to see a full cycle"
	@echo "  make teardown              remove containers/network/data created by bgdeploy"

setup:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e . -r requirements-dev.txt
	@echo "setup complete - run: make test"

test:
	$(PY) -m pytest

deploy:
	$(BG) $(ENGINE_ARG) deploy --version $(VERSION)

rollback:
	$(BG) $(ENGINE_ARG) rollback

status:
	$(BG) $(ENGINE_ARG) status

shift:
	$(BG) shift --color $(COLOR) --percent $(PERCENT)

watch:
	$(BG) $(ENGINE_ARG) watch

recover:
	$(BG) $(ENGINE_ARG) recover

api:
	$(BG) $(ENGINE_ARG) api --port $(PORT)

smoke:
	$(BG) smoke --url $(URL)

demo:
	$(BG) $(ENGINE_ARG) deploy --version $(VERSION1)
	$(BG) $(ENGINE_ARG) deploy --version $(VERSION2)
	$(BG) $(ENGINE_ARG) status

teardown:
	-docker rm -f $$(docker ps -aq --filter label=bgdeploy.managed=true) 2>/dev/null || true
	-docker network rm bgdeploy 2>/dev/null || true
	-podman rm -f $$(podman ps -aq --filter label=bgdeploy.managed=true) 2>/dev/null || true
	-podman network rm bgdeploy 2>/dev/null || true
	-rm -rf .bgdeploy
	@echo "teardown complete"
