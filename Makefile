.PHONY: help infra-up infra-down infra-prune install migrate migrate-ch migrate-pg migrate-chat seed demo backend workers frontend docs test send-trace demo-failures gate replay sdk-example auto-openai auto-agent run-all-examples seed-demo seed-regression fmt

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

infra-up:    ## start clickhouse, postgres, redis, minio
	docker compose up -d --wait

infra-down:  ## stop infra (keeps volumes)
	docker compose down

infra-prune: ## stop infra and delete volumes
	docker compose down -v

install:     ## sync python deps (uv, all workspace packages + provider extras) + frontend deps (pnpm)
	uv sync --all-packages --all-extras
	cd frontend && pnpm install

migrate: migrate-ch migrate-pg migrate-chat ## run all migrations

migrate-ch:  ## apply ClickHouse migrations
	uv run python -m tracely.infrastructure.clickhouse.migrations

migrate-pg:  ## apply Postgres (Alembic) migrations
	cd backend && uv run alembic upgrade head

migrate-chat:  ## create LangGraph's checkpoint tables (durable judge conversations)
	uv run python -m tracely.infrastructure.llm.checkpointer

seed:        ## create the default project + ingest key (tracely_dev_key)
	uv run python -m tracely.services.seeding_service

demo:        ## populate the WHOLE product in one go — traces + clusters + Cases + Gates (needs backend+worker up)
	TRACELY_API=$(TRACELY_API) uv run python scripts/seed_demo.py

backend:     ## run FastAPI (ingestion + reads) on :8000
	uv run uvicorn tracely.api.main:app --reload --port 8000

workers:     ## run the Celery worker
	uv run celery -A tracely_workers.worker worker --pool=solo --loglevel=info

frontend:    ## run Next.js on :3000
	cd frontend && pnpm dev

docs:        ## run the SDK documentation site (Nextra) on :3002
	cd docs && pnpm install && pnpm dev

test:        ## run backend tests
	uv run pytest -q backend/tests

send-trace:  ## post a sample OTLP trace to the running API
	uv run python scripts/send_test_trace.py

# Override the target when the API is not on :8000 — e.g. `make demo TRACELY_API=http://localhost:8088`
TRACELY_API ?= http://localhost:8000
demo-failures: ## seed a mix of failing runs (errors + silent + hallucinations) for the clustering demo
	@for i in 1 2 3 4 5; do TRACELY_API=$(TRACELY_API) RANDOM=1 uv run python scripts/send_test_trace.py; done
	@for i in 1 2 3 4 5 6 7 8 9; do TRACELY_API=$(TRACELY_API) RANDOM=1 SILENT=1 uv run python scripts/send_test_trace.py; done
	@for i in 1 2 3 4 5; do TRACELY_API=$(TRACELY_API) RANDOM=1 HALLUCINATE=1 uv run python scripts/send_test_trace.py; done
	@echo "seeded — now hit 'Analyze failures' in the UI (or POST /api/clusters/rebuild)"

gate:        ## run the CI/CD regression gate locally for an agent (TRACELY_AGENT=planner)
	TRACELY_API=$(TRACELY_API) uv run tracely gate $${TRACELY_AGENT:-planner} --env $${GATE_ENV:-ci}

replay:      ## re-run the example agent on the promoted suite, then gate (ENTRYPOINT=weather_agent:run)
	TRACELY_API=$(TRACELY_API) PYTHONPATH=sdk/examples uv run tracely replay $${TRACELY_AGENT:-planner} \
		--entrypoint $${ENTRYPOINT:-weather_agent:run} --env $${GATE_ENV:-replay}

sdk-example: ## emit the demo trace via the Tracely SDK
	uv run python sdk/example.py

auto-openai: ## automatic tracing of plain OpenAI calls (needs tracely-ai[openai] + OPENAI_API_KEY)
	TRACELY_API=$(TRACELY_API) uv run python sdk/examples/auto_openai.py

auto-agent:  ## automatic tracing via @observe + trace() (agent -> gen/tool/gen tree)
	TRACELY_API=$(TRACELY_API) uv run python sdk/examples/auto_agent.py

run-all-examples: ## run every sdk/examples/*.py (skips ones missing a key/dep); pass WIPE=1 to clear traces first
	TRACELY_API=$(TRACELY_API) scripts/run_all_examples.sh $${WIPE:+--wipe}

seed-demo:   ## seed rich demo conversations (every trace shape: RAG, multi-agent, multimodal, …)
	TRACELY_API=$(TRACELY_API) uv run python sdk/examples/seed_conversations.py

seed-regression: ## promote a failing trace, then run red→green CI gates (fills Cases + Gates)
	TRACELY_API=$(TRACELY_API) uv run python sdk/examples/seed_regression.py

fmt:         ## format python
	uv run ruff format .
