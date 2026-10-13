import { useState } from "react";
import { SCENARIOS } from "../data/scenarios";
import { useStore } from "../store";
import type { EventKey } from "../types";
import mainLogo from "../../figure/darami-main-logo.png";

export default function ScenarioPicker() {
  const {
    startScenario,
    dynamicEvents,
    startDynamic,
    removeDynamic,
    generateAndStart,
    generating,
    genError,
    sessions,
    clearSession,
  } = useStore();

  const hasSession = (key: string) =>
    !!sessions[key] && sessions[key].messages.length > 0;

  const [loading, setLoading] = useState<string | null>(null);
  const [showInput, setShowInput] = useState(false);
  const [text, setText] = useState("");

  const start = async (key: EventKey) => {
    setLoading(key);
    try {
      await startScenario(key);
    } finally {
      setLoading(null);
    }
  };

  const submitCustom = async () => {
    if (!text.trim() || generating) return;
    const desc = text;
    setText("");
    await generateAndStart(desc);
    // 성공 시 화면이 대화로 전환되므로 여기 이후는 실패한 경우만 도달
    setShowInput(true);
  };

  return (
    <div className="flex-1 overflow-y-auto no-scrollbar px-6 pt-14 pb-8 flex flex-col">
      <div className="text-center mb-7">
        <img src={mainLogo} alt="다람이" className="w-44 h-44 mx-auto mb-1 object-contain" />
        <p className="text-sm text-acorn/70 mt-1">생활 이벤트 기반 공공서비스 AI 에이전트</p>
        <p className="text-xs text-acorn/50 mt-3 leading-relaxed">
          여러 창구를 헤매지 않아도,<br />한 번의 대화로 모든 절차를.
        </p>
      </div>

      <p className="text-xs font-semibold text-acorn/60 mb-3 px-1">어떤 생활 이벤트를 도와드릴까요?</p>

      <div className="space-y-3">
        {SCENARIOS.map((s) => (
          <EventCard
            key={s.key}
            emoji={s.emoji}
            title={s.title}
            sub={s.persona}
            blurb={s.blurb}
            loading={loading === s.key}
            onClick={() => start(s.key)}
            disabled={loading !== null || generating}
            resumable={hasSession(s.key)}
            onReload={() => clearSession(s.key)}
          />
        ))}
      </div>

      {/* 내가 추가한 동적 이벤트 */}
      {dynamicEvents.length > 0 && (
        <>
          <p className="text-xs font-semibold text-acorn/60 mt-6 mb-3 px-1">내가 추가한 이벤트</p>
          <div className="space-y-3">
            {dynamicEvents.map((d) => (
              <EventCard
                key={d.key}
                emoji={d.emoji}
                title={d.title}
                blurb={d.blurb}
                loading={loading === d.key}
                disabled={loading !== null || generating}
                onClick={async () => {
                  setLoading(d.key);
                  try {
                    await startDynamic(d.key);
                  } finally {
                    setLoading(null);
                  }
                }}
                resumable={hasSession(d.key)}
                onReload={() => clearSession(d.key)}
                onDelete={() => removeDynamic(d.key)}
              />
            ))}
          </div>
        </>
      )}

      {/* 직접 입력 (?) */}
      <p className="text-xs font-semibold text-acorn/60 mt-6 mb-3 px-1">직접 입력</p>
      {!showInput ? (
        <button
          onClick={() => setShowInput(true)}
          disabled={generating}
          className="w-full text-left bg-white/60 rounded-2xl p-4 border border-dashed border-nut/40 active:scale-[0.98] transition disabled:opacity-60"
        >
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-nut/10 text-nut text-xl font-bold flex items-center justify-center shrink-0">
              ?
            </div>
            <div className="flex-1">
              <div className="font-bold text-acorn">다른 생활 이벤트 입력</div>
              <div className="text-[11px] text-acorn/60 mt-0.5">
                결혼·창업·상속·반려동물 등록… 무엇이든 다람이가 절차를 모아줘요.
              </div>
            </div>
          </div>
        </button>
      ) : (
        <div className="bg-white rounded-2xl p-3 border border-nut/40 shadow-sm">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoFocus
            rows={3}
            placeholder="예: 부모님이 돌아가셔서 상속 절차와 사망신고를 해야 해요. 서울에 살아요."
            disabled={generating}
            className="w-full resize-none text-[13px] text-acorn outline-none placeholder:text-acorn/35 disabled:opacity-60"
          />
          {genError && <div className="text-[11px] text-red-600 mb-1.5">⚠️ {genError}</div>}
          <div className="flex items-center gap-2 mt-1">
            <button
              onClick={() => {
                setShowInput(false);
                setText("");
              }}
              disabled={generating}
              className="text-[12px] text-acorn/50 px-2 py-2"
            >
              취소
            </button>
            <button
              onClick={submitCustom}
              disabled={generating || !text.trim()}
              className="flex-1 bg-nut text-white text-[13px] font-semibold rounded-xl py-2.5 disabled:opacity-40 active:scale-[0.98] transition flex items-center justify-center gap-2"
            >
              {generating ? (
                <>
                  <span className="typing-dot w-1.5 h-1.5 bg-white rounded-full" />
                  다람이가 절차를 모으는 중…
                </>
              ) : (
                "다람이에게 맡기기"
              )}
            </button>
          </div>
        </div>
      )}

      <p className="text-[10px] text-acorn/40 text-center mt-auto pt-8">
        Team Nutcrackers
      </p>
    </div>
  );
}

function EventCard({
  emoji,
  title,
  sub,
  blurb,
  loading,
  disabled,
  onClick,
  resumable,
  onReload,
  onDelete,
}: {
  emoji: string;
  title: string;
  sub?: string;
  blurb: string;
  loading: boolean;
  disabled: boolean;
  onClick: () => void;
  resumable?: boolean;
  onReload?: () => void;
  onDelete?: () => void;
}) {
  return (
    <div className="relative group">
      <button
        disabled={disabled}
        onClick={onClick}
        className={`w-full text-left bg-white rounded-2xl p-4 shadow-sm border active:scale-[0.98] transition disabled:opacity-60 ${
          resumable ? "border-leaf/50" : "border-sand"
        }`}
      >
        <div className="flex items-center gap-3">
          <div className="text-3xl">{emoji}</div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className="font-bold text-acorn">{title}</span>
              {loading && <span className="text-[11px] text-nut animate-pulse">다람이 준비 중…</span>}
              {!loading && resumable && (
                <span className="text-[10px] font-semibold text-leaf bg-leaf/12 rounded-full px-1.5 py-0.5 flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-leaf" />
                  대화 이어가기
                </span>
              )}
            </div>
            {sub && <div className="text-[11px] text-acorn/55 mt-0.5">{sub}</div>}
            <div className="text-[11px] text-acorn/70 mt-1 leading-snug">{blurb}</div>
          </div>
        </div>
      </button>

      {/* 커서를 올리면 우상단에 액션: (대화가 있으면) 새로고침 ↻ · (동적이면) 삭제 ✕ */}
      <div className="absolute -top-2 -right-2 flex gap-1 opacity-0 group-hover:opacity-100 transition">
        {resumable && onReload && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onReload();
            }}
            title="대화 기록 초기화"
            className="w-6 h-6 rounded-full bg-white border border-sand text-acorn text-xs flex items-center justify-center shadow active:scale-90 active:-rotate-180 transition-transform"
          >
            ↻
          </button>
        )}
        {onDelete && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            title="이벤트 삭제"
            className="w-6 h-6 rounded-full bg-bark text-white text-xs flex items-center justify-center shadow active:scale-90"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}
