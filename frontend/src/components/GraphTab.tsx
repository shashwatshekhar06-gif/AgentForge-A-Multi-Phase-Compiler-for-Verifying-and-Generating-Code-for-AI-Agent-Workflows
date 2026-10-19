import { useMemo } from "react";
import {
  Background, BaseEdge, Controls, EdgeLabelRenderer, Handle, MarkerType, Position, ReactFlow,
  type Edge, type EdgeProps, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { CompileResult, IrEdge, IrNode } from "../types";
import Skipped from "./Skipped";

type FlowNodeData = { ir: IrNode; removed: boolean };

const STATUS_STYLE = {
  ok: "bg-emerald-50 border-emerald-500",
  warning: "bg-amber-50 border-amber-500",
  error: "bg-red-50 border-red-500",
};

// ---------- custom node: shows the agent, colored by status; hover shows reads / writes ----------

function AgentNode({ data }: NodeProps<Node<FlowNodeData>>) {
  const { ir, removed } = data;
  const special = ir.kind !== "agent";
  return (
    <div className="group relative">
      <Handle type="target" position={Position.Left} className="!bg-slate-400" />
      <div
        className={`border-2 px-4 py-2 text-center shadow-sm ${STATUS_STYLE[ir.status]} ${
          special ? "rounded-full font-bold uppercase text-xs tracking-wider" : "rounded-lg min-w-[130px]"
        } ${removed ? "opacity-50 border-dashed" : ""}`}
      >
        <div className="font-semibold">{ir.id}</div>
        {removed && <div className="text-[10px] text-slate-500">removed (dead)</div>}
      </div>
      <Handle type="source" position={Position.Right} className="!bg-slate-400" />
      {!special && (
        <div className="hidden group-hover:block absolute z-50 left-1/2 -translate-x-1/2 top-full mt-2 w-56 bg-slate-800 text-white text-xs rounded-md p-2 shadow-lg text-left">
          <div><span className="text-sky-300">reads:</span> {ir.reads.join(", ") || "nothing"}</div>
          <div><span className="text-pink-300">writes:</span> {ir.writes.join(", ") || "nothing"}</div>
          <div><span className="text-emerald-300">always writes:</span> {ir.must_writes.join(", ") || "nothing"}</div>
          <div className="text-slate-400 mt-1">line {ir.line}</div>
        </div>
      )}
    </div>
  );
}

// ---------- custom edge for loops back to the same node ----------

function SelfLoopEdge({ sourceX, sourceY, targetX, targetY, label, markerEnd, style }: EdgeProps) {
  const path = `M ${sourceX} ${sourceY} C ${sourceX + 60} ${sourceY - 100}, ${targetX - 60} ${targetY - 100}, ${targetX} ${targetY}`;
  return (
    <>
      <BaseEdge path={path} markerEnd={markerEnd} style={style} />
      <EdgeLabelRenderer>
        <div
          className="absolute bg-white border border-violet-300 text-violet-800 text-xs px-1.5 py-0.5 rounded font-mono"
          style={{ transform: `translate(-50%, -50%) translate(${(sourceX + targetX) / 2}px, ${sourceY - 80}px)` }}
        >
          {label}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

const nodeTypes = { agent: AgentNode };
const edgeTypes = { selfloop: SelfLoopEdge };

// ---------- layout: columns by distance from start (breadth first search) ----------

function layout(nodes: IrNode[], edges: IrEdge[]): Map<string, { x: number; y: number }> {
  const level = new Map<string, number>([["start", 0]]);
  const queue = ["start"];
  while (queue.length) {
    const id = queue.shift()!;
    for (const e of edges) {
      if (e.from === id && !level.has(e.to) && e.to !== "end") {
        level.set(e.to, level.get(id)! + 1);
        queue.push(e.to);
      }
    }
  }
  const maxLevel = Math.max(0, ...level.values());
  level.set("end", maxLevel + 1);

  // unreachable agents go in their own row underneath
  const columns = new Map<number, string[]>();
  const unreachable: string[] = [];
  for (const n of nodes) {
    if (level.has(n.id)) {
      const l = level.get(n.id)!;
      columns.set(l, [...(columns.get(l) ?? []), n.id]);
    } else {
      unreachable.push(n.id);
    }
  }

  const positions = new Map<string, { x: number; y: number }>();
  let tallest = 0;
  for (const [l, ids] of columns) {
    tallest = Math.max(tallest, ids.length);
    ids.forEach((id, i) => positions.set(id, { x: l * 240, y: (i - (ids.length - 1) / 2) * 130 }));
  }
  unreachable.forEach((id, i) => positions.set(id, { x: 240 + i * 240, y: (tallest / 2) * 130 + 140 }));
  return positions;
}

function edgeLabel(e: IrEdge): string | undefined {
  const base = e.is_default ? "else" : e.condition ?? undefined;
  if (e.max_iterations != null) return `${base ?? ""}  · max ${e.max_iterations}`;
  return base;
}

export default function GraphTab({
  phase, removed, onSelect,
}: {
  phase: CompileResult["phases"]["ir"];
  removed: string[];
  onSelect: (line: number, col: number) => void;
}) {
  const { flowNodes, flowEdges } = useMemo(() => {
    const positions = layout(phase.nodes, phase.edges);
    const flowNodes: Node<FlowNodeData>[] = phase.nodes.map((n) => ({
      id: n.id,
      type: "agent",
      position: positions.get(n.id)!,
      data: { ir: n, removed: removed.includes(n.id) },
    }));
    const flowEdges: Edge[] = phase.edges.map((e, i) => {
      const loop = e.from === e.to;
      const conditional = e.condition !== null || e.is_default;
      return {
        id: `e${i}`,
        source: e.from,
        target: e.to,
        type: loop ? "selfloop" : "default",
        label: edgeLabel(e),
        labelStyle: { fontFamily: "monospace", fontSize: 12 },
        labelBgPadding: [4, 2] as [number, number],
        labelBgStyle: { fill: e.is_default ? "#f1f5f9" : "#ede9fe" },
        markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18 },
        style: { strokeWidth: 2, stroke: conditional ? "#7c3aed" : "#64748b", strokeDasharray: e.is_default ? "6 4" : undefined },
      };
    });
    return { flowNodes, flowEdges };
  }, [phase, removed]);

  if (phase.status === "skipped") return <Skipped phase="IR" />;

  return (
    <div className="h-full flex flex-col">
      <div className="px-4 py-2 text-sm text-slate-500 flex flex-wrap gap-4 border-b border-slate-100">
        <span>The workflow as a graph IR: agents are nodes, transitions are edges. Hover a node for its reads and writes.</span>
        <span className="flex gap-3">
          <span className="text-emerald-700">● ok</span>
          <span className="text-amber-600">● warning</span>
          <span className="text-red-600">● error</span>
        </span>
      </div>
      <div className="flex-1 min-h-[400px]">
        <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          fitViewOptions={{ padding: 0.25 }}
          nodesConnectable={false}
          onNodeClick={(_, node) => {
            const line = (node.data as FlowNodeData).ir.line;
            if (line) onSelect(line, 1);
          }}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={20} color="#e2e8f0" />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  );
}
