import { useEffect, useMemo, useRef, useState } from "react";
import {
  Background, Controls, EdgeLabelRenderer, Handle, MarkerType, MiniMap, Position, ReactFlow,
  ReactFlowProvider, useNodesInitialized, useNodesState, useReactFlow,
  type Edge, type EdgeProps, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { CompileResult, Diagnostic, IrEdge, IrNode } from "../types";
import { computeLayout, NODE_WIDTH, type Direction, type EdgeRoute } from "./graphLayout";
import Skipped from "./Skipped";

// ---------- data attached to nodes and edges ----------

type NodeData = { ir: IrNode; removed: boolean; dimmed: boolean; onPath: boolean; direction: Direction };
type EdgeKind = "plain" | "conditional" | "else" | "loop";
type EdgeData = {
  route: EdgeRoute; direction: Direction; span: number; offset: number; kind: EdgeKind; text?: string; dimmed: boolean; highlighted: boolean;
};

const STATUS = {
  ok: { box: "bg-white border-emerald-500", dot: "bg-emerald-500", mini: "#10b981" },
  warning: { box: "bg-amber-50 border-amber-500", dot: "bg-amber-500", mini: "#f59e0b" },
  error: { box: "bg-red-50 border-red-500 af-pulse", dot: "bg-red-500", mini: "#ef4444" },
};

const EDGE_COLORS: Record<EdgeKind, string> = {
  plain: "#64748b",        // slate
  conditional: "#7c3aed",  // violet
  else: "#94a3b8",         // light slate, dashed
  loop: "#d97706",         // amber
};

// ---------- node ----------

function AgentNode({ data }: NodeProps<Node<NodeData>>) {
  const { ir, removed, dimmed, onPath, direction } = data;
  // where edges attach: forward edges in/out, loop-back arcs on one side, self loops on another
  const lr = direction === "LR";
  const handles = (
    <>
      <Handle id="in" type="target" position={lr ? Position.Left : Position.Top} className="af-handle" />
      <Handle id="out" type="source" position={lr ? Position.Right : Position.Bottom} className="af-handle" />
      <Handle id="back-in" type="target" position={lr ? Position.Bottom : Position.Right}
              style={lr ? { left: "35%" } : { top: "35%" }} className="af-handle" />
      <Handle id="back-out" type="source" position={lr ? Position.Bottom : Position.Right}
              style={lr ? { left: "65%" } : { top: "65%" }} className="af-handle" />
      <Handle id="self-in" type="target" position={lr ? Position.Top : Position.Left}
              style={lr ? { left: "30%" } : { top: "30%" }} className="af-handle" />
      <Handle id="self-out" type="source" position={lr ? Position.Top : Position.Left}
              style={lr ? { left: "70%" } : { top: "70%" }} className="af-handle" />
    </>
  );
  const fade = dimmed ? "opacity-25" : "opacity-100";
  const ring = onPath ? "ring-4 ring-red-400 ring-offset-2" : "";

  if (ir.kind !== "agent") {
    return (
      <div className={`transition-opacity duration-200 ${fade}`}>
        {handles}
        <div className={`w-[72px] h-[72px] rounded-full flex items-center justify-center font-bold text-xs tracking-widest text-white shadow-md ${
          ir.kind === "start" ? "bg-slate-800" : "bg-slate-500"} ${ring}`}>
          {ir.id.toUpperCase()}
        </div>
      </div>
    );
  }

  const style = STATUS[ir.status];
  return (
    <div className={`group relative transition-opacity duration-200 ${fade}`}>
      {handles}
      <div style={{ width: NODE_WIDTH }} className={`border-2 rounded-xl shadow-sm px-3.5 py-2.5 ${style.box} ${ring} ${
        removed ? "opacity-50 border-dashed" : ""}`}>
        <div className="flex items-center gap-2">
          <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${style.dot}`} />
          <span className="font-semibold text-slate-800 truncate">{ir.id}</span>
        </div>
        <div className="mt-1 text-[11px] text-slate-500 flex gap-3">
          <span title={ir.reads.join(", ")}>reads {ir.reads.length}</span>
          <span title={ir.writes.join(", ")}>writes {ir.writes.length}</span>
          {ir.llm_calls > 0 ? (
            <span className="ml-auto font-semibold text-violet-700" title="most LLM calls one run of this agent makes (after optimization)">
              LLM ×{ir.llm_calls - (ir.removed_llm_calls ?? 0)}
              {ir.removed_llm_calls > 0 && <span className="ml-1 text-emerald-600 line-through">{ir.removed_llm_calls}</span>}
            </span>
          ) : (
            <span className="ml-auto">L{ir.line}</span>
          )}
        </div>
        {removed && <div className="text-[10px] font-medium text-slate-500 mt-0.5">dead: removed by optimizer</div>}
      </div>
      <div className="hidden group-hover:block absolute z-50 left-1/2 -translate-x-1/2 top-full mt-2 w-64 bg-slate-800 text-white text-xs rounded-lg p-2.5 shadow-xl">
        <div><span className="text-sky-300 font-semibold">reads</span> {ir.reads.join(", ") || "nothing"}</div>
        <div><span className="text-pink-300 font-semibold">may write</span> {ir.writes.join(", ") || "nothing"}</div>
        <div><span className="text-emerald-300 font-semibold">always writes</span> {ir.must_writes.join(", ") || "nothing"}</div>
        <div className="text-slate-400 mt-1">click to jump to line {ir.line}</div>
      </div>
    </div>
  );
}

// ---------- edge: one component draws forward edges, loop-back arcs and self loops ----------

function FlowEdge({ sourceX: sx, sourceY: sy, targetX: tx, targetY: ty, data, markerEnd }: EdgeProps<Edge<EdgeData>>) {
  const d = data!;
  // control points of a cubic bezier curve, depending on the kind of route
  let c1: [number, number], c2: [number, number];
  const lr = d.direction === "LR";
  if (d.route === "forward") {
    // an edge that skips layers would run straight behind the nodes in between, so bend it out sideways
    const inLine = lr ? Math.abs(sy - ty) < 60 : Math.abs(sx - tx) < NODE_WIDTH;
    const bend = d.span > 1 && inLine ? (lr ? 110 : NODE_WIDTH) + Math.abs(d.offset) : 0;
    if (lr) {
      const dx = Math.max(60, (tx - sx) * (bend ? 0.25 : 0.45));
      c1 = [sx + dx, sy + d.offset - bend];
      c2 = [tx - dx, ty + d.offset - bend];
    } else {
      const dy = Math.max(45, (ty - sy) * (bend ? 0.25 : 0.45));
      c1 = [sx + d.offset - bend, sy + dy];
      c2 = [tx + d.offset - bend, ty - dy];
    }
  } else if (d.route === "back") {
    // loop-back edges arc around the side of the graph instead of cutting through it
    if (lr) {
      const depth = 80 + Math.abs(sx - tx) * 0.12 + d.offset;
      c1 = [sx, Math.max(sy, ty) + depth];
      c2 = [tx, Math.max(sy, ty) + depth];
    } else {
      const depth = 90 + Math.abs(sy - ty) * 0.12 + d.offset;
      c1 = [Math.max(sx, tx) + depth, sy];
      c2 = [Math.max(sx, tx) + depth, ty];
    }
  } else {
    const h = 80 + d.offset;   // self loop: a small arc out of one side and back in
    c1 = lr ? [sx + 45, sy - h] : [sx - h, sy + 20];
    c2 = lr ? [tx - 45, ty - h] : [tx - h, ty - 20];
  }
  const path = `M ${sx} ${sy} C ${c1[0]} ${c1[1]}, ${c2[0]} ${c2[1]}, ${tx} ${ty}`;
  // midpoint of the bezier (t = 0.5) for the label
  const mx = (sx + 3 * c1[0] + 3 * c2[0] + tx) / 8;
  const my = (sy + 3 * c1[1] + 3 * c2[1] + ty) / 8;

  const color = d.highlighted ? "#dc2626" : EDGE_COLORS[d.kind];
  const width = d.highlighted ? 3.5 : 2;
  const dash = d.highlighted ? undefined : d.kind === "else" ? "7 5" : undefined;
  const label = d.text && d.text.length > 34 ? d.text.slice(0, 32) + "…" : d.text;

  return (
    <>
      <path d={path} fill="none" stroke="transparent" strokeWidth={14} />
      <path
        d={path}
        fill="none"
        stroke={color}
        strokeWidth={width}
        strokeDasharray={dash}
        markerEnd={markerEnd}
        className={`transition-opacity duration-200 ${d.highlighted ? "af-flow" : ""}`}
        style={{ opacity: d.dimmed ? 0.12 : 1 }}
      />
      {label && (
        <EdgeLabelRenderer>
          <div
            title={d.text}
            className={`nodrag nopan absolute px-2 py-0.5 rounded-full border text-[11px] font-mono shadow-sm whitespace-nowrap transition-opacity duration-200 ${
              d.highlighted ? "bg-red-50 border-red-300 text-red-800"
                : d.kind === "loop" ? "bg-amber-50 border-amber-300 text-amber-800"
                : d.kind === "else" ? "bg-slate-50 border-slate-300 text-slate-500"
                : "bg-violet-50 border-violet-200 text-violet-800"}`}
            style={{ transform: `translate(-50%, -50%) translate(${mx}px, ${my}px)`, opacity: d.dimmed ? 0.15 : 1, pointerEvents: "all" }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const nodeTypes = { agent: AgentNode };
const edgeTypes = { flow: FlowEdge };

// ---------- helpers ----------

function edgeKind(e: IrEdge): EdgeKind {
  if (e.max_iterations != null) return "loop";
  if (e.is_default) return "else";
  if (e.condition !== null) return "conditional";
  return "plain";
}

function edgeText(e: IrEdge): string | undefined {
  if (e.max_iterations != null) return `↻ ${e.condition} · max ${e.max_iterations}`;
  if (e.is_default) return "else";
  return e.condition ?? undefined;
}

/** Edges between the same two nodes get different offsets so they do not overlap. */
function offsets(edges: IrEdge[], routes: EdgeRoute[]): number[] {
  const groups = new Map<string, number[]>();
  edges.forEach((e, i) => {
    const key = `${e.from}->${e.to}`;
    groups.set(key, [...(groups.get(key) ?? []), i]);
  });
  const result = new Array(edges.length).fill(0);
  groups.forEach((indices) =>
    indices.forEach((edgeIndex, k) => {
      const route = routes[edgeIndex];
      result[edgeIndex] = route === "forward" ? (k - (indices.length - 1) / 2) * 34 : k * 34;
    }),
  );
  return result;
}

// ---------- the canvas ----------

interface Props {
  phase: CompileResult["phases"]["ir"];
  removed: string[];
  diagnostics: Diagnostic[];
  onSelect: (line: number, col: number) => void;
}

interface CanvasProps extends Props {
  fullscreen: boolean;
  onToggleFullscreen: () => void;
}

function GraphCanvas({ phase, removed, diagnostics, onSelect, fullscreen, onToggleFullscreen }: CanvasProps) {
  const { fitView } = useReactFlow();
  const nodesInitialized = useNodesInitialized();
  const wrapper = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [pathIndex, setPathIndex] = useState<number>(-1);
  const [direction, setDirection] = useState<Direction | "auto">("auto");
  const [panel, setPanel] = useState({ width: 800, height: 600 });

  const witnessPaths = useMemo(() => diagnostics.filter((d) => d.path && d.path.length > 1), [diagnostics]);
  const activePath = pathIndex >= 0 ? witnessPaths[pathIndex]?.path ?? null : null;

  // "auto" picks the direction that fits the panel best; only re-decide when the panel shape changes a lot
  const panelShape = Math.round((panel.width / Math.max(panel.height, 1)) * 4) / 4;
  const layout = useMemo(
    () => computeLayout(phase.nodes, phase.edges, direction, { width: panelShape * 1000, height: 1000 }),
    [phase, direction, panelShape],
  );
  const layoutKey = useMemo(
    () => layout.direction + "|" + phase.nodes.map((n) => n.id).join(",") + "|" +
      phase.edges.map((e) => e.from + ">" + e.to).join(","),
    [phase, layout.direction],
  );

  // nodes live in state so they can be dragged; they are reset when the program changes
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<NodeData>>([]);
  useEffect(() => {
    setNodes(phase.nodes.map((n) => ({
      id: n.id,
      type: "agent",
      position: layout.positions.get(n.id)!,
      data: { ir: n, removed: removed.includes(n.id), dimmed: false, onPath: false, direction: layout.direction },
    })));
  }, [phase, layout, removed, setNodes]);
  useEffect(() => setPathIndex(-1), [phase]);

  // auto zoom: fit the whole graph whenever the program changes or the panel is resized
  useEffect(() => {
    if (nodesInitialized) fitView({ padding: 0.12, duration: 450 });
  }, [nodesInitialized, layoutKey, fitView]);
  useEffect(() => {
    if (!wrapper.current) return;
    let timer: number | undefined;
    const observer = new ResizeObserver(([entry]) => {
      setPanel({ width: entry.contentRect.width, height: entry.contentRect.height });
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fitView({ padding: 0.12, duration: 200 }), 120);
    });
    observer.observe(wrapper.current);
    return () => observer.disconnect();
  }, [fitView]);

  // which nodes and edges to emphasise: a selected witness path wins over hovering
  const pathPairs = useMemo(() => {
    const pairs = new Set<string>();
    activePath?.forEach((id, i) => i > 0 && pairs.add(`${activePath[i - 1]}->${id}`));
    return pairs;
  }, [activePath]);

  const shownNodes = useMemo(() => nodes.map((n) => {
    let dimmed = false;
    if (activePath) dimmed = !activePath.includes(n.id);
    else if (hovered) {
      const linked = phase.edges.some((e) => (e.from === hovered && e.to === n.id) || (e.to === hovered && e.from === n.id));
      dimmed = n.id !== hovered && !linked;
    }
    return { ...n, data: { ...n.data, dimmed, onPath: !!activePath?.includes(n.id) } };
  }), [nodes, activePath, hovered, phase.edges]);

  const offs = useMemo(() => offsets(phase.edges, layout.routes), [phase.edges, layout.routes]);
  const shownEdges: Edge<EdgeData>[] = useMemo(() => phase.edges.map((e, i) => {
    const route = layout.routes[i];
    const kind = edgeKind(e);
    const highlighted = pathPairs.has(`${e.from}->${e.to}`);
    const dimmed = activePath ? !highlighted : hovered ? e.from !== hovered && e.to !== hovered : false;
    const color = highlighted ? "#dc2626" : EDGE_COLORS[kind];
    return {
      id: `e${i}`,
      source: e.from,
      target: e.to,
      type: "flow",
      sourceHandle: route === "forward" ? "out" : route === "back" ? "back-out" : "self-out",
      targetHandle: route === "forward" ? "in" : route === "back" ? "back-in" : "self-in",
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color },
      zIndex: highlighted ? 10 : 0,
      data: { route, direction: layout.direction, span: layout.spans[i], offset: offs[i], kind, text: edgeText(e), dimmed, highlighted },
    };
  }), [phase.edges, layout.routes, layout.spans, layout.direction, offs, pathPairs, activePath, hovered]);

  return (
    <div className="h-full w-full flex flex-col">
      {/* toolbar sits above the canvas, so it never covers part of the graph */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-slate-200 bg-slate-50 text-xs">
        <button
          onClick={() => fitView({ padding: 0.12, duration: 450 })}
          className="border border-slate-300 bg-white rounded-md px-2 py-1 font-medium hover:bg-slate-100"
          title="Zoom to fit the whole graph"
        >
          ⤢ Fit
        </button>
        <div className="flex border border-slate-300 bg-white rounded-md overflow-hidden" title="Layout direction">
          {(["auto", "LR", "TB"] as const).map((d) => (
            <button
              key={d}
              onClick={() => setDirection(d)}
              className={`px-2 py-1 ${direction === d ? "bg-violet-600 text-white" : "hover:bg-slate-50"}`}
            >
              {d === "auto" ? `Auto ${layout.direction === "LR" ? "↔" : "↕"}` : d === "LR" ? "↔" : "↕"}
            </button>
          ))}
        </div>
        <button
          onClick={onToggleFullscreen}
          className="border border-slate-300 bg-white rounded-md px-2 py-1 font-medium hover:bg-slate-100"
          title={fullscreen ? "Back to the normal view (Esc)" : "Show the graph full screen"}
        >
          {fullscreen ? "✕ Exit full screen" : "⛶ Full screen"}
        </button>
        {witnessPaths.length > 0 && (
          <select
            value={pathIndex}
            onChange={(e) => setPathIndex(Number(e.target.value))}
            className="border border-red-300 text-red-800 bg-red-50 rounded-md px-2 py-1 max-w-[320px]"
            title="Highlight the path that proves a bug"
          >
            <option value={-1}>Highlight a witness path…</option>
            {witnessPaths.map((d, i) => (
              <option key={i} value={i}>{d.code}: {d.path!.join(" → ")}</option>
            ))}
          </select>
        )}
        <span className="flex items-center gap-3 text-slate-600 ml-1">
          <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-emerald-500" />ok</span>
          <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-amber-500" />warning</span>
          <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-red-500" />error</span>
          <span className="text-slate-300">|</span>
          <span style={{ color: EDGE_COLORS.conditional }}>━ condition</span>
          <span style={{ color: EDGE_COLORS.else }}>┅ else</span>
          <span style={{ color: EDGE_COLORS.loop }}>↻ bounded loop</span>
        </span>
      </div>
      <div ref={wrapper} className="flex-1 min-h-0">
        <ReactFlow
          nodes={shownNodes}
          edges={shownEdges}
          onNodesChange={onNodesChange}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          fitViewOptions={{ padding: 0.12 }}
          minZoom={0.05}
          maxZoom={1.5}
          nodesConnectable={false}
          onNodeMouseEnter={(_, n) => setHovered(n.id)}
          onNodeMouseLeave={() => setHovered(null)}
          onNodeClick={(_, n) => {
            const line = (n.data as NodeData).ir.line;
            if (line) onSelect(line, 1);
          }}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={22} size={1.5} color="#cbd5e1" />
          <Controls showInteractive={false} position="bottom-left" />
          {phase.nodes.length > 10 && (
            <MiniMap
              pannable
              zoomable
              position="bottom-right"
              nodeColor={(n) => {
                const ir = (n.data as NodeData).ir;
                return ir.kind === "agent" ? STATUS[ir.status].mini : "#334155";
              }}
              maskColor="rgba(241,245,249,0.7)"
            />
          )}
        </ReactFlow>
      </div>
    </div>
  );
}

export default function GraphTab(props: Props) {
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setFullscreen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  if (props.phase.status === "skipped") return <Skipped phase="IR" />;
  return (
    <div className={fullscreen ? "fixed inset-0 z-50 bg-white" : "h-full min-h-[420px]"}>
      <ReactFlowProvider>
        <GraphCanvas {...props} fullscreen={fullscreen} onToggleFullscreen={() => setFullscreen(!fullscreen)} />
      </ReactFlowProvider>
    </div>
  );
}
