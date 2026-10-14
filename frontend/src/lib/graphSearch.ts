import type { Playbook } from "../types";

/**
 * 채팅 첫 턴의 '그래프 탐색 애니메이션'(components/GraphSearchBubble) 이 쓰는 매칭 로직.
 * 절차 그래프 전체(data/procgraph.json, 808 노드) 안에서
 *   - 이벤트 절차: 플레이북 절차를 그래프 노드에 대응(키 → 이름 → 가장 긴 공통 부분문자열)
 *   - 질문 절차: 사용자 질문 문장과 이름이 겹치는 노드(공통 부분문자열 ≥ 3자, 일반어 제외)
 * 를 골라 강조·확대 대상으로 돌려준다. 실제 답변의 근거가 아니라 '어디를 보고 있는지' 보여주는 용도라
 * 결정적이고 가벼운 문자열 매칭만 쓴다.
 */
export interface PGNode {
  k: string; // 그래프 노드 키(common:/law:/svc:/x:)
  n: string; // 절차 이름
  t: "law" | "svc" | "x";
  x: number;
  y: number;
  d: number; // 차수(연결 수)
}
export interface ProcGraph {
  w: number;
  h: number;
  nodes: PGNode[];
  links: [number, number, 0 | 1][]; // [선행, 후속, 필수 여부]
}

export interface Focus {
  /** 플레이북(이벤트) 절차에 대응한 노드 인덱스 — 플레이북 순서 */
  event: number[];
  /** 질문 문장과 닿는 노드 인덱스(이벤트 절차 제외) — 점수 순 */
  question: number[];
}

/** 두 문자열의 가장 긴 공통 부분문자열 (길이, 문자열) */
export function longestCommon(a: string, b: string): { len: number; text: string } {
  if (!a || !b) return { len: 0, text: "" };
  let best = 0, end = 0;
  let prev = new Array<number>(b.length + 1).fill(0);
  for (let i = 1; i <= a.length; i++) {
    const cur = new Array<number>(b.length + 1).fill(0);
    for (let j = 1; j <= b.length; j++) {
      if (a[i - 1] === b[j - 1]) {
        cur[j] = prev[j - 1] + 1;
        if (cur[j] > best) { best = cur[j]; end = i; }
      }
    }
    prev = cur;
  }
  return { len: best, text: a.slice(end - best, end) };
}

/** 비교 전에 공백·괄호·구두점을 걷어낸다 (이름 표기 차이 흡수) */
export const squash = (s: string) => s.replace(/[\s·()\[\]/,.\-·…"'「」]/g, "");

/** 겹쳐도 의미 없는 일반어·지역명 — 이것만 겹치면 매칭으로 치지 않는다 */
const GENERIC = new Set([
  "지원금", "지원사업", "지원", "신청", "신고", "변경", "확인", "수령", "서비스", "사업", "제도", "급여", "자격",
  "가입", "발급", "등록", "접수", "지급", "안내", "상담", "검사", "교육", "센터", "바우처", "수당", "보험", "건강",
  "아동", "가구", "가족", "청년", "노인", "장애인", "여성", "학생", "국민", "구청", "시청", "정부", "중앙정부",
  "서울", "서울시", "서울특별시", "부산시", "대구시", "인천시", "광주시", "대전시", "울산시", "세종시", "경기도", "강원도", "제주도",
]);

/** 이름을 토큰으로(공백·괄호·가운뎃점 기준), 일반어는 뺀다 */
const tokens = (s: string) => s.split(/[\s·()\[\]/,.\-…"'「」]+/).filter((t) => t.length >= 2 && !GENERIC.has(t));

/**
 * 플레이북 절차 이름 → 그래프 노드. 절차 이름의 토큰(일반어 제외)이 노드 이름에 얼마나 들어 있는지로 본다.
 * "실업급여(구직급여) 신청" ↔ "구직급여(실업급여) 신청", "양육수당" ↔ "가정양육수당 지원" 은 붙고,
 * "초등학교 전학" ↔ "중학교 전학 배정" 은 (초등학교 가 없어) 붙지 않는다.
 */
function matchProcedure(nodes: PGNode[], name: string, exclude: Set<number>): number | undefined {
  const toks = tokens(name);
  if (toks.length === 0) return undefined;
  let best: { i: number; hit: number; extra: number; d: number } | null = null;
  nodes.forEach((n, i) => {
    if (exclude.has(i)) return;
    const hit = toks.filter((t) => n.n.includes(t)).length;
    if (hit / toks.length < 0.6) return;
    const extra = squash(n.n).length - toks.join("").length; // 노드 이름에 더 붙은 글자 수(짧게 맞는 쪽 선호)
    if (!best || hit > best.hit || (hit === best.hit && (Math.abs(extra) < Math.abs(best.extra) || (Math.abs(extra) === Math.abs(best.extra) && n.d > best.d)))) {
      best = { i, hit, extra, d: n.d };
    }
  });
  return best ? (best as { i: number }).i : undefined;
}

/** 질문 문장과 이름이 겹치는 노드. 공통 부분문자열 ≥ minLen(일반어 제외), 그래프 안쪽(연결된) 노드 우선 */
function matchQuestion(nodes: PGNode[], question: string, exclude: Set<number>, max: number): number[] {
  const q = squash(question);
  const out: { i: number; score: number }[] = [];
  nodes.forEach((n, i) => {
    if (exclude.has(i)) return;
    const { len, text } = longestCommon(q, squash(n.n));
    // 미검증(x)·서비스 노드는 더 길게 겹쳐야 한다(이름이 길고 일반어가 많아 우연히 잘 겹친다)
    if (len < (n.t === "law" ? 3 : 4) || GENERIC.has(text)) return;
    out.push({ i, score: len });
  });
  out.sort(
    (a, b) =>
      (nodes[b.i].d > 0 ? 1 : 0) - (nodes[a.i].d > 0 ? 1 : 0) || // 연결된 노드 먼저
      b.score - a.score ||
      (nodes[a.i].t === "x" ? 1 : 0) - (nodes[b.i].t === "x" ? 1 : 0) ||
      nodes[b.i].d - nodes[a.i].d
  );
  return out.slice(0, max).map((c) => c.i);
}

export function findFocus(g: ProcGraph, question: string, playbook: Playbook | null, maxQuestion = 2): Focus {
  const byKey = new Map<string, number>();
  const byName = new Map<string, number>();
  g.nodes.forEach((n, i) => {
    byKey.set(n.k, i);
    if (!byName.has(n.n)) byName.set(n.n, i);
  });

  const event: number[] = [];
  const taken = new Set<number>();
  for (const p of playbook?.procedures ?? []) {
    let i: number | undefined = p.key ? byKey.get(p.key) : undefined;
    if (i === undefined) i = byName.get(p.name);
    if (i === undefined) i = matchProcedure(g.nodes, p.name, taken);
    if (i !== undefined && !taken.has(i)) {
      taken.add(i);
      event.push(i);
    }
  }
  return { event, question: matchQuestion(g.nodes, question, taken, maxQuestion) };
}

/** 강조 노드들과 그 1홉 이웃, 그 사이 간선 */
export function neighborhood(g: ProcGraph, focus: number[]) {
  const core = new Set(focus);
  const near = new Set<number>();
  const edges: { s: number; t: number; hard: boolean; core: boolean }[] = [];
  for (const [s, t, h] of g.links) {
    const cs = core.has(s), ct = core.has(t);
    if (!cs && !ct) continue;
    if (!cs) near.add(s);
    if (!ct) near.add(t);
    edges.push({ s, t, hard: h === 1, core: cs && ct });
  }
  return { near: [...near], edges };
}
