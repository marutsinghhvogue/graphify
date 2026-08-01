// Response shapes for graphify's v1 REST API (see graphify/webapi.py).

export interface NodeView {
  id: string;
  label: string;
  file_type?: string | null;
  kind?: string | null;
  source_file?: string | null;
  source_location?: string | null;
  service?: string | null;
}

export interface ImpactHit extends NodeView {
  depth: number;
  via_relation: string;
  confidence: string; // EXTRACTED | INFERRED | AMBIGUOUS
}

export interface ImpactResponse {
  seed: NodeView;
  depth: number;
  count: number;
  affected: ImpactHit[];
}

export interface SubgraphEdge {
  source: string;
  target: string;
  relation?: string | null;
  confidence?: string | null;
  confidence_score?: number | null;
}

export interface SubgraphResponse {
  seed: string;
  depth: number;
  nodes: NodeView[];
  edges: SubgraphEdge[];
}

export interface Seed {
  symbol_id: string;
  name: string;
  path?: string | null;
  kind?: string | null;
  score: number;
  matched: string; // lexical | vector | both
}

export interface SeedsResponse {
  query: string;
  seeds: Seed[];
}

export interface StatsResponse {
  nodes: number;
  edges: number;
  communities: number;
  confidence: Record<string, number>;
}
