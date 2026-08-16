# Graphify UI

React + TypeScript (Vite) dashboard over the Graphify JSON API. Four views:

- **Query explorer** — traverse the graph, list callers/callees.
- **Taint** — source→sink findings (SQL/command/code injection) grouped by vuln,
  each showing the flow with `file:line`. Populated when the graph is built with
  `graphify extract --taint` (findings persist as `flows_to` edges).
- **Review queue** — INFERRED/AMBIGUOUS edges and auto-generated verification
  questions; confirm a real equivalence to promote it to a deterministic alias.
- **Alias manager** — list and add entity aliases (doc `User` ≡ code `Customer`).

## Run

Start the API (from the repo root):

```bash
graphify serve-web --graph graphify-out/graph.json   # http://127.0.0.1:8756
```

Then the UI:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Vite proxies `/api` and `/health` to the Flask server, so there's no CORS
config. Point at a different backend with `GRAPHIFY_API=http://host:port npm run dev`.

## Build

```bash
npm run build      # type-checks, then emits static assets to dist/
```

Serve `dist/` from any static host (or from Flask) on the same origin as the API.

> Aliases added here are **persisted but applied on the next `graphify` build** —
> the API records them to `.graphify_aliases.json`; it does not rebuild the graph.
