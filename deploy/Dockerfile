# Graphify app image for Railway — serves either the Flask REST API or the
# MCP HTTP transport over a pre-baked graph.json (graphify's own repo graph).
# Build context is the repo root; this file is referenced via
# RAILWAY_DOCKERFILE_PATH=deploy/Dockerfile.
FROM python:3.12-slim

WORKDIR /app

# Install the package first (cache-friendly): only metadata + source change
# invalidates this layer, not the baked graph.
COPY pyproject.toml README.md LICENSE /app/
COPY graphify /app/graphify
# Pin mcp to the version graphify was built against (uv.lock). Newer mcp
# releases dropped `mcp.types.AnyUrl`, which serve.py imports.
RUN pip install --no-cache-dir ".[web,postgres]" "mcp==1.27.1"

# Baked graph + role dispatcher.
COPY deploy/graph.json /app/graph.json
COPY deploy/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && useradd --create-home --uid 10001 graphify \
    && chown -R graphify /app
USER graphify

# Default role; Railway overrides per service.
ENV GRAPHIFY_ROLE=api
CMD ["/app/entrypoint.sh"]
