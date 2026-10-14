import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import raw from "../data/procgraph.json";
import { findFocus, neighborhood, type ProcGraph } from "../lib/graphSearch";
import type { Playbook } from "../types";
import daramiAvatar from "../../figure/darami-chat-logo.png";

/**
 * 첫 질문을 받고 답을 만드는 동안(점 세 개 대신) 보여 주는 '절차 그래프 탐색' 애니메이션.
 *   scan  → 그래프 전체(808 노드)가 떠오르고 탐색 파문이 퍼진다
 *   lit   → 이벤트 절차(플레이북)와 질문에 닿는 절차가 하나씩 켜진다
 *   zoom  → 켜진 절차들 쪽으로 화면이 확대된다
 *   focus → 이름이 붙고, 절차 사이 선행 관계 간선을 따라 흐름이 흐른다 (답이 올 때까지 유지)
 * 어떤 노드를 켤지는 lib/graphSearch 의 결정적 문자열 매칭으로 고른다(실제 답변 근거가 아니라 '어디를 보는지' 안내).
 */
const G = raw as ProcGraph;
const { w: W, h: H } = G;

const C = {
  acorn: "#6B4226",
  nut: "#E8862E",
  nutDeep: "#D2691E",
  leaf: "#7BA05B",
  cream: "#FAF3E7",
  law: "rgba(107,66,38,0.42)",
  svc: "rgba(123,160,91,0.55)",
  x: "rgba(232,134,46,0.40)",
};

const SCAN_MS = 700; // 첫 노드가 켜지기 전 훑는 시간
const LIGHT_GAP = 150; // 노드가 하나씩 켜지는 간격
const ZOOM_DELAY = 420; // 마지막 노드가 켜지고 확대까지
const ZOOM_MS = 900;
const MAX_ZOOM = 4.2;

type Phase = "scan" | "lit" | "zoom" | "focus";
interface VB { x: number; y: number; w: number; h: number }

interface Label { i: number; x: number; y: number; anchor: "start" | "end"; text: string }

/**
 * 이름표 자리 잡기(겹침 회피, 화면 px 기준). 노드마다 오른쪽 → 왼쪽 → 오른쪽 위/아래 → 왼쪽 위/아래 순으로
 * 다른 이름표·강조 노드와 겹치지 않는 첫 자리를 쓰고, 다 겹치면 가장 덜 겹치는 자리.
 */
