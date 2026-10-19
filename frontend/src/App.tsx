import { useCallback, useEffect, useRef, useState } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";
import type * as Monaco from "monaco-editor";
import { compileSource, fetchExamples } from "./api";
import type { CompileResult, Example } from "./types";
import PhaseBar from "./components/PhaseBar";
import TokensTab from "./components/TokensTab";
import AstTab from "./components/AstTab";
import GraphTab from "./components/GraphTab";
import VerifyTab from "./components/VerifyTab";
import CodeTab from "./components/CodeTab";

const TABS = ["Tokens", "AST", "Graph", "Verification", "Generated code"] as const;
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

  const errors = result?.diagnostics.filter((d) => d.severity === "error").length ?? 0;
  const warnings = result?.diagnostics.filter((d) => d.severity === "warning").length ?? 0;

  return (
    <div className="h-full flex flex-col">
      {/* header */}
      <header className="flex items-center gap-4 px-5 py-3 bg-white border-b border-slate-200">
        <h1 className="text-xl font-bold tracking-tight">
          Agent<span className="text-violet-600">Forge</span>
        </h1>
        <span className="text-sm text-slate-500 hidden md:inline">
          a compiler that structurally verifies multi-agent workflows and generates LangGraph code
        </span>
        <div className="ml-auto flex items-center gap-3">
          <select
            className="border border-slate-300 rounded-md px-2 py-1.5 text-sm bg-white"
            value=""
            onChange={(e) => loadExample(e.target.value)}
          >
            <option value="" disabled>Load an example…</option>
            {(["valid", "invalid"] as const).map((category) => (
              <optgroup key={category} label={category === "valid" ? "Valid workflows" : "Invalid (one bug each)"}>
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
        <section className="flex flex-col min-h-0 border-r border-slate-200 bg-white">
          <div className="flex-1 min-h-[300px]">
            <Editor
              language="agentforge"
              theme="agentforge-light"
              value={source}
              onChange={(value) => setSource(value ?? "")}
              onMount={onMount}
              options={{ fontSize: 15, minimap: { enabled: false }, scrollBeyondLastLine: false, tabSize: 2 }}
            />
          </div>
          <div className="border-t border-slate-200 max-h-44 overflow-auto text-sm">
            <div className="px-3 py-1.5 bg-slate-50 font-semibold text-slate-600 flex gap-3">
              <span>Problems</span>
              <span className="text-red-600">{errors} error{errors === 1 ? "" : "s"}</span>
              <span className="text-amber-600">{warnings} warning{warnings === 1 ? "" : "s"}</span>
            </div>
            {serverError && <div className="px-3 py-1.5 text-red-700">{serverError}</div>}
            {result?.diagnostics.map((d, i) => (
              <button
                key={i}
                onClick={() => revealLine(d.line, d.col)}
                className="w-full text-left px-3 py-1 hover:bg-slate-100 flex gap-2"
              >
                <span className={d.severity === "error" ? "text-red-600" : "text-amber-600"}>
                  {d.severity === "error" ? "✗" : "⚠"}
                </span>
                <span className="font-mono text-slate-500 shrink-0">{d.code}</span>
                <span className="font-mono text-slate-400 shrink-0">{d.line}:{d.col}</span>
                <span>{d.message}</span>
              </button>
            ))}
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
              <GraphTab phase={result.phases.ir} removed={result.phases.optimize.removed_agents} onSelect={revealLine} />
            ) : tab === "Verification" ? (
              <VerifyTab result={result} />
            ) : (
              <CodeTab phase={result.phases.codegen} />
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
