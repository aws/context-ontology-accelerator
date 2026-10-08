.PHONY: setup generate format lint test test-unit test-integ build load-test load-test-slow load-test-teardown deploy-dev deploy-serve deploy-example-connector deploy-databricks-connector destroy-dev preflight docs web-dev vkg-dev version version-check

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
## and belongs to no single Nx project, and ci/tests, which pins internal CI and
## integ-test contracts and is not published to the public mirror.
test-unit:
	pnpm nx run-many -t test
	uv run pytest tests/unit -q
	@if [ -d ci/tests ]; then uv run pytest ci/tests -q; fi
	uv run pytest scripts/agents -q

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
	@if ls packages/*/tests/integ/ >/dev/null 2>&1; then \
		uv run pytest packages/*/tests/integ/ -m integ -v --tb=short --import-mode=importlib; \
	else echo "⚠ integ tests not available (stripped from public mirror)"; fi

## Load test: validates the pipeline at scale (50 → 50k tables).
## `make load-test` runs the non-billable smoke rung only ('not slow').
## `make load-test-slow RUNG=500` opts into billable 500+ rungs.
## Crash recovery: make load-test-teardown RUN_ID=<id>
load-test:
	@if [ -d tests/integ/load ]; then \
		uv run pytest tests/integ/load/ -q -m "not slow" --rung $(or $(RUNG),50); \
	else echo "⚠ load tests not available (stripped from public mirror)"; fi

load-test-slow:
	@if [ -d tests/integ/load ]; then \
		uv run pytest tests/integ/load/ -q -m slow --rung $(or $(RUNG),500); \
	else echo "⚠ load tests not available (stripped from public mirror)"; fi

load-test-teardown:
	@if [ -f tests/integ/load/teardown_orphan.py ]; then \
		uv run python tests/integ/load/teardown_orphan.py $(RUN_ID); \
	else echo "⚠ load tests not available (stripped from public mirror)"; fi
build: notice
	pnpm nx run-many -t build

## Regenerate the root NOTICE (third-party attribution, ORR PAK-A-3) from the
## resolved environment. Run as part of `make build`; commit the result if it
## changes so the checked-in NOTICE stays current with the dependency set.
notice:
	uv run python -m scripts.supply_chain.cli notice

preflight:
	./scripts/preflight-deploy.sh

## Full dev deploy: the platform stacks, then the COA-operated Databricks connector.
## The connector is chained rather than left to be remembered: a DATABRICKS_SQL_WAREHOUSE
## source cannot be registered without it, so a deployment missing it advertises a
## sub-type nothing can serve. The ordering is the one deploy-databricks-connector
## requires anyway, and it is the same order ci/mainline.yml models with a blocking job.
deploy-dev:
	./scripts/deploy.sh dev
	$(MAKE) deploy-databricks-connector

deploy-serve:
	./scripts/deploy-serve.sh dev

## Deploy the example Athena federation connector (connectors/) into a dev account,
## giving integration tests a federated source to query. Separate from deploy-dev on
## purpose: it stands in for something a customer deploys in their own account, and it
## must run AFTER COA — it reads COA's serve and discovery role ARNs from SSM.
## Optional env vars: SCL_PREFIX (default: coa — matches the CDK app),
## FUNCTION_NAME_PREFIX (default <prefix>-<env>-), EXAMPLE_BULK_ROWS /
## EXAMPLE_BULK_ROW_BYTES to size the fixture past Athena's 6 MB limit and exercise spill.
deploy-example-connector:
	./scripts/deploy-example-connector.sh dev

## Deploy the COA-OPERATED Databricks SQL Warehouse connector into a dev account. Unlike
## deploy-example-connector, this one is not a sample standing in for something a customer
## deploys: it is COA's own, one Lambda serving every DATABRICKS_SQL_WAREHOUSE source in
## the environment, and a source of that sub-type cannot be registered until it exists —
## registration resolves this connector's ARN from SSM and fails the create if it is absent.
## Must run AFTER deploy-dev, which writes the role ARNs its stack reads.
## Its function name carries a reserved `-managed-` segment and can never change, because
## every Athena catalog COA creates embeds this function's ARN (see connectors/databricks).
## Optional env vars: SCL_PREFIX (default: coa — matches the CDK app). Refuses to run with
## any single-endpoint DATABRICKS_* / CREDENTIAL_* variable set: those belong to the
## customer-deployed mode of the same connector, and would make one workspace serve every
## namespace.
deploy-databricks-connector:
	./scripts/deploy-managed-databricks-connector.sh dev

## Tear down all dev stacks in one command. Deletes AgentCore Runtimes,
## waits for their ENIs to detach (and stops if they do not),
## deletes VKG's ECS services, force-deletes the DataZone domain (cascades
## to RETAINed child resources CFN can't clear on its own), deletes every connector
## stack (separate CDK apps, so `cdk destroy --all` never sees them), then runs
## `cdk destroy --all` and verifies no stacks remain (see #660, #661, #707).
## Optional env vars: SCL_PREFIX (default: coa — matches the CDK app), SCL_DESTROY_YES=1 to skip
## the confirmation prompt (e.g. in CI), and the wait budgets
## SCL_ENI_WAIT_MAX_SECONDS (600), SCL_ECS_WAIT_MAX_SECONDS (300),
## SCL_DOMAIN_WAIT_MAX_SECONDS (300), SCL_CLOUDMAP_WAIT_MAX_SECONDS (180),
## SCL_CONNECTOR_DELETE_WAIT_MAX_SECONDS (1800).
destroy-dev:
	make generate
	pnpm install
	./scripts/destroy.sh dev

docs:
	@if [ -d docs ]; then cd docs && mkdocs serve; \
	else echo "⚠ docs/ not available (stripped from public mirror); see external-docs/"; fi

web-dev:
	cd packages/web-app && pnpm install && pnpm dev

vkg-dev:
	docker build -t vkg-local packages/vkg/
	docker run --rm -p 8080:8080 -e ONTOLOGY_BUCKET=local -e NAMESPACE=default -e VERSION=latest vkg-local
