import { useEffect, useMemo, useState } from "react";
import { createClient, type GraphifyClientOptions } from "./client";
import type { ImpactResponse, SeedsResponse, SubgraphResponse } from "./types";

export interface AsyncState<T> {
  data?: T;
  error?: string;
  loading: boolean;
}

/** Memoized client keyed on connection identity. */
export function useGraphifyClient(opts: GraphifyClientOptions) {
  return useMemo(
    () => createClient(opts),
    [opts.apiBase, opts.apiKey, opts.fetchImpl],
  );
}

function useAsync<T>(run: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({ loading: true });
  useEffect(() => {
    let alive = true;
    setState({ loading: true });
    run()
      .then((data) => alive && setState({ data, loading: false }))
      .catch((e) => alive && setState({ error: String(e?.message ?? e), loading: false }));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

/** Blast radius of `label` — what a change to it could affect. */
export function useImpact(
  opts: GraphifyClientOptions,
  label: string | null | undefined,
  depth = 2,
): AsyncState<ImpactResponse> {
  const client = useGraphifyClient(opts);
  return useAsync<ImpactResponse>(
    () => (label ? client.impact(label, depth) : Promise.resolve({ seed: { id: "", label: "" }, depth, count: 0, affected: [] })),
    [opts.apiBase, opts.apiKey, label, depth],
  );
}

/** Nodes + edges around `label` for visualization. */
export function useSubgraph(
  opts: GraphifyClientOptions,
  label: string | null | undefined,
  depth = 1,
): AsyncState<SubgraphResponse> {
  const client = useGraphifyClient(opts);
  return useAsync<SubgraphResponse>(
    () => (label ? client.subgraph(label, depth) : Promise.resolve({ seed: "", depth, nodes: [], edges: [] })),
    [opts.apiBase, opts.apiKey, label, depth],
  );
}

/** Stage 2 — prose requirement → ranked seed symbols. */
export function useSeeds(
  opts: GraphifyClientOptions,
  query: string | null | undefined,
  top = 10,
): AsyncState<SeedsResponse> {
  const client = useGraphifyClient(opts);
  return useAsync<SeedsResponse>(
    () => (query ? client.seeds(query, top) : Promise.resolve({ query: "", seeds: [] })),
    [opts.apiBase, opts.apiKey, query, top],
  );
}
