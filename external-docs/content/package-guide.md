# Package Guide

This guide explains how to add a new package to the Context Ontology Accelerator monorepo and how to implement one.

## Adding a New Package

### 1. Create the package structure

All Python packages live under `packages/` and follow this structure:

```
packages/my-new-package/
├── src/
│   └── coa_<name>/
│       ├── __init__.py
│       └── # your modules here
├── tests/
│   ├── unit/
│   │   ├── conftest.py
│   │   └── test_*.py
│   └── integ/
│       └── test_*.py
├── pyproject.toml
└── project.json
```

**Key conventions:**
- Package source lives in `src/coa_<name>/` (snake_case, prefixed with `coa_`)
- The package name in `pyproject.toml` is `coa-<name>` (kebab-case)
- Tests are split into `tests/unit/` and `tests/integ/`

### 2. Create `pyproject.toml`

Define the package metadata and dependencies:

```toml
[project]
name = "coa-my-new-package"
version = "0.0.1"
description = "Brief description of the package"
requires-python = ">=3.12"

dependencies = [
    "coa-common",           # Always depend on common for shared utilities
    "boto3>=1.35.0",
    # Add other dependencies here
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/coa_my_new_package"]

[dependency-groups]
integ = [
    "requests>=2.32.0",
    "pytest-xdist>=3.0",
]

[tool.pytest.ini_options]
markers = [
    "unit: Unit tests",
    "integ: Integration tests",
]
pythonpath = ["."]

[tool.uv.sources]
coa-common = { workspace = true }
# Add other workspace dependencies here
```

### 3. Create `project.json` for Nx

Nx orchestrates builds, tests, and linting across all packages. Add `packages/my-new-package/project.json`:

```json
{
  "name": "my-new-package",
  "namedInputs": { "default": ["python"] },
  "implicitDependencies": ["common"],
  "targets": {
    "lint": {
      "command": "uv run ruff check packages/my-new-package && uv run ruff format --check packages/my-new-package && uv run mypy packages/my-new-package/src --ignore-missing-imports --cache-dir packages/my-new-package/.mypy_cache"
    },
    "test": {
      "command": "COVERAGE_FILE=packages/my-new-package/.coverage uv run pytest packages/my-new-package/tests/unit -v --tb=short --cov=packages/my-new-package/src --cov-report=term-missing --cov-report=xml:packages/my-new-package/coverage.xml --cov-fail-under=80 --junitxml=packages/my-new-package/junit.xml"
    },
    "format": {
      "command": "uv run ruff format packages/my-new-package"
    },
    "format:check": {
      "command": "uv run ruff format --check packages/my-new-package"
    }
  }
}
```

**Important fields:**
- `name`: Must match the directory name under `packages/`
- `implicitDependencies`: List packages this one depends on (usually includes `"common"`)
- `targets`: Defines commands that Nx can run (`lint`, `test`, `format`)

### 4. Register in the workspace

Add the new package to the root `pyproject.toml` workspace:

```toml
[tool.uv.workspace]
members = [
    "packages/control-plane",
    # ... other packages ...
    "packages/my-new-package",    # Add your package here
    "libs/common",
]
```

### 5. Install and verify

```bash
# Sync all workspace packages (including the new one)
uv sync

# Verify the package is recognized
uv run python -c "import coa_my_new_package; print('OK')"

# Run lint and tests
pnpm nx run my-new-package:lint
pnpm nx run my-new-package:test
```

## Implementing a Package

### Code structure

Follow these conventions:

```
src/coa_<name>/
├── __init__.py              # Package exports
├── api/                     # API handlers (if this is a service)
│   ├── __init__.py
│   └── handlers.py
├── services/                # Business logic
│   ├── __init__.py
│   └── my_service.py
├── models/                  # Pydantic models and data structures
│   ├── __init__.py
│   └── schemas.py
└── utils/                   # Helper functions
    ├── __init__.py
    └── helpers.py
```

### Use shared libraries