function placeLabels(targets: number[], vb: VB, upp: number, nodeR: number, fontPx: number): Label[] {
  const px = (v: number) => v / upp;
  const placed: { x0: number; x1: number; y0: number; y1: number }[] = targets.map((i) => {
    const n = G.nodes[i];
    const r = nodeR * 1.9 + 1;
    return { x0: px(n.x - vb.x) - r, x1: px(n.x - vb.x) + r, y0: px(n.y - vb.y) - r, y1: px(n.y - vb.y) + r };
  });
  const viewW = vb.w / upp, viewH = vb.h / upp;
  const out: Label[] = [];
  for (const i of targets) {
    const n = G.nodes[i];
    const text = n.n.length > 13 ? n.n.slice(0, 12) + "…" : n.n;
    const w = text.length * fontPx * 0.98, h = fontPx * 1.25;
    const cx = px(n.x - vb.x), cy = px(n.y - vb.y);
    const gap = nodeR + 4;
    const cands: { dx: number; dy: number; anchor: "start" | "end" }[] = [
      { dx: gap, dy: 0, anchor: "start" }, { dx: -gap, dy: 0, anchor: "end" },
      { dx: gap * 0.6, dy: -h * 0.95, anchor: "start" }, { dx: gap * 0.6, dy: h * 0.95, anchor: "start" },
      { dx: -gap * 0.6, dy: -h * 0.95, anchor: "end" }, { dx: -gap * 0.6, dy: h * 0.95, anchor: "end" },
      // 밀집 구간용: 두 줄 위/아래, 더 멀리 옆
      { dx: gap * 0.4, dy: -h * 1.9, anchor: "start" }, { dx: gap * 0.4, dy: h * 1.9, anchor: "start" },
      { dx: -gap * 0.4, dy: -h * 1.9, anchor: "end" }, { dx: -gap * 0.4, dy: h * 1.9, anchor: "end" },
      { dx: gap + h * 1.6, dy: 0, anchor: "start" }, { dx: -(gap + h * 1.6), dy: 0, anchor: "end" },
    ];
    let best: { r: { x0: number; x1: number; y0: number; y1: number }; c: (typeof cands)[number]; cost: number } | null = null;
    for (const c of cands) {
      const x0 = c.anchor === "start" ? cx + c.dx : cx + c.dx - w;
      const r = { x0, x1: x0 + w, y0: cy + c.dy - h / 2, y1: cy + c.dy + h / 2 };
      let cost = 0;
      for (const q of placed) cost += Math.max(0, Math.min(r.x1, q.x1) - Math.max(r.x0, q.x0)) * Math.max(0, Math.min(r.y1, q.y1) - Math.max(r.y0, q.y0));
      if (r.x0 < 2 || r.x1 > viewW - 2 || r.y0 < 2 || r.y1 > viewH - 2) cost += w * h * 3; // 화면 밖으로 나가면 큰 벌점
      if (!best || cost < best.cost) best = { r, c, cost };
      if (cost === 0) break;
    }
    if (!best || best.cost > w * h * 0.15) continue; // 어디에 놓아도 많이 겹치면 이름표 생략(점만)
    placed.push(best.r);
    out.push({ i, x: n.x + best.c.dx * upp, y: n.y + best.c.dy * upp, anchor: best.c.anchor, text });
  }
  return out;
}

const ease = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

