import { useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from "react";
import { useStore } from "../store";
import { ddayLabel, dueLabel, missingHint } from "../lib/dday";
import { forceLayout, edgePath, NODE_R, type Pt } from "../lib/forceLayout";
import type { BoardGraph, GraphNode } from "../lib/graph";

/* 테마 색(tailwind.config 와 동일) — SVG 속성에 직접 쓴다 */
const C = {
  acorn: "#6B4226",
  nut: "#E8862E",
  nutDeep: "#D2691E",
  leaf: "#7BA05B",
  sand: "#F3E7D3",
  muted: "rgba(107,66,38,0.32)",
};

const CLOSE_DELAY = 180; // 마우스가 잠깐 벗어나도 바로 닫히지 않게
const TWEEN_MS = 450; // 노드가 새 자리로 미끄러지는 시간

const STATE_LABEL: Record<GraphNode["state"], { text: string; cls: string }> = {
  done: { text: "완료", cls: "bg-leaf/15 text-leaf" },
  ready: { text: "지금 할 수 있어요", cls: "bg-nut/15 text-nut-deep" },
  blocked: { text: "선행 대기", cls: "bg-acorn/10 text-acorn/60" },
};
const PRIORITY_LABEL = { must: "필수", nice: "권장", qna: "참고" } as const;
const VERIFY_LABEL: Record<string, string> = {
  verified: "🛡️ 공식 출처 검증됨",
  partial: "△ 근거 일부 확인",
  needs_review: "⚠️ 확인 필요",
  unverified: "ℹ️ AI 생성 · 자동 검증 전",
};

type Tip = { id: string; top?: number; bottom?: number; left: number };

/**
 * 할 일 화면 오른쪽 아래의 동그란 버튼. 마우스를 올리면(터치는 탭) 버튼에서 원이 퍼지듯 절차 그래프 팝업이 열린다.
 * - 클릭하면 열린 채로 고정(다시 클릭·바깥 탭·Esc 로 닫힘), 마우스만 올렸으면 벗어날 때 닫힘.
 * - 그래프는 큰 타원 안의 방향 그래프(힘 기반 배치, lib/forceLayout). 노드 번호 = 추천 순서, 화살표 = 선행 → 후속.
 * - 노드에 올리면 이름·상태·기한·기관·선행/후속·검증 툴팁과 함께 이어진 간선이 강조되고, 누르면 절차 상세 시트.
 */
export default function GraphPopover({ graph }: { graph: BoardGraph }) {
  const openProc = useStore((s) => s.openProc);
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const open = hover || pinned;
  const closeTimer = useRef<number | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const fabRef = useRef<HTMLButtonElement>(null);
  const areaRef = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<Tip | null>(null);
  const [areaW, setAreaW] = useState(300);

  const readyCount = graph.nodes.filter((n) => n.state === "ready").length;
  const doneCount = graph.nodes.filter((n) => n.state === "done").length;
  const N = graph.nodes.length;
  const areaH = Math.round(Math.max(200, Math.min(340, 130 + N * 13)));

  useLayoutEffect(() => {
    const el = areaRef.current;
    if (!el) return;
    const measure = () => setAreaW(Math.max(220, el.clientWidth));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 배치는 구조(노드·간선)와 크기가 바뀔 때만 다시 계산 — 완료 체크만으로는 그림이 움직이지 않는다
  const sig = useMemo(
    () => `${areaW}x${areaH}|${graph.nodes.map((n) => `${n.id}:${n.layer}`).join(",")}|${graph.edges.map((e) => `${e.from}>${e.to}`).join(",")}`,
    [graph, areaW, areaH]
  );
  const last = useRef<{ size: string; pos: Record<string, Pt> } | null>(null);
  const layout = useMemo(() => {
    // 이전 위치에서 이어 풀기는 크기가 같을 때만 — 폭을 재기 전 기본값 배치가 출발점이 되면 같은 보드도 그림이 달라진다
    const size = `${areaW}x${areaH}`;
    const l = forceLayout(graph, areaW, areaH, last.current?.size === size ? last.current.pos : undefined);
    last.current = { size, pos: l.pos };
    return l;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  // 새 배치로 부드럽게 이동(이전 표시 위치 → 새 위치 보간)
  const [shown, setShown] = useState<Record<string, Pt>>(layout.pos);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  useEffect(() => {
    const from = shownRef.current;
    const to = layout.pos;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !Object.keys(from).length) {
      setShown(to);
      return;
    }
    let raf = 0;
    const t0 = performance.now();
    const ease = (t: number) => 1 - Math.pow(1 - t, 3);
    const step = (now: number) => {
      const t = Math.min(1, (now - t0) / TWEEN_MS);
      const e = ease(t);
      const next: Record<string, Pt> = {};
      for (const id of Object.keys(to)) {
        const a = from[id] ?? to[id];
        next[id] = { x: a.x + (to[id].x - a.x) * e, y: a.y + (to[id].y - a.y) * e };
      }
      setShown(next);
      if (t < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [layout]);

  const cancelClose = () => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };
  const enter = (e: RPointerEvent) => {
    if (e.pointerType !== "mouse") return; // 터치는 탭(click)으로만 연다
    cancelClose();
    setHover(true);
  };
  const leave = (e: RPointerEvent) => {
    if (e.pointerType !== "mouse") return;
    cancelClose();
    closeTimer.current = window.setTimeout(() => {
      setHover(false);
      setTip(null);
    }, CLOSE_DELAY);
  };
  const close = () => {
    cancelClose();
    setHover(false);
    setPinned(false);
    setTip(null);
  };

  // 열려 있을 때 바깥을 누르거나 Esc → 닫기
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || fabRef.current?.contains(t)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  useEffect(() => () => cancelClose(), []);

  const showTip = (id: string, el: Element) => {
    const panel = panelRef.current;
    if (!panel) return;
    const pr = panel.getBoundingClientRect();
    const nr = el.getBoundingClientRect();
    const left = Math.max(8, Math.min(nr.left + nr.width / 2 - pr.left - 116, pr.width - 232 - 8));
    if (pr.bottom - nr.bottom > 190) setTip({ id, top: nr.bottom - pr.top + 6, left });
    else setTip({ id, bottom: pr.bottom - nr.top + 6, left });
  };

  const hovered = tip?.id ?? null;
  const near = useMemo(() => {
    if (!hovered) return null;
    const s = new Set([hovered]);
    for (const e of graph.edges) {
      if (e.from === hovered) s.add(e.to);
      if (e.to === hovered) s.add(e.from);
    }
    return s;
  }, [hovered, graph]);
  const hasSoft = graph.edges.some((e) => e.soft);

  return (
    <>
      {/* 팝업 패널: 오른쪽 아래 모서리가 버튼과 겹치고, 버튼 중심에서 원이 퍼지며 열린다 */}
      <div
        ref={panelRef}
        role="dialog"
        aria-label="할 일 그래프"
        aria-hidden={!open}
        data-open={open}
        onPointerEnter={enter}
        onPointerLeave={leave}
        className="graph-pop absolute right-4 bottom-4 z-30 w-[min(340px,calc(100%-32px))] max-h-[calc(100%-32px)] flex flex-col bg-cream rounded-[22px] border border-sand shadow-[0_12px_40px_rgba(74,47,27,0.22)]"
      >
        <div className="flex items-baseline justify-between px-4 pt-3 pb-1.5 border-b border-sand/80">
          <span className="text-[13px] font-bold text-acorn">할 일 그래프</span>
          <span className="text-[10.5px] text-acorn/50">
            {doneCount}/{N} 완료 · 지금 가능 {readyCount}
          </span>
        </div>

        <div ref={areaRef} className="relative flex-1 min-h-0 overflow-y-auto no-scrollbar">
          <svg width={areaW} height={areaH} className="block" role="img" aria-label="절차 선후 관계 그래프">
            <defs>
              {(["done", "todo", "hot"] as const).map((k) => (
                <marker key={k} id={`gf-arrow-${k}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5.5" markerHeight="5.5" orient="auto">
                  <path d="M0,0 L8,4 L0,8 z" fill={k === "done" ? C.leaf : k === "hot" ? C.nutDeep : "rgba(107,66,38,0.5)"} />
                </marker>
              ))}
            </defs>


            {/* 간선: 선행 → 후속 */}
            {graph.edges.map((e, i) => {
              const a = shown[e.from], b = shown[e.to];
              if (!a || !b) return null;
              const others = graph.nodes.filter((n) => n.id !== e.from && n.id !== e.to).map((n) => shown[n.id]).filter(Boolean) as Pt[];
              const hot = hovered !== null && (e.from === hovered || e.to === hovered);
              const dim = hovered !== null && !hot;
              const color = hot ? C.nutDeep : e.satisfied ? C.leaf : C.muted;
              return (
                <path
                  key={`${e.from}>${e.to}`}
                  d={edgePath(a, b, others)}
                  fill="none"
                  stroke={color}
                  strokeWidth={hot ? 2.2 : 1.6}
                  strokeDasharray={e.soft ? "3 3.5" : undefined}
                  markerEnd={`url(#gf-arrow-${hot ? "hot" : e.satisfied ? "done" : "todo"})`}
                  className="gp-fade"
                  style={{
                    opacity: dim ? 0.25 : undefined,
                    transition: "opacity 160ms ease, stroke 160ms ease",
                    transitionDelay: open && !hovered ? `${200 + i * 30}ms` : "0ms",
                  }}
                />
              );
            })}

            {/* 노드: 번호 = 추천 순서. 이름은 툴팁으로 */}
            {graph.nodes.map((n) => {
              const p = shown[n.id];
              if (!p) return null;
              const dim = near !== null && !near.has(n.id);
              return (
                <g key={n.id} transform={`translate(${p.x},${p.y})`} style={{ opacity: dim ? 0.35 : 1, transition: "opacity 160ms ease" }}>
                  <g
                    role="button"
                    tabIndex={open ? 0 : -1}
                    aria-label={`${n.order}. ${n.p.name} (${STATE_LABEL[n.state].text})`}
                    className="gp-node cursor-pointer outline-none"
                    style={{ transitionDelay: open ? `${90 + n.order * 35}ms` : "0ms" }}
                    onPointerEnter={(ev) => showTip(n.id, ev.currentTarget)}
                    onPointerLeave={() => setTip((t) => (t?.id === n.id ? null : t))}
                    onFocus={(ev) => showTip(n.id, ev.currentTarget)}
                    onBlur={() => setTip((t) => (t?.id === n.id ? null : t))}
                    onClick={() => {
                      close();
                      openProc(n.id);
                    }}
                    onKeyDown={(ev) => {
                      if (ev.key === "Enter" || ev.key === " ") {
                        ev.preventDefault();
                        close();
                        openProc(n.id);
                      }
                    }}
                  >
                    <NodeDot n={n} hot={hovered === n.id} />
                  </g>
                </g>
              );
            })}
          </svg>

          {!graph.edges.length && (
            <p className="absolute left-0 right-0 bottom-2 text-center text-[10.5px] text-acorn/50">
              아직 순서가 정해진 절차가 없어요. 어떤 순서로 해도 괜찮아요.
            </p>
          )}
        </div>

        {/* 범례 — 오른쪽은 버튼 자리 */}
        <div className="flex items-center gap-x-2.5 gap-y-0.5 flex-wrap pl-4 pr-[64px] h-[56px] border-t border-sand/80 text-[10px] text-acorn/55">
          <LegendDot fill={C.leaf} stroke={C.leaf} label="완료" />
          <LegendDot fill="#fff" stroke={C.nut} label="지금 가능" />
          <LegendDot fill={C.sand} stroke="rgba(107,66,38,0.35)" dashed label="대기" />
          <span className="inline-flex items-center gap-1">
            <svg width="18" height="8" aria-hidden>
              <line x1="0" y1="4" x2="13" y2="4" stroke="rgba(107,66,38,0.5)" strokeWidth="1.4" />
              <path d="M12,1 L17,4 L12,7 z" fill="rgba(107,66,38,0.5)" />
            </svg>
            선행 → 후속
          </span>
          {hasSoft && (
            <span className="inline-flex items-center gap-1">
              <svg width="16" height="8" aria-hidden>
                <line x1="0" y1="4" x2="16" y2="4" stroke="rgba(107,66,38,0.5)" strokeWidth="1.4" strokeDasharray="2 3" />
              </svg>
              권고
            </span>
          )}
          <span>번호 = 추천 순서</span>
        </div>

        {/* 노드 툴팁 */}
        {tip && graph.byId[tip.id] && <NodeTooltip tip={tip} graph={graph} />}
      </div>

      {/* 동그란 버튼 */}
      <button
        ref={fabRef}
        type="button"
        aria-label={pinned ? "할 일 그래프 닫기" : "할 일 그래프 보기"}
        aria-expanded={open}
        onPointerEnter={enter}
        onPointerLeave={leave}
        onClick={() => {
          cancelClose();
          if (pinned) close();
          else setPinned(true);
        }}
        className={`absolute right-4 bottom-4 z-30 w-[52px] h-[52px] rounded-full flex items-center justify-center border transition-all duration-300 ${
          open
            ? "bg-nut border-nut text-white shadow-[0_6px_18px_rgba(232,134,46,0.45)]"
            : "bg-white border-sand text-nut-deep shadow-[0_6px_18px_rgba(74,47,27,0.18)] hover:scale-105"
        }`}
      >
        <span className={`transition-transform duration-300 ${pinned ? "rotate-90" : ""}`}>
          {pinned ? <CloseIcon /> : <GraphIcon />}
        </span>
        {!open && readyCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-nut-deep text-white text-[10px] font-bold flex items-center justify-center border-2 border-cream">
            {readyCount}
          </span>
        )}
      </button>
    </>
  );
}

function NodeDot({ n, hot }: { n: GraphNode; hot: boolean }) {
  const r = NODE_R;
  const fill = n.state === "done" ? C.leaf : n.state === "ready" ? "#FFFFFF" : C.sand;
  const stroke = n.state === "done" ? C.leaf : n.state === "ready" ? C.nut : "rgba(107,66,38,0.38)";
  const text = n.state === "done" ? "#FFFFFF" : n.state === "ready" ? C.nutDeep : "rgba(107,66,38,0.55)";
  return (
    <>
      {/* 호버 시 바깥 고리 */}
      <circle r={r + 5} fill="none" stroke={C.nut} strokeOpacity={hot ? 0.35 : 0} strokeWidth={3} style={{ transition: "stroke-opacity 150ms ease" }} />
      <circle
        r={r}
        fill={fill}
        stroke={stroke}
        strokeWidth={n.state === "ready" ? 2.2 : 1.6}
        strokeDasharray={n.state === "blocked" ? "2.5 2" : undefined}
        style={{ filter: hot ? "drop-shadow(0 2px 4px rgba(74,47,27,0.25))" : undefined }}
      />
      <text textAnchor="middle" dominantBaseline="central" fontSize={n.state === "done" ? 12 : 10.5} fontWeight={700} fill={text} style={{ pointerEvents: "none", userSelect: "none" }}>
        {n.state === "done" ? "✓" : n.order}
      </text>
    </>
  );
}

function NodeTooltip({ tip, graph }: { tip: Tip; graph: BoardGraph }) {
  const n = graph.byId[tip.id];
  const p = n.p;
  const name = (id: string) => graph.byId[id]?.p.name ?? id;
  const st = STATE_LABEL[n.state];
  const dueText =
    n.state === "done"
      ? p.deadline
      : n.deadline.due
        ? `${dueLabel(n.deadline)} · ${ddayLabel(n.deadline.days as number)}`
        : n.deadline.missing
          ? missingHint(n.deadline)
          : p.deadline;
  return (
    <div
      key={n.id}
      role="tooltip"
      className="gp-tip pointer-events-none absolute z-10 w-[232px] bg-white rounded-xl border border-sand shadow-[0_8px_24px_rgba(74,47,27,0.18)] px-3 py-2.5 text-[11px] text-acorn/75"
      style={{ left: tip.left, top: tip.top, bottom: tip.bottom }}
    >
      <div className="flex items-start gap-1.5">
        <span className="shrink-0 mt-[1px] w-[18px] h-[18px] rounded-full bg-sand text-[10px] font-bold text-acorn/70 flex items-center justify-center">{n.order}</span>
        <span className="font-bold text-[12.5px] text-acorn leading-snug">{p.name}</span>
      </div>
      <div className="flex items-center gap-1.5 mt-1 mb-1.5">
        <span className={`rounded-md px-1.5 py-0.5 text-[10px] font-bold ${st.cls}`}>{st.text}</span>
        <span className="text-[10px] text-acorn/50">{PRIORITY_LABEL[p.priority]}</span>
      </div>
      <dl className="grid grid-cols-[34px_1fr] gap-x-1.5 gap-y-0.5">
        {dueText && (
          <>
            <dt className="text-acorn/45">기한</dt>
            <dd className={n.deadline.days !== null && n.deadline.days < 0 && n.state !== "done" ? "text-red-500 font-semibold" : ""} title={n.deadline.basis}>{dueText}</dd>
          </>
        )}
        {p.agency && (
          <>
            <dt className="text-acorn/45">기관</dt>
            <dd>
              {p.agency}
              {p.online && <span className="ml-1 text-leaf">· 온라인 가능</span>}
            </dd>
          </>
        )}
        {n.parents.length > 0 && (
          <>
            <dt className="text-acorn/45">먼저</dt>
            <dd>{n.parents.map(name).join(", ")}</dd>
          </>
        )}
        {n.softParents.length > 0 && (
          <>
            <dt className="text-acorn/45">권고</dt>
            <dd>{n.softParents.map(name).join(", ")} 뒤에</dd>
          </>
        )}
        {n.children.length > 0 && (
          <>
            <dt className="text-acorn/45">다음</dt>
            <dd>{n.children.map(name).join(", ")}</dd>
          </>
        )}
      </dl>
      {p.verification_status && <div className="mt-1.5 text-[10px] text-acorn/50">{VERIFY_LABEL[p.verification_status] ?? ""}</div>}
      <div className="mt-1 text-[10px] text-acorn/35">누르면 서류·바로가기 상세</div>
    </div>
  );
}

function LegendDot({ fill, stroke, label, dashed }: { fill: string; stroke: string; label: string; dashed?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1">
      <svg width="10" height="10" aria-hidden>
        <circle cx="5" cy="5" r="4" fill={fill} stroke={stroke} strokeWidth="1.5" strokeDasharray={dashed ? "1.5 1.5" : undefined} />
      </svg>
      {label}
    </span>
  );
}

/** 작은 방향 그래프 그림(노드 셋, 화살표 둘) */
function GraphIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M7.5 7.5 L15.2 10.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M7 9 L7 15.2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M13.6 9.2 L15.6 10.8 L13.2 11.8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5.6 13.8 L7 15.6 L8.4 13.8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="6" cy="6" r="2.6" fill="currentColor" />
      <circle cx="18" cy="11.5" r="2.6" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="7" cy="18.5" r="2.6" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden>
      <path d="M4 4 L14 14 M14 4 L4 14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