**`libs/common`** provides:
- `coa_common.config` — `SCLConfig` (Pydantic settings) + `resolve_region()`
- `coa_common.logging` — structlog setup
- `coa_common.exceptions` — `SCLError`, `ConfigurationError`, `DataSourceError`, `IngestionError`
- `coa_common.s3` — S3 operations
- `coa_common.dao` — `DynamoDBDAO` class
- `coa_common.constants` — shared pipeline constants (`IngestionStatus`, `ExtractionMode`)

Always import from `common` instead of duplicating utilities:

```python
from coa_common.config import SCLConfig, resolve_region
from coa_common.logging import setup_logging
from coa_common.exceptions import SCLError
```

### Writing tests

**Unit tests** (`tests/unit/`):
- Mock all external dependencies (AWS services, HTTP clients, Neptune, OpenSearch)
- Test individual functions and classes in isolation
- Use `conftest.py` for shared fixtures
- Run with: `uv run --directory packages/<pkg> pytest tests/unit -q`

Example unit test:

```python
from unittest.mock import Mock
import pytest
from coa_my_package.services.my_service import MyService

def test_my_function_success():
    # Arrange
    mock_client = Mock()
    service = MyService(client=mock_client)
    
    # Act
    result = service.do_something("input")
    
    # Assert
    assert result == "expected"
    mock_client.some_method.assert_called_once()
```


**Integration tests** (`tests/integ/`):
- Test through the real deployed API
- Use shared fixtures from `tests/integ/fixtures.py` for auth and endpoint discovery
- Never hardcode credentials, endpoints, or resource IDs
- Run with: `uv run pytest packages/<pkg>/tests/integ -m integ`


Example integration test:

```python
import pytest
from tests.integ.fixtures import api_endpoint, auth_token

def test_my_api_endpoint(api_endpoint, auth_token):
    import requests
    
    url = f"{api_endpoint}/namespaces/test-ns/my-resource"
    headers = {"Authorization": f"Bearer {auth_token}"}
    
    response = requests.get(url, headers=headers)
    
    assert response.status_code == 200
    assert "data" in response.json()
```

### Python standards

See `AGENTS.md` for full Python standards. Key rules:

- Python 3.12+, use `from __future__ import annotations`
- Type hints on all function signatures
- Use `structlog` for logging (not `print`)
- Pydantic `BaseModel` for data structures that cross boundaries
- Async by default for I/O operations
- No bare `except:` — catch specific exceptions
- Max line length: 120 (enforced by ruff)

### Running common tasks

```bash
# Format code (auto-fixes lint errors)
make format

# Run all linters
make lint

# Run unit tests for your package
uv run --directory packages/my-new-package pytest tests/unit -q

# Run all unit tests (via Nx)
make test

# Run integration tests (requires deployed dev stack)
make test-integ

# Build all packages
make build
```

## Packages

| Package                       | Description                                        |
| ----------------------------- | -------------------------------------------------- |
| `control-plane`               | Namespace lifecycle, roles, grants, and Cedar authorizer — the management-plane API |
| `data-layer`                  | Runtime query/retrieval REST API — invokes the Serve (context-manager) runtime and formats responses |
| `sources`                     | Unified source registry API (DATABASE + DOCUMENTS): discovery, scanning, review, metadata enrichment pipelines |
| `ontology-engine`             | Ontology induction, validation, graph/catalog storage, and reasoning |
| `metric-service`              | Metric authoring, validation, and OSI v1.0 import/export |
| `vkg`                         | Virtual Knowledge Graph translation server (Ontop-backed SPARQL/SQL federation) |
| `mcp-server`                  | Model Context Protocol server exposing discovery and execution tools for AI agents |
| `mcp-proxy`                   | Stdio-to-Streamable-HTTP bridge so local MCP clients (Kiro, Claude Desktop, Cursor) can reach the MCP server on AgentCore Runtime |
| `context-manager`             | Serve layer — tiered query orchestration (metrics, NL-to-SQL/SPARQL, knowledge retrieval) and upstream service clients |

