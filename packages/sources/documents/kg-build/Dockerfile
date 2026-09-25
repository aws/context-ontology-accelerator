# Build context must be the repo root:
#   docker build -f packages/sources/documents/kg-build/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation. The pin is for
# supply-chain integrity only, NOT patch currency — see the apt-get upgrade below.
# python:3.12-slim, digest resolved from the tag on 2026-09-01.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

WORKDIR /app

# Apply Debian security patches on top of the pinned base. This image installs no
# apt packages of its own, so without this layer its OS patch level was frozen at
# whatever the pinned base shipped — which is how it ended up on openssl
# 3.5.6-1~deb13u2 with 10 open Inspector findings. `upgrade` (not `dist-upgrade`)
# keeps this to security and point-release updates from the trixie suites only.
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

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

# Install the documents package, then remediate scanner findings in the SAME final layer:
# upgrade pip + setuptools (CVE-2025-47273; build toolchain, not a declared dep;
# unconstrained — does not touch the boto3/botocore cap above), and REMOVE msgpack — an
# orphan that nothing imports or declares (verified via grep + pipdeptree --reverse),
# pulled in transiently and left behind. `pip uninstall -y` is a no-op exit 0 if absent.
COPY packages/sources/ /tmp/sources/
RUN pip install --no-cache-dir /tmp/sources/ && rm -rf /tmp/sources/ && \
    pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" && \
    pip uninstall -y msgpack

USER appuser
CMD ["python", "-m", "coa_sources.documents.kg_build.graph_build"]
