import { useState } from "react";
import type { RealProfileStatus } from "../api";
import type { CompareReport, CompileResult, NecessityAgent, NecessityReport } from "../types";
import Skipped from "./Skipped";

function show(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function Scoreboard({ report }: { report: CompareReport }) {
  return (
    <ul className="space-y-2">
      {report.scores.map((s) => {
        const best = s.agent === report.best;
        const wired = report.wired.includes(s.agent);
        const share = s.total ? (s.passed / s.total) * 100 : 0;
        return (
          <li key={s.agent} className={`border rounded-lg p-3 ${best ? "border-emerald-300 bg-emerald-50" : "border-slate-200"}`}>
            <div className="flex items-center gap-2">
              <span className="font-semibold font-mono">{s.agent}</span>
              {wired && !report.applied && <span className="text-[11px] bg-slate-100 border border-slate-300 rounded px-1.5 py-0.5">in the flow</span>}
              {wired && report.applied && <span className="text-[11px] bg-slate-100 border border-slate-300 rounded px-1.5 py-0.5 line-through">was in the flow</span>}
              {s.agent === report.applied && <span className="text-[11px] bg-emerald-100 border border-emerald-300 rounded px-1.5 py-0.5">now in the generated code</span>}
              {best && <span className="text-[11px] bg-emerald-600 text-white rounded px-1.5 py-0.5">best</span>}
              <span className="ml-auto text-lg font-bold">{s.passed}/{s.total}</span>
            </div>
            <div className="mt-2 h-2 rounded bg-slate-100 overflow-hidden">
              <div className={`h-full ${best ? "bg-emerald-500" : "bg-slate-400"}`} style={{ width: `${share}%` }} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function Recommendation({ report }: { report: CompareReport }) {
  const best = report.scores.find((s) => s.agent === report.best);
  const wired = report.wired.length === 1 ? report.wired[0] : null;
  if (!best) return null;
  if (report.applied && wired) {
    const old = report.scores.find((s) => s.agent === wired);
    return (
      <div className="border border-emerald-300 bg-emerald-50 rounded-lg p-3 text-sm text-emerald-900">
        <b>Optimization applied.</b> The flow used <b className="font-mono">{wired}</b> ({old?.passed}/{old?.total}); the
        optimizer replaced it with <b className="font-mono">{report.applied}</b> ({best.passed}/{best.total}) in the
        generated code. The compiler first checked that the swap keeps every verification result.
      </div>
    );
  }
  if (report.blocked) {
    return (
      <div className="border border-amber-300 bg-amber-50 rounded-lg p-3 text-sm text-amber-900">
        <b>Not applied.</b> {report.blocked}.
      </div>
    );
  }
  if (report.wired.length === 0) {
    return (
      <div className="border border-amber-300 bg-amber-50 rounded-lg p-3 text-sm text-amber-900">
        None of the candidates is used in the flow (warning AF-W311). The best one is <b className="font-mono">{report.best}</b>.
      </div>
    );
  }
  if (wired === null) {
    return (
      <div className="border border-slate-200 bg-slate-50 rounded-lg p-3 text-sm text-slate-700">
        Several candidates are wired into the flow, so no single one is swapped. The best scorer is{" "}
        <b className="font-mono">{report.best}</b> ({best.passed}/{best.total}).
      </div>
    );
  }
  if (wired === report.best) {
    return (
      <div className="border border-emerald-300 bg-emerald-50 rounded-lg p-3 text-sm text-emerald-900">
        <b className="font-mono">{wired}</b>, the agent already in the flow, is the best candidate
        {report.tie ? " (tied with another candidate, so nothing is swapped)" : ""}. No change needed.
      </div>
    );
  }
  const current = report.scores.find((s) => s.agent === wired);
  return (
    <div className="border border-amber-300 bg-amber-50 rounded-lg p-3 text-sm text-amber-900">
      The flow uses <b className="font-mono">{wired}</b> ({current?.passed}/{current?.total}), but{" "}
      <b className="font-mono">{report.best}</b> scored higher ({best.passed}/{best.total}). Selection is switched off, so
      the generated code keeps <span className="font-mono">{wired}</span>.
    </div>
  );
}

function ReportView({ report }: { report: CompareReport }) {
  return (
    <section className="mb-8">
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <h3 className="text-lg font-bold">
          compare <span className="font-mono text-violet-700">{report.name}</span>
        </h3>
        {report.mode === "mock" ? (
          <span className="text-xs bg-slate-100 border border-slate-300 rounded px-2 py-0.5" title="The answer each candidate gives is written in the .af file (mock lines)">
            mock profile: answers scripted in the .af file
          </span>
        ) : (
          <span className="text-xs bg-emerald-100 border border-emerald-300 rounded px-2 py-0.5">measured with a real model</span>
        )}
        {report.scope === "end_to_end" && (
          <span className="text-xs bg-violet-50 border border-violet-300 text-violet-800 rounded px-2 py-0.5" title="Each case runs the whole workflow with the candidate in place; the final output is judged, so a candidate that hurts the agents after it loses">
            end to end: judged on the whole workflow's final output
          </span>
        )}
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <Scoreboard report={report} />
        <Recommendation report={report} />
      </div>

      <h4 className="font-semibold mt-5 mb-2">Test cases</h4>
      <div className="space-y-3">
        {report.cases.map((c) => (
          <div key={c.index} className="border border-slate-200 rounded-lg overflow-hidden">
            <div className="bg-slate-50 px-3 py-2 text-sm border-b border-slate-200">
              <span className="font-semibold mr-2">case {c.index}</span>
              {Object.entries(c.inputs).map(([k, v]) => (
                <span key={k} className="mr-3 text-slate-600">
                  state.{k} = <span className="font-mono">{show(v)}</span>
                </span>
              ))}
              <div className="text-slate-600">
                expect <span className="font-mono">state.{c.expect.field}</span> {c.expect.op}{" "}
                <span className="font-mono">{show(c.expect.value)}</span>
              </div>
            </div>
            <table className="w-full text-sm">
              <tbody>
                {c.results.map((r) => (
                  <tr key={r.agent} className="border-t border-slate-100 first:border-t-0">
                    <td className="px-3 py-2 w-16">
                      <span className={`text-xs font-bold rounded px-1.5 py-0.5 ${r.passed ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700"}`}>
                        {r.passed ? "PASS" : "FAIL"}
                      </span>
                    </td>
                    <td className="px-2 py-2 font-mono whitespace-nowrap">{r.agent}</td>
                    <td className="px-2 py-2 text-slate-700">{show(r.output)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </section>
  );
}

function Verdict({ row, report }: { row: NecessityAgent; report: NecessityReport }) {
  const badge = "text-xs font-bold rounded px-1.5 py-0.5 whitespace-nowrap";
  if (row.status !== "testable") {
    const needed = row.status === "structural";
    return (
      <div>
        <span className={`${badge} ${needed ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-600"}`}>
          {needed ? "needed" : "not tested"} ({row.status})
        </span>
        <div className="text-xs text-slate-600 mt-1">{row.reason}</div>
      </div>
    );
  }
  if (row.needed === null) {
    return (
      <div>
        <span className={`${badge} bg-amber-100 text-amber-800`}>can be tested</span>
        <div className="text-xs text-slate-600 mt-1">Skipping it keeps the workflow valid, so only a real model can say whether it is needed.</div>
      </div>
    );
  }
  if (row.score_without === null) {
    return (
      <div>
        <span className={`${badge} bg-slate-100 text-slate-600`}>not tested</span>
        <div className="text-xs text-slate-600 mt-1">The workflow as written fails every case, so there is no evidence either way.</div>
      </div>
    );
  }
  return (
    <div>
      <span className={`${badge} ${row.needed ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700"}`}>
        {row.needed ? "needed" : "not needed"}
      </span>
      <span className="text-xs text-slate-700 ml-2">
        {row.score_without}/{report.total} cases without it
        {row.needed ? ", so the output got worse" : ", no worse than with it"}
      </span>
      {row.removed && (
        <div className="text-xs text-emerald-800 mt-1">
          Removed from the generated code: saves {row.llm_calls} LLM call{row.llm_calls === 1 ? "" : "s"} on every run.
        </div>
      )}
    </div>
  );
}

function NecessityView({ report }: { report: NecessityReport }) {
  const measured = report.mode === "measured";
  return (
    <section className="mb-8">
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <h3 className="text-lg font-bold">
          necessity <span className="font-mono text-violet-700">{report.name}</span>
        </h3>
        {measured ? (
          <span className="text-xs bg-emerald-100 border border-emerald-300 rounded px-2 py-0.5">measured with a real model</span>
        ) : (
          <span className="text-xs bg-slate-100 border border-slate-300 rounded px-2 py-0.5">not measured yet: only the static part</span>
        )}
      </div>
      <p className="text-sm text-slate-600 mb-3">
        Which agents does the workflow need? Each case runs the whole workflow once as written and once with an agent left
        out. If the final output is no worse without it, the agent is not needed and the optimizer drops it from the generated
        code. Agents that call a tool, choose the next step, or that later agents depend on are never removed this way.
        {measured && report.baseline !== null && (
          <> The workflow as written passes <b>{report.baseline}/{report.total}</b> cases.</>
        )}
      </p>
      <div className="border border-slate-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <tbody>
            {report.agents.map((row) => (
              <tr key={row.agent} className={`border-t border-slate-100 first:border-t-0 ${row.removed ? "bg-emerald-50" : ""}`}>
                <td className="px-3 py-2 font-mono whitespace-nowrap align-top">{row.agent}</td>
                <td className="px-2 py-2 text-xs text-slate-500 whitespace-nowrap align-top">
                  {row.llm_calls} LLM call{row.llm_calls === 1 ? "" : "s"}
                </td>
                <td className="px-2 py-2">
                  <Verdict row={row} report={report} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {report.blocked.map((why) => (
        <div key={why} className="mt-2 border border-amber-300 bg-amber-50 rounded-lg p-2 text-xs text-amber-900">
          <b>Not applied.</b> {why}
        </div>
      ))}
    </section>
  );
}

interface MeasureProps {
  real: RealProfileStatus;
  measuring: boolean;
  measureError: string | null;
  onMeasure: () => void;
}

// The only place in the UI that can spend money: it asks first, says how many calls at most, and the server
// enforces the same limit. The key stays on the server.
function RealModelPanel({
  reports,
  necessity,
  real,
  measuring,
  measureError,
  onMeasure,
}: MeasureProps & { reports: CompareReport[]; necessity: NecessityReport[] }) {
  const [confirming, setConfirming] = useState(false);
  const calls =
    reports.reduce((sum, r) => sum + (r.model_calls ?? 0), 0) + necessity.reduce((sum, r) => sum + r.model_calls, 0);
  const measured = reports.some((r) => r.mode === "measured") || necessity.some((r) => r.mode === "measured");
  const tooMany = calls > real.max_calls;

  return (
    <div className="mt-2 border border-slate-200 rounded-lg p-3 text-sm text-slate-700">
      <div className="font-semibold mb-1">Measure with a real model</div>
      {measured ? (
        <p className="text-xs text-slate-600 mb-2">
          These scores came from a real model. Pressing Compile again goes back to the scripted answers.
        </p>
      ) : (
        <p className="text-xs text-slate-600 mb-2">
          The scores above use the answers scripted in the file, so they show how the mechanism works and say nothing about a
          real model. This runs the test cases against the model whose key the server has, then recompiles with the winner.
        </p>
      )}
      {measuring ? (
        <p className="text-xs text-slate-600">Asking the model... this can take up to a minute.</p>
      ) : confirming ? (
        <div className="border border-amber-300 bg-amber-50 rounded p-3 text-xs text-amber-900">
          <p className="mb-2">
            This makes <b>up to {calls} paid model call{calls === 1 ? "" : "s"}</b> using the API key on the server (limit{" "}
            {real.max_calls}). Costs are small but real.
          </p>
          {tooMany ? (
            <p className="mb-2 font-semibold">That is over the limit. Use fewer test cases or candidates, or run it from the command line.</p>
          ) : null}
          <div className="flex gap-2">
            <button
              disabled={tooMany}
              onClick={() => {
                setConfirming(false);
                onMeasure();
              }}
              className="px-3 py-1 rounded bg-violet-600 text-white font-medium disabled:opacity-40"
            >
              Run {calls} call{calls === 1 ? "" : "s"}
            </button>
            <button onClick={() => setConfirming(false)} className="px-3 py-1 rounded border border-slate-300 bg-white">
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setConfirming(true)}
          className="px-3 py-1.5 rounded bg-violet-600 text-white text-xs font-medium hover:bg-violet-700"
        >
          Measure with a real model (up to {calls} calls)
        </button>
      )}
      {measureError && <p className="mt-2 text-xs text-red-700">{measureError}</p>}
    </div>
  );
}

export default function EvaluationTab({ result, real, measuring, measureError, onMeasure }: { result: CompileResult } & MeasureProps) {
  const reports = result.evaluation;
  const necessity = result.necessity ?? [];
  if (reports === undefined) {
    return <p className="p-6 text-slate-500">The API server is running an older version without the compare feature. Restart it.</p>;
  }
  if (!result.ok && result.phases.codegen.status !== "ok") {
    return <Skipped phase="evaluation" />;
  }

  return (
    <div className="p-4">
      <p className="text-sm text-slate-500 mb-4">
        Verification proves the workflow is wired correctly; it cannot tell which agent gives the <b>better answer</b>. A{" "}
        <span className="font-mono">compare</span> block lists agents that do the same job and test cases that judge their output.
        This is <b>profile-guided optimization</b>: the compiler proves each candidate could replace the agent in the flow
        (AF-E216, AF-W310), scores them on the test cases, and the optimizer wires the winner into the generated LangGraph code.
      </p>

      {reports.length === 0 && necessity.length === 0 ? (
        <div className="border border-slate-200 rounded-lg p-4 text-sm text-slate-700">
          <div className="font-semibold mb-1">No compare or necessity block in this program.</div>
          <p className="mb-2">
            A <span className="font-mono">necessity</span> block finds the agents the workflow does not need (load{" "}
            <span className="font-mono">necessity_demo.af</span>). A compare block picks the better of several agents. For example, load{" "}
            <span className="font-mono">compare_agents.af</span>:
          </p>
          <pre className="bg-slate-50 border border-slate-200 rounded p-3 font-mono text-xs overflow-auto">{`compare best_responder {
  candidates responder_a, responder_b
  case {
    state.query = "What is the capital of France?"
    expect state.response contains "Paris"
    mock responder_a "The capital is Lyon."
    mock responder_b "Paris is the capital of France."
  }
}`}</pre>
        </div>
      ) : (
        <>
          {reports.map((r) => <ReportView key={r.name} report={r} />)}
          {necessity.map((r) => <NecessityView key={r.name} report={r} />)}
        </>
      )}

      {(reports.length > 0 || necessity.length > 0) && real.available && (
        <RealModelPanel
          reports={reports}
          necessity={necessity}
          real={real}
          measuring={measuring}
          measureError={measureError}
          onMeasure={onMeasure}
        />
      )}

      <div className="mt-2 border border-slate-200 rounded-lg p-3 text-xs text-slate-600">
        <div className="font-semibold text-slate-700 mb-1">Using a real model</div>
        By default the answers are the scripted ones (<span className="font-mono">mock</span> lines), so the selection shows how
        the mechanism works and says nothing about a real model. The compiler only calls an API when you ask for it:{" "}
        {real.available
          ? "use the button above, or"
          : "the button above appears when the server has a key (put OPENAI_API_KEY or ANTHROPIC_API_KEY in backend/.env and restart it), or"}{" "}
        compile from the command line with{" "}
        <span className="font-mono">python -m compiler file.af -o workflow.py --profile real</span>. Either way the test cases
        run against the model, and the compiler recompiles with the winner. Normal compiles never call an API.
      </div>
    </div>
  );
}