## Shared Libraries

| Library          | Description                        |
| ---------------- | ---------------------------------- |
| `libs/common`    | Shared config, logging, exceptions, S3 utilities, DynamoDB helpers (`DDBAccess`), and ingestion pipeline constants |
| `libs/ts-shared` | Shared TypeScript types and enums (mirrors `libs/common` for cross-language type safety) |

## Web App

| Component | Description                           |
| --------- | -------------------------------------- |
| `web-app` | React + Cloudscape management console |

## Error Reference

`libs/common` exports the shared AOSS vector-client exceptions from
`coa_common.opensearch` (both listed in that package's `__all__`). Both exist
so an AOSS write or index that cannot be trusted fails **loudly** instead of
silently reporting success — catch them where you want to retry or surface a
deploy/ingest failure rather than let corrupt state through.

| Exception               | Type           | Raised by                                | When |
| ----------------------- | -------------- | ---------------------------------------- | ---- |
| `PartialIndexError`     | `Exception`    | `bulk_with_retry(...)`                    | A bulk embedding write did not durably index every document — transient (429/5xx) per-item failures that survive `OSS_MAX_RETRIES` re-submits, **or** any non-transient (terminal 4xx) per-item error. |
| `IncompatibleEngineError` | `RuntimeError` | `AossVectorClient.ensure_index(...)`    | A vector index resolved to an ANN engine that cannot serve filtered k-NN (i.e. not Faiss/Lucene), or the created index's mapping could not be read back / did not echo a filter-capable engine. |

### `PartialIndexError`

Raised by `bulk_with_retry` when, after transient-fault retries are exhausted,
one or more documents still failed to index — or when any document failed with
a terminal status. This is the correctness contract behind
[#173](https://github.com/aws/context-ontology-accelerator/issues/173): at
`aoss_min_ocu=0` the NEXTGEN circuit breaker sheds bulk-write load with HTTP
429s, and `opensearch-py`'s own per-item retry can exhaust and **drop** those
docs from the success stream without raising, so a naive caller reports "N
written" when far fewer landed. Surfacing the shortfall as a raise turns silent
data loss into a visible failure.

It carries the shortfall for callers/tests to inspect:

- `failed` (`int`) — number of documents that never durably indexed.
- `submitted` (`int`) — number of documents handed to this bulk write.
- `errors` (`list[dict]`) — the per-item error dicts (`opensearch-py` bulk error shape).

Representative messages:

```
OSS bulk: <N> doc(s) failed with a terminal (non-retryable) status out of <M> submitted
OSS bulk: <N> doc(s) still failing transiently after <OSS_MAX_RETRIES> re-submit(s); index is incomplete
```

`OSS_MAX_RETRIES` (default `6`) and `OSS_MAX_BACKOFF_S` (default `30`) are
env-tunable and also exported from `coa_common.opensearch`.

### `IncompatibleEngineError`

Raised by `AossVectorClient.ensure_index` after it reads the created index's
mapping back, when the `embedding` field's resolved ANN engine is not
filter-capable. Filtered k-NN requires **Faiss** (HNSW) or **Lucene**; **NMSLIB
does not support filters**. This turns the
[#174](https://github.com/aws/context-ontology-accelerator/issues/174) failure —
silently getting an NMSLIB index and only discovering it when a filtered query
returns HTTP 400 — into a loud, creation-time failure. The resolved engine is
always logged at INFO.

Representative messages:

```
index '<name>' resolved to ANN engine '<engine>', which cannot serve filtered k-NN
  (need one of ['faiss', 'lucene']). Filtered vector search would fail with
  'Engine [<ENGINE>] does not support filters'. See #174.

index '<name>' was created with an explicit filter-capable method but its mapping
  reports no ANN engine; refusing to trust an unverifiable index. See #174.

could not read back mapping for index '<name>' to verify its ANN engine: <cause>
```
