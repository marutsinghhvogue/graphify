import { useEffect, useState } from "react";
import { api } from "../api";
import type { AliasMode, ReviewQuestion, UncertainEdge } from "../types";

export default function ReviewQueue() {
  const [edges, setEdges] = useState<UncertainEdge[]>([]);
  const [questions, setQuestions] = useState<ReviewQuestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.uncertainEdges(), api.questions()])
      .then(([e, q]) => {
        setEdges(e.edges);
        setQuestions(q.questions);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p>Loading review queue…</p>;
  if (error) return <p className="error">{error}</p>;

  return (
    <section>
      <h2>Uncertain edges ({edges.length})</h2>
      <p className="hint">Model-inferred or ambiguous links. Confirm a real equivalence to promote it to a deterministic alias edge.</p>
      {edges.length === 0 && <p className="hint">None — the graph has no INFERRED/AMBIGUOUS edges.</p>}
      <ul className="cards">
        {edges.map((e, i) => (
          <EdgeCard key={i} edge={e} onConfirmed={() => setEdges((prev) => prev.filter((_, j) => j !== i))} />
        ))}
      </ul>

      <h2>Verification questions ({questions.length})</h2>
      <ul className="cards">
        {questions.map((q, i) => (
          <li key={i} className="card">
            <div className="badge">{q.type}</div>
            <div className="q">{q.question}</div>
            <div className="why">{q.why}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function EdgeCard({ edge, onConfirmed }: { edge: UncertainEdge; onConfirmed: () => void }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<AliasMode>("same_as");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setSaving(true);
    setError(null);
    try {
      await api.addAlias({ from: edge.source.label, to: edge.target.label, mode, reason });
      setDone(`Recorded ${edge.source.label} ${mode} ${edge.target.label} — applies on next build.`);
      setTimeout(onConfirmed, 1200);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <li className="card">
      <div className="badge">
        {edge.confidence}
        {edge.confidence_score != null && ` ${edge.confidence_score.toFixed(2)}`}
      </div>
      <div className="q">
        <strong>{edge.source.label}</strong> <em>{edge.relation}</em> <strong>{edge.target.label}</strong>
      </div>
      <div className="why">
        {edge.source.file_type} → {edge.target.file_type}
        {edge.source_file ? ` · ${edge.source_file}` : ""}
      </div>
      {done ? (
        <div className="ok-note">{done}</div>
      ) : open ? (
        <div className="confirm-form">
          <select value={mode} onChange={(e) => setMode(e.target.value as AliasMode)}>
            <option value="same_as">same_as (add edge)</option>
            <option value="merge">merge (fold into one node)</option>
          </select>
          <input placeholder="reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <button onClick={confirm} disabled={saving}>
            {saving ? "…" : "Save alias"}
          </button>
          <button className="ghost" onClick={() => setOpen(false)}>
            Cancel
          </button>
          {error && <span className="error">{error}</span>}
        </div>
      ) : (
        <button className="ghost" onClick={() => setOpen(true)}>
          Confirm as alias
        </button>
      )}
    </li>
  );
}
