import type { BoardItem, DeadlineRule, EventKey, Procedure, UserPersona } from "../types";
import { HOLIDAY_YEARS, holidayName, isNonBusinessDay } from "./holidays";

/**
 * 절차 기한 계산.
 * 백엔드(sources/deadline.py)가 기한 문구를 구조화 규칙 deadlineRule 로 바꿔 보내면, 여기서 민법식으로 평가한다.
 *   - 초일 불산입(§157): 기준일 다음 날부터 센다 → "출생 후 30일" = 출생일 + 30일
 *   - 주·월·년은 역(달력)으로 계산(§160): "1개월"은 30일이 아니라 다음 달 같은 날(없으면 말일)
 *   - 말일이 토요일·공휴일이면 다음 날로(§161) — lib/holidays
 *   - "다음 달 15일까지", "다음 해 5월 31일까지" 같은 고정일, "전입신고 완료 후 N일" 같은 선행 절차 기준도 지원
 * 규칙이 없으면 옛 방식(기준일 + deadlineDays)으로, 기준일을 모르면 '무엇을 알려 주면 되는지'를 돌려준다.
 */

/** 이벤트별 기본 기준일(anchor) key. 규칙의 from 이 "event" 거나 절차에 anchor 가 없으면 이걸 쓴다. */
const DEFAULT_ANCHOR: Record<string, string> = {
  move: "move_date",
  birth: "birth_date",
  resignation: "resignation_date",
  marriage: "marriage_date",
  death: "death_date",
};
export const ANCHOR_LABEL: Record<string, string> = {
  move_date: "이사일",
  birth_date: "출생일",
  resignation_date: "퇴사일",
  marriage_date: "혼인일",
  death_date: "사망일",
  event_date: "사건 발생일",
};
const UNIT_KO = { day: "일", week: "주", month: "개월", year: "년" } as const;

export interface DeadlineInfo {
  /** 오늘 기준 남은 일수(만료일을 알 때). 0 = 오늘까지, 음수 = 지남 */
  days: number | null;
  due: Date | null;
  /** 기준일(신청 가능 시작일에 가까운 날). 미래면 아직 시작 전 */
  anchor: Date | null;
  /** 사람이 읽는 근거: "출생일 10월 5일 기준 · 1개월 이내" */
  basis: string;
  /** 계산에 필요한데 모르는 것: "출생일", "전입신고 완료" … 없으면 null */
  missing: string | null;
  /** 말일이 휴일이라 다음 날로 넘어갔으면 그 사유 */
  rolled: string | null;
  /** 규칙 없이 옛 방식(deadlineDays)으로 계산했는지 */
  legacy: boolean;
}

/** "YYYY-MM-DD"(앞부분) → 로컬 자정 Date. 형식이 아니면 null. */
export function parseISO(v: unknown): Date | null {
  if (typeof v !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v.trim());
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return isNaN(d.getTime()) ? null : d;
}

export function startOfToday(): Date {
  const t = new Date();
  return new Date(t.getFullYear(), t.getMonth(), t.getDate());
}
const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const lastDayOf = (y: number, m0: number) => new Date(y, m0 + 1, 0).getDate();
/** 역에 의한 월 가산: 같은 날짜, 그 달에 없으면 말일(민법 §160③) */
function addMonthsCivil(d: Date, months: number): Date {
  const y = d.getFullYear(), m0 = d.getMonth() + months;
  const ty = y + Math.floor(m0 / 12), tm = ((m0 % 12) + 12) % 12;
  return new Date(ty, tm, Math.min(d.getDate(), lastDayOf(ty, tm)));
}
/** 말일이 토·일·공휴일이면 다음 날로(§161). 넘어간 사유를 함께 돌려준다 */
function rollForward(d: Date): { date: Date; rolled: string | null } {
  let cur = d, why: string | null = null, guard = 0;
  while (isNonBusinessDay(cur) && guard++ < 10) {
    why = why ?? (cur.getDay() === 6 ? "토요일" : holidayName(cur) ?? "휴일");
    cur = addDays(cur, 1);
  }
  return { date: cur, rolled: why };
}

const diffDays = (a: Date, b: Date) => Math.round((a.getTime() - b.getTime()) / 86400000);

