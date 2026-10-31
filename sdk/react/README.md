# graphify-react

Embeddable React components + hooks for the [graphify](../../README.md)
code-intelligence REST API. Drop **"where does this change impact?"** and prose→code
seed discovery straight into your app — the engine (parsing, graph, Postgres) stays
server-side; these components only talk to graphify's `/api/v1` over HTTP with an API key.

## Install

```bash
npm install graphify-react   # peer deps: react >=18, react-dom >=18
```

## Serve the API

```bash
GRAPHIFY_API_KEY=your-key GRAPHIFY_CORS_ORIGINS=https://your-app.com \
  graphify serve-web --graph graphify-out/graph.json --host 0.0.0.0 --port 8756
```

## Embed

```tsx
import { GraphifyImpact, GraphifySeeds } from "graphify-react";

const conn = { apiBase: "https://graph.your-co.com", apiKey: import.meta.env.VITE_GRAPHIFY_KEY };

// Blast radius of a symbol — list (confidence-tiered) + graph view
<GraphifyImpact {...conn} symbol="rollup_daily" depth={2}
  onSelect={(hit) => console.log("navigate to", hit.source_file)} />

// Stage 2: prose → seed symbols; feed a pick into GraphifyImpact
<GraphifySeeds {...conn} onSelect={(s) => setSymbol(s.name)} />

// Taint: untrusted input → dangerous sink flows (needs `extract --taint`)
import { GraphifyTaint } from "graphify-react";
<GraphifyTaint {...conn} onSelect={(f) => console.log(f.vuln, f.sink.file)} />
```

Prefer data over UI? Use the hooks:

```tsx
import { useImpact } from "graphify-react";
const { data, loading, error } = useImpact(conn, "rollup_daily", 2);
// data.affected: [{ id, label, depth, via_relation, confidence, service, source_file, ... }]
```

## Components & hooks

| Export | What |
|---|---|
| `<GraphifyImpact>` | Blast radius as a grouped, confidence-tiered list + `<GraphifySubgraph>` |
| `<GraphifySubgraph>` | Dependency-free SVG graph around a symbol (BFS-layered) |
| `<GraphifySeeds>` | Prose → ranked seed symbols (Stage 2) |
| `<GraphifyTaint>` | Taint findings (source→…→sink) grouped by vuln, severity-colored |
| `useImpact` / `useSubgraph` / `useSeeds` / `useTaint` | Data-only hooks |
| `createClient` | Framework-free typed client for `/api/v1` |

`<GraphifyTaint>` reads `/api/v1/taint`, which is populated when the graph is built
with `graphify extract --taint` (findings persist as `flows_to` edges).

All take `{ apiBase, apiKey? }`. Confidence tiers are colored EXTRACTED (green) /
INFERRED (amber) / AMBIGUOUS (red) — recall-first, so nothing is silently dropped.

## Auth note

These call the API **directly with the key** (simplest). For production, prefer
routing through your app's backend as a proxy so the key stays server-side, or scope
the key and lock `GRAPHIFY_CORS_ORIGINS` to your app's origin.
