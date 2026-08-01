import { useState } from "react";
import { useSeeds } from "./hooks";
import type { GraphifyClientOptions } from "./client";
import type { Seed } from "./types";

export interface GraphifySeedsProps extends GraphifyClientOptions {
  /** How many seeds to return (default 10). */
  top?: number;
  placeholder?: string;
  className?: string;
  style?: React.CSSProperties;
  /** Called when a seed is clicked — e.g. feed it into <GraphifyImpact>. */
  onSelect?: (seed: Seed) => void;
}

/**
 * Stage 2: type a requirement in prose, get the code symbols it likely touches.
 * Feed a selected seed into <GraphifyImpact> to compute its blast radius.
 */
export function GraphifySeeds(props: GraphifySeedsProps) {
  const { top = 10, placeholder, className, style, onSelect, ...conn } = props;
  const [text, setText] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const { data, error, loading } = useSeeds(conn, query, top);

  return (
    <div className={className} style={{ fontFamily: "system-ui, sans-serif", ...style }}>
      <form onSubmit={(e) => { e.preventDefault(); setQuery(text.trim() || null); }} style={{ display: "flex", gap: 8 }}>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={placeholder ?? "Describe the change… e.g. 'nightly billing rollup'"}
          style={{ flex: 1, padding: "8px 10px", borderRadius: 6, border: "1px solid #30363d",
                   background: "#0d1117", color: "#e6edf3" }}
        />
        <button type="submit" style={{ padding: "8px 14px", borderRadius: 6, border: "1px solid #30363d",
                 background: "#21262d", color: "#e6edf3", cursor: "pointer" }}>Find</button>
      </form>
      {loading && <div style={{ color: "#6e7681", marginTop: 8 }}>Searching…</div>}
      {error && <div style={{ color: "#f85149", marginTop: 8 }}>Error: {error}</div>}
      {data && query && (
        <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 0" }}>
          {data.seeds.length === 0 && <li style={{ color: "#6e7681" }}>No matches.</li>}
          {data.seeds.map((s) => (
            <li key={s.symbol_id}>
              <button onClick={() => onSelect?.(s)}
                      style={{ display: "flex", gap: 8, width: "100%", textAlign: "left", padding: "6px 8px",
                               background: "transparent", border: "none", borderRadius: 6, cursor: "pointer",
                               color: "#e6edf3", fontSize: 13 }}>
                <span style={{ fontWeight: 500 }}>{s.name}</span>
                <span style={{ fontSize: 11, background: "#21262d", padding: "1px 6px", borderRadius: 4, color: "#8b949e" }}>{s.matched}</span>
                <span style={{ color: "#6e7681", fontSize: 11, marginLeft: "auto" }}>{s.path}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
