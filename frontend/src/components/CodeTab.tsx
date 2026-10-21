import { useState } from "react";
import Editor from "@monaco-editor/react";
import type { CompileResult } from "../types";
import Skipped from "./Skipped";

export default function CodeTab({ phase }: { phase: CompileResult["phases"]["codegen"] }) {
  const [copied, setCopied] = useState(false);
  if (phase.status === "skipped") return <Skipped phase="code generation" />;

  const copy = async () => {
    await navigator.clipboard.writeText(phase.code);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-3 px-4 py-2 border-b border-slate-100 text-sm">
        <span className="text-slate-500">
          One runnable LangGraph file. Save it as <code>workflow.py</code> and run <code>python workflow.py</code> (mock
          mode, no API keys needed).
        </span>
        <button
          onClick={copy}
          className="ml-auto shrink-0 border border-slate-300 rounded-md px-3 py-1 hover:bg-slate-50 font-medium"
        >
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>
      <div className="flex-1 min-h-[400px]">
        <Editor
          language="python"
          value={phase.code}
          options={{ readOnly: true, fontSize: 14, minimap: { enabled: false }, scrollBeyondLastLine: false, automaticLayout: true }}
        />
      </div>
    </div>
  );
}
