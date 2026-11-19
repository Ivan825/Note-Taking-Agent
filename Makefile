.PHONY: up down logs ps health test venv scale kill-worker chaos-429 frame-fail dlq burst clean

GATEWAY ?= http://localhost:8000
WORKERS ?= 3

up:            ## build and start the whole stack (gateway, broker, db, redis, 2 workers)
	docker compose up -d --build
	@./scripts/wait_healthy.sh

down:          ## stop everything (data volumes are kept)
	docker compose down

clean:         ## stop everything and delete volumes
	docker compose down -v

logs:          ## follow worker + gateway logs
	docker compose logs -f --tail=50 gateway worker

ps:
	docker compose ps

health:
	@curl -s $(GATEWAY)/healthz | python3 -m json.tool

scale:         ## run N workers: make scale WORKERS=4
	docker compose up -d --scale worker=$(WORKERS)

venv:
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

test:          ## unit tests (SQLite, no network)
	@test -x .venv/bin/pytest || $(MAKE) venv
	.venv/bin/pytest -q tests

# ---- failure-handling demo helpers (see README "Demo script") ----
kill-worker:   ## SIGKILL the worker processing JOB=<id>
	./scripts/demo/kill_worker.sh $(JOB)

chaos-429:     ## next N calls to API=llm|notion|youtube return 429
	./scripts/demo/chaos_429.sh $(API) $(or $(N),1)

frame-fail:    ## make frame capture fail: SPEC=all | first:2 | 461,700
	./scripts/demo/frame_fail.sh $(or $(SPEC),first:1)

dlq:           ## submit an unavailable video (ends in DEAD_LETTERED)
	./scripts/demo/private_video.sh

burst:         ## submit several videos at once (needs NOTION_TOKEN env)
	./scripts/demo/burst.sh
