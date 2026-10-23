import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";
import type * as Monaco from "monaco-editor";
import { compileReal, compileSource, fetchExamples, fetchRealProfile, type RealProfileStatus } from "./api";
import type { CompileResult, Example } from "./types";
import PhaseBar from "./components/PhaseBar";
import TokensTab from "./components/TokensTab";
import AstTab from "./components/AstTab";
import GraphTab from "./components/GraphTab";
import VerifyTab from "./components/VerifyTab";
import CodeTab from "./components/CodeTab";
import CostTab from "./components/CostTab";
import EvaluationTab from "./components/EvaluationTab";
import ProblemsPanel from "./components/ProblemsPanel";

const TABS = ["Tokens", "AST", "Graph", "Verification", "Cost", "Evaluation", "Generated code"] as const;
type Tab = (typeof TABS)[number];

const STARTER = `# Write an AgentForge workflow here, or pick an example above.
state S {
  input question: string
  output answer: string
}

agent answerer {
  state.answer = llm("Answer the question", state.question)
}

flow main {
  start -> answerer -> end
}
`;

export default function App() {
  const [source, setSource] = useState(STARTER);
  const [result, setResult] = useState<CompileResult | null>(null);
  const [examples, setExamples] = useState<Example[]>([]);
  const [tab, setTab] = useState<Tab>("Graph");
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [problemsHeight, setProblemsHeight] = useState(() => {
    try {
      return Number(localStorage.getItem("af-problems-height")) || 200;
    } catch {
      return 200;
    }
  });
  const leftRef = useRef<HTMLElement>(null);
  // agents named in a compare block (a stable array, so the graph is not rebuilt on every render)
  const candidates = useMemo(
    () => (result?.phases.semantic.symbols.compares ?? []).flatMap((c) => c.candidates),
    [result],
  );

  const editorRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<typeof Monaco | null>(null);
  const highlightRef = useRef<Monaco.editor.IEditorDecorationsCollection | null>(null);
  const sourceRef = useRef(source);
  sourceRef.current = source;

  const compile = useCallback(async () => {
    setBusy(true);
    setServerError(null);
    try {
      setResult(await compileSource(sourceRef.current));
    } catch (e) {
      setServerError(`Could not reach the compiler API (${(e as Error).message}). Is the backend running on port 8000?`);
    } finally {
      setBusy(false);
    }
  }, []);

  // measuring compare candidates with a real model: only offered when the server has a key
  const [real, setReal] = useState<RealProfileStatus>({ available: false, max_calls: 0 });
  const [measuring, setMeasuring] = useState(false);
  const [measureError, setMeasureError] = useState<string | null>(null);
  useEffect(() => {
    fetchRealProfile().then(setReal).catch(() => setReal({ available: false, max_calls: 0 }));
  }, []);
  const measure = useCallback(async () => {
    setMeasuring(true);
    setMeasureError(null);
    try {
      setResult(await compileReal(sourceRef.current));
    } catch (e) {
      setMeasureError((e as Error).message);
    } finally {
      setMeasuring(false);
    }
  }, []);

  // load the examples list once, and compile the starter program
  // (or the example named in the URL, e.g. ?example=fraud.af&tab=Graph, handy for demo bookmarks)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const wantedTab = params.get("tab");
    if (wantedTab && (TABS as readonly string[]).includes(wantedTab)) setTab(wantedTab as Tab);
    fetchExamples()
      .then((list) => {
        setExamples(list);
        const wanted = list.find((e) => e.name === params.get("example"));
        if (wanted) {
          setSource(wanted.source);
          sourceRef.current = wanted.source;
        }
      })
      .catch(() => setExamples([]))
      .finally(() => compile());
  }, [compile]);

  // show diagnostics as squiggles in the editor
  useEffect(() => {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const model = editor?.getModel();
    if (!editor || !monaco || !model) return;
    const markers = (result?.diagnostics ?? []).map((d) => {
      const lineLength = model.getLineMaxColumn(Math.min(d.line, model.getLineCount()));
      return {
        severity: d.severity === "error" ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Warning,
        message: `${d.code}: ${d.message}${d.path ? "\npath: " + d.path.join(" → ") : ""}`,
        startLineNumber: d.line,
        startColumn: d.col,
        endLineNumber: d.line,
        endColumn: Math.max(d.col + 1, lineLength),
      };
    });
    monaco.editor.setModelMarkers(model, "agentforge", markers);
  }, [result]);

  const onMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
    highlightRef.current = editor.createDecorationsCollection([]);
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => compile());
  };

  // jump to a source position and highlight that line (used by the AST tab and the problems list)
  const revealLine = useCallback((line: number, col = 1) => {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    if (!editor || !monaco) return;
    editor.revealLineInCenter(line);
    editor.setPosition({ lineNumber: line, column: col });
    highlightRef.current?.set([
      { range: new monaco.Range(line, 1, line, 1), options: { isWholeLine: true, className: "af-highlight-line" } },
    ]);
    editor.focus();
  }, []);

  const loadExample = (name: string) => {
    const example = examples.find((e) => e.name === name);
    if (!example) return;
    setSource(example.source);
    sourceRef.current = example.source;
    highlightRef.current?.clear();
    compile();
  };

  // drag the bar above the Problems panel to resize it
  const startResize = (event: React.PointerEvent) => {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = problemsHeight;
    const maxHeight = (leftRef.current?.clientHeight ?? 800) - 120;   // always leave room for the editor
    let latest = startHeight;
    const onMove = (e: PointerEvent) => {
      latest = Math.min(maxHeight, Math.max(36, startHeight + (startY - e.clientY)));
      setProblemsHeight(latest);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.cursor = "";
      try {
        localStorage.setItem("af-problems-height", String(latest));
      } catch {
        /* storage unavailable: the size just will not be remembered */
      }
    };
    document.body.style.cursor = "row-resize";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  return (
    <div className="h-full flex flex-col">
      {/* header */}
      <header className="flex items-center gap-4 px-5 py-3 bg-white border-b border-slate-200">
        <div className="min-w-0">
          <h1 className="text-base lg:text-lg font-bold tracking-tight leading-tight">
            <span className="text-violet-600">AgentForge:</span> A Multi-Phase Compiler for Verifying and Generating Code for AI Agent Workflows
          </h1>
          <p className="text-xs text-slate-500 hidden md:block">
            a compiler that structurally verifies multi-agent workflows and generates LangGraph code
          </p>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <select
            className="border border-slate-300 rounded-md px-2 py-1.5 text-sm bg-white"
            value=""
            onChange={(e) => loadExample(e.target.value)}
          >
            <option value="" disabled>Load an example…</option>
            {(["valid", "invalid"] as const).map((category) => (
              <optgroup key={category} label={category === "valid" ? "Valid workflows" : "With problems (errors, warnings, savings)"}>
                {examples.filter((e) => e.category === category).map((e) => (
                  <option key={e.name} value={e.name}>{e.name}</option>
                ))}
              </optgroup>
            ))}
          </select>
          <button
            onClick={compile}
            disabled={busy}
            className="bg-violet-600 hover:bg-violet-700 disabled:opacity-60 text-white font-semibold px-4 py-1.5 rounded-md"
            title="Ctrl+Enter"
          >
            {busy ? "Compiling…" : "Compile"}
          </button>
        </div>
      </header>

      <main className="flex-1 grid grid-cols-1 lg:grid-cols-2 min-h-0">
        {/* left: editor + problems */}
        <section ref={leftRef} className="flex flex-col min-h-0 border-r border-slate-200 bg-white">
          <div className="flex-1 min-h-[120px]">
            <Editor
              language="agentforge"
              theme="agentforge-light"
              value={source}
              onChange={(value) => setSource(value ?? "")}
              onMount={onMount}
              options={{ fontSize: 15, minimap: { enabled: false }, scrollBeyondLastLine: false, tabSize: 2, automaticLayout: true }}
            />
          </div>
          <div
            onPointerDown={startResize}
            onDoubleClick={() => setProblemsHeight(problemsHeight > 60 ? 36 : 200)}
            className="h-2 shrink-0 cursor-row-resize bg-slate-200 hover:bg-violet-400 flex items-center justify-center group"
            title="Drag to resize the Problems panel (double-click to collapse or restore)"
          >
            <div className="w-10 h-0.5 rounded bg-slate-400 group-hover:bg-white" />
          </div>
          <div style={{ height: problemsHeight }} className="shrink-0 min-h-0">
            <ProblemsPanel diagnostics={result?.diagnostics ?? []} serverError={serverError} onSelect={revealLine} />
          </div>
        </section>

        {/* right: phases + tabs */}
        <section className="flex flex-col min-h-0">
          <PhaseBar result={result} />
          <nav className="flex gap-1 px-3 pt-2 bg-slate-100 border-b border-slate-200">
            {TABS.map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-4 py-2 text-sm font-medium rounded-t-md ${
                  tab === t ? "bg-white border border-b-0 border-slate-200 text-violet-700" : "text-slate-600 hover:text-slate-900"
                }`}
              >
                {t}
              </button>
            ))}
          </nav>
          <div className="flex-1 min-h-0 overflow-auto bg-white">
            {!result ? (
              <p className="p-6 text-slate-500">Press Compile to run the compiler.</p>
            ) : tab === "Tokens" ? (
              <TokensTab phase={result.phases.lexer} onSelect={revealLine} />
            ) : tab === "AST" ? (
              <AstTab phase={result.phases.parser} onSelect={revealLine} />
            ) : tab === "Graph" ? (
              <GraphTab
                phase={result.phases.ir}
                removed={result.phases.optimize.removed_agents}
                candidates={candidates}
                diagnostics={result.diagnostics}
                onSelect={revealLine}
              />
            ) : tab === "Verification" ? (
              <VerifyTab result={result} />
            ) : tab === "Cost" ? (
              <CostTab result={result} />
            ) : tab === "Evaluation" ? (
              <EvaluationTab
                result={result}
                real={real}
                measuring={measuring}
                measureError={measureError}
                onMeasure={measure}
              />
            ) : (
              <CodeTab phase={result.phases.codegen} />
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
