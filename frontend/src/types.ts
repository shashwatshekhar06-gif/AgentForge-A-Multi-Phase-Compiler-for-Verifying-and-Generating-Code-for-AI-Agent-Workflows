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
      symbols: { state: { name: string; type: string; modifier: string | null }[]; tools: unknown[]; agents: AgentSymbol[] };
    };
    ir: { status: PhaseStatus; nodes: IrNode[]; edges: IrEdge[] };
    verify: { status: PhaseStatus; checks: Check[] };
    optimize: {
      status: PhaseStatus;
      removed_agents: string[];
      removed_stores: { agent: string; field: string; line: number; llm_calls_saved: number }[];
    };
    cost: CostPhase;
    codegen: { status: PhaseStatus; code: string };
  };
  diagnostics: Diagnostic[];
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
