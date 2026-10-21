// Layered graph layout for the IR (a small version of the Sugiyama method used by graph drawing tools).
//
// 1. Back edges: a depth first search from `start` finds the edges that point backwards (loops).
//    Ignoring them leaves a DAG (no cycles), which can be drawn in one direction.
// 2. Layers: each node goes one layer after its furthest predecessor (longest path).
//    `end` always gets the last layer.
// 3. Ordering: inside each layer, nodes are sorted by the average position of their neighbours
//    (barycenter heuristic). A few sweeps forwards and backwards reduce edge crossings.
// 4. Coordinates: layer -> x and position -> y for left-to-right ("LR"),
//    or layer -> y and position -> x for top-to-bottom ("TB").
// Agents that cannot be reached from start are placed apart, after the main graph.

import type { IrEdge, IrNode } from "../types";

export type Direction = "LR" | "TB";
export type EdgeRoute = "forward" | "back" | "self";

export const NODE_WIDTH = 170;
export const NODE_HEIGHT = 62;

// distance between layers and between nodes in the same layer, per direction
const SPACING = {
  LR: { layer: 250, node: 105 },
  TB: { layer: 118, node: 210 },
};

export interface Layout {
  direction: Direction;
  positions: Map<string, { x: number; y: number }>;
  routes: EdgeRoute[];        // one per edge, same order as the input edges
  spans: number[];            // how many layers each edge crosses (more than 1 = skips nodes)
  width: number;              // bounding box of the whole drawing
  height: number;
}

interface Ranking {
  layers: number[];           // the layer numbers in use, sorted
  columns: Map<number, string[]>;   // reachable nodes per layer, in crossing-reduced order
  dead: string[];
  layer: Map<string, number>;
}

/** Steps 1-3: independent of the drawing direction. */
function rank(nodes: IrNode[], edges: IrEdge[]): Ranking {
  const ids = nodes.map((n) => n.id);
  const succ = new Map<string, string[]>(ids.map((id) => [id, []]));
  edges.forEach((e) => succ.get(e.from)?.push(e.to));

  // 1. back edges: an edge to a node that is still on the DFS stack closes a loop
  const backEdge = new Set<string>();
  const state = new Map<string, "visiting" | "done">();
  const visit = (u: string) => {
    state.set(u, "visiting");
    for (const v of succ.get(u) ?? []) {
      if (state.get(v) === "visiting") backEdge.add(`${u}->${v}`);
      else if (!state.has(v)) visit(v);
    }
    state.set(u, "done");
  };
  visit("start");
  const reachable = new Set(state.keys());
  ids.forEach((id) => !state.has(id) && visit(id));   // dead agents too, so their loops are found
  const forward = edges.filter((e) => e.from !== e.to && !backEdge.has(`${e.from}->${e.to}`));

  // 2. longest path layering in topological order (Kahn's algorithm)
  const indegree = new Map<string, number>(ids.map((id) => [id, 0]));
  forward.forEach((e) => indegree.set(e.to, indegree.get(e.to)! + 1));
  const layer = new Map<string, number>(ids.map((id) => [id, 0]));
  const queue = ids.filter((id) => indegree.get(id) === 0);
  while (queue.length) {
    const u = queue.shift()!;
    for (const e of forward.filter((f) => f.from === u)) {
      layer.set(e.to, Math.max(layer.get(e.to)!, layer.get(u)! + 1));
      indegree.set(e.to, indegree.get(e.to)! - 1);
      if (indegree.get(e.to) === 0) queue.push(e.to);
    }
  }
  const maxLayer = Math.max(0, ...ids.filter((id) => id !== "end" && reachable.has(id)).map((id) => layer.get(id)!));
  layer.set("end", maxLayer + 1);
  ids.forEach((id) => !reachable.has(id) && layer.set(id, Math.max(1, layer.get(id)!)));

  // 3. order each layer with barycenter sweeps
  const columns = new Map<number, string[]>();
  ids.filter((id) => reachable.has(id)).forEach((id) => {
    const l = layer.get(id)!;
    columns.set(l, [...(columns.get(l) ?? []), id]);
  });
  const order = new Map<string, number>();
  columns.forEach((col) => col.forEach((id, i) => order.set(id, i)));
  const neighbours = (id: string, dir: "in" | "out") =>
    forward.filter((e) => (dir === "in" ? e.to === id : e.from === id)).map((e) => (dir === "in" ? e.from : e.to));
  const sortLayer = (l: number, dir: "in" | "out") => {
    const col = columns.get(l);
    if (!col) return;
    const score = (id: string) => {
      const ns = neighbours(id, dir).filter((n) => order.has(n));
      return ns.length ? ns.reduce((sum, n) => sum + order.get(n)!, 0) / ns.length : order.get(id)!;
    };
    col.sort((a, b) => score(a) - score(b));
    col.forEach((id, i) => order.set(id, i));
  };
  const layers = [...columns.keys()].sort((a, b) => a - b);
  for (let sweep = 0; sweep < 4; sweep++) {
    layers.forEach((l) => sortLayer(l, "in"));
    [...layers].reverse().forEach((l) => sortLayer(l, "out"));
  }
  return { layers, columns, dead: ids.filter((id) => !reachable.has(id)), layer };
}

