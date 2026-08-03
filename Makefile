.PHONY: setup generate format lint test test-unit test-integ build load-test load-test-slow load-test-teardown deploy-dev deploy-serve destroy-dev preflight docs web-dev vkg-dev version version-check

setup: generate
	./scripts/setup-dev.sh

generate:
	./scripts/smithy-generate.sh

format:
	uv run ruff format .
	uv run ruff check --fix .
	pnpm nx run-many -t format

## Propagate the repo-root VERSION into every package manifest. Bump VERSION,
## then run this — never edit a package's version by hand.
version:
	@python3 scripts/sync_version.py

## Fail if any package manifest has drifted from VERSION (runs as part of lint).
version-check:
	@python3 scripts/sync_version.py --check

lint: version-check
	pnpm nx run-many -t lint

test: test-unit

## Per-package unit tests (Nx) plus the repo-level suite in tests/unit, which
## covers cross-package concerns (version sync, NOTICE generation, doc accuracy)
## and belongs to no single Nx project.
test-unit:
	pnpm nx run-many -t test
	uv run pytest tests/unit -q

coverage:
	@python3 scripts/coverage-summary.py

## Run integration tests against all packages with deployed stacks.
## Optional env vars (all auto-resolved if not set):
##   AWS_DEFAULT_REGION  — AWS region (default: us-east-1)
##   ENV_NAME            — environment name (default: dev)
##   INTEG_SECRET_ARN    — skip user provisioning, use this Secrets Manager ARN
##   API_ENDPOINT        — skip CloudFormation lookup, use this API Gateway URL
##   INTEG_NAMESPACE_ID  — reuse an existing namespace (skip create)
test-integ:
	uv run pytest packages/*/tests/integ/ -m integ -v --tb=short --import-mode=importlib

## Load test: validates the pipeline at scale (50 → 50k tables).
## `make load-test` runs the non-billable smoke rung only ('not slow').
## `make load-test-slow RUNG=500` opts into billable 500+ rungs.
## Crash recovery: make load-test-teardown RUN_ID=<id>
load-test:
	uv run pytest tests/integ/load/ -q -m "not slow" --rung $(or $(RUNG),50)

load-test-slow:
	uv run pytest tests/integ/load/ -q -m slow --rung $(or $(RUNG),500)

load-test-teardown:
	uv run python tests/integ/load/teardown_orphan.py $(RUN_ID)
build: notice
	pnpm nx run-many -t build

## Regenerate the root NOTICE (third-party attribution, ORR PAK-A-3) from the
## resolved environment. Run as part of `make build`; commit the result if it
## changes so the checked-in NOTICE stays current with the dependency set.
notice:
	uv run python -m scripts.supply_chain.cli notice

preflight:
	./scripts/preflight-deploy.sh

deploy-dev:
	./scripts/deploy.sh dev

deploy-serve:
	./scripts/deploy-serve.sh dev

## Tear down all dev stacks in one command. Deletes AgentCore Runtimes,
## waits for their ENIs to detach (currently disabled — see destroy.sh),
## deletes VKG's ECS services, force-deletes the DataZone domain (cascades
## to RETAINed child resources CFN can't clear on its own), then runs
## `cdk destroy --all` and verifies no stacks remain (see #660, #661, #707).
## Optional env vars: SCL_PREFIX (default: coa — matches the CDK app), SCL_DESTROY_YES=1 to skip
## the confirmation prompt (e.g. in CI), SCL_ENI_WAIT_MAX_SECONDS (default
## 600), SCL_ECS_WAIT_MAX_SECONDS (default 300), and
## SCL_DOMAIN_WAIT_MAX_SECONDS (default 300) to tune wait budgets.
destroy-dev:
	make generate
	pnpm install
	./scripts/destroy.sh dev

docs:
	cd docs && mkdocs serve

web-dev:
	cd packages/web-app && pnpm install && pnpm dev

vkg-dev:
	docker build -t vkg-local packages/vkg/
	docker run --rm -p 8080:8080 -e ONTOLOGY_BUCKET=local -e NAMESPACE=default -e VERSION=latest vkg-local
