import { useEffect, useState } from "react";
import { api } from "../api";
import type { TaintFinding, TaintResponse, TaintStep } from "../types";

// Injection classes are high severity (red); others fall back to amber.
const HIGH = new Set(["sql_injection", "command_injection", "code_injection", "ssti"]);
const sev = (vuln: string) => (HIGH.has(vuln) ? "var(--bad)" : "#d9a441");

export default function TaintView() {
  const [data, setData] = useState<TaintResponse | null>(null);
  const [vuln, setVuln] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api
      .taint(vuln)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [vuln]);

  if (loading) return <p>Analyzing taint flows…</p>;
  if (error) return <p className="error">{error}</p>;
  if (!data) return null;

  const vulns = Object.keys(data.by_vuln).sort();

  return (
    <section>
      <h2>Taint findings ({data.count})</h2>
      <p className="hint">
        Untrusted/sensitive input (source) reaching a dangerous operation (sink) without a
        sanitizer, inter-procedurally. Recorded as <code>flows_to</code> edges by{" "}
        <code>graphify extract --taint</code>.
      </p>

      <div className="row">
        <button className={vuln === "" ? "" : "ghost"} onClick={() => setVuln("")}>
          all
        </button>
        {vulns.map((v) => (
          <button
            key={v}
            className={vuln === v ? "" : "ghost"}
            onClick={() => setVuln(v)}
            style={vuln === v ? undefined : { borderColor: sev(v), color: sev(v) }}
          >
            {v} · {data.by_vuln[v]}
          </button>
        ))}
      </div>

      {data.count === 0 && (
        <p className="hint">
          No taint findings{vuln ? ` for ${vuln}` : ""}. Rebuild the graph with{" "}
          <code>graphify extract --taint</code>.
        </p>
      )}

      <ul className="cards">
        {data.findings.map((f, i) => (
          <FindingCard key={i} f={f} />
        ))}
      </ul>
    </section>
  );
}

function FindingCard({ f }: { f: TaintFinding }) {
  const color = sev(f.vuln);
  const mid = f.path.slice(1, -1);
  return (
    <li className="card">
      <div className="row" style={{ margin: 0, gap: 8 }}>
        <span className="badge" style={{ color, background: "transparent", border: `1px solid ${color}` }}>
          {f.vuln}
        </span>
        <span className="badge">{f.category}</span>
        <span className="badge">{f.confidence}</span>
        {f.cross_function && <span className="badge">cross-function</span>}
      </div>
      <div style={{ marginTop: 8, fontFamily: "ui-monospace, Menlo, monospace", fontSize: 13 }}>
        <Flow label="source" color={color} step={f.source} />
        {mid.map((s, j) => (
          <Flow key={j} label="→" color="var(--muted)" step={s} />
        ))}
        {f.callee ? (
          <Flow label="sink" color={color} step={f.sink} note={`in ${f.callee}()`} />
        ) : (
          <Flow label="sink" color={color} step={f.sink} />
        )}
      </div>
    </li>
  );
}

function Flow({
  label,
  color,
  step,
  note,
}: {
  label: string;
  color: string;
  step: TaintStep;
  note?: string;
}) {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "baseline", padding: "2px 0" }}>
      <span style={{ color, flex: "0 0 52px", fontSize: 11, textTransform: "uppercase" }}>{label}</span>
      <code style={{ flex: 1, background: "transparent", padding: 0, wordBreak: "break-word" }}>
        {note ? <em style={{ color: "var(--accent)", fontStyle: "normal" }}>{note} </em> : null}
        {step.text || ""}
      </code>
      <span style={{ color: "var(--muted)", fontSize: 11, whiteSpace: "nowrap" }}>{loc(step)}</span>
    </div>
  );
}

function loc(s: TaintStep): string {
  if (!s.file && !s.line) return "";
  const short = (s.file || "").replace(/\\/g, "/").split("/").slice(-2).join("/");
  return `${short}${s.line ? `:${s.line}` : ""}`;
}
