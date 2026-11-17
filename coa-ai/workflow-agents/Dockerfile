# A self-hosted container for the workflow-agents MCP server. Build from the repo root:
#   docker build -f coa-ai/workflow-agents/Dockerfile -t coa-workflow-agents .
# Run it with the port published on loopback only, so nothing beyond this host can reach it:
#   docker run --env-file workflow-agents.env -p 127.0.0.1:8000:8000 coa-workflow-agents
# Same base, digest pin and lock-constrained installs as packages/context-manager/Dockerfile;
# see the comments there for why each step is shaped the way it is.
FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:fe18d1ee02440b6480eb4a045be052e9aeaf9bf5029368f09d3425ddab2bfa0a

WORKDIR /app

RUN dnf upgrade -y --releasever=latest && \
    dnf install -y --releasever=latest python3.12 python3.12-pip shadow-utils && \
    ln -sf /usr/bin/python3.12 /usr/local/bin/python && \
    ln -sf /usr/bin/python3.12 /usr/local/bin/python3 && \
    ln -sf /usr/bin/pip3.12 /usr/local/bin/pip && \
    dnf clean all && rm -rf /var/cache/dnf

RUN useradd --no-create-home --shell /bin/false appuser

# Every project install is constrained to the versions uv.lock pins, which the tests ran on.
COPY uv.lock scripts/uv_lock_constraints.py /tmp/lock/
RUN python /tmp/lock/uv_lock_constraints.py /tmp/lock/uv.lock > /tmp/constraints.txt && \
    rm -rf /tmp/lock/

COPY libs/common/ /tmp/common/
COPY smithy-generated/control-plane-python-server/ /tmp/control-plane-server/
COPY coa-ai/workflow-agents/pyproject.toml ./
COPY coa-ai/workflow-agents/src/ ./src/

# The workspace sources are dropped from pyproject: coa-common and coa-control-plane-server install from their copies.
RUN pip install --no-cache-dir uv==0.13.4 && \
    sed -i '/\[tool.uv.sources\]/,$ d' pyproject.toml && \
    uv pip install --system --python /usr/bin/python3.12 --no-cache --constraint /tmp/constraints.txt \
        /tmp/common/ /tmp/control-plane-server/ . && \
    rm -rf /tmp/common/ /tmp/control-plane-server/ && \
    pip install --no-cache-dir -c /tmp/constraints.txt --upgrade pip "setuptools>=78.1.1" && \
    pip uninstall -y uv && \
    hash -r && ! command -v uv && ! command -v uvx

# Inside the container the server listens on every interface; the published port is the boundary.
ENV SCL_MCP_HOST=0.0.0.0
EXPOSE 8000

USER appuser

COPY scripts/image_import_check.py /tmp/
# llama_index is optional: coa_common.embeddings imports it lazily, and this server never embeds.
RUN python /tmp/image_import_check.py coa_workflow_agents.__main__ --optional llama_index

# The server accepts TCP on its port. A plain connect, since /mcp answers a bare GET with an error by design.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), timeout=3).close()" || exit 1

CMD ["python", "-m", "coa_workflow_agents"]
