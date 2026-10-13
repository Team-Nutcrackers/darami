/** 정적 시나리오 3종 + 동적 생성 이벤트(임의 슬러그)를 모두 허용 */
export type EventKey = "move" | "birth" | "resignation" | (string & {});

export type Priority = "must" | "nice" | "qna";
export type Status = "waiting" | "in_progress" | "done";

export interface Source {
  name: string;
  tier: "official" | "reference";
  checkedAt: string;
}

export interface Procedure {
  id: string;
  name: string;
  priority: Priority;
  deadline: string;
  deadlineDays: number;
  agency: string;
  online: boolean;
  documents: string[];
  link: string;
  source: Source;
  condition: string;
  summary: string;
  /** deadlineDays를 어느 기준일(페르소나 날짜 key)로부터 셀지. 없으면 이벤트 기본값. */
  anchor?: string;
  /** 큐레이션 agent의 grounding 상태 (verified/partial=공식출처, ai=AI생성·미검증) */
  verification_status?: "verified" | "partial" | "needs_review" | "unverified";
  /** 먼저 끝내야 하는 선행 절차 id 목록 (playbooks/dependencies.json 카탈로그에서 계산) */
  dependsOn?: string[];
  /** 권고 선행(잠그지 않음) — AI 제안 절차 뒤에 하라는 학습 간선. 흐름도에 점선으로만 표시 */
  suggestedAfter?: string[];
  /** 선행 절차라서 자동으로 추가된 항목 */
  autoAdded?: boolean;
  /** 백엔드 절차 그래프(sources/graph.py)가 매긴 추천 순서·여는 후속 수. 프론트는 보드 기준으로 다시 계산한다(lib/graph.ts) */
  order?: number;
  unlocks?: number;
  /** 그래프 노드 안정 키(common:/law:/svc:/x:) */
  key?: string;
  /** 절차 지식베이스(playbooks/procedures.json)에서 채운 창구·수수료·진행 순서·주의점 */
  where?: string;
  fee?: string;
  steps?: string[];
  pitfalls?: string[];
  sourceUrl?: string;
  curated?: boolean;
}

export interface Playbook {
  event: EventKey;
  title: string;
  emoji: string;
  intro: string;
  procedures: Procedure[];
}

/** 세션별 사용자 페르소나 — 대화로 파악한 '확정 사실'들의 평평한 맵 */
export interface UserPersona {
  event?: EventKey;
  facts: Record<string, string | number | boolean>;
}

/** 페르소나 갱신 시 발견된 모순 (사용자 확인 필요) */
export interface PersonaConflict {
  question: string;
  options: string[];
}

/** /api/persona 응답 */
export interface PersonaResult {
  facts: Record<string, string | number | boolean>;
  conflict: PersonaConflict | null;
}

/** 이벤트별 대화 세션 스냅샷 (localStorage 영속, 다시 들어가면 복원) */
export interface StoredSession {
  messages: ChatMessage[];
  board: BoardItem[];
  quickReplies: string[];
  persona?: UserPersona;
  /** 플레이북에 없던 사용자 추가 절차 (복원 시 procIndex에 다시 병합) */
  procExtras?: Procedure[];
  /** 사용자가 직접 추가한 할 일(User defined) */
  userTodos?: UserTodo[];
  updatedAt: number;
}

/** 동적으로 생성된 사용자 정의 이벤트 (홈 화면에 누적, localStorage 영속) */
export interface DynamicEntry {
  key: string; // 고유 키 (dyn_<timestamp>)
  title: string;
  emoji: string;
  blurb: string; // 카드에 보일 짧은 설명
  opener: string; // 첫 대화로 재생할 원본 입력
  playbook: Playbook;
}

export interface BoardItem {
  id: string;
  status: Status;
}

export interface BoardOp {
  op: "add" | "update";
  id: string;
  status: Status;
  /** 플레이북에 없는 절차를 사용자가 직접 추가 요청한 경우의 상세 (선택) */
  procedure?: Partial<Procedure>;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  /** 이 어시스턴트 턴에 '할 일'에 새로 추가된 절차 (결정론적으로 계산) */
  added?: { count: number; names: string[] };
}

/** 절차의 법적 근거 (국가법령정보 OPEN API 실시간 조회 결과) */
export interface LegalBasis {
  supported: boolean;
  verification_status?: "verified" | "partial" | "needs_review" | "stale";
  law_name?: string;
  article?: string;
  enforcement_date?: string;
  promulgation_date?: string;
  ministry?: string;
  deadline_in_law_text?: string | null;
  deadline_days_in_law?: number | null;
  deadline_days_expected?: number | null;
  evidence_span?: string | null;
  source_url?: string;
  fetched_at?: string;
}

/** 사용자가 직접 추가한 할 일 (검증/grounding 대상 아님, 클릭 시 메모) */
export interface UserTodo {
  id: string;
  title: string;
  note: string;
  done: boolean;
}

export interface MetaPayload {
  eventDetected: EventKey | null;
  askMissing: string[];
  quickReplies: string[];
  boardOps: BoardOp[];
  userTodos?: string[];
}
