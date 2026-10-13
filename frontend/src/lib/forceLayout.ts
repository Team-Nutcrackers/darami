import type { BoardGraph } from "./graph";

/**
 * 할 일 그래프를 '교과서식' 방향 그래프로 배치한다 — 큰 타원 안에 노드가 고르게 퍼지고, 간선은 선행 → 후속 화살표.
 *
 * Fruchterman–Reingold 힘 기반 배치에 세 가지를 더했다.
 *  1) 타원 경계: 매 반복 타원 밖으로 나간 노드를 경계 안으로 되돌리고, 타원 모양에 맞춘 약한 중심 인력으로 고르게 채운다.
 *  2) 방향 보정: 연결된 절차를 층(선행 사슬 깊이)에 맞는 가로 위치로 당기고, 간선 u→v 마다 v 가 u 보다 오른쪽(최소 0.7k)에
 *     오도록 민다 → 화살표가 왼쪽 → 오른쪽으로 흐르며 타원 가로 폭을 쓴다.
 *  3) 숲 분리: 연결 요소(트리)가 다르면 더 세게 밀어 덩어리가 구분된다. 간선이 없는 절차는 타원 아래쪽 가장자리에서 시작한다.
 *
 * 난수를 쓰지 않는다(초기 위치 = 층·추천 순서). 같은 보드는 항상 같은 그림이고,
 * 이전 위치(prev)를 주면 거기서 약하게만 다시 풀어 새 노드만 자리를 잡는다(체크·추가 때 그림이 흔들리지 않게).
 */
export interface Pt {
  x: number;
  y: number;
}

export interface ForceLayout {
  pos: Record<string, Pt>;
  width: number;
  height: number;
  /** 타원 중심·반지름(배경 그리기용) */
  cx: number;
  cy: number;
  rx: number;
  ry: number;
}

export const NODE_R = 13;
const MARGIN = NODE_R + 10;
const ITER = 360;

