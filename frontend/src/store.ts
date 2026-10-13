import { create } from "zustand";
import type {
  BoardItem,
  BoardOp,
  ChatMessage,
  DynamicEntry,
  EventKey,
  Playbook,
  Procedure,
  StoredSession,
  UserPersona,
  UserTodo,
} from "./types";
import {
  fetchPlaybook,
  generatePlaybook,
  streamChat,
  updatePersona,
} from "./api/chat";
import { SCENARIO_MAP } from "./data/scenarios";

type Tab = "chat" | "board";

const DYN_KEY = "darami.dynamicEvents.v2"; // v2: 동적 grounding 전환으로 옛 미검증 캐시 무효화
const SESS_KEY = "darami.sessions.v2"; // v2: 큐레이션 grounding 전환으로 옛 보드 캐시 무효화

function loadDynamic(): DynamicEntry[] {
  try {
    return JSON.parse(localStorage.getItem(DYN_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveDynamic(list: DynamicEntry[]) {
  try {
    localStorage.setItem(DYN_KEY, JSON.stringify(list));
  } catch {
    /* ignore */
  }
}

// 시도 약칭 → 보조금24 소관기관명 접두(거주지 문자열에서 지역 추출)
const SIDO: [string, string][] = [
  ["서울", "서울특별시"], ["부산", "부산광역시"], ["대구", "대구광역시"], ["인천", "인천광역시"],
  ["광주", "광주광역시"], ["대전", "대전광역시"], ["울산", "울산광역시"], ["세종", "세종특별자치시"],
  ["경기", "경기도"], ["강원", "강원"], ["충북", "충청북도"], ["충남", "충청남도"],
  ["전북", "전북"], ["전남", "전라남도"], ["경북", "경상북도"], ["경남", "경상남도"], ["제주", "제주"],
];

/** 페르소나 facts에서 거주 시도를 추출 (예: "서울 양천구 목동" → "서울특별시") */
function deriveRegion(facts: Record<string, string | number | boolean> | undefined): string | null {
  if (!facts) return null;
  const blob = Object.values(facts)
    .filter((v) => typeof v === "string")
    .join(" ");
  for (const [short, canonical] of SIDO) {
    if (blob.includes(short)) return canonical;
  }
  return null;
}

function loadSessions(): Record<string, StoredSession> {
  try {
    return JSON.parse(localStorage.getItem(SESS_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveSessions(map: Record<string, StoredSession>) {
  try {
    localStorage.setItem(SESS_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

interface State {
  started: boolean;
  event: EventKey | null;
  playbook: Playbook | null;
  procIndex: Record<string, Procedure>;

  messages: ChatMessage[];
  streaming: boolean;
  quickReplies: string[];
  error: string | null;
  persona: UserPersona | null;
  conflictPending: boolean;
  appliedRegion: string | null; // 플레이북에 반영된 거주 지역(시도)

  board: BoardItem[];
  userTodos: UserTodo[];
  openTodoId: string | null;
  /** 보드에서 막힌 동작 안내(선행 절차 미완료 등). 잠시 뒤 사라진다 */
  boardNotice: string | null;
  tab: Tab;
  openProcId: string | null;

  // 동적 이벤트
  dynamicEvents: DynamicEntry[];
  generating: boolean;
  genError: string | null;

  // 이벤트별 대화 세션 (영속)
  sessions: Record<string, StoredSession>;

  // actions
  startScenario: (key: EventKey) => Promise<void>;
  enterEvent: (event: EventKey, playbook: Playbook, opener: string) => Promise<void>;
  generateAndStart: (description: string) => Promise<void>;
  startDynamic: (key: string) => Promise<void>;
  removeDynamic: (key: string) => void;
  clearSession: (key: string) => void;
  persistCurrent: () => void;
  send: (text: string) => Promise<void>;
  setTab: (t: Tab) => void;
  openProc: (id: string | null) => void;
  toggleDone: (id: string) => void;
  // User defined 할 일
  addUserTodo: (title: string) => void;
  toggleUserTodo: (id: string) => void;
  removeUserTodo: (id: string) => void;
  openTodo: (id: string | null) => void;
  updateTodoNote: (id: string, note: string) => void;
  reset: () => void;
}

/** 플레이북에 없는 사용자 추가 절차(boardOp.procedure)를 완전한 Procedure로 정규화 */
function procFromOp(op: BoardOp): Procedure | null {
  const p = op.procedure;
  if (!p) return null;
  return {
    id: op.id,
    name: p.name || "직접 추가한 할 일",
    priority: p.priority === "must" || p.priority === "qna" ? p.priority : "nice",
    deadline: p.deadline || "",
    deadlineDays: typeof p.deadlineDays === "number" ? p.deadlineDays : 9999,
    agency: p.agency || "",
    online: Boolean(p.online),
    documents: p.documents || [],
    link: p.link || "",
    source: p.source || { name: "사용자 추가", tier: "reference", checkedAt: "" },
    condition: p.condition || "",
    summary: p.summary || "",
  };
}

function applyBoardOps(board: BoardItem[], ops: BoardOp[]): BoardItem[] {
  const map = new Map(board.map((b) => [b.id, { ...b }]));
  for (const op of ops) {
    if (!op?.id) continue;
    const existing = map.get(op.id);
    if (existing) {
      // 이미 보드에 있는 절차의 재-add 는 무시(모델이 add 로 다시 보내면 done 이 waiting 으로 풀리던 버그)
      if (op.op !== "add" && op.status) existing.status = op.status;
    } else {
      // add(또는 모르는 id의 update)는 새 항목으로 보드에 올린다
      map.set(op.id, { id: op.id, status: op.status ?? "waiting" });
    }
  }
  return Array.from(map.values());
}

export const useStore = create<State>((set, get) => ({
  started: false,
  event: null,
  playbook: null,
  procIndex: {},
  messages: [],
  streaming: false,
  quickReplies: [],
  error: null,
  persona: null,
  conflictPending: false,
  appliedRegion: null,
  board: [],
  userTodos: [],
  openTodoId: null,
  boardNotice: null,
  tab: "chat",
  openProcId: null,
  dynamicEvents: loadDynamic(),
  generating: false,
  genError: null,
  sessions: loadSessions(),

  // 현재 이벤트의 대화 상태를 세션에 저장
  persistCurrent: () => {
    const { event, messages, board, quickReplies, persona, procIndex, playbook, userTodos, sessions } = get();
    if (!event) return;
    const kept = messages.filter((m) => m.content.length > 0);
    if (kept.length === 0 && userTodos.length === 0) return;
    // 플레이북에 없던 사용자 추가 절차만 따로 저장(복원용)
    const playbookIds = new Set((playbook?.procedures ?? []).map((p) => p.id));
    const procExtras = Object.values(procIndex).filter((p) => !playbookIds.has(p.id));
    const next = {
      ...sessions,
      [event]: {
        messages: kept,
        board,
        quickReplies,
        persona: persona ?? undefined,
        procExtras: procExtras.length > 0 ? procExtras : undefined,
        userTodos: userTodos.length > 0 ? userTodos : undefined,
        updatedAt: Date.now(),
      },
    };
    saveSessions(next);
    set({ sessions: next });
  },

  // 이벤트 진입: 저장된 세션이 있으면 복원, 없으면 새로 시작(opener 전송)
  enterEvent: async (event, playbook, opener) => {
    const procIndex = Object.fromEntries(
      playbook.procedures.map((p) => [p.id, p])
    );
    const sess = get().sessions[event];
    if (sess && sess.messages.length > 0) {
      // 이전 대화 복원 (페르소나 + 사용자 추가 절차 포함)
      const mergedIndex = { ...procIndex };
      for (const p of sess.procExtras ?? []) mergedIndex[p.id] = p;
      set({
        started: true,
        event,
        playbook,
        procIndex: mergedIndex,
        messages: sess.messages,
        board: sess.board,
        quickReplies: sess.quickReplies,
        persona: sess.persona ?? { event, facts: {} },
        conflictPending: false,
        userTodos: sess.userTodos ?? [],
        openTodoId: null,
        appliedRegion: null,
        error: null,
        streaming: false,
        tab: "chat",
      });
      return;
    }
    // 새 대화 — 빈 페르소나로 시작
    set({
      started: true,
      event,
      playbook,
      procIndex,
      messages: [],
      board: [],
      quickReplies: [],
      persona: { event, facts: {} },
      conflictPending: false,
      userTodos: [],
      openTodoId: null,
      appliedRegion: null,
      error: null,
      tab: "chat",
    });
    await get().send(opener);
  },

  startScenario: async (key) => {
    const playbook = await fetchPlaybook(key);
    await get().enterEvent(key, playbook, SCENARIO_MAP[key].opener);
  },

  clearSession: (key) => {
    const sessions = { ...get().sessions };
    delete sessions[key];
    saveSessions(sessions);
    // 현재 보고 있는 이벤트라면 라이브 상태도 비운다
    const patch: Partial<State> = { sessions };
    if (get().event === key) {
      patch.messages = [];
      patch.board = [];
      patch.userTodos = [];
      patch.openTodoId = null;
      patch.quickReplies = [];
      patch.persona = { event: key, facts: {} };
      patch.conflictPending = false;
    }
    set(patch);
  },

  // 자유 입력 → 동적 플레이북 생성 후 홈에 누적하고 바로 시작
  generateAndStart: async (description) => {
    const desc = description.trim();
    if (!desc || get().generating) return;
    set({ generating: true, genError: null });
    try {
      const playbook = await generatePlaybook(desc, get().persona);
      if (!playbook.procedures || playbook.procedures.length === 0) {
        set({
          generating: false,
          genError:
            "이 이벤트에 대한 행정 절차를 찾지 못했어요. 조금 더 구체적으로 적어 주세요.",
        });
        return;
      }
      const key = `dyn_${Date.now()}`;
      const entry: DynamicEntry = {
        key,
        title: playbook.title || "내 생활 이벤트",
        emoji: playbook.emoji || "🌰",
        blurb: desc.length > 42 ? desc.slice(0, 42) + "…" : desc,
        opener: desc,
        playbook,
      };
      const list = [...get().dynamicEvents, entry];
      saveDynamic(list);
      set({ dynamicEvents: list, generating: false });
      await get().enterEvent(key, playbook, desc);
    } catch (e) {
      set({
        generating: false,
        genError: e instanceof Error ? e.message : "생성 중 오류가 발생했어요.",
      });
    }
  },

  startDynamic: async (key) => {
    const entry = get().dynamicEvents.find((d) => d.key === key);
    if (!entry) return;
    await get().enterEvent(entry.key, entry.playbook, entry.opener);
  },

  removeDynamic: (key) => {
    const list = get().dynamicEvents.filter((d) => d.key !== key);
    saveDynamic(list);
    // 동적 이벤트를 지우면 세션도 함께 정리
    const sessions = { ...get().sessions };
    delete sessions[key];
    saveSessions(sessions);
    set({ dynamicEvents: list, sessions });
  },

  send: async (text) => {
    const trimmed = text.trim();
    if (!trimmed || get().streaming) return;

    const userMsg: ChatMessage = { role: "user", content: trimmed };
    // 사용자 메시지 + 빈 assistant 메시지(타이핑 표시) 추가
    set((s) => ({
      messages: [...s.messages, userMsg, { role: "assistant", content: "" }],
      streaming: true,
      quickReplies: [],
      error: null,
    }));

    const history = get().messages.filter((m) => m.content.length > 0);

    // ① 페르소나 갱신 + 충돌 검사 (1차 호출)
    let personaFacts = get().persona?.facts ?? {};
    try {
      const pr = await updatePersona(get().persona, history);
      // 비충돌 facts는 conflict 여부와 무관하게 항상 커밋한다.
      // (conflict일 때 facts를 통째로 버리면 확정값이 영영 저장되지 않아 같은 질문이 무한 반복됨)
      if (pr.facts) personaFacts = pr.facts;

      // conflict는 '한 턴'만 대화를 막을 수 있다. 직전 턴에 이미 같은 확인을 띄웠다면(conflictPending)
      // 사용자의 답을 확정으로 보고 그대로 진행한다 → 모델이 무엇을 하든 무한 루프 차단(결정론적 안전장치).
      if (pr.conflict && pr.conflict.question && !get().conflictPending) {
        const pendingPersona: UserPersona = { event: get().event ?? undefined, facts: personaFacts };
        set((s) => {
          const msgs = [...s.messages];
          const last = msgs[msgs.length - 1];
          if (last?.role === "assistant") last.content = pr.conflict!.question;
          return {
            messages: msgs,
            quickReplies: pr.conflict!.options ?? [],
            streaming: false,
            persona: pendingPersona, // 비충돌 facts는 저장(disputed key만 다음 답으로 확정)
            conflictPending: true,
          };
        });
        get().persistCurrent();
        return;
      }
    } catch {
      // 페르소나 호출 실패 시 기존 페르소나로 계속 진행
    }
    // 진행(충돌 해소 또는 없음) → pending 해제하고 갱신된 페르소나 반영
    const nextPersona: UserPersona = { event: get().event ?? undefined, facts: personaFacts };
    set({ persona: nextPersona, conflictPending: false });

    // 거주지가 파악되면 지자체/광역 혜택까지 포함해 플레이북을 재큐레이션(on-demand)
    const region = deriveRegion(personaFacts);
    const ev = get().event;
    if (region && ev && region !== get().appliedRegion) {
      try {
        const rpb = await fetchPlaybook(ev, region);
        if (rpb?.procedures?.length) {
          const procIndex = Object.fromEntries(rpb.procedures.map((p) => [p.id, p]));
          set({ playbook: rpb, procIndex, appliedRegion: region });
        }
      } catch {
        /* 지역 재큐레이션 실패 시 기존 플레이북 유지 */
      }
    }

    // ② 대화 생성 (갱신된 페르소나 주입, 2차 호출 — 스트리밍)
    await streamChat(get().event, history, {
      onToken: (t) =>
        set((s) => {
          const msgs = [...s.messages];
          const last = msgs[msgs.length - 1];
          if (last?.role === "assistant") last.content += t;
          return { messages: msgs };
        }),
      onMeta: (meta) =>
        set((s) => {
          const ops = meta.boardOps ?? [];
          const prevIds = new Set(s.board.map((b) => b.id));
          const board = applyBoardOps(s.board, ops);
          // 플레이북에 없는 사용자 추가 절차 상세를 procIndex에 병합
          let procIndex = s.procIndex;
          const extras = ops.map(procFromOp).filter((p): p is Procedure => Boolean(p));
          if (extras.length > 0) {
            procIndex = { ...s.procIndex };
            // 이미 grounding된 절차는 LLM이 임베드한 추측 데이터로 덮어쓰지 않는다(환각 방지)
            for (const p of extras) if (!procIndex[p.id]) procIndex[p.id] = p;
          }
          // 이번 턴에 '새로' 추가된 절차만 골라(결정론적) 이름을 모은다
          const addedNames = ops
            .filter((o) => o.op === "add" && !prevIds.has(o.id))
            .map((o) => procIndex[o.id]?.name)
            .filter((n): n is string => Boolean(n));
          // 사용자가 직접 추가 요청한 할 일 → User defined 섹션(중복 제목 제외)
          const existingTitles = new Set(s.userTodos.map((t) => t.title));
          const newTodos = (meta.userTodos ?? [])
            .filter((t) => t && !existingTitles.has(t))
            .map((t, i) => ({ id: `ut_${Date.now()}_${i}`, title: t, note: "", done: false }));
          const userTodos = newTodos.length ? [...s.userTodos, ...newTodos] : s.userTodos;
          const msgs = [...s.messages];
          const last = msgs[msgs.length - 1];
          if (last?.role === "assistant" && addedNames.length > 0) {
            last.added = { count: addedNames.length, names: addedNames };
          }
          return { board, procIndex, userTodos, quickReplies: meta.quickReplies ?? [], messages: msgs };
        }),
      onDone: () => {
        set({ streaming: false });
        get().persistCurrent(); // 응답 완료 시 세션 저장
      },
      onError: (message) =>
        set((s) => {
          const msgs = [...s.messages];
          const last = msgs[msgs.length - 1];
          if (last?.role === "assistant" && !last.content) msgs.pop();
          return { streaming: false, error: message, messages: msgs };
        }),
    },
    // 현재 플레이북 + 페르소나 + 할 일 보드(완료 체크)를 함께 보낸다
    get().playbook,
    nextPersona,
    get().board);
  },

  setTab: (t) => set({ tab: t }),
  openProc: (id) => set({ openProcId: id }),
  toggleDone: (id) => {
    const { board, procIndex } = get();
    const me = board.find((b) => b.id === id);
    if (!me) return;
    const nameOf = (pid: string) => procIndex[pid]?.name ?? pid;
    const notify = (msg: string) => {
      set({ boardNotice: msg });
      setTimeout(() => set((s) => (s.boardNotice === msg ? { boardNotice: null } : {})), 2500);
    };
    if (me.status !== "done") {
      // 선행 절차(dependsOn)가 아직 안 끝났으면 완료 처리 불가
      const pending = (procIndex[id]?.dependsOn ?? []).filter(
        (d) => board.some((b) => b.id === d) && board.find((b) => b.id === d)?.status !== "done"
      );
      if (pending.length > 0) {
        notify(`먼저 ${pending.map(nameOf).join(", ")}을(를) 완료해야 해요`);
        return;
      }
    } else {
      // 이 절차에 기대고 있는 후속 절차가 완료 상태면 해제 불가
      const dependents = board.filter(
        (b) => b.status === "done" && (procIndex[b.id]?.dependsOn ?? []).includes(id)
      );
      if (dependents.length > 0) {
        notify(`${dependents.map((b) => nameOf(b.id)).join(", ")}이(가) 완료 상태라 해제할 수 없어요`);
        return;
      }
    }
    set((s) => ({
      board: s.board.map((b) =>
        b.id === id
          ? { ...b, status: b.status === "done" ? "waiting" : "done" }
          : b
      ),
    }));
    get().persistCurrent(); // 체크 상태도 세션에 반영
  },

  // ---- User defined 할 일 ----
  addUserTodo: (title) => {
    const t = title.trim();
    if (!t) return;
    set((s) => ({
      userTodos: [...s.userTodos, { id: `ut_${Date.now()}`, title: t, note: "", done: false }],
    }));
    get().persistCurrent();
  },
  toggleUserTodo: (id) => {
    set((s) => ({
      userTodos: s.userTodos.map((t) => (t.id === id ? { ...t, done: !t.done } : t)),
    }));
    get().persistCurrent();
  },
  removeUserTodo: (id) => {
    set((s) => ({
      userTodos: s.userTodos.filter((t) => t.id !== id),
      openTodoId: s.openTodoId === id ? null : s.openTodoId,
    }));
    get().persistCurrent();
  },
  openTodo: (id) => set({ openTodoId: id }),
  updateTodoNote: (id, note) => {
    set((s) => ({
      userTodos: s.userTodos.map((t) => (t.id === id ? { ...t, note } : t)),
    }));
    get().persistCurrent();
  },

  reset: () => {
    get().persistCurrent(); // 홈으로 나가기 전에 현재 세션 저장
    set({
      started: false,
      event: null,
      playbook: null,
      procIndex: {},
      messages: [],
      board: [],
      userTodos: [],
      openTodoId: null,
      quickReplies: [],
      persona: null,
      conflictPending: false,
      appliedRegion: null,
      error: null,
      tab: "chat",
      openProcId: null,
      streaming: false,
      genError: null,
      // dynamicEvents, sessions는 홈에서 유지(영속)
    });
  },
}));
