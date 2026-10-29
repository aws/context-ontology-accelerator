# Documents

Document ingestion pipeline for the Context Ontology Accelerator. Copied from `unstructured-data-ingestion` and refactored into the `coa_sources.documents` submodule.

## Package Structure

```text
packages/sources/documents/       ← deployment unit artifacts (Dockerfiles, requirements)
├── preprocessing/                 # Dockerfile + requirements for preprocessing Lambda
├── kg-build/                      # Dockerfile + requirements for KG Build ECS task
├── deletion/                      # requirements for deletion Lambda
└── trigger/                       # requirements for trigger Lambda

packages/sources/src/
└── coa_sources/
    └── documents/                 ← Python source (importable package)
        ├── api/                   # Doc source CRUD Lambda handler
        ├── preprocessing/         # PDF/DOCX → text conversion
        ├── kg_build/              # GraphRAG KG build + cleanup
        ├── deletion/              # S3 cleanup
        └── trigger/               # SQS → Step Functions trigger
```

### Running the preprocessing image locally

The preprocessing image does not ship the Lambda Runtime Interface Emulator. The Dockerfile deletes `/usr/local/bin/aws-lambda-rie` from the base image to drop its CVEs; in Lambda, `/lambda-entrypoint.sh` never runs it. To invoke the image with `docker run`, download `aws-lambda-rie` from the [aws-lambda-runtime-interface-emulator releases](https://github.com/aws/aws-lambda-runtime-interface-emulator/releases) and mount it at that path:

```bash
docker run -p 9000:8080 \
  -v ~/.aws-lambda-rie/aws-lambda-rie:/usr/local/bin/aws-lambda-rie \
  <image>
curl -s http://localhost:9000/2015-03-31/functions/function/invocations -d '{}'
```

## Relationship to unstructured-data-ingestion

This is a copy of `packages/unstructured-data-ingestion` with the business logic moved into `src/coa_sources/documents/` following the standard `src/` package layout used by `structured-data-ingestion` and `context-manager`. The original package remains unchanged and continues to serve the existing `/doc-sources` API.
