# Build context: monorepo root ($CI_PROJECT_DIR)
#   docker build -f packages/ontology-engine/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation. The pin is for
# supply-chain integrity only, NOT patch currency: the previous digest sat at
# openssl 3.5.6-1~deb13u2 and Inspector flagged 10 CVEs against it. Patch level
# now comes from the apt-get upgrade below, so a stale pin can no longer hold
# back an OS security fix.
# python:3.12-slim, digest resolved from the tag on 2026-09-01.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

WORKDIR /app

# Java runtime for owlready2 HermiT reasoner (tier-1 validation).
# `upgrade` runs after our own installs so it also patches what we just pulled
# in. Debian has no apt equivalent of `dnf update --security`, but on a stable
# release the configured suites are trixie + trixie-updates + trixie-security,
# so a plain `upgrade` is security/point-release only. Use `upgrade`, never
# `dist-upgrade` — the latter may remove packages to satisfy dependencies.
RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jre-headless curl && \
    apt-get upgrade -y && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd --no-create-home --shell /bin/false appuser

# Install shared common library
COPY libs/common/ /tmp/common/
RUN pip install --no-cache-dir /tmp/common/ && rm -rf /tmp/common/

# Install Smithy-generated control-plane server package
# (provides shared API enums/models — single source of truth for control-plane contracts)
COPY smithy-generated/control-plane-python-server/ /tmp/control-plane-server/
RUN pip install --no-cache-dir /tmp/control-plane-server/ && rm -rf /tmp/control-plane-server/

# Install ontology-engine dependencies, then remediate the two packages scanners flag
# as HIGH, in the SAME final layer (a separate RUN would leave the old versions in a
# lower layer that scanners still attribute to the image):
#   - setuptools (CVE-2025-47273): build toolchain, not a declared dependency — bump it.
#   - msgpack: an ORPHAN — nothing in first-party code or any installed package imports
#     or declares it (verified via grep + pipdeptree --reverse); it is pulled in
#     transiently during resolution and left behind. Remove it outright rather than ship
#     a patched-but-dead package (`pip uninstall -y` is a no-op exit 0 if it is absent).
COPY packages/ontology-engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" && \
    pip uninstall -y msgpack

# Strip the unused Pellet reasoner that owlready2 vendors. Tier-1 validation uses
# HermiT only (sync_reasoner_hermit, see validation/validators/tier1.py); Pellet
# is never invoked. Its bundled JARs (jena-core, log4j 2.19, httpclient 4.2,
# xercesImpl, …) carry Critical/High CVEs that have no bearing on our code path —
# emptying the directory deletes them from the image. HermiT.jar (in ../hermit)
# is self-contained and untouched. Also upgrade pip to pick up its own fixes.
#
# The directory itself MUST survive: owlready2/reasoning.py runs
# os.listdir(<pkg>/pellet) at *import time* (to build the Pellet classpath), so
# deleting the whole dir makes `import owlready2` raise FileNotFoundError and
# breaks HermiT too — surfacing as REASONER_ERROR at runtime. An empty dir makes
# os.listdir return [] (empty classpath), which is fine since Pellet is unused.
RUN PELLET_DIR="$(python -c 'import owlready2, os; print(os.path.join(os.path.dirname(owlready2.__file__), "pellet"))')" && \
    find "$PELLET_DIR" -mindepth 1 -delete && \
    pip install --no-cache-dir --upgrade pip
# Fail the build loudly if owlready2 can't import after the Pellet strip (e.g. a
# future change removes the pellet dir), instead of degrading to REASONER_ERROR
# at runtime.
RUN python -c "import owlready2; print('owlready2', owlready2.VERSION, 'OK')"

# Copy application source
COPY --chown=appuser:appuser packages/ontology-engine/src/ ./src/
COPY --chown=appuser:appuser packages/ontology-engine/tests/fixtures/data-catalog/sample_data/ ./fixtures/sample_data/
RUN mkdir -p /app/output /app/data && chown appuser:appuser /app/output /app/data
ENV PYTHONPATH=/app/src

USER appuser
EXPOSE 8001

CMD ["sh", "-c", "uvicorn coa_ontology.main:app --host 0.0.0.0 --port ${PORT:-8001}"]
