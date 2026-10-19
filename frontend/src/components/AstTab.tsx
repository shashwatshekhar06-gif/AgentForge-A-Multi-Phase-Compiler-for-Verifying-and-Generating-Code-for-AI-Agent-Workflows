import { useState } from "react";
import type { AstNode, CompileResult } from "../types";
import Skipped from "./Skipped";

function isNode(value: unknown): value is AstNode {
  return typeof value === "object" && value !== null && "type" in value && "line" in value;
}

// one line summary of a node's simple fields, e.g. `name: classifier` or `op: ==`
function summary(node: AstNode): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(node)) {
    if (["type", "line", "col"].includes(key)) continue;
    if (value === null || value === false || isNode(value)) continue;
    if (Array.isArray(value) && value.some(isNode)) continue;
    if (Array.isArray(value) && value.length === 0) continue;
    parts.push(`${key}: ${JSON.stringify(value)}`);
  }
  return parts.join("  ");
}

// child nodes, labelled with the field they belong to
function children(node: AstNode): { label: string; node: AstNode }[] {
  const result: { label: string; node: AstNode }[] = [];
  for (const [key, value] of Object.entries(node)) {
    if (isNode(value)) result.push({ label: key, node: value });
    if (Array.isArray(value)) value.forEach((v, i) => isNode(v) && result.push({ label: `${key}[${i}]`, node: v }));
  }
  return result;
}

const TYPE_COLORS: Record<string, string> = {
  Program: "text-slate-800",
  StateDecl: "text-cyan-700", FieldDecl: "text-cyan-700",
  ToolDecl: "text-amber-700", Param: "text-amber-700",
  AgentDecl: "text-violet-700", Assign: "text-violet-700", If: "text-violet-700",
  FlowDecl: "text-pink-700", Edge: "text-pink-700",
};

function TreeNode({
  node, label, depth, onSelect,
}: {
  node: AstNode; label?: string; depth: number; onSelect: (line: number, col: number) => void;
}) {
  const kids = children(node);
  const [open, setOpen] = useState(depth < 3);
  return (
    <div className="ml-4">
      <div className="flex items-center gap-1.5 py-0.5 group">
        <button
          className="w-4 text-slate-400 hover:text-slate-700"
          onClick={() => setOpen(!open)}
          aria-label={open ? "collapse" : "expand"}
        >
          {kids.length ? (open ? "▾" : "▸") : "·"}
        </button>
        <button onClick={() => onSelect(node.line, node.col)} className="text-left hover:bg-yellow-100 rounded px-1">
          {label && <span className="text-slate-400 text-xs mr-1.5">{label}</span>}
          <span className={`font-semibold ${TYPE_COLORS[node.type] ?? "text-emerald-700"}`}>{node.type}</span>
          <span className="font-mono text-xs text-slate-600 ml-2">{summary(node)}</span>
          <span className="text-[10px] text-slate-400 ml-2">{node.line}:{node.col}</span>
        </button>
      </div>
      {open && kids.map((k, i) => <TreeNode key={i} node={k.node} label={k.label} depth={depth + 1} onSelect={onSelect} />)}
    </div>
  );
}

export default function AstTab({
  phase, onSelect,
}: {
  phase: CompileResult["phases"]["parser"];
  onSelect: (line: number, col: number) => void;
}) {
  if (phase.status === "skipped" || !phase.ast) return <Skipped phase="parser" />;
  return (
    <div className="p-4 text-sm">
      <p className="text-slate-500 mb-3">
        The abstract syntax tree built by the recursive descent parser. Click a node to highlight its source line;
        click the arrow to fold it.
      </p>
      <div className="-ml-4">
        <TreeNode node={phase.ast} depth={0} onSelect={onSelect} />
      </div>
    </div>
  );
}
