# Build context must be the repo root:
#   docker build -f packages/context-manager/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation.
# python:3.12-slim as of 2026-05-21 (linux/arm64)
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim@sha256:0568e6111802e74c03e8dda76565cdf4b88881d77de0d9b769846e9dfcb8d80a

WORKDIR /app
RUN useradd --no-create-home --shell /bin/false appuser

# Install shared common library (changes less often -> better layer caching)
COPY libs/common/ /tmp/common/
RUN pip install --no-cache-dir /tmp/common/ && rm -rf /tmp/common/

# Install package with dependencies.
COPY packages/context-manager/pyproject.toml ./
COPY packages/context-manager/src/ ./src/

# Include MCP server package (same image, different entrypoint for MCP protocol runtime)
COPY packages/mcp-server/pyproject.toml /tmp/mcp-server/pyproject.toml
COPY packages/mcp-server/src/ /tmp/mcp-server/src/

RUN pip install --no-cache-dir uv && \
    sed -i '/\[tool.uv.sources\]/,$ d' pyproject.toml && \
    sed -i '/\[tool.uv.sources\]/,$ d' /tmp/mcp-server/pyproject.toml && \
    uv pip install --system --no-cache . /tmp/mcp-server/ && \
    rm -rf /tmp/mcp-server/

EXPOSE 8080

USER appuser

# Health probe for container orchestrators (ECS, etc.) to detect unresponsive processes.
# BedrockAgentCoreApp exposes GET /ping per the AgentCore protocol.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping')" || exit 1

# Default: Context Manager (HTTP protocol, port 8080)
# When SCL_MCP_MODE=true: MCP Server (MCP protocol, port 8000)
CMD ["sh", "-c", "if [ \"$SCL_MCP_MODE\" = \"true\" ]; then python -m coa_mcp; else python -m coa_serve.main; fi"]
