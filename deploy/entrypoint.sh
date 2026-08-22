#!/usr/bin/env bash
# Single image, two roles. Railway sets GRAPHIFY_ROLE per service.
#   api  -> populate Postgres (idempotent), then serve the Flask REST API
#   mcp  -> serve the MCP HTTP transport
# Connection to Postgres uses libpq PG* env vars (wired from ${{Postgres.*}}),
# so `--dsn` is omitted everywhere.
set -uo pipefail

GRAPH=/app/graph.json
ROLE="${GRAPHIFY_ROLE:-api}"
PORT="${PORT:-8080}"
REPO="${GRAPHIFY_REPO:-graphify}"

echo "[entrypoint] role=${ROLE} port=${PORT} repo=${REPO} pghost=${PGHOST:-unset}"

if [ "$ROLE" = "api" ]; then
  echo "[entrypoint] populating Postgres (idempotent)…"
  graphify export-pg "$GRAPH" --repo "$REPO" --source llm \
    && echo "[entrypoint] export-pg OK" \
    || echo "[entrypoint] WARN export-pg failed — serving file graph anyway"
  graphify export-chunks "$GRAPH" --repo "$REPO" --embed hashing \
    && echo "[entrypoint] export-chunks OK" \
    || echo "[entrypoint] WARN export-chunks failed — semantic PG search unavailable"
  echo "[entrypoint] starting Flask REST API on :${PORT}"
  exec graphify serve-web --graph "$GRAPH" --host 0.0.0.0 --port "$PORT" --root /app

elif [ "$ROLE" = "mcp" ]; then
  echo "[entrypoint] starting MCP HTTP server on :${PORT}"
  exec python -m graphify.serve "$GRAPH" --transport http --host 0.0.0.0 --port "$PORT" \
    ${GRAPHIFY_API_KEY:+--api-key "$GRAPHIFY_API_KEY"}

else
  echo "[entrypoint] unknown GRAPHIFY_ROLE=${ROLE} (expected api|mcp)" >&2
  exit 1
fi
