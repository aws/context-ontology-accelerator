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

### Running preprocessing outside the image

`preprocessing/requirements.txt` alone is not enough to run the step; the `Dockerfile` adds the rest. On a host without the image (for example Amazon Linux 2023), run these steps from the repository root:

```bash
# System packages: poppler for PDF text, file-libs for libmagic file-type detection
sudo dnf install -y poppler-utils file-libs

# First-party library the handler imports
pip install libs/common/

# CPU-only torch first, constrained so the next install can't pull the ~4 GB CUDA build
pip install -c packages/sources/documents/preprocessing/constraints.txt \
  torch==2.13.0+cpu --extra-index-url https://download.pytorch.org/whl/cpu
pip install -c packages/sources/documents/preprocessing/constraints.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  -r packages/sources/documents/preprocessing/requirements.txt

# unstructured pulls in opencv-python, which needs libGL.so.1 (not on AL2023);
# the headless build doesn't
pip uninstall -y opencv-python && pip install opencv-python-headless

# unstructured loads spaCy's English model at runtime
pip install spacy==3.8.7 && python -m spacy download en_core_web_sm

# Lift unstructured's filelock<4 cap past CVE-2026-112940
pip install --no-deps "filelock>=4.2.1,<5"

# The generated server models and the sources package, which holds the handler
# (run `make generate` first if smithy-generated/ is empty)
pip install smithy-generated/control-plane-python-server/ packages/sources/
```

The `Dockerfile` is the source of truth; check it for the current versions.

The step logs `No languages specified, defaulting to English.` for DOCX files and for text-native PDFs, including Japanese ones. Those go through `unstructured`, and preprocessing passes it no language. Text-native PDFs use its `fast` strategy, which reads the PDF's embedded text layer without OCR. Scanned PDFs, and every PDF when `enableTableExtraction` is on, go to Textract instead and don't log this line. The namespace language is applied later, during extraction and enrichment (see [Namespace Language](../../../external-docs/content/namespaces.md#namespace-language)).

## Relationship to unstructured-data-ingestion

This is a copy of `packages/unstructured-data-ingestion` with the business logic moved into `src/coa_sources/documents/` following the standard `src/` package layout used by `structured-data-ingestion` and `context-manager`. The original package remains unchanged and continues to serve the existing `/doc-sources` API.
