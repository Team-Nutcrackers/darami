// 절차 그래프 전체(docs/graph/graph.json 의 3D 배치)를 채팅 '탐색 애니메이션'용 2D 경량 JSON 으로 내보낸다.
//   node scripts/build-procgraph.mjs   (npm run graph)
// 입력: ../docs/graph/graph.json (python -m sources.draw_graph --json 산출물, 노드 순서 = graph.base + graph.learned)
//       ../backend/playbooks/graph.base.json, graph.learned.json (노드 키 — 플레이북 절차와 매칭용)
// 출력: src/data/procgraph.json  { w, h, nodes:[{k,n,t,x,y,d}], links:[[s,t,hard]] }
// 투영: README 그림과 같은 카메라(elev 22°, azim 38°)에서 본 정사영.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const g = JSON.parse(readFileSync(resolve(root, "docs/graph/graph.json"), "utf8"));
const base = JSON.parse(readFileSync(resolve(root, "backend/playbooks/graph.base.json"), "utf8"));
let learned = { nodes: {} };
try {
  learned = JSON.parse(readFileSync(resolve(root, "backend/playbooks/graph.learned.json"), "utf8"));
} catch {
  /* 학습판 없음 */
}
const keys = [...Object.keys(base.nodes), ...Object.keys(learned.nodes ?? {})];
if (keys.length !== g.nodes.length) {
  console.error(`노드 수 불일치: graph.json ${g.nodes.length} vs base+learned ${keys.length} — draw_graph --json 을 다시 돌리세요`);
  process.exit(1);
}

const W = 1000, H = 600, MARGIN = 0.04;
const a = (38 * Math.PI) / 180, e = (22 * Math.PI) / 180;
const proj = ([x, y, z]) => [
  -x * Math.sin(a) + y * Math.cos(a),
  -x * Math.sin(e) * Math.cos(a) - y * Math.sin(e) * Math.sin(a) + z * Math.cos(e),
];
const pts = g.nodes.map((n) => proj(n.p));
const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
const s = Math.min((W * (1 - 2 * MARGIN)) / (x1 - x0), (H * (1 - 2 * MARGIN)) / (y1 - y0));
const ox = (W - (x1 - x0) * s) / 2, oy = (H - (y1 - y0) * s) / 2;

const nodes = g.nodes.map((n, i) => {
  const name = keys[i] && (base.nodes[keys[i]] ?? learned.nodes?.[keys[i]])?.name;
  if (name !== n.name) throw new Error(`노드 ${i} 이름 불일치: ${name} vs ${n.name}`);
  return {
    k: keys[i],
    n: n.name,
    t: n.kind,
    x: +((pts[i][0] - x0) * s + ox).toFixed(1),
    y: +(H - ((pts[i][1] - y0) * s + oy)).toFixed(1), // SVG 는 y 가 아래로
    d: n.deg,
  };
});
// 고립 노드(간선 없음)는 3D 에서 바깥 구면에 있어 정사영하면 중심까지 덮는다 → 2D 에서는 바깥 고리로 밀어낸다
const cx = W / 2, cy = H / 2, rx = (W / 2) * (1 - MARGIN), ry = (H / 2) * (1 - MARGIN);
nodes.forEach((n, i) => {
  if (n.d > 0) return;
  const ang = Math.atan2((n.y - cy) / ry, (n.x - cx) / rx);
  const u = ((Math.sin(i * 12.9898) * 43758.5453) % 1 + 1) % 1; // 결정적 난수
  const f = 0.74 + 0.26 * u;
  n.x = +(cx + Math.cos(ang) * rx * f).toFixed(1);
  n.y = +(cy + Math.sin(ang) * ry * f).toFixed(1);
});
const links = g.links.map((l) => [l.s, l.t, l.hard ? 1 : 0]);
const out = { w: W, h: H, nodes, links };
const dst = resolve(here, "..", "src/data/procgraph.json");
writeFileSync(dst, JSON.stringify(out));
console.log(`wrote ${dst}: nodes ${nodes.length}, links ${links.length}, ${(JSON.stringify(out).length / 1024).toFixed(0)}KB`);
