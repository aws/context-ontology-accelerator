# Build context: monorepo root ($CI_PROJECT_DIR)
#   docker build -f packages/ontology-engine/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation.
# python:3.12-slim built 2026-05-19 — includes glibc deb13u3, systemd/sed/libcap2 deb13u1 CVE fixes.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

WORKDIR /app

# Java runtime for owlready2 HermiT reasoner (tier-1 validation)
RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jre-headless curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd --no-create-home --shell /bin/false appuser

# Install shared common library
COPY libs/common/ /tmp/common/
RUN pip install --no-cache-dir /tmp/common/ && rm -rf /tmp/common/

# Install Smithy-generated control-plane server package
# (provides shared API enums/models — single source of truth for control-plane contracts)
COPY smithy-generated/control-plane-python-server/ /tmp/control-plane-server/
RUN pip install --no-cache-dir /tmp/control-plane-server/ && rm -rf /tmp/control-plane-server/

# Install ontology-engine dependencies
COPY packages/ontology-engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY --chown=appuser:appuser packages/ontology-engine/src/ ./src/
COPY --chown=appuser:appuser packages/ontology-engine/tests/fixtures/data-catalog/sample_data/ ./fixtures/sample_data/
RUN mkdir -p /app/output /app/data && chown appuser:appuser /app/output /app/data
ENV PYTHONPATH=/app/src

USER appuser
EXPOSE 8001

CMD ["sh", "-c", "uvicorn coa_ontology.main:app --host 0.0.0.0 --port ${PORT:-8001}"]
