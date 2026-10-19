import type { CompileResult } from "../types";
import Skipped from "./Skipped";

const SYMBOLS = new Set([
  "LBRACE", "RBRACE", "LPAREN", "RPAREN", "LBRACKET", "RBRACKET", "COMMA", "COLON", "DOT",
  "ASSIGN", "EQ", "NEQ", "LT", "GT", "LTE", "GTE", "ARROW", "EDGE_OPEN",
]);

// color a token chip by its category
function category(type: string): { label: string; style: string } {
  if (type === "IDENT") return { label: "name", style: "bg-sky-50 border-sky-300 text-sky-900" };
  if (type === "STRING" || type === "INT" || type === "TRUE" || type === "FALSE")
    return { label: "literal", style: "bg-emerald-50 border-emerald-300 text-emerald-900" };
  if (type === "EOF") return { label: "end", style: "bg-slate-100 border-slate-300 text-slate-500" };
  if (SYMBOLS.has(type)) return { label: "symbol", style: "bg-pink-50 border-pink-300 text-pink-900" };
  return { label: "keyword", style: "bg-violet-50 border-violet-300 text-violet-900" };
}

export default function TokensTab({
  phase,
  onSelect,
}: {
  phase: CompileResult["phases"]["lexer"];
  onSelect: (line: number, col: number) => void;
}) {
  if (phase.status === "skipped") return <Skipped phase="lexer" />;
  return (
    <div className="p-4">
      <p className="text-sm text-slate-500 mb-3">
        {phase.tokens.length} tokens. The lexer groups characters into words and symbols and records the line and column
        of each. Click a token to find it in the source.
      </p>
      <div className="flex gap-3 mb-4 text-xs">
        {["keyword", "name", "literal", "symbol"].map((label) => {
          const sample = { keyword: "STATE", name: "IDENT", literal: "STRING", symbol: "ARROW" }[label]!;
          return (
            <span key={label} className={`border rounded px-2 py-0.5 ${category(sample).style}`}>{label}</span>
          );
        })}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {phase.tokens.map((t, i) => {
          const { style } = category(t.type);
          return (
            <button
              key={i}
              onClick={() => onSelect(t.line, t.col)}
              title={`${t.type} at ${t.line}:${t.col}`}
              className={`border rounded-md px-2 py-1 text-left leading-tight hover:shadow ${style}`}
            >
              <div className="font-mono text-sm">{t.type === "STRING" ? `"${t.lexeme}"` : t.lexeme || "EOF"}</div>
              <div className="text-[10px] opacity-70">{t.type}</div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
