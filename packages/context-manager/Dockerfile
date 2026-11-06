# Build context must be the repo root:
#   docker build -f packages/context-manager/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation. The pin is for
# supply-chain integrity only, NOT patch currency — see the dnf upgrade below.
# amazonlinux:2023-minimal (not Debian python:3.12-slim, whose OS findings are
# mostly unfixed upstream), digest resolved from the tag on 2026-10-13.
FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:fe18d1ee02440b6480eb4a045be052e9aeaf9bf5029368f09d3425ddab2bfa0a

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

# Every project dependency install below is constrained to the versions uv.lock pins, which the tests ran on: the query service's SQL and graph parsing depends on those versions, so an untested release inside a pyproject
# range can change query behavior. The script needs only the standard library.
COPY uv.lock scripts/uv_lock_constraints.py /tmp/lock/
RUN python /tmp/lock/uv_lock_constraints.py /tmp/lock/uv.lock > /tmp/constraints.txt && \
    rm -rf /tmp/lock/ && \
    pip install --no-cache-dir uv==0.13.4

# Install shared common library (changes less often -> better layer caching)
COPY libs/common/ /tmp/common/
RUN uv pip install --system --python /usr/bin/python3.12 --no-cache --constraint /tmp/constraints.txt /tmp/common/ && \
    rm -rf /tmp/common/

# Install package with dependencies.
COPY packages/context-manager/pyproject.toml ./
COPY packages/context-manager/src/ ./src/

# Include MCP server package (same image, different entrypoint for MCP protocol runtime)
COPY packages/mcp-server/pyproject.toml /tmp/mcp-server/pyproject.toml
COPY packages/mcp-server/src/ /tmp/mcp-server/src/

# pip itself is upgraded to its newest release, not pinned: like the OS packages above, it floats for security
# fixes, and the service never imports it. setuptools stays at the lock's version through the constraints.
RUN sed -i '/\[tool.uv.sources\]/,$ d' pyproject.toml && \
    sed -i '/\[tool.uv.sources\]/,$ d' /tmp/mcp-server/pyproject.toml && \
    uv pip install --system --python /usr/bin/python3.12 --no-cache --constraint /tmp/constraints.txt . /tmp/mcp-server/ && \
    rm -rf /tmp/mcp-server/ && \
    pip install --no-cache-dir -c /tmp/constraints.txt --upgrade pip "setuptools>=78.1.1" && \
    pip uninstall -y msgpack

EXPOSE 8080

USER appuser

# Health probe for container orchestrators (ECS, etc.) to detect unresponsive processes.
# BedrockAgentCoreApp exposes GET /ping per the AgentCore protocol.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping')" || exit 1

# Default: Context Manager (HTTP protocol, port 8080)
# When SCL_MCP_MODE=true: MCP Server (MCP protocol, port 8000)
CMD ["sh", "-c", "if [ \"$SCL_MCP_MODE\" = \"true\" ]; then python -m coa_mcp; else python -m coa_serve.main; fi"]