export function fmtDate(d: Date, withYear = false): string {
  const wd = ["일", "월", "화", "수", "목", "금", "토"][d.getDay()];
  const y = withYear || d.getFullYear() !== startOfToday().getFullYear() ? `${d.getFullYear()}년 ` : "";
  return `${y}${d.getMonth() + 1}월 ${d.getDate()}일(${wd})`;
}

function anchorKey(from: string | undefined, event: EventKey | null): string {
  if (from && from !== "event") return from;
  return (event ? DEFAULT_ANCHOR[event] : undefined) ?? "event_date";
}

const NONE: DeadlineInfo = { days: null, due: null, anchor: null, basis: "", missing: null, rolled: null, legacy: false };

/** 선행 절차 기준 규칙에서, 보드의 어느 절차가 그 선행인지 찾는다(이름 포함 또는 키 끝 일치) */
function findBoardProc(proc: string, board: BoardItem[] | undefined, procIndex: Record<string, Procedure> | undefined) {
  if (!board || !procIndex) return null;
  const norm = (s: string) => s.replace(/\s/g, "");
  for (const b of board) {
    const p = procIndex[b.id];
    if (!p) continue;
    if (norm(p.name).includes(norm(proc)) || (p.key && p.key.endsWith(":" + proc))) return { b, p };
  }
  return null;
}

export function evaluateRule(
  rule: DeadlineRule,
  persona: UserPersona | null,
  event: EventKey | null,
  board?: BoardItem[],
  procIndex?: Record<string, Procedure>
): DeadlineInfo {
  const facts = persona?.facts ?? {};
  const today = startOfToday();
  const finish = (due: Date, anchor: Date | null, basis: string, roll = true): DeadlineInfo => {
    const r = roll ? rollForward(due) : { date: due, rolled: null };
    return { days: diffDays(r.date, today), due: r.date, anchor, basis, missing: null, rolled: r.rolled, legacy: false };
  };

  if (rule.kind === "after_procedure") {
    const hit = findBoardProc(rule.proc, board, procIndex);
    const span = `${rule.amount}${UNIT_KO[rule.unit]}`;
    const doneAt = hit?.b.status === "done" ? parseISO(hit.b.doneAt) : null;
    if (!doneAt) {
      return { ...NONE, basis: `${rule.proc} 완료 후 ${span} 이내`, missing: `${rule.proc} 완료` };
    }
    return finish(addSpan(doneAt, rule.amount, rule.unit), doneAt, `${rule.proc} 완료일 ${fmtDate(doneAt)} 기준 · ${span} 이내`);
  }

  if (rule.kind === "annual") {
    if (rule.from === "other" && rule.yearOffset != null) return NONE;
    const key = anchorKey(rule.from, event);
    const base = rule.yearOffset != null ? parseISO(facts[key]) : null;
    if (rule.yearOffset != null && !base) {
      return { ...NONE, basis: `${rule.yearOffset === 1 ? "다음 해" : "해당 연도"} ${rule.month}월 ${rule.day === "last" ? "말일" : rule.day + "일"}까지`, missing: ANCHOR_LABEL[key] ?? key };
    }
    const y = (base ?? today).getFullYear() + (rule.yearOffset ?? 0);
    const mk = (yy: number) => new Date(yy, rule.month - 1, rule.day === "last" ? lastDayOf(yy, rule.month - 1) : rule.day);
    let due = mk(y);
    if (rule.yearOffset == null && due < today) due = mk(y + 1); // 매년 돌아오는 기한은 다음 회차
    return finish(due, base, `${base ? fmtDate(base) + " 기준 · " : ""}${rule.yearOffset === 1 ? "다음 해 " : ""}${rule.month}월 ${rule.day === "last" ? "말일" : rule.day + "일"}까지`);
  }

  // 페르소나로 알 수 없는 기산점(납부기한·취득일 등): 계산하지 않고 원문 기한만 보여 준다
  if (rule.from === "other") return NONE;
  const key = anchorKey(rule.from, event);
  const anchor = parseISO(facts[key]);
  const label = ANCHOR_LABEL[key] ?? key;
  if (!anchor) {
    const span = rule.kind === "relative" ? `${Math.abs(rule.amount)}${UNIT_KO[rule.unit]} ${rule.amount < 0 ? "전까지" : "이내"}` : `다음 달 ${rule.day === "last" ? "말일" : rule.day + "일"}까지`;
    return { ...NONE, basis: `${label} 기준 ${span}`, missing: label };
  }

  if (rule.kind === "month_offset") {
    const first = new Date(anchor.getFullYear(), anchor.getMonth() + rule.months, 1);
    const day = rule.day === "last" ? lastDayOf(first.getFullYear(), first.getMonth()) : Math.min(rule.day, lastDayOf(first.getFullYear(), first.getMonth()));
    const due = new Date(first.getFullYear(), first.getMonth(), day);
    const which = rule.months === 0 ? "그 달" : rule.months === 1 ? "다음 달" : `${rule.months}개월 뒤 달`;
    return finish(due, anchor, `${label} ${fmtDate(anchor)} 기준 · ${which} ${rule.day === "last" ? "말일" : rule.day + "일"}까지`);
  }

  // relative
  if (rule.amount < 0) {
    const due = addSpan(anchor, rule.amount, rule.unit);
    return finish(due, anchor, `${label} ${fmtDate(anchor)} ${Math.abs(rule.amount)}${UNIT_KO[rule.unit]} 전까지`, false);
  }
  // 초일 불산입: 기준일 다음 날부터 세므로 만료일 = 기준일 + 기간 ("다음 날부터"도 같은 결과)
  const due = addSpan(anchor, rule.amount, rule.unit);
  return finish(due, anchor, `${label} ${fmtDate(anchor)} 기준 · ${rule.amount}${UNIT_KO[rule.unit]} 이내`);
}

