import type { BoardItem, EventKey, Procedure, UserPersona } from "../types";
import { effectiveDday } from "./dday";

/**
 * 할 일 보드를 절차 그래프(DAG)로 본다.
 * - 노드 = 보드에 올라온 절차 하나, 간선 = dependsOn(선행 → 후속; 둘 다 보드에 있을 때만)
 * - layer   = 선행 사슬의 깊이(가장 긴 경로). 0 = 지금 바로 시작 가능한 절차
 * - state   = done(완료) | ready(선행 모두 완료, 지금 가능) | blocked(선행 미완료)
 * - order   = 추천 순서(1부터). 선행→후속을 지키는 위상 정렬, 같은 층에서는 must → 실효 D-day 짧은 것 → 여는 후속이 많은 것
 * 백엔드 sources/graph.py 의 rank() 와 같은 규칙이되, D-day 는 페르소나 기준일로 환산한 값을 쓴다.
 */
export type NodeState = "done" | "ready" | "blocked";

export interface GraphNode {
  id: string;
  p: Procedure;
  status: BoardItem["status"];
  state: NodeState;
  layer: number;
  order: number;
  dday: number;
  /** 이 절차가 끝나면 열리는 후속 절차 수(보드 안에서) */
  unlocks: number;
  /** 잠그는 선행(dependsOn) */
  parents: string[];
  /** 권고 선행(suggestedAfter) — 순서·배치에는 쓰지만 체크를 잠그지 않는다 */
  softParents: string[];
  children: string[];
}

export interface GraphEdge {
  from: string; // 선행
  to: string; // 후속
  satisfied: boolean; // 선행이 완료됨
  soft: boolean; // 권고(잠그지 않음)
}

export interface BoardGraph {
  nodes: GraphNode[];
  byId: Record<string, GraphNode>;
  edges: GraphEdge[];
  /** 간선이 하나라도 걸린 노드(흐름도 상단) */
  connected: GraphNode[];
  /** 간선이 전혀 없는 노드(순서 제약 없음) */
  isolated: GraphNode[];
  layers: number;
}

export function buildGraph(
  board: BoardItem[],
  procIndex: Record<string, Procedure>,
  persona: UserPersona | null,
  event: EventKey | null,
  fallback: (id: string) => Procedure
): BoardGraph {
  const onBoard = new Set(board.map((b) => b.id));
  const nodes: GraphNode[] = board.map((b) => {
    const p = procIndex[b.id] ?? fallback(b.id);
    return {
      id: b.id,
      p,
      status: b.status,
      state: "ready",
      layer: 0,
      order: 0,
      dday: effectiveDday(p, persona, event),
      unlocks: 0,
      parents: (p.dependsOn ?? []).filter((d) => d !== b.id && onBoard.has(d)),
      softParents: (p.suggestedAfter ?? []).filter((d) => d !== b.id && onBoard.has(d) && !(p.dependsOn ?? []).includes(d)),
      children: [],
    };
  });
  const byId: Record<string, GraphNode> = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const allParents = (n: GraphNode) => [...n.parents, ...n.softParents];
  for (const n of nodes) for (const d of allParents(n)) byId[d].children.push(n.id);

  // layer = 가장 긴 선행 경로 길이(순환은 없다고 보지만 방어적으로 방문 집합을 둔다)
  const memo: Record<string, number> = {};
  const depth = (id: string, stack: Set<string>): number => {
    if (memo[id] !== undefined) return memo[id];
    if (stack.has(id)) return 0;
    stack.add(id);
    const d = allParents(byId[id]).reduce((m, pid) => Math.max(m, depth(pid, stack) + 1), 0);
    stack.delete(id);
    memo[id] = d;
    return d;
  };
  for (const n of nodes) n.layer = depth(n.id, new Set());

  // unlocks = 후손 수
  const desc = (id: string, seen: Set<string>): Set<string> => {
    for (const c of byId[id].children) if (!seen.has(c)) { seen.add(c); desc(c, seen); }
    return seen;
  };
  for (const n of nodes) n.unlocks = desc(n.id, new Set()).size;

  // state
  for (const n of nodes) {
    if (n.status === "done") n.state = "done";
    else n.state = n.parents.every((pid) => byId[pid].status === "done") ? "ready" : "blocked";
  }

  // order = 위상 정렬(권고 선행 포함) + 우선순위 타이브레이크
  const indeg: Record<string, number> = Object.fromEntries(nodes.map((n) => [n.id, allParents(n).length]));
  const pos: Record<string, number> = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
  const key = (n: GraphNode) => [
    n.p.priority === "must" ? 0 : n.p.priority === "nice" ? 1 : 2,
    n.id.startsWith("emergency-") ? 0 : 1,
    n.dday,
    -n.unlocks,
    pos[n.id],
  ];
  const cmp = (a: GraphNode, b: GraphNode) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
    return 0;
  };
  let ready = nodes.filter((n) => indeg[n.id] === 0);
  let k = 1;
  const placed = new Set<string>();
  while (ready.length) {
    ready.sort(cmp);
    const n = ready.shift()!;
    n.order = k++;
    placed.add(n.id);
    for (const c of n.children) if (--indeg[c] === 0) ready.push(byId[c]);
  }
  for (const n of nodes) if (!placed.has(n.id)) n.order = k++; // 순환 방어

  const edges: GraphEdge[] = [];
  for (const n of nodes) {
    for (const pid of n.parents) edges.push({ from: pid, to: n.id, satisfied: byId[pid].status === "done", soft: false });
    for (const pid of n.softParents) edges.push({ from: pid, to: n.id, satisfied: byId[pid].status === "done", soft: true });
  }
  const connected = nodes.filter((n) => allParents(n).length || n.children.length);
  const isolated = nodes.filter((n) => !allParents(n).length && !n.children.length);
  const layers = connected.reduce((m, n) => Math.max(m, n.layer + 1), 0);
  return { nodes, byId, edges, connected, isolated, layers };
}
