import type {
  ChatMessage,
  EventKey,
  LegalBasis,
  MetaPayload,
  Playbook,
  PersonaResult,
  UserPersona,
  BoardItem,
} from "../types";

/** 대화 입력으로 사용자 페르소나 갱신 + 충돌 검사 (턴당 1차 호출) */
export async function updatePersona(
  persona: UserPersona | null,
  messages: ChatMessage[]
): Promise<PersonaResult> {
  const res = await fetch("/api/persona", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona, messages }),
  });
  if (!res.ok) throw new Error(`페르소나 갱신 실패 (${res.status})`);
  return (await res.json()) as PersonaResult;
}

/** 자유 입력 생활 이벤트 → 동적 플레이북 생성 (source-first 실데이터 큐레이션) */
export async function generatePlaybook(
  description: string,
  persona?: UserPersona | null
): Promise<Playbook> {
  const res = await fetch("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description, persona: persona ?? null }),
  });
  if (!res.ok) throw new Error(`생성 실패 (${res.status})`);
  const pb = (await res.json()) as Playbook & { error?: string };
  if (pb.error) throw new Error(pb.error);
  return pb;
}

interface StreamHandlers {
  onToken: (text: string) => void;
  onMeta: (meta: MetaPayload) => void;
  onDone: () => void;
  onError: (message: string) => void;
}

/**
 * /api/chat 으로 대화 메시지를 보내고 SSE 스트림을 파싱한다.
 * - event: token → reply 텍스트 조각 (타이핑 효과)
 * - event: meta  → 보드 갱신·빠른답변 등 구조화 데이터
 * - event: done  → 종료
 */
export async function streamChat(
  event: EventKey | null,
  messages: ChatMessage[],
  handlers: StreamHandlers,
  playbook?: Playbook | null,
  persona?: UserPersona | null,
  board?: BoardItem[]
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      event,
      playbook: playbook ?? null,
      persona: persona ?? null,
      messages,
      // 현재 '할 일' 보드(완료 체크 포함) — 모델이 어떤 절차가 끝났는지 알고 답하도록
      board: (board ?? []).map((b) => ({ id: b.id, status: b.status })),
    }),
  });

  if (!res.ok || !res.body) {
    handlers.onError(`서버 오류 (${res.status})`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE는 빈 줄(\n\n)로 이벤트가 구분된다
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let evName = "message";
      let dataStr = "";
      for (const line of lines) {
        if (line.startsWith("event:")) evName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
      }
      if (!dataStr) continue;
      let data: any;
      try {
        data = JSON.parse(dataStr);
      } catch {
        continue;
      }

      if (evName === "token") handlers.onToken(data.text ?? "");
      else if (evName === "meta") handlers.onMeta(data as MetaPayload);
      else if (evName === "done") handlers.onDone();
      else if (evName === "error") handlers.onError(data.message ?? "알 수 없는 오류");
    }
  }
}

export async function fetchPlaybook(event: EventKey, region?: string | null): Promise<Playbook> {
  const q = region ? `?region=${encodeURIComponent(region)}` : "";
  const res = await fetch(`/api/playbook/${event}${q}`);
  return (await res.json()) as Playbook;
}

/** 절차의 법적 근거를 국가법령정보 API로 실시간 조회 */
export async function fetchLegalBasis(procedureId: string): Promise<LegalBasis> {
  try {
    const res = await fetch(`/api/legal-basis/${encodeURIComponent(procedureId)}`);
    if (!res.ok) return { supported: false };
    return (await res.json()) as LegalBasis;
  } catch {
    return { supported: false };
  }
}