type Unit = "day" | "week" | "month" | "year";
function addSpan(d: Date, amount: number, unit: Unit): Date {
  if (unit === "day") return addDays(d, amount);
  if (unit === "week") return addDays(d, amount * 7);
  if (unit === "month") return addMonthsCivil(d, amount);
  return addMonthsCivil(d, amount * 12);
}

/**
 * 절차의 기한 정보. 규칙이 있으면 규칙으로, 없으면 옛 방식(기준일 + deadlineDays).
 * 기한이 아예 없는 절차(deadlineDays ≥ 3650, 규칙 없음)는 days=null·basis="" 로 돌아온다.
 */
export function deadlineInfo(
  p: Procedure,
  persona: UserPersona | null,
  event: EventKey | null,
  board?: BoardItem[],
  procIndex?: Record<string, Procedure>
): DeadlineInfo {
  if (p.deadlineRule) return evaluateRule(p.deadlineRule, persona, event, board, procIndex);
  const win = p.deadlineDays;
  if (typeof win !== "number" || win <= 0 || win >= 3650) return NONE;
  const key = p.anchor || anchorKey(undefined, event);
  const anchor = parseISO(persona?.facts?.[key]);
  const label = ANCHOR_LABEL[key] ?? key;
  if (!anchor) return { ...NONE, basis: `${label} 기준 ${win}일 이내`, missing: label, legacy: true };
  const r = rollForward(addDays(anchor, win));
  return { days: diffDays(r.date, startOfToday()), due: r.date, anchor, basis: `${label} ${fmtDate(anchor)} 기준 · ${win}일 이내`, missing: null, rolled: r.rolled, legacy: true };
}

/** 정렬·우선순위용 실효 D-day(만료일 알면 환산값, 모르면 법정기간 deadlineDays 그대로). */
export function effectiveDday(
  p: Procedure,
  persona: UserPersona | null,
  event: EventKey | null,
  board?: BoardItem[],
  procIndex?: Record<string, Procedure>
): number {
  return deadlineInfo(p, persona, event, board, procIndex).days ?? p.deadlineDays;
}

/** D-day 뱃지 문구. 양수=D-N, 0=D-day, 음수=기한 지남. */
export function ddayLabel(days: number): string {
  if (days > 0) return `D-${days}`;
  if (days === 0) return "D-day";
  return `기한 지남 (D+${Math.abs(days)})`;
}

/** "11월 5일(목)까지" */
export function dueLabel(info: DeadlineInfo): string {
  return info.due ? `${fmtDate(info.due)}까지` : "";
}

/** 기준일을 모를 때 보여 줄 안내: "출생일을 알려 주시면 정확한 기한을 계산해요" */
export function missingHint(info: DeadlineInfo): string {
  if (!info.missing) return "";
  if (info.missing.endsWith("완료")) return `${info.missing} 후 기한을 계산해요`;
  return `${info.missing}을 알려 주시면 정확한 기한을 계산해요`;
}

export { HOLIDAY_YEARS };
