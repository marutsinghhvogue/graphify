import { useState } from "react";
import { useImpact } from "./hooks";
import type { GraphifyClientOptions } from "./client";
import type { ImpactHit } from "./types";
import { GraphifySubgraph } from "./GraphifySubgraph";

const CONF_COLOR: Record<string, string> = {
  EXTRACTED: "#3fb950",
  INFERRED: "#d29922",
  AMBIGUOUS: "#f85149",
};

export interface GraphifyImpactProps extends GraphifyClientOptions {
  /** Symbol id or label to analyze. */
  symbol: string;
  /** Max reverse-reachability depth (default 2). */
  depth?: number;
  /** Show the graph view alongside the list (default true). */
  graph?: boolean;
  className?: string;
  style?: React.CSSProperties;
  /** Called when the user clicks an affected symbol (e.g. to navigate). */
  onSelect?: (hit: ImpactHit) => void;
}

/**
 * "Where does changing this symbol have impact?" — the blast radius as a
 * grouped, confidence-tiered list, plus an optional graph view. Talks only to
 * graphify's v1 REST API; drop it into any React app with an apiBase + apiKey.
 */
export function GraphifyImpact(props: GraphifyImpactProps) {
  const { symbol, depth = 2, graph = true, className, style, onSelect, ...conn } = props;
  const { data, error, loading } = useImpact(conn, symbol, depth);
  const [selected, setSelected] = useState<string | null>(null);

  if (loading) return <div style={box(style)}>Analyzing impact of {symbol}…</div>;
  if (error) return <div style={{ ...box(style), color: "#f85149" }}>Error: {error}</div>;
  if (!data || !data.seed.id) return <div style={box(style)}>No match for “{symbol}”.</div>;

  const byDepth = new Map<number, ImpactHit[]>();
  for (const h of data.affected) {
    (byDepth.get(h.depth) ?? byDepth.set(h.depth, []).get(h.depth)!).push(h);
  }
  const focus = selected || data.seed.id;

  return (
    <div className={className} style={{ display: "flex", gap: 16, fontFamily: "system-ui, sans-serif", ...style }}>
      <div style={{ flex: "1 1 340px", minWidth: 280 }}>
        <div style={{ fontWeight: 600, marginBottom: 8 }}>
          Blast radius of <code>{data.seed.label}</code>{" "}
          <span style={{ color: "#8b949e", fontWeight: 400 }}>
            — {data.count} affected (depth ≤ {data.depth})
          </span>
        </div>
        {data.count === 0 && <div style={{ color: "#8b949e" }}>Nothing depends on this symbol.</div>}
        {[...byDepth.keys()].sort((a, b) => a - b).map((d) => (
          <div key={d} style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".08em", color: "#8b949e", margin: "6px 0" }}>
              depth {d}
            </div>
            {byDepth.get(d)!.map((h) => (
              <button
                key={h.id}
                onClick={() => { setSelected(h.id); onSelect?.(h); }}
                style={row(h.id === focus)}
              >
                <span style={dot(CONF_COLOR[h.confidence] || "#8b949e")} title={h.confidence} />
                <span style={{ fontWeight: 500 }}>{h.label}</span>
                {h.service && <span style={tag}>{h.service}</span>}
                <span style={{ color: "#8b949e", fontSize: 12 }}>[{h.via_relation}]</span>
                <span style={{ color: "#6e7681", fontSize: 11, marginLeft: "auto" }}>
                  {short(h.source_file)}{h.source_location ? `:${h.source_location}` : ""}
                </span>
              </button>
            ))}
          </div>
        ))}
      </div>
      {graph && (
        <div style={{ flex: "1 1 420px", minWidth: 320 }}>
          <GraphifySubgraph {...conn} symbol={focus} depth={Math.max(1, depth - 1)} height={360} />
        </div>
      )}
    </div>
  );
}

const box = (style?: React.CSSProperties): React.CSSProperties => ({
  fontFamily: "system-ui, sans-serif", color: "#6e7681", padding: 8, ...style,
});
const row = (active: boolean): React.CSSProperties => ({
  display: "flex", gap: 8, alignItems: "center", width: "100%", textAlign: "left",
  padding: "6px 8px", border: "1px solid " + (active ? "#58a6ff" : "transparent"),
  background: active ? "rgba(88,166,255,.08)" : "transparent",
  borderRadius: 6, cursor: "pointer", fontSize: 13, color: "inherit",
});
const dot = (color: string): React.CSSProperties => ({
  width: 8, height: 8, borderRadius: "50%", background: color, flex: "0 0 auto",
});
const tag: React.CSSProperties = {
  fontSize: 11, background: "#21262d", color: "#c9d1d9", padding: "1px 6px", borderRadius: 4,
};
function short(path?: string | null): string {
  if (!path) return "";
  const parts = path.replace(/\\/g, "/").split("/");
  return parts.slice(-2).join("/");
}
