import type { CompileResult } from "../types";

const PHASES: [keyof CompileResult["phases"], string][] = [
  ["lexer", "Lexer"],
  ["parser", "Parser"],
  ["semantic", "Semantic"],
  ["ir", "IR"],
  ["verify", "Verify"],
  ["optimize", "Optimize"],
  ["cost", "Cost"],
  ["codegen", "Codegen"],
];

const STYLE = {
  ok: "bg-emerald-100 text-emerald-800 border-emerald-300",
  error: "bg-red-100 text-red-800 border-red-300",
  skipped: "bg-slate-100 text-slate-400 border-slate-200",
};

const ICON = { ok: "✓", error: "✗", skipped: "–" };

// The compiler pipeline as a row of chips, colored by each phase's status.
export default function PhaseBar({ result }: { result: CompileResult | null }) {
  return (
    <div className="flex flex-wrap items-center gap-1 px-3 py-2 bg-white border-b border-slate-200 text-sm">
      {PHASES.map(([key, label], i) => {
        const status = result?.phases[key]?.status ?? "skipped";   // ?. : an older backend has fewer phases
        return (
          <div key={key} className="flex items-center gap-1">
            {i > 0 && <span className="text-slate-300">→</span>}
            <span className={`border rounded-full px-2.5 py-0.5 font-medium ${STYLE[status]}`} title={status}>
              {ICON[status]} {label}
            </span>
          </div>
        );
      })}
    </div>
  );
}