export function forceLayout(g: BoardGraph, width: number, height: number, prev?: Record<string, Pt>): ForceLayout {
  const nodes = [...g.nodes].sort((a, b) => a.order - b.order);
  const N = nodes.length;
  const a = Math.max(40, width / 2 - MARGIN); // 타원 가로 반지름
  const b = Math.max(40, height / 2 - MARGIN); // 세로 반지름
  const out: ForceLayout = { pos: {}, width, height, cx: width / 2, cy: height / 2, rx: a + MARGIN - 6, ry: b + MARGIN - 6 };
  if (!N) return out;

  const idx: Record<string, number> = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
  const edges = g.edges.filter((e) => idx[e.from] !== undefined && idx[e.to] !== undefined).map((e) => [idx[e.from], idx[e.to]] as const);

  // 연결 요소(무방향)
  const comp = nodes.map((_, i) => i);
  const find = (i: number): number => (comp[i] === i ? i : (comp[i] = find(comp[i])));
  for (const [u, v] of edges) comp[find(u)] = find(v);
  const compOf = nodes.map((_, i) => find(i));
  const deg = nodes.map(() => 0);
  for (const [u, v] of edges) {
    deg[u]++;
    deg[v]++;
  }

  // 이상적인 간선 길이: 타원 넓이를 노드 수로 나눈 칸의 한 변
  const k = Math.max(44, Math.min(96, Math.sqrt((Math.PI * a * b) / N) * 0.95));

  // ---- 초기 위치(결정적) ----
  const xs = new Float64Array(N), ys = new Float64Array(N);
  const connectedComps: number[] = []; // 간선이 있는 연결 요소(추천 순서가 빠른 것부터) → 위에서 아래로 띠를 나눠 시작
  nodes.forEach((_, i) => {
    if (deg[i] > 0 && !connectedComps.includes(compOf[i])) connectedComps.push(compOf[i]);
  });
  const maxLayer = Math.max(1, ...nodes.filter((_, i) => deg[i] > 0).map((n) => n.layer));
  const bandH = (2 * b * 0.75) / Math.max(1, connectedComps.length);
  const isolated = nodes.map((_, i) => i).filter((i) => deg[i] === 0);
  const fresh = new Set<number>();
  const centered = (p: Pt): Pt => ({ x: p.x - width / 2, y: p.y - height / 2 });

  const byCompLayer: Record<string, number[]> = {};
  nodes.forEach((n, i) => {
    if (deg[i] === 0) return;
    const key = `${compOf[i]}:${n.layer}`;
    (byCompLayer[key] ??= []).push(i);
  });
  nodes.forEach((n, i) => {
    const p = prev?.[n.id];
    if (p) {
      const c = centered(p);
      xs[i] = c.x;
      ys[i] = c.y;
      return;
    }
    fresh.add(i);
    if (deg[i] > 0) {
      const band = connectedComps.indexOf(compOf[i]);
      const bandMid = -b * 0.75 + bandH * (band + 0.5);
      const same = byCompLayer[`${compOf[i]}:${n.layer}`];
      const j = same.indexOf(i);
      const spread = Math.min(k * 0.9, bandH / Math.max(1, same.length));
      xs[i] = -a * 0.55 + (1.1 * a * n.layer) / maxLayer;
      ys[i] = bandMid + (j - (same.length - 1) / 2) * spread;
    } else {
      // 간선 없는 절차: 타원 아래쪽 호(30°~150°, y 는 아래로 +)에 고르게
      const j = isolated.indexOf(i), m = isolated.length;
      const th = m === 1 ? Math.PI / 2 : Math.PI * (1 / 6 + (2 / 3) * (j / (m - 1)));
      xs[i] = Math.cos(th) * a * 0.8;
      ys[i] = Math.sin(th) * b * 0.8;
    }
  });

  const layerX = (layer: number) => -a * 0.55 + (1.1 * a * layer) / maxLayer;

  // ---- 반복 ----
  const warm = prev && fresh.size < N; // 이전 그림에서 이어 풀기 → 온도를 낮춰 기존 노드가 크게 움직이지 않게
  const t0 = warm ? k * 0.18 : k * 0.6;
  const dx = new Float64Array(N), dy = new Float64Array(N);
  const minGap = NODE_R * 2 + 10;

  for (let it = 0; it < ITER; it++) {
    const t = t0 * (1 - it / ITER) + 0.3;
    dx.fill(0);
    dy.fill(0);
    // 척력(+겹침 방지, 다른 트리끼리는 더 세게)
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        let ex = xs[i] - xs[j], ey = ys[i] - ys[j];
        let d = Math.hypot(ex, ey);
        if (d < 0.01) {
          ex = (i - j) * 0.01; // 같은 자리면 결정적으로 살짝 떨어뜨린다
          ey = 0.01;
          d = Math.hypot(ex, ey);
        }
        let f = (k * k) / d;
        if (compOf[i] !== compOf[j]) f *= 1.15;
        if (d < minGap) f += (minGap - d) * k * 0.4;
        dx[i] += (ex / d) * f;
        dy[i] += (ey / d) * f;
        dx[j] -= (ex / d) * f;
        dy[j] -= (ey / d) * f;
      }
    }
    // 인력(간선) + 방향 보정(후속은 오른쪽으로)
    for (const [u, v] of edges) {
      const ex = xs[v] - xs[u], ey = ys[v] - ys[u];
      const d = Math.max(0.01, Math.hypot(ex, ey));
      // 허브(후속이 많은 절차)가 간선 수만큼 끌려가 한쪽으로 쏠리지 않게 차수로 나눈다
      const f = (d * d) / k / Math.sqrt(Math.max(deg[u], deg[v]));
      dx[u] += (ex / d) * f;
      dy[u] += (ey / d) * f;
      dx[v] -= (ex / d) * f;
      dy[v] -= (ey / d) * f;
      const gap = xs[u] + k * 0.7 - xs[v];
      if (gap > 0) {
        dx[u] -= gap * 1.2;
        dx[v] += gap * 1.2;
      }
    }
    // 타원 모양 중심 인력: 가장자리로 갈수록 강해져 노드가 둘레에 달라붙지 않고 타원 안을 고르게 채운다
    for (let i = 0; i < N; i++) {
      dx[i] -= (xs[i] / a) * k * 1.1;
      dy[i] -= (ys[i] / b) * k * 1.1;
    }
    // 이동(온도로 제한) + 타원 경계
    for (let i = 0; i < N; i++) {
      const len = Math.hypot(dx[i], dy[i]);
      if (len > 0) {
        const step = Math.min(len, warm && !fresh.has(i) ? t * 0.5 : t);
        xs[i] += (dx[i] / len) * step;
        ys[i] += (dy[i] / len) * step;
      }
      // 층 위치로 끌기: 연결된 절차는 층에 맞는 가로 위치(왼쪽 = 선행, 오른쪽 = 후속)로 매번 조금씩 옮긴다.
      // 힘이 아니라 위치를 직접 당기므로 허브의 인력에 밀리지 않고, 타원 가로 폭을 흐름에 쓴다(끝은 좁아 ±0.55 까지)
      if (deg[i] > 0) xs[i] += (layerX(nodes[i].layer) - xs[i]) * 0.12;
      const e = (xs[i] / a) ** 2 + (ys[i] / b) ** 2;
      if (e > 1) {
        const s = 0.99 / Math.sqrt(e);
        xs[i] *= s;
        ys[i] *= s;
      }
    }
  }

  // 무게중심을 타원 중심으로, 덩어리가 작으면 타원을 채우도록 살짝 키운다(최대 1.5배)
  let mx = 0, my = 0;
  for (let i = 0; i < N; i++) {
    mx += xs[i];
    my += ys[i];
  }
  mx /= N;
  my /= N;
  let rho = 0;
  for (let i = 0; i < N; i++) rho = Math.max(rho, Math.hypot((xs[i] - mx) / a, (ys[i] - my) / b));
  const scale = !warm && N > 1 && rho > 0 ? Math.min(1.5, Math.max(1, 0.9 / rho)) : 1;
  for (let i = 0; i < N; i++) {
    xs[i] = (xs[i] - (warm ? 0 : mx)) * scale;
    ys[i] = (ys[i] - (warm ? 0 : my)) * scale;
  }
  // 겹침 정리: 층 위치로 끌다 보면 같은 열의 노드가 붙을 수 있다 → 최소 간격까지 서로 밀어내고 다시 타원 안으로
  for (let pass = 0; pass < 80; pass++) {
    let moved = false;
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        let ex = xs[i] - xs[j], ey = ys[i] - ys[j];
        let d = Math.hypot(ex, ey);
        if (d >= minGap) continue;
        if (d < 0.01) {
          ex = 0;
          ey = i < j ? -1 : 1;
          d = 1;
        }
        const push = (minGap - d) / 2 + 0.5;
        xs[i] += (ex / d) * push;
        ys[i] += (ey / d) * push;
        xs[j] -= (ex / d) * push;
        ys[j] -= (ey / d) * push;
        moved = true;
      }
    }
    for (let i = 0; i < N; i++) {
      const e = (xs[i] / a) ** 2 + (ys[i] / b) ** 2;
      if (e > 1) {
        xs[i] /= Math.sqrt(e);
        ys[i] /= Math.sqrt(e);
      }
    }
    if (!moved) break;
  }
  nodes.forEach((n, i) => {
    out.pos[n.id] = { x: xs[i] + width / 2, y: ys[i] + height / 2 };
  });
  return out;
}

