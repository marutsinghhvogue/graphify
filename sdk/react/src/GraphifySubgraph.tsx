import { useMemo } from "react";
import { useSubgraph } from "./hooks";
import type { GraphifyClientOptions } from "./client";
import type { NodeView, SubgraphEdge } from "./types";

export interface GraphifySubgraphProps extends GraphifyClientOptions {
  symbol: string;
  depth?: number;
  height?: number;
  className?: string;
  style?: React.CSSProperties;
  onSelect?: (node: NodeView) => void;
}

/**
 * A dependency-free SVG view of the subgraph around `symbol`: nodes laid out in
 * columns by BFS distance from the seed, edges as lines. Deliberately no heavy
 * graph library so it drops cleanly into a host app.
 */
export function GraphifySubgraph(props: GraphifySubgraphProps) {
  const { symbol, depth = 1, height = 320, className, style, onSelect, ...conn } = props;
  const { data, error, loading } = useSubgraph(conn, symbol, depth);

  const layout = useMemo(() => (data ? computeLayout(data.nodes, data.edges, data.seed, height) : null), [data, height]);

  if (loading) return <div style={{ padding: 8, color: "#6e7681" }}>Loading graph…</div>;
  if (error) return <div style={{ padding: 8, color: "#f85149" }}>Error: {error}</div>;
  if (!layout || layout.nodes.length === 0) return <div style={{ padding: 8, color: "#6e7681" }}>No graph.</div>;

  const { nodes, edges, width } = layout;
  const pos = new Map(nodes.map((n) => [n.id, n]));

  return (
    <svg className={className} viewBox={`0 0 ${width} ${height}`} width="100%" height={height}
         style={{ background: "#0d1117", borderRadius: 8, border: "1px solid #30363d", ...style }}>
      {edges.map((e, i) => {
        const a = pos.get(e.source), b = pos.get(e.target);
        if (!a || !b) return null;
        return (
          <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={CONF_STROKE[e.confidence || ""] || "#30363d"} strokeWidth={1.2} />
        );
      })}
      {nodes.map((n) => (
        <g key={n.id} transform={`translate(${n.x},${n.y})`} style={{ cursor: "pointer" }}
           onClick={() => onSelect?.(n.node)}>
          <circle r={n.seed ? 7 : 5} fill={n.seed ? "#58a6ff" : "#c9d1d9"}
                  stroke="#0d1117" strokeWidth={1.5}>
            <title>{n.node.label}{n.node.service ? ` (${n.node.service})` : ""}</title>
          </circle>
          <text x={9} y={4} fontSize={10} fill="#8b949e" fontFamily="system-ui, sans-serif">
            {trunc(n.node.label, 22)}
          </text>
        </g>
      ))}
    </svg>
  );
}

const CONF_STROKE: Record<string, string> = {
  EXTRACTED: "#2ea04355", INFERRED: "#d2992255", AMBIGUOUS: "#f8514955",
};

interface Placed { id: string; x: number; y: number; seed: boolean; node: NodeView }

function computeLayout(nodes: NodeView[], edges: SubgraphEdge[], seed: string, height: number) {
  // BFS distance (undirected) from the seed → column index.
  const adj = new Map<string, string[]>();
  for (const n of nodes) adj.set(n.id, []);
  for (const e of edges) {
    adj.get(e.source)?.push(e.target);
    adj.get(e.target)?.push(e.source);
  }
  const dist = new Map<string, number>([[seed, 0]]);
  let frontier = [seed];
  while (frontier.length) {
    const next: string[] = [];
    for (const u of frontier)
      for (const v of adj.get(u) || [])
        if (!dist.has(v)) { dist.set(v, (dist.get(u) || 0) + 1); next.push(v); }
    frontier = next;
  }
  const cols = new Map<number, NodeView[]>();
  for (const n of nodes) {
    const d = dist.get(n.id) ?? 99;
    (cols.get(d) ?? cols.set(d, []).get(d)!).push(n);
  }
  const colKeys = [...cols.keys()].sort((a, b) => a - b);
  const colW = 180;
  const width = Math.max(colW * colKeys.length + 40, 300);
  const placed: Placed[] = [];
  colKeys.forEach((c, ci) => {
    const list = cols.get(c)!;
    const gap = height / (list.length + 1);
    list.forEach((n, ni) => {
      placed.push({ id: n.id, x: 30 + ci * colW, y: gap * (ni + 1), seed: n.id === seed, node: n });
    });
  });
  return { nodes: placed, edges, width };
}

function trunc(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
