import { useEffect, useState } from "react";
import { api } from "../api";
import type { Alias, AliasMode } from "../types";

export default function AliasManager() {
  const [aliases, setAliases] = useState<Alias[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [mode, setMode] = useState<AliasMode>("same_as");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  function refresh() {
    api
      .aliases()
      .then((a) => setAliases(a.aliases))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!from.trim() || !to.trim()) return;
    setSaving(true);
    setFormError(null);
    try {
      await api.addAlias({ from, to, mode, reason });
      setFrom("");
      setTo("");
      setReason("");
      refresh();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section>
      <h2>Add alias</h2>
      <p className="hint">
        Declare two entities are the same concept (e.g. doc <code>User</code> ≡ code <code>Customer</code>). Applies on the next build.
      </p>
      <form className="row" onSubmit={add}>
        <input placeholder="from (e.g. User)" value={from} onChange={(e) => setFrom(e.target.value)} />
        <span className="arrow">→</span>
        <input placeholder="to (e.g. Customer)" value={to} onChange={(e) => setTo(e.target.value)} />
        <select value={mode} onChange={(e) => setMode(e.target.value as AliasMode)}>
          <option value="same_as">same_as</option>
          <option value="merge">merge</option>
        </select>
        <input placeholder="reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
        <button type="submit" disabled={saving}>
          {saving ? "…" : "Add"}
        </button>
      </form>
      {formError && <p className="error">{formError}</p>}

      <h2>Recorded aliases ({aliases.length})</h2>
      {loading ? (
        <p>Loading…</p>
      ) : error ? (
        <p className="error">{error}</p>
      ) : aliases.length === 0 ? (
        <p className="hint">None yet.</p>
      ) : (
        <table className="aliases">
          <thead>
            <tr>
              <th>From</th>
              <th>Mode</th>
              <th>To</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {aliases.map((a, i) => (
              <tr key={i}>
                <td>{a.from}</td>
                <td>
                  <span className="badge">{a.mode}</span>
                </td>
                <td>{a.to}</td>
                <td className="why">{a.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
