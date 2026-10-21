import { useState } from "react";
import type { CompileResult } from "../types";

function money(value: number): string {
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function NumberInput({ label, value, onChange, step }: { label: string; value: number; onChange: (v: number) => void; step: number }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-600">
      {label}
      <input
        type="number"
        min={0}
        step={step}
        value={value}
        onChange={(e) => onChange(Math.max(0, Number(e.target.value) || 0))}
        className="border border-slate-300 rounded-md px-2 py-1 w-36 text-sm text-slate-900"
      />
    </label>
  );
}

function Card({ title, value, sub, tone }: { title: string; value: string; sub: string; tone: "red" | "green" | "slate" | "amber" }) {
  const tones = {
    red: "border-red-200 bg-red-50",
    green: "border-emerald-200 bg-emerald-50",
    slate: "border-slate-200 bg-white",
    amber: "border-amber-300 bg-amber-50",
  };
  return (
    <div className={`border rounded-lg p-3 ${tones[tone]}`}>
      <div className="text-xs text-slate-500">{title}</div>
      <div className="text-2xl font-bold text-slate-900 mt-0.5">{value}</div>
      <div className="text-xs text-slate-500 mt-0.5">{sub}</div>
    </div>
  );
}

export default function CostTab({ result }: { result: CompileResult }) {
  const cost = result.phases.cost;
  const [llmPrice, setLlmPrice] = useState(0.01);
  const [toolPrice, setToolPrice] = useState(0);
  const [runs, setRuns] = useState(10000);

  if (!cost) {
    return <p className="p-6 text-slate-500">The API server is running an older version without cost analysis. Restart it.</p>;
  }

  if (cost.status === "skipped") {
    const blocked = result.phases.verify.status === "error";
    return (
      <div className="p-6">
        {blocked ? (
          <div className="border border-emerald-300 bg-emerald-50 rounded-lg p-4">
            <div className="font-semibold text-emerald-800">Blocked before spending anything</div>
            <p className="text-sm text-emerald-900 mt-1">
              Verification found structural bugs, so no code was generated and not a single LLM call was made. In a hand
              written LangGraph program these bugs would only show up at runtime, after paying for the calls that led to
              them. Fix the problems and compile again to see the cost analysis.
            </p>
          </div>
        ) : (
          <p className="text-slate-500">Cost analysis was skipped because an earlier phase reported errors.</p>
        )}
      </div>
    );
  }

  const perRun = (llm: number | null, tool: number | null) =>
    llm === null || tool === null ? null : llm * llmPrice + tool * toolPrice;
  const worstRun = perRun(cost.worst.llm, cost.worst.tool);
  const bestRun = perRun(cost.best.llm, cost.best.tool);
  const saved = cost.removed_llm_calls;

  return (
    <div className="p-4 space-y-5">
      <p className="text-sm text-slate-500">
        How many LLM and tool calls can <b>one run</b> of this workflow make? Computed statically, before anything runs,
        from the graph and the <code>max_iterations</code> bounds (an idea from worst-case execution time analysis).
      </p>

      <div className="flex flex-wrap gap-4 items-end">
        <NumberInput label="Price per LLM call ($)" value={llmPrice} onChange={setLlmPrice} step={0.001} />
        <NumberInput label="Price per tool call ($)" value={toolPrice} onChange={setToolPrice} step={0.001} />
        <NumberInput label="Runs per month" value={runs} onChange={setRuns} step={1000} />
      </div>

      <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
        <Card
          title="Worst case LLM calls / run"
          value={cost.worst.llm === null ? "∞" : String(cost.worst.llm)}
          sub={cost.worst.tool === null ? "tool calls: unbounded" : `+ ${cost.worst.tool} tool calls`}
          tone={cost.worst.llm === null ? "amber" : "slate"}
        />
        <Card
          title="Best case LLM calls / run"
          value={cost.best.llm === null ? "–" : String(cost.best.llm)}
          sub={cost.best.tool === null ? "" : `+ ${cost.best.tool} tool calls`}
          tone="slate"
        />
        <Card
          title="Cost per run"
          value={worstRun === null ? "unbounded" : money(worstRun)}
          sub={bestRun === null ? "" : `best case ${money(bestRun)}`}
          tone={worstRun === null ? "amber" : "slate"}
        />
        <Card
          title={`Worst case per month (${runs.toLocaleString()} runs)`}
          value={worstRun === null ? "unbounded" : money(worstRun * runs)}
          sub={bestRun === null ? "" : `best case ${money(bestRun * runs)}`}
          tone={worstRun === null ? "amber" : "slate"}
        />
      </div>

      {cost.unbounded_loops.length > 0 && (
        <div className="border border-amber-300 bg-amber-50 rounded-lg p-3 text-sm">
          <div className="font-semibold text-amber-900">No cost ceiling (AF-W308)</div>
          {cost.unbounded_loops.map((loop, i) => (
            <div key={i} className="text-amber-900 mt-1">
              The loop <code className="font-mono">{loop.join(" → ")}</code> can repeat without limit, so one run could keep
              paying for LLM calls forever. Add <code>max_iterations</code> to one of its edges to cap the spend.
            </div>
          ))}
        </div>
      )}

      {saved > 0 && (
        <div className="border border-emerald-300 bg-emerald-50 rounded-lg p-3 text-sm">
          <div className="font-semibold text-emerald-800">The optimizer removed {saved} wasted LLM call{saved === 1 ? "" : "s"} per run</div>
          <div className="text-emerald-900 mt-1">
            Dead LLM call elimination (liveness analysis) found results that nothing ever reads and left them out of the
            generated code.{" "}
            {cost.worst.llm !== null && (
              <>
                Worst case went from <b>{cost.worst.llm + saved}</b> to <b>{cost.worst.llm}</b> LLM calls per run
                (−{Math.round((saved / (cost.worst.llm + saved)) * 100)}%), saving{" "}
                <b>{money(saved * llmPrice * runs)}</b> per month at {runs.toLocaleString()} runs.
              </>
            )}
          </div>
          <ul className="mt-2 text-xs text-emerald-900 list-disc ml-5">
            {result.phases.optimize.removed_stores.map((r, i) => (
              <li key={i}>
                <code>{r.agent}</code>: <code>state.{r.field} = …</code> (line {r.line})
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <h3 className="font-semibold mb-2 text-sm">Per agent</h3>
        <table className="w-full text-sm border border-slate-200 rounded-lg overflow-hidden">
          <thead className="bg-slate-50 text-slate-600 text-xs">
            <tr>
              <th className="text-left px-3 py-2">Agent</th>
              <th className="text-right px-3 py-2">LLM calls</th>
              <th className="text-right px-3 py-2">Tool calls</th>
              <th className="text-right px-3 py-2">Max runs</th>
              <th className="text-right px-3 py-2">Worst case LLM calls</th>
            </tr>
          </thead>
          <tbody>
            {cost.agents.map((a) => (
              <tr key={a.id} className="border-t border-slate-100">
                <td className="px-3 py-1.5 font-mono">{a.id}</td>
                <td className="px-3 py-1.5 text-right">{a.llm_min === a.llm_max ? a.llm_max : `${a.llm_min}–${a.llm_max}`}</td>
                <td className="px-3 py-1.5 text-right">{a.tool_min === a.tool_max ? a.tool_max : `${a.tool_min}–${a.tool_max}`}</td>
                <td className="px-3 py-1.5 text-right">{a.max_visits === null ? "∞" : a.max_visits}</td>
                <td className="px-3 py-1.5 text-right font-semibold">
                  {a.max_visits === null ? (a.llm_max ? "∞" : 0) : a.llm_max * a.max_visits}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-xs text-slate-500 mt-2">
          "Max runs" is how many times the agent can execute in one run: 1, or 1 + the <code>max_iterations</code> of the
          loops it is in. An <code>if</code> inside an agent counts its most expensive branch for the worst case and its
          cheapest for the best case. Prices are your own estimates; the call counts are guaranteed bounds: no run can make more calls than the
          worst case, or fewer than the best case.
        </p>
      </div>
    </div>
  );
}