export default function GraphSearchBubble({
  question,
  playbook,
  eventTitle,
  pending,
}: {
  question: string;
  playbook: Playbook | null;
  eventTitle?: string;
  /** 플레이북(이벤트 절차)을 아직 만드는 중 — 직접 입력한 이벤트. 도착하면 켜지는 노드가 늘고 그쪽으로 다시 확대된다 */
  pending?: boolean;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [svgPx, setSvgPx] = useState(280);
  useLayoutEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const measure = () => setSvgPx(Math.max(160, el.clientWidth));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 무엇을 켤지 + 어디로 확대할지 (마운트 때 한 번)
  const plan = useMemo(() => {
    const f = findFocus(G, question, playbook);
    // 이벤트 절차가 충분하면, 그 무리에서 멀리 떨어진 질문 매칭은 뺀다(한 노드 때문에 확대가 풀리지 않게)
    let qs = f.question;
    if (f.event.length >= 3) {
      const ex = f.event.map((i) => G.nodes[i].x), ey = f.event.map((i) => G.nodes[i].y);
      const px = (Math.max(...ex) - Math.min(...ex)) * 0.4 + 60, py = (Math.max(...ey) - Math.min(...ey)) * 0.4 + 40;
      qs = qs.filter((i) => G.nodes[i].x >= Math.min(...ex) - px && G.nodes[i].x <= Math.max(...ex) + px && G.nodes[i].y >= Math.min(...ey) - py && G.nodes[i].y <= Math.max(...ey) + py);
    }
    const targets = [...f.event, ...qs].slice(0, 10);
    const qset = new Set(f.question);
    const nb = neighborhood(G, targets);
    const full: VB = { x: 0, y: 0, w: W, h: H };
    let goal: VB;
    let cx = W / 2, cy = H / 2;
    if (targets.length === 0) {
      goal = { x: W * 0.2, y: H * 0.2, w: W * 0.6, h: H * 0.6 };
    } else {
      // 외딴 노드(그것만 빼면 범위가 절반 아래로 줄어드는)는 확대 범위에서 뺀다 — 켜지긴 하되 화면 밖
      let zoomSet = [...targets];
      const span = (ids: number[]) => {
        const xs = ids.map((i) => G.nodes[i].x), ys = ids.map((i) => G.nodes[i].y);
        return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
      };
      while (zoomSet.length > 3) {
        const b = span(zoomSet);
        const mx = (b.x0 + b.x1) / 2, my = (b.y0 + b.y1) / 2;
        const far = zoomSet.reduce((a, i) => (Math.hypot(G.nodes[i].x - mx, G.nodes[i].y - my) > Math.hypot(G.nodes[a].x - mx, G.nodes[a].y - my) ? i : a), zoomSet[0]);
        const rest = zoomSet.filter((i) => i !== far);
        const rb = span(rest);
        const shrink = Math.max((rb.x1 - rb.x0 + 1) / (b.x1 - b.x0 + 1), (rb.y1 - rb.y0 + 1) / (b.y1 - b.y0 + 1));
        if (shrink > 0.5) break;
        zoomSet = rest;
      }
      const { x0, x1, y0, y1 } = span(zoomSet);
      cx = (x0 + x1) / 2; cy = (y0 + y1) / 2;
      // 여백 1.5배 + 이름표 자리, 너무 가까이 가지 않게(MAX_ZOOM), 화면 비율 유지
      let w = Math.max((x1 - x0) * 1.5 + 140, ((y1 - y0) * 1.5 + 130) * (W / H), W / MAX_ZOOM);
      w = Math.min(w, W);
      const h = w * (H / W);
      goal = { x: cx - w / 2, y: cy - h / 2, w, h };
    }
    return { targets, qset, near: nb.near, edges: nb.edges, full, goal, center: { x: cx, y: cy }, nEvent: f.event.length, nQuestion: qs.length };
  }, [question, playbook]);

  const reduce = useMemo(() => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false, []);
  const [phase, setPhase] = useState<Phase>(reduce ? "focus" : "scan");
  const [vb, setVb] = useState<VB>(reduce ? plan.goal : plan.full);
  const planRef = useRef(plan);
  planRef.current = plan;
  const vbRef = useRef(vb);
  vbRef.current = vb;
  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  const rafRef = useRef(0);

  // 지금 보이는 곳에서 목표(최신 plan.goal)까지 rAF 보간 → 끝나면 focus
  const zoomTo = (ms: number) => {
    cancelAnimationFrame(rafRef.current);
    setPhase("zoom");
    const t0 = performance.now();
    const a = { ...vbRef.current };
    const step = (now: number) => {
      const b = planRef.current.goal;
      const t = Math.min(1, (now - t0) / ms);
      const e = ease(t);
      setVb({ x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e, w: a.w + (b.w - a.w) * e, h: a.h + (b.h - a.h) * e });
      if (t < 1) rafRef.current = requestAnimationFrame(step);
      else setPhase("focus");
    };
    rafRef.current = requestAnimationFrame(step);
  };

  // 타임라인(마운트 때 한 번): scan → lit → zoom → focus
  useEffect(() => {
    if (reduce) return;
    const timers: number[] = [];
    timers.push(window.setTimeout(() => setPhase("lit"), SCAN_MS));
    const zoomAt = SCAN_MS + Math.max(1, planRef.current.targets.length) * LIGHT_GAP + ZOOM_DELAY;
    timers.push(window.setTimeout(() => zoomTo(ZOOM_MS), zoomAt));
    return () => {
      timers.forEach(clearTimeout);
      cancelAnimationFrame(rafRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reduce]);

  // 플레이북이 뒤늦게 도착해 켤 노드가 바뀌면: 이미 확대 중/확대 뒤라면 새 목표로 이어서 이동
  const firstPlan = useRef(true);
  useEffect(() => {
    if (firstPlan.current) {
      firstPlan.current = false;
      return;
    }
    if (reduce) {
      setVb(plan.goal);
      return;
    }
    if (phaseRef.current === "zoom" || phaseRef.current === "focus") zoomTo(700);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan]);

  const z = W / vb.w; // 확대 배율
  const upp = vb.w / svgPx; // 1px 가 몇 단위인지 — 점·글자 크기를 화면 px 기준으로 맞춘다
  const zq = Math.round(z * 6) / 6; // 배경은 배율을 띄엄띄엄 반영해 다시 그리는 횟수를 줄인다
  const lit = phase !== "scan";
  const focus = phase === "focus";

  const caption =
    phase === "scan"
      ? `절차 그래프 ${G.nodes.length.toLocaleString()}개 노드를 훑는 중`
      : phase === "lit"
        ? plan.nEvent > 0
          ? `${eventTitle ? eventTitle + " 관련 " : ""}절차 ${plan.nEvent}개 발견${plan.nQuestion > 0 ? ` · 질문과 닿는 절차 ${plan.nQuestion}개` : ""}`
          : plan.nQuestion > 0
            ? `질문과 닿는 절차 ${plan.nQuestion}개 발견${pending ? " · 생활 이벤트 파악 중" : ""}`
            : pending
              ? "생활 이벤트를 파악하고 절차를 모으는 중"
              : "관련 절차를 찾는 중"
        : pending
          ? "생활 이벤트에 맞는 절차를 모으는 중"
          : "선후 관계를 확인하고 답을 준비하는 중";

  const targetR = 5.5 * upp;
  const nearR = 2.4 * upp;
  const fontPx = 10.5 * upp;
  const labels = useMemo(() => (focus ? placeLabels(plan.targets, vb, upp, 5.5, 10.5) : []), [focus, plan, vb, upp]);

  return (
    <div className="flex justify-start gap-2 gs-enter">
      <img src={daramiAvatar} alt="다람이" className="w-7 h-7 rounded-full object-cover shrink-0 mt-0.5 border border-sand bg-white" />
      <div className="w-[84%] max-w-[320px] bg-white border border-sand rounded-2xl rounded-bl-md p-2 pb-2.5">
        <svg
          ref={svgRef}
          viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
          className="block w-full rounded-xl bg-cream/70 pointer-events-none"
          style={{ aspectRatio: `${W} / ${H}`, ["--gs-flow" as string]: `${-22 * upp}` }}
          role="img"
          aria-label="절차 그래프에서 관련 절차를 찾는 중"
        >
          <Background upp={(W / zq) / svgPx} />

          {/* 탐색 파문: 켜질 자리(강조 노드들의 중심)에서 퍼진다 */}
          {!focus && phase !== "zoom" && (
            <g className="gs-sonar" style={{ opacity: 1 }}>
              {[0, 1].map((k) => (
                <circle key={k} cx={plan.center.x} cy={plan.center.y} r={34 * upp} fill="none" stroke={C.nut} strokeWidth={1.4} vectorEffect="non-scaling-stroke" className="gs-sonar-ring" style={{ animationDelay: `${k * 650}ms` }} />
              ))}
            </g>
          )}

          {/* 강조 노드 주변 간선: 노드 사이(core)는 진하게·흐름, 1홉 이웃으로는 옅게 */}
          {lit &&
            plan.edges.map((e) => {
              const a = G.nodes[e.s], b = G.nodes[e.t];
              return (
                <line
                  key={`${e.s}-${e.t}`}
                  x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                  stroke={e.core ? C.nutDeep : C.acorn}
                  strokeOpacity={e.core ? 0.9 : focus ? 0.35 : 0.2}
                  strokeWidth={e.core ? 1.8 : 1}
                  vectorEffect="non-scaling-stroke"
                  strokeDasharray={e.core && focus ? `${8 * upp} ${14 * upp}` : e.hard ? undefined : `${3 * upp} ${3 * upp}`}
                  className={e.core && focus ? "gs-flow" : undefined}
                  style={{ transition: "stroke-opacity 300ms ease" }}
                />
              );
            })}

          {/* 1홉 이웃 — 확대 뒤에만 보이는 작은 점 */}
          {focus && plan.near.map((i) => <circle key={i} cx={G.nodes[i].x} cy={G.nodes[i].y} r={nearR} fill={C.cream} stroke={C.acorn} strokeOpacity={0.55} strokeWidth={1.2} vectorEffect="non-scaling-stroke" className="gs-fade-in" />)}

          {/* 강조 노드: 하나씩 톡 켜지고 고리가 맥동한다. 질문에 닿은 노드는 진한 주황 */}
          {lit &&
            plan.targets.map((i, k) => {
              const n = G.nodes[i];
              const isQ = plan.qset.has(i);
              const delay = reduce ? 0 : k * LIGHT_GAP;
              return (
                <g key={i} transform={`translate(${n.x},${n.y})`}>
                  <circle r={targetR * 1.9} fill="none" stroke={isQ ? C.nutDeep : C.nut} strokeWidth={1.5} vectorEffect="non-scaling-stroke" className="gs-ring" style={{ animationDelay: `${delay + 350}ms` }} />
                  <circle r={targetR} fill={isQ ? C.nutDeep : C.nut} stroke="#fff" strokeWidth={2} vectorEffect="non-scaling-stroke" className="gs-pop" style={{ animationDelay: `${delay}ms` }} />
                </g>
              );
            })}

          {/* 이름표 — 확대가 끝난 뒤 */}
          {labels.map((l, k) => (
                <text
                  key={l.i}
                  x={l.x}
                  y={l.y}
                  textAnchor={l.anchor}
                  dominantBaseline="central"
                  fontSize={fontPx}
                  fontWeight={700}
                  fill={C.acorn}
                  stroke={C.cream}
                  strokeWidth={3 * upp}
                  paintOrder="stroke"
                  className="gs-fade-in"
                  style={{ animationDelay: `${k * 60}ms` }}
                >
                  {l.text}
                </text>
          ))}
        </svg>

        <div className="mt-1.5 px-1 flex items-center gap-1.5 text-[10.5px] text-acorn/60">
          <span className="truncate">{caption}</span>
          <span className="flex items-center gap-[3px] shrink-0">
            <span className="typing-dot w-1 h-1 bg-acorn/40 rounded-full" />
            <span className="typing-dot w-1 h-1 bg-acorn/40 rounded-full" />
            <span className="typing-dot w-1 h-1 bg-acorn/40 rounded-full" />
          </span>
        </div>
      </div>
    </div>
  );
}

/** 그래프 전체(간선 한 path + 종류별 점 path 셋). 배율이 띄엄띄엄 바뀔 때만 다시 그린다 */
const Background = memo(function Background({ upp }: { upp: number }) {
  const edges = useMemo(() => G.links.map(([s, t]) => `M${G.nodes[s].x},${G.nodes[s].y}L${G.nodes[t].x},${G.nodes[t].y}`).join(""), []);
  const dots = useMemo(() => {
    const by: Record<"law" | "svc" | "x", string[]> = { law: [], svc: [], x: [] };
    for (const n of G.nodes) {
      const r = (1.1 + Math.min(1.4, n.d * 0.12)) * upp;
      by[n.t].push(`M${n.x - r},${n.y}a${r},${r} 0 1,0 ${2 * r},0a${r},${r} 0 1,0 ${-2 * r},0`);
    }
    return by;
  }, [upp]);
  return (
    <g className="gs-bg">
      <path d={edges} fill="none" stroke={C.acorn} strokeOpacity={0.16} strokeWidth={0.7} vectorEffect="non-scaling-stroke" />
      <path d={dots.law.join("")} fill={C.law} />
      <path d={dots.svc.join("")} fill={C.svc} />
      <path d={dots.x.join("")} fill={C.x} />
    </g>
  );
});
