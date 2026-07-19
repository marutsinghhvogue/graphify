import type { Alias, AliasMode, NodeView, ReviewQuestion, UncertainEdge } from "./types";

// All requests are same-origin: Vite proxies /api and /health to the Flask
// server in dev (see vite.config.ts), and in production the built assets are
// served from the same host as the API.

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error((body as { error?: string }).error || `${r.status} ${r.statusText}`);
  }
  return r.json() as Promise<T>;
}

async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data as { error?: string }).error || `${r.status} ${r.statusText}`);
  return data as T;
}

export const api = {
  health: () => getJSON<{ status: string; graph: string }>("/health"),

  query: (q: string, mode: "bfs" | "dfs" = "bfs", depth = 3) =>
    getJSON<{ result: string }>(
      `/api/query?q=${encodeURIComponent(q)}&mode=${mode}&depth=${depth}`
    ),

  nodes: (label: string) =>
    getJSON<{ matches: NodeView[] }>(`/api/nodes?label=${encodeURIComponent(label)}`),

  callers: (label: string) =>
    getJSON<{ result: string }>(`/api/callers?label=${encodeURIComponent(label)}`),

  callees: (label: string) =>
    getJSON<{ result: string }>(`/api/callees?label=${encodeURIComponent(label)}`),

  uncertainEdges: (confidence = "INFERRED,AMBIGUOUS") =>
    getJSON<{ count: number; edges: UncertainEdge[] }>(
      `/api/review/uncertain-edges?confidence=${encodeURIComponent(confidence)}`
    ),

  questions: () => getJSON<{ questions: ReviewQuestion[] }>("/api/review/questions"),

  aliases: () => getJSON<{ aliases: Alias[] }>("/api/aliases"),

  addAlias: (body: { from: string; to: string; mode: AliasMode; reason?: string; contributor?: string }) =>
    postJSON<{ alias: Alias; note: string }>("/api/aliases", body),
};
