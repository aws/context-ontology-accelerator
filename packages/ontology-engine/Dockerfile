# Build context: monorepo root ($CI_PROJECT_DIR)
#   docker build -f packages/ontology-engine/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation. The pin is for
# supply-chain integrity only, NOT patch currency — see the dnf upgrade below.
# amazonlinux:2023-minimal (not Debian python:3.12-slim, whose OS findings are
# mostly unfixed upstream), digest resolved from the tag on 2026-10-13.
FROM --platform=linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:fe18d1ee02440b6480eb4a045be052e9aeaf9bf5029368f09d3425ddab2bfa0a

WORKDIR /app

# Java runtime for owlready2 HermiT reasoner (tier-1 validation) — Corretto 21,
# matching the OpenJDK 21 that Debian's default-jre-headless shipped. curl-minimal
# (already in the base) provides curl, findutils the `find` the Pellet strip below
# needs, and shadow-utils useradd.
# python3.12 comes from the AL2023 repos; `python`/`pip` are symlinked because
# the CMD/ENTRYPOINT and the pip steps below call them unversioned.
# `--releasever=latest` is what makes the upgrade effective: AL2023 locks dnf to
# the repo snapshot the base image was cut from, so without it a stale pin
# freezes the OS patch level exactly as it did on the Debian base.
RUN dnf upgrade -y --releasever=latest && \
    dnf install -y --releasever=latest python3.12 python3.12-pip java-21-amazon-corretto-headless findutils shadow-utils && \
    ln -sf /usr/bin/python3.12 /usr/local/bin/python && \
    ln -sf /usr/bin/python3.12 /usr/local/bin/python3 && \
    ln -sf /usr/bin/pip3.12 /usr/local/bin/pip && \
    dnf clean all && rm -rf /var/cache/dnf

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