/** Step 4: turn the ranking into coordinates for one direction. */
function place(ranking: Ranking, edges: IrEdge[], direction: Direction): Layout {
  const { layer: gapL, node: gapN } = SPACING[direction];
  const positions = new Map<string, { x: number; y: number }>();
  const put = (id: string, along: number, across: number) =>
    positions.set(id, direction === "LR" ? { x: along, y: across } : { x: across, y: along });

  let widest = 1;
  ranking.columns.forEach((col, l) => {
    widest = Math.max(widest, col.length);
    col.forEach((id, i) => put(id, l * gapL, (i - (col.length - 1) / 2) * gapN));
  });
  // dead agents: an extra lane beside the main graph
  const laneStart = (widest / 2) * gapN + (direction === "LR" ? 150 : 120);
  const used = new Map<number, number>();
  ranking.dead.forEach((id) => {
    const l = ranking.layer.get(id)!;
    const k = used.get(l) ?? 0;
    used.set(l, k + 1);
    put(id, l * gapL, laneStart + k * gapN);
  });

  const xs = [...positions.values()].map((p) => p.x);
  const ys = [...positions.values()].map((p) => p.y);
  const routes: EdgeRoute[] = edges.map((e) =>
    e.from === e.to ? "self" : ranking.layer.get(e.to)! > ranking.layer.get(e.from)! ? "forward" : "back",
  );
  // extra room for loop arcs and labels around the nodes
  const spans = edges.map((e) => ranking.layer.get(e.to)! - ranking.layer.get(e.from)!);
  return {
    direction,
    positions,
    routes,
    spans,
    width: Math.max(...xs) - Math.min(...xs) + NODE_WIDTH + 160,
    height: Math.max(...ys) - Math.min(...ys) + NODE_HEIGHT + 160,
  };
}

/**
 * Lay out the graph. With direction "auto", both directions are tried and the one that can be
 * shown at the larger zoom in a panel of the given size wins (long chains usually fit better
 * top-to-bottom, wide branching graphs left-to-right).
 */
export function computeLayout(
  nodes: IrNode[], edges: IrEdge[], direction: Direction | "auto", panel: { width: number; height: number },
): Layout {
  const ranking = rank(nodes, edges);
  if (direction !== "auto") return place(ranking, edges, direction);
  const lr = place(ranking, edges, "LR");
  const tb = place(ranking, edges, "TB");
  const zoom = (l: Layout) => Math.min(panel.width / l.width, panel.height / l.height);
  return zoom(tb) > zoom(lr) * 1.05 ? tb : lr;
}