/** 간선 경로: 노드 원 둘레에서 출발·도착. 중간에 다른 노드가 걸리면 그 반대쪽으로 휘어 피한다 */
export function edgePath(from: Pt, to: Pt, others: Pt[], r = NODE_R): string {
  const ex = to.x - from.x, ey = to.y - from.y;
  const d = Math.max(0.01, Math.hypot(ex, ey));
  const ux = ex / d, uy = ey / d;
  // 가로막는 노드 찾기(선분까지 거리)
  let bend = 0;
  for (const o of others) {
    const t = ((o.x - from.x) * ux + (o.y - from.y) * uy) / d;
    if (t <= 0.08 || t >= 0.92) continue;
    const px = from.x + ux * d * t, py = from.y + uy * d * t;
    const dist = Math.hypot(o.x - px, o.y - py);
    if (dist < r + 7) {
      const side = (o.x - from.x) * -uy + (o.y - from.y) * ux; // 노드가 선의 어느 쪽인지
      bend = (side > 0 ? -1 : 1) * (r * 2 + 6 - dist);
      break;
    }
  }
  const nx = -uy, ny = ux;
  const mx = (from.x + to.x) / 2 + nx * bend, my = (from.y + to.y) / 2 + ny * bend;
  // 시작·끝을 원 둘레로(곡선이면 제어점 방향 기준)
  const sdx = mx - from.x, sdy = my - from.y, sl = Math.hypot(sdx, sdy) || 1;
  const tdx = to.x - mx, tdy = to.y - my, tl = Math.hypot(tdx, tdy) || 1;
  const sx = from.x + (sdx / sl) * (r + 1), sy = from.y + (sdy / sl) * (r + 1);
  const tx = to.x - (tdx / tl) * (r + 2), ty = to.y - (tdy / tl) * (r + 2);
  return bend ? `M${sx},${sy} Q${mx},${my} ${tx},${ty}` : `M${sx},${sy} L${tx},${ty}`;
}

