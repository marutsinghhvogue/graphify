import { useState } from "react";
import { api } from "../api";

type Action = "query" | "callers" | "callees";

export default function QueryExplorer() {
  const [term, setTerm] = useState("");
  const [action, setAction] = useState<Action>("query");
  const [mode, setMode] = useState<"bfs" | "dfs">("bfs");
  const [depth, setDepth] = useState(3);
  const [result, setResult] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!term.trim()) return;
    setLoading(true);
    setError(null);
    try {
      if (action === "query") setResult((await api.query(term, mode, depth)).result);
      else if (action === "callers") setResult((await api.callers(term)).result);
      else setResult((await api.callees(term)).result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setResult("");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section>
      <form className="row" onSubmit={run}>
        <input
          placeholder={action === "query" ? "Ask about the codebase…" : "Function / node label…"}
          value={term}
          onChange={(e) => setTerm(e.target.value)}
        />
        <select value={action} onChange={(e) => setAction(e.target.value as Action)}>
          <option value="query">Traverse</option>
          <option value="callers">Callers</option>
          <option value="callees">Callees</option>
        </select>
        {action === "query" && (
          <>
            <select value={mode} onChange={(e) => setMode(e.target.value as "bfs" | "dfs")}>
              <option value="bfs">BFS</option>
              <option value="dfs">DFS</option>
            </select>
            <input
              className="depth"
              type="number"
              min={1}
              max={6}
              value={depth}
              onChange={(e) => setDepth(Number(e.target.value))}
            />
          </>
        )}
        <button type="submit" disabled={loading}>
          {loading ? "…" : "Run"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}
      {result && <pre className="result">{result}</pre>}
    </section>
  );
}
