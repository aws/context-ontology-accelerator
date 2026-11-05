# Smithy Code Generation

This project uses [Smithy](https://smithy.io) as the single source of truth for all service APIs. Running `make generate` builds the Smithy models and produces typed clients and server stubs for both TypeScript and Python.

## Overview

```
models/src/main/smithy/*.smithy   ← single source of truth
        │
        ▼  gradlew build
        ├── TypeScript clients     → smithy-generated/{service}-typescript-client/
        ├── OpenAPI specs          → smithy-generated/openapi/
        │
        ▼  openapi-generator-cli -g python (from OpenAPI specs)
        ├── Control Plane models  → smithy-generated/control-plane-python-server/
        └── Data Layer models     → smithy-generated/data-layer-python-server/
```

TypeScript and Python use **different generation paths** — see below.

## TypeScript clients (native Smithy codegen)

TypeScript clients are generated directly from Smithy using the [`smithy-aws-typescript-codegen`](https://github.com/smithy-lang/smithy-typescript) Gradle plugin. This produces AWS SDK v3-style clients with full type safety.

**Configuration** (`models/smithy-build.json`):
```json
"data-layer-ts": {
    "plugins": {
        "typescript-client-codegen": {
            "service": "com.amazon.semanticcontext.datalayer#DataLayerService",
            "package": "@coa/data-layer-client",
            "packageVersion": "1.0.0"
        }
    }
}
```

**Output**: `smithy-generated/data-layer-typescript-client/`

**Usage** (web app):
```ts
import { DataLayerServiceClient, ListDocSourcesCommand } from "@coa/data-layer-client";

const client = new DataLayerServiceClient({ endpoint: "https://..." });
const result = await client.send(new ListDocSourcesCommand({ namespaceId: "..." }));
```

The generated package is registered as a pnpm workspace member in `pnpm-workspace.yaml` so the web app can import it directly without publishing to npm.

## Python server models (OpenAPI → openapi-generator)

Python does not yet have a stable native Smithy codegen plugin. Instead, the Smithy models are first converted to OpenAPI specs (via the `openapi` Gradle plugin), then [`openapi-generator-cli`](https://openapi-generator.tech) with `-g python` generates **Pydantic v2** model classes from those specs.

**Why two steps?**
- `smithy-python` ([github.com/smithy-lang/smithy-python](https://github.com/smithy-lang/smithy-python)) is pre-alpha and not yet suitable for production use.
- The OpenAPI → openapi-generator path is stable and produces Pydantic v2 models with full constraint validation (`@field_validator` for patterns, `Field(min_length=..., max_length=...)` for length).

**Generated packages:**

| Package | Source spec | Output |
|---------|------------|--------|
| `coa-control-plane-server` | ControlPlaneService | `smithy-generated/control-plane-python-server/` |
| `coa-data-layer-server` | DataLayerService | `smithy-generated/data-layer-python-server/` |

**Configuration** (`models/smithy-build.json`):
```json
"openapi-control-plane": {
    "plugins": {
        "openapi": {
            "service": "com.amazon.semanticcontext#ControlPlaneService"
        }
    }
}
```

**Usage** (Lambda handler — input validation):
```python
from coa_control_plane_server.models.create_namespace_request_content import (
    CreateNamespaceRequestContent,
)

# model_validate() enforces all Smithy constraints (pattern, length)
request = CreateNamespaceRequestContent.model_validate(body)
```

**Usage** (Lambda handler — response construction):
```python
from coa_control_plane_server.models.namespace_detail import NamespaceDetail

# model_construct() skips validation for trusted server-generated data
response = NamespaceDetail.model_construct(
    namespace_id=namespace_id,
    name=request.name,
    ...
)
# Serialize with camelCase keys for the API response
return response.model_dump(by_alias=True, exclude_none=True)
```

### When to use `model_validate()` vs `model_construct()`

| Scenario | Method | Why |
|----------|--------|-----|
| Parsing client input (request bodies) | `model_validate()` | Enforces all Smithy constraints |
| Building responses from trusted data (DB reads, server-generated values) | `model_construct()` | Skips re-validation of data we control |

`model_construct()` is used for response objects because:
1. Data from DynamoDB was validated on write — re-validating on read adds latency with no safety benefit.
2. Server-generated UUIDs are guaranteed valid — re-validating them against the UUID v4 regex is redundant.

### Adding Smithy constraints

To add validation to a field, use Smithy constraint traits in `models/src/main/smithy/`:

```smithy
/// Namespace name: 3-64 lowercase alphanumeric or hyphens.
@pattern("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
@length(min: 3, max: 64)
string NamespaceName
```

After `make generate`, the Python model will include:
- `Field(min_length=3, max_length=64)` for length constraints
- `@field_validator` with `re.match(...)` for pattern constraints

The generated packages are registered as uv workspace members in `pyproject.toml` and bundled into Lambda zips via CDK bundling.

## Two services, one API Gateway

The project defines two Smithy services:

| Service | File | Purpose |
|---------|------|---------|
| `ControlPlaneService` | `control-plane.smithy` | Management operations (namespace, source, metric, ontology CRUD) |
| `DataLayerService` | `data-layer.smithy` + `serve.smithy` | Runtime query/retrieval operations |

Both are served through a **single API Gateway**. At deploy time, `api-stack.ts` merges the two generated OpenAPI specs (method-level merge) into one unified spec. This keeps the Smithy services architecturally separate while sharing auth, domain, and gateway infrastructure.

```
ControlPlaneService.openapi.json ─┐
                                  ├─ merged at deploy → API Gateway
DataLayerService.openapi.json ────┘
```

If both specs define the same path (e.g., `GET /namespaces/{namespaceId}/metrics`), the data-layer version takes precedence for that method. Path parameters must use consistent names (`{namespaceId}`) across both specs to avoid API Gateway conflicts.

## Running code generation

```sh
make generate
```

This runs `scripts/smithy-generate.sh` which:
1. Runs `./gradlew build` in `models/` — produces TypeScript clients and OpenAPI specs
2. Installs the workspace from `pnpm-lock.yaml` with `pnpm install --frozen-lockfile` and builds the TypeScript clients
3. Runs `openapi-generator-cli -g python` — produces Pydantic v2 server models from the OpenAPI specs

The script needs pnpm on `PATH` and stops before the Gradle build if it is missing. `make setup` installs the version `.mise.toml` pins when pnpm is missing (`scripts/install-pnpm.sh`). To run the script on its own, install it first, with `mise install` or `./scripts/install-pnpm.sh`. After generation, run `uv sync` to link the new Python workspace packages.
