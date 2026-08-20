# Build context must be the repo root:
#   docker build -f packages/sources/documents/kg-build/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation.
# python:3.12-slim built 2026-05-19 — includes glibc deb13u3, systemd/sed/libcap2 deb13u1 CVE fixes.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

WORKDIR /app
RUN useradd --no-create-home --shell /bin/false appuser

# Install shared common library (changes less often -> better layer caching)
# Constrain boto3 here so it doesn't install latest then get downgraded by
# graphrag-lexical-graph (whose transitive deps cap botocore < 1.41).
COPY libs/common/ /tmp/common/
COPY packages/sources/documents/kg-build/requirements.txt /tmp/constraints.txt
RUN pip install --no-cache-dir --constraint /tmp/constraints.txt /tmp/common/ && rm -rf /tmp/common/

# Install smithy-generated control-plane models
COPY smithy-generated/control-plane-python-server/ /tmp/server/
RUN pip install --no-cache-dir /tmp/server/ && rm -rf /tmp/server/

# Install KG Build-specific dependencies
COPY packages/sources/documents/kg-build/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the documents package
COPY packages/sources/ /tmp/sources/
RUN pip install --no-cache-dir /tmp/sources/ && rm -rf /tmp/sources/

USER appuser
CMD ["python", "-m", "coa_sources.documents.kg_build.graph_build"]
