import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import daramiAvatar from "../../figure/darami-chat-logo.png";
import GraphSearchBubble from "./GraphSearchBubble";

/** 채팅 어시스턴트 프로필 이미지 (다람이) */
function DaramiAvatar() {
  return (
    <img
      src={daramiAvatar}
      alt="다람이"
      className="w-7 h-7 rounded-full object-cover shrink-0 mt-0.5 border border-sand bg-white"
    />
  );
}

export default function ChatPanel() {
  const { messages, streaming, quickReplies, error, send, setTab, playbook, generating } = useStore();
  const [input, setInput] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, quickReplies, streaming]);

  // 새 질문(quickReplies 변경)이 오면 이전 선택을 초기화
  useEffect(() => {
    setSelected([]);
  }, [quickReplies]);

  const submit = (text: string) => {
    if (!text.trim() || streaming) return;
    setInput("");
    void send(text);
  };

  const toggle = (q: string) =>
    setSelected((prev) =>
      prev.includes(q) ? prev.filter((x) => x !== q) : [...prev, q]
    );

  const submitSelected = () => {
    if (selected.length === 0 || streaming) return;
    // 선택 순서를 quickReplies 표시 순서대로 정렬해 자연스럽게 합친다
    const ordered = quickReplies.filter((q) => selected.includes(q));
    setSelected([]);
    void send(ordered.join(", "));
  };

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div ref={scrollRef} className="flex-1 overflow-y-auto no-scrollbar px-4 py-4 space-y-3">
        {messages.map((m, i) => {
          const isLast = i === messages.length - 1;
          if (m.role === "assistant" && m.content === "" && isLast && streaming) {
            // 첫 질문: 점 세 개 대신 절차 그래프에서 관련 절차를 찾아 확대하는 애니메이션
            if (i === 1 && messages[0]?.role === "user") {
              return <GraphSearchBubble key="graph-search" question={messages[0].content} playbook={playbook} eventTitle={playbook?.title || undefined} pending={generating} />;
            }
            return <TypingBubble key={i} />;
          }
          return (
            <div key={i} className="space-y-1.5">
              <Bubble role={m.role} text={m.content} />
              {m.role === "assistant" && m.added && m.added.count > 0 && (
                <AddedToBoardChip added={m.added} onOpen={() => setTab("board")} />
              )}
            </div>
          );
        })}

        {error && (
          <div className="text-[12px] text-red-600 bg-red-50 border border-red-200 rounded-xl px-3 py-2">
            ⚠️ {error}
          </div>
        )}

        {/* 빠른답변 칩 (여러 개 선택 가능) */}
        {!streaming && quickReplies.length > 0 && (
          <div className="pt-1 pop-in">
            <div className="text-[10px] text-acorn/45 mb-1.5 px-1">
              해당하는 항목을 모두 선택해 주세요 · 여러 개 선택 가능
            </div>
            <div className="flex flex-wrap gap-2">
              {quickReplies.map((q) => {
                const on = selected.includes(q);
                return (
                  <button
                    key={q}
                    onClick={() => toggle(q)}
                    className={`text-[12px] font-medium rounded-full px-3 py-1.5 border transition active:scale-95 flex items-center gap-1 ${
                      on
                        ? "bg-nut text-white border-nut"
                        : "bg-white text-nut-deep border-nut/40"
                    }`}
                  >
                    <span
                      className={`inline-flex items-center justify-center w-3.5 h-3.5 rounded-full border text-[9px] leading-none ${
                        on ? "bg-white text-nut border-white" : "border-nut/40 text-transparent"
                      }`}
                    >
                      ✓
                    </span>
                    {q}
                  </button>
                );
              })}
            </div>
            <button
              onClick={submitSelected}
              disabled={selected.length === 0}
              className="mt-2.5 w-full bg-nut text-white text-[13px] font-semibold rounded-xl py-2.5 disabled:opacity-40 active:scale-[0.98] transition"
            >
              {selected.length > 0 ? `${selected.length}개 선택 완료 · 보내기` : "선택 후 보내기"}
            </button>
          </div>
        )}
      </div>

      {/* 입력 */}
      <div className="border-t border-sand bg-cream px-3 py-2.5 flex items-center gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit(input)}
          placeholder={streaming ? "다람이가 답하고 있어요…" : "메시지를 입력하세요"}
          maxLength={1000} // 서버 상한(MAX_INPUT_CHARS)과 같다
          disabled={streaming}
          className="flex-1 bg-white rounded-full px-4 py-2.5 text-sm outline-none border border-sand focus:border-nut/50 disabled:opacity-60"
        />
        <button
          onClick={() => submit(input)}
          disabled={streaming || !input.trim()}
          className="w-10 h-10 rounded-full bg-nut text-white text-lg flex items-center justify-center disabled:opacity-40 active:scale-90 transition shrink-0"
        >
          ↑
        </button>
      </div>
    </div>
  );
}

function AddedToBoardChip({
  added,
  onOpen,
}: {
  added: { count: number; names: string[] };
  onOpen: () => void;
}) {
  const preview = added.names.slice(0, 3).join(", ");
  const more = added.count > 3 ? ` 외 ${added.count - 3}건` : "";
  return (
    <div className="flex justify-start pl-9">
      <button
        onClick={onOpen}
        className="text-left bg-leaf/10 border border-leaf/30 rounded-xl px-3 py-2 active:scale-[0.98] transition max-w-[82%]"
      >
        <div className="flex items-center gap-1.5 text-[12px] font-semibold text-leaf">
          <span>🌰 할 일에 {added.count}개 추가됨</span>
          <span className="text-leaf/70">· 보기 →</span>
        </div>
        <div className="text-[11px] text-acorn/60 mt-0.5 leading-snug">
          {preview}
          {more}
        </div>
      </button>
    </div>
  );
}

function Bubble({ role, text }: { role: string; text: string }) {
  const isUser = role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} gap-2`}>
      {!isUser && <DaramiAvatar />}
      <div
        className={`max-w-[78%] px-3.5 py-2.5 text-[13.5px] leading-relaxed whitespace-pre-wrap rounded-2xl ${
          isUser
            ? "bg-nut text-white rounded-br-md"
            : "bg-white text-acorn border border-sand rounded-bl-md"
        }`}
      >
        {text || "…"}
      </div>
    </div>
  );
}

function TypingBubble() {
  return (
    <div className="flex justify-start gap-2">
      <DaramiAvatar />
      <div className="bg-white border border-sand rounded-2xl rounded-bl-md px-4 py-3 flex items-center gap-1">
        <span className="typing-dot w-1.5 h-1.5 bg-acorn/40 rounded-full" />
        <span className="typing-dot w-1.5 h-1.5 bg-acorn/40 rounded-full" />
        <span className="typing-dot w-1.5 h-1.5 bg-acorn/40 rounded-full" />
      </div>
    </div>
  );
}
