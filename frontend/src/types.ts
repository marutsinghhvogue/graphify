export interface NodeView {
  id: string;
  label: string;
  file_type: string | null;
  source_file: string | null;
  source_location: string | null;
}

export interface UncertainEdge {
  source: NodeView;
  target: NodeView;
  relation: string | null;
  confidence: string;
  confidence_score: number | null;
  source_file: string | null;
}

export interface ReviewQuestion {
  type: string;
  question: string;
  why: string;
}

export interface TaintStep {
  stmt_id?: string;
  line?: number;
  file?: string | null;
  func?: string | null;
  text?: string | null;
  callee?: string | null;
}

export interface TaintFinding {
  vuln: string;
  category: string;
  confidence: string;
  cross_function: boolean;
  callee?: string | null;
  source: TaintStep;
  sink: TaintStep;
  path: TaintStep[];
}

export interface TaintResponse {
  count: number;
  by_vuln: Record<string, number>;
  findings: TaintFinding[];
}

export type AliasMode = "same_as" | "merge";

export interface Alias {
  from: string;
  to: string;
  mode: AliasMode;
  reason: string;
  contributor?: string;
  date?: string;
}
