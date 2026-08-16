import type {
  ImpactResponse,
  SeedsResponse,
  StatsResponse,
  SubgraphResponse,
  TaintResponse,
} from "./types";

export interface GraphifyClientOptions {
  /** Base URL of the graphify REST API, e.g. "https://graph.internal". */
  apiBase: string;
  /** Optional API key sent as `X-API-Key` (matches GRAPHIFY_API_KEY on the server). */
  apiKey?: string;
  /** Optional custom fetch (SSR / testing). */
  fetchImpl?: typeof fetch;
}

export interface GraphifyClient {
  impact(label: string, depth?: number): Promise<ImpactResponse>;
  subgraph(label: string, depth?: number): Promise<SubgraphResponse>;
  seeds(query: string, top?: number): Promise<SeedsResponse>;
  stats(): Promise<StatsResponse>;
  taint(vuln?: string): Promise<TaintResponse>;
}

/** A thin typed client for the graphify v1 REST API. */
export function createClient(opts: GraphifyClientOptions): GraphifyClient {
  const base = opts.apiBase.replace(/\/+$/, "");
  const doFetch = opts.fetchImpl ?? fetch;
  const headers: Record<string, string> = {};
  if (opts.apiKey) headers["X-API-Key"] = opts.apiKey;

  async function get<T>(path: string): Promise<T> {
    const r = await doFetch(base + path, { headers });
    if (!r.ok) {
      const body = (await r.json().catch(() => ({}))) as { error?: string };
      throw new Error(body.error || `${r.status} ${r.statusText}`);
    }
    return (await r.json()) as T;
  }

  const q = encodeURIComponent;
  return {
    impact: (label, depth = 2) =>
      get<ImpactResponse>(`/api/v1/impact?label=${q(label)}&depth=${depth}`),
    subgraph: (label, depth = 1) =>
      get<SubgraphResponse>(`/api/v1/subgraph?label=${q(label)}&depth=${depth}`),
    seeds: (query, top = 10) =>
      get<SeedsResponse>(`/api/v1/seeds?q=${q(query)}&top=${top}`),
    stats: () => get<StatsResponse>(`/api/v1/stats`),
    taint: (vuln) =>
      get<TaintResponse>(`/api/v1/taint${vuln ? `?vuln=${q(vuln)}` : ""}`),
  };
}
