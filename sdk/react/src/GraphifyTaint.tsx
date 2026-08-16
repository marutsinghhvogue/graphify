import { useTaint } from "./hooks";
import type { GraphifyClientOptions } from "./client";
import type { TaintFinding, TaintStep } from "./types";

// Severity colour per vuln class (fallback amber). Injection classes are high.
const VULN_COLOR: Record<string, string> = {
  sql_injection: "#f85149",
  command_injection: "#f85149",
  code_injection: "#f85149",
  ssti: "#f85149",
  path_traversal: "#db6d28",
  open_redirect: "#d29922",
};

export interface GraphifyTaintProps extends GraphifyClientOptions {
  /** Filter to a single vuln class (e.g. "sql_injection"). Omit for all. */
  vuln?: string;
  className?: string;
  style?: React.CSSProperties;
  /** Called when the user clicks a finding (e.g. to open the source). */
  onSelect?: (finding: TaintFinding) => void;
}

/**
 * "Where does untrusted input reach a dangerous sink?" — taint findings as a
 * grouped, severity-coloured list, each showing the source→…→sink flow with
 * file:line. Reads graphify's v1 REST API (`/api/v1/taint`), which serves the
 * `flows_to` edges recorded by `graphify extract --taint`. Drop into any React
 * app with an apiBase + apiKey.
 */
export function GraphifyTaint(props: GraphifyTaintProps) {
  const { vuln, className, style, onSelect, ...conn } = props;
  const { data, error, loading } = useTaint(conn, vuln);

  if (loading) return <div style={box(style)}>Loading taint findings…</div>;
  if (error) return <div style={{ ...box(style), color: "#f85149" }}>Error: {error}</div>;
  if (!data || data.count === 0) {
    return (
      <div style={box(style)}>
        No taint findings{vuln ? ` for ${vuln}` : ""}.{" "}
        <span style={{ color: "#6e7681" }}>
          Build the graph with <code>graphify extract --taint</code>.
        </span>
      </div>
    );
  }

  // Group findings by vuln so the same class of bug reads together.
  const groups = new Map<string, TaintFinding[]>();
  for (const f of data.findings) {
    (groups.get(f.vuln) ?? groups.set(f.vuln, []).get(f.vuln)!).push(f);
  }

  return (
    <div className={className} style={{ fontFamily: "system-ui, sans-serif", ...style }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        Taint findings{" "}
        <span style={{ color: "#8b949e", fontWeight: 400 }}>— {data.count} total</span>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
        {Object.entries(data.by_vuln).map(([v, n]) => (
          <span key={v} style={badge(VULN_COLOR[v] || "#d29922")}>
            {v} · {n}
          </span>
        ))}
      </div>
      {[...groups.entries()].map(([v, findings]) => (
        <div key={v} style={{ marginBottom: 14 }}>
          {findings.map((f, i) => {
            const color = VULN_COLOR[f.vuln] || "#d29922";
            const mid = f.path.slice(1, -1);
            return (
              <button
                key={i}
                onClick={() => onSelect?.(f)}
                style={card(!!onSelect)}
              >
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                  <span style={dot(color)} title={f.confidence} />
                  <span style={{ fontWeight: 600, color }}>{f.vuln}</span>
                  <span style={tag}>{f.category}</span>
                  <span style={{ color: "#8b949e", fontSize: 11 }}>{f.confidence}</span>
                  {f.cross_function && <span style={tag}>cross-function</span>}
                </div>
                <FlowLine kind="source" step={f.source} color={color} />
                {mid.map((s, j) => (
                  <FlowLine key={j} kind="via" step={s} color="#8b949e" />
                ))}
                <SinkLine finding={f} color={color} />
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function FlowLine({ kind, step, color }: { kind: "source" | "via"; step: TaintStep; color: string }) {
  return (
    <div style={flowRow}>
      <span style={{ ...pill, color, borderColor: color }}>{kind}</span>
      <code style={codeCell}>{step.text || ""}</code>
      <span style={locCell}>{loc(step)}</span>
    </div>
  );
}

function SinkLine({ finding, color }: { finding: TaintFinding; color: string }) {
  const { sink, callee } = finding;
  return (
    <div style={flowRow}>
      <span style={{ ...pill, color, borderColor: color, background: color, WebkitTextFillColor: "#0d1117" }}>
        sink
      </span>
      {callee ? (
        <code style={codeCell}>
          in <b>{callee}()</b> <span style={{ color: "#8b949e" }}>(called here)</span>
        </code>
      ) : (
        <code style={codeCell}>{sink.text || ""}</code>
      )}
      <span style={locCell}>{loc(sink)}</span>
    </div>
  );
}

function loc(s: TaintStep): string {
  if (!s.file && !s.line) return "";
  return `${short(s.file)}${s.line ? `:${s.line}` : ""}`;
}
function short(path?: string | null): string {
  if (!path) return "";
  const parts = path.replace(/\\/g, "/").split("/");
  return parts.slice(-2).join("/");
}

const box = (style?: React.CSSProperties): React.CSSProperties => ({
  fontFamily: "system-ui, sans-serif", color: "#6e7681", padding: 8, ...style,
});
const badge = (color: string): React.CSSProperties => ({
  fontSize: 11, fontWeight: 600, color, border: `1px solid ${color}`,
  borderRadius: 999, padding: "1px 8px",
});
const card = (clickable: boolean): React.CSSProperties => ({
  display: "block", width: "100%", textAlign: "left", marginBottom: 8,
  padding: 10, border: "1px solid #30363d", borderRadius: 8,
  background: "transparent", color: "inherit",
  cursor: clickable ? "pointer" : "default",
});
const dot = (color: string): React.CSSProperties => ({
  width: 8, height: 8, borderRadius: "50%", background: color, flex: "0 0 auto",
});
const tag: React.CSSProperties = {
  fontSize: 11, background: "#21262d", color: "#c9d1d9", padding: "1px 6px", borderRadius: 4,
};
const flowRow: React.CSSProperties = {
  display: "flex", gap: 8, alignItems: "baseline", padding: "2px 0", fontSize: 12.5,
};
const pill: React.CSSProperties = {
  fontSize: 10, textTransform: "uppercase", letterSpacing: ".05em",
  border: "1px solid", borderRadius: 4, padding: "0 5px", flex: "0 0 auto", width: 44,
  textAlign: "center",
};
const codeCell: React.CSSProperties = {
  flex: "1 1 auto", whiteSpace: "pre-wrap", wordBreak: "break-word",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
};
const locCell: React.CSSProperties = {
  color: "#6e7681", fontSize: 11, flex: "0 0 auto", marginLeft: "auto",
};
