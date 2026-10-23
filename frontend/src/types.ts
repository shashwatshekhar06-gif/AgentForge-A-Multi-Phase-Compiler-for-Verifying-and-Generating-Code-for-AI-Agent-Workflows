// Types for the API response. They mirror docs/contract.md section 2.

export type PhaseStatus = "ok" | "error" | "skipped";

export interface Token {
  type: string;
  lexeme: string;
  line: number;
  col: number;
}

export interface AstNode {
  type: string;
  line: number;
  col: number;
  [field: string]: unknown;
}

export interface AgentSymbol {
  name: string;
  reads: string[];
  writes: string[];
  must_writes: string[];
}

export interface IrNode {
  id: string;
  kind: "start" | "agent" | "end";
  reads: string[];
  writes: string[];
  must_writes: string[];
  status: "ok" | "warning" | "error";
  line: number;
  llm_calls: number;    // most llm(...) calls one run of this agent can make
  tool_calls: number;
  removed_llm_calls: number;   // llm calls the optimizer removed from this agent
}

export interface IrEdge {
  from: string;
  to: string;
  condition: string | null;
  max_iterations: number | null;
  is_default: boolean;
}

export interface Check {
  name: string;
  status: "pass" | "warn" | "fail";
  message: string;
  path: string[] | null;
}

export interface Diagnostic {
  severity: "error" | "warning";
  code: string;
  phase: string;
  message: string;
  line: number;
  col: number;
  path: string[] | null;
}

export interface CompileResult {
  ok: boolean;
  phases: {
    lexer: { status: PhaseStatus; tokens: Token[] };
    parser: { status: PhaseStatus; ast: AstNode | null };
    semantic: {
      status: PhaseStatus;
      symbols: {
        state: { name: string; type: string; modifier: string | null }[];
        tools: unknown[];
        agents: AgentSymbol[];
        compares?: { name: string; candidates: string[]; cases: number; end_to_end?: boolean; ensure?: string | null }[];
      };
    };
    ir: { status: PhaseStatus; nodes: IrNode[]; edges: IrEdge[] };
    verify: { status: PhaseStatus; checks: Check[] };
    optimize: {
      status: PhaseStatus;
      removed_agents: string[];
      removed_stores: { agent: string; field: string; line: number; llm_calls_saved: number }[];
      pruned?: { necessity: string; agent: string; llm_calls_saved: number; score_with: number | null; score_without: number | null; total: number }[];
      selected?: { compare: string; replaced: string; with: string; scores: Record<string, string>; profile: "mock" | "measured" }[];
    };
    cost: CostPhase;
    codegen: { status: PhaseStatus; code: string };
  };
  diagnostics: Diagnostic[];
  evaluation?: CompareReport[];   // one report per compare block
  necessity?: NecessityReport[];  // one report per necessity block
}

export interface CaseResult {
  agent: string;
  output: string | number | boolean | string[];
  passed: boolean;
  scripted: boolean;
}

export interface CompareReport {
  name: string;
  mode: "mock" | "measured";   // scripted answers, or scores measured with a real model
  scope?: "agent" | "end_to_end";   // each candidate judged alone, or by the final output of the whole workflow
  model_calls?: number;            // upper bound on the model calls a real measurement makes
  candidates: string[];
  wired: string[];       // the candidate(s) used in the flow
  cases: {
    index: number;
    inputs: Record<string, unknown>;
    expect: { field: string; op: "contains" | "=="; value: string | number | boolean };
    results: CaseResult[];
  }[];
  scores: { agent: string; passed: number; total: number }[];
  best: string;
  tie: boolean;
  applied: string | null;   // the candidate the optimizer put into the generated code instead of the wired one
  blocked: string | null;   // why a better candidate was not applied
}

export interface NecessityAgent {
  agent: string;
  status: "testable" | "structural" | "branching" | "tool" | "candidate";   // why it can or cannot be tested
  reason: string;
  llm_calls: number;
  score_without: number | null;   // cases passed with this agent left out (null = not tested)
  needed: boolean | null;         // null = not measured
  removed: boolean;               // the optimizer dropped it from the generated code
}

export interface NecessityReport {
  name: string;
  mode: "static" | "measured";    // nothing measured yet, or measured with a real model
  total: number;                  // test cases
  baseline: number | null;        // cases the workflow as written passes
  agents: NecessityAgent[];
  removable: string[];
  applied: string[];
  blocked: string[];
  model_calls: number;            // upper bound on the model calls a real measurement makes
}

export interface CostCount {
  llm: number | null;   // null = unbounded
  tool: number | null;
}

export interface CostPhase {
  status: PhaseStatus;
  worst: CostCount;
  best: CostCount;
  agents: { id: string; llm_min: number; llm_max: number; tool_min: number; tool_max: number; max_visits: number | null }[];
  unbounded_loops: string[][];
  removed_llm_calls: number;
}

export interface Example {
  name: string;
  category: "valid" | "invalid";
  source: string;
}
