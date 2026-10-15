# Build context must be the repo root:
#   docker build -f packages/sources/documents/kg-build/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation. The pin is for
# supply-chain integrity only, NOT patch currency — see the dnf upgrade below.
# amazonlinux:2023-minimal (not Debian python:3.12-slim, whose OS findings are
# mostly unfixed upstream), digest resolved from the tag on 2026-10-13.
FROM --platform=linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:fe18d1ee02440b6480eb4a045be052e9aeaf9bf5029368f09d3425ddab2bfa0a

WORKDIR /app

# Apply AL2023 security updates on top of the pinned base. shadow-utils provides useradd.
# python3.12 comes from the AL2023 repos; `python`/`pip` are symlinked because
# the CMD/ENTRYPOINT and the pip steps below call them unversioned.
# `--releasever=latest` is what makes the upgrade effective: AL2023 locks dnf to
# the repo snapshot the base image was cut from, so without it a stale pin
# freezes the OS patch level exactly as it did on the Debian base.
RUN dnf upgrade -y --releasever=latest && \
    dnf install -y --releasever=latest python3.12 python3.12-pip shadow-utils && \
    ln -sf /usr/bin/python3.12 /usr/local/bin/python && \
    ln -sf /usr/bin/python3.12 /usr/local/bin/python3 && \
    ln -sf /usr/bin/pip3.12 /usr/local/bin/pip && \
    dnf clean all && rm -rf /var/cache/dnf

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
