import { useEffect, useState } from "react";
import type { Diagnostic } from "../types";
import { EXPLANATIONS } from "../explanations";

interface Props {
  diagnostics: Diagnostic[];
  serverError: string | null;
  onSelect: (line: number, col: number) => void;
}

// The list of errors and warnings under the editor. Click a problem to jump to its line;
// click "Why?" to see how this kind of bug comes about, how to fix it and which compiler phase caught it.
export default function ProblemsPanel({ diagnostics, serverError, onSelect }: Props) {
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => setOpen(null), [diagnostics]);   // a new compile means a new list: close any open explanation
  const errors = diagnostics.filter((d) => d.severity === "error").length;
  const warnings = diagnostics.length - errors;

  return (
    <div className="h-full flex flex-col text-sm">
      <div className="px-3 py-1.5 bg-slate-50 border-b border-slate-200 font-semibold text-slate-600 flex gap-3 shrink-0">
        <span>Problems</span>
        <span className="text-red-600">{errors} error{errors === 1 ? "" : "s"}</span>
        <span className="text-amber-600">{warnings} warning{warnings === 1 ? "" : "s"}</span>
        {diagnostics.length > 0 && <span className="ml-auto font-normal text-slate-400">click a problem for the line · Why? for the explanation</span>}
      </div>
      <div className="flex-1 overflow-auto">
        {serverError && <div className="px-3 py-1.5 text-red-700">{serverError}</div>}
        {diagnostics.length === 0 && !serverError && (
          <div className="px-3 py-2 text-slate-400">No problems. Every check passed.</div>
        )}
        {diagnostics.map((d, i) => {
          const info = EXPLANATIONS[d.code];
          const expanded = open === i;
          return (
            <div key={i} className={`border-b border-slate-100 ${expanded ? "bg-slate-50" : ""}`}>
              <div className="flex items-start gap-2 px-3 py-1.5 hover:bg-slate-100">
                <button onClick={() => onSelect(d.line, d.col)} className="flex-1 text-left flex gap-2 min-w-0">
                  <span className={d.severity === "error" ? "text-red-600" : "text-amber-600"}>
                    {d.severity === "error" ? "✗" : "⚠"}
                  </span>
                  <span className="font-mono text-slate-500 shrink-0">{d.code}</span>
                  <span className="font-mono text-slate-400 shrink-0">{d.line}:{d.col}</span>
                  <span className="min-w-0">{d.message}</span>
                </button>
                {info && (
                  <button
                    onClick={() => setOpen(expanded ? null : i)}
                    className={`shrink-0 text-xs border rounded px-1.5 py-0.5 ${
                      expanded ? "bg-violet-600 border-violet-600 text-white" : "border-violet-300 text-violet-700 hover:bg-violet-50"}`}
                  >
                    {expanded ? "Hide" : "Why?"}
                  </button>
                )}
              </div>
              {expanded && info && (
                <div className="mx-3 mb-2 ml-9 border-l-4 border-violet-400 bg-white rounded-r-md px-3 py-2 text-[13px] space-y-1.5">
                  <div className="font-semibold text-slate-800">{info.title}</div>
                  <div><span className="font-semibold text-slate-600">How this bug happens: </span>{info.cause}</div>
                  <div><span className="font-semibold text-slate-600">How to fix it: </span>{info.fix}</div>
                  {d.path && (
                    <div><span className="font-semibold text-slate-600">Witness path: </span>
                      <span className="font-mono">{d.path.join(" → ")}</span>
                    </div>
                  )}
                  <div className="text-slate-500"><span className="font-semibold">Caught by: </span>{info.concept}</div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
