import type { CompileResult } from "../types";
import Skipped from "./Skipped";

const ICON = {
  pass: { symbol: "✓", style: "bg-emerald-100 text-emerald-700" },
  warn: { symbol: "⚠", style: "bg-amber-100 text-amber-700" },
  fail: { symbol: "✗", style: "bg-red-100 text-red-700" },
};

const PASS_INFO: Record<string, string> = {
  "Dead agents": "reachability analysis: breadth first search from start",
  Cycles: "cycle detection: Tarjan's strongly connected components",
  "Reaches end": "backward search from end",
  "State completeness": "forward dataflow analysis (definite assignment)",
  "Outputs written": "definite assignment checked at end",
  "Else edges": "every branching node should have a default",
  "Prompt injection": "taint analysis: untrusted data reaching a sensitive tool",
};

export default function VerifyTab({ result }: { result: CompileResult }) {
  const verify = result.phases.verify;
  const removed = result.phases.optimize.removed_agents;
  if (verify.status === "skipped") return <Skipped phase="verification" />;

  return (
    <div className="p-4">
      <p className="text-sm text-slate-500 mb-4">
        Static checks on the workflow graph. They check its <b>structure</b> (reachability, loops, data availability on
        every path); they do not predict what an LLM will answer.
      </p>
      <ul className="space-y-2">
        {verify.checks.map((c, i) => (
          <li key={i} className="border border-slate-200 rounded-lg p-3 flex gap-3">
            <span className={`w-7 h-7 shrink-0 rounded-full flex items-center justify-center font-bold ${ICON[c.status].style}`}>
              {ICON[c.status].symbol}
            </span>
            <div className="min-w-0">
              <div className="font-semibold">
                {c.name}
                <span className="font-normal text-xs text-slate-400 ml-2">{PASS_INFO[c.name]}</span>
              </div>
              <div className="text-sm text-slate-700">{c.message}</div>
              {c.path && (
                <div className="mt-1.5 flex flex-wrap items-center gap-1 text-xs">
                  <span className="text-slate-400 mr-1">witness path:</span>
                  {c.path.map((step, j) => (
                    <span key={j} className="flex items-center gap-1">
                      {j > 0 && <span className="text-slate-400">→</span>}
                      <span className="bg-slate-100 border border-slate-300 rounded px-1.5 py-0.5 font-mono">{step}</span>
                    </span>
                  ))}
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>

      <h3 className="font-semibold mt-6 mb-2">Optimization</h3>
      {result.phases.optimize.status === "skipped" ? (
        <p className="text-sm text-slate-500">Skipped because verification failed.</p>
      ) : (
        <ul className="text-sm space-y-1 list-disc ml-5">
          <li>
            Dead agent elimination:{" "}
            {removed.length ? <>removed <b>{removed.join(", ")}</b> (no code is generated for unreachable agents).</>
              : "nothing to remove, every agent is reachable."}
          </li>
          <li>
            Dead LLM call elimination (liveness analysis):{" "}
            {(result.phases.optimize.removed_stores ?? []).length ? (
              <>removed {(result.phases.optimize.removed_stores ?? []).map((r) => `${r.agent}: state.${r.field}`).join(", ")}.
                See the Cost tab for the saving.</>
            ) : "every computed value is used."}
          </li>
        </ul>
      )}
    </div>
  );
}
