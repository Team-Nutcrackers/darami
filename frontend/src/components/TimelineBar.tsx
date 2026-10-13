import { useStore } from "../store";
import { effectiveDday, ddayLabel } from "../lib/dday";
import mainLogo from "../../figure/darami-main-logo.png";

/** 상단 바: 로고 + 이벤트 + 가장 임박한 Must 절차(D-day) + 탭 전환 */
export default function TimelineBar() {
  const { playbook, board, procIndex, userTodos, tab, setTab, reset, persona, event } = useStore();

  // 보드에 올라온 미완료 Must 절차 중 가장 임박한(작은 실효 D-day) 항목
  const urgent = board
    .filter((b) => b.status !== "done")
    .map((b) => procIndex[b.id])
    .filter(Boolean)
    .filter((p) => p.priority === "must" && effectiveDday(p, persona, event) < 3650)
    .map((p) => ({ p, dday: effectiveDday(p, persona, event) }))
    .sort((a, b) => a.dday - b.dday)[0];

  const activeCount =
    board.filter((b) => b.status !== "done").length +
    userTodos.filter((t) => !t.done).length;

  return (
    <div className="bg-cream/95 backdrop-blur border-b border-sand z-20">
      <div className="px-4 pt-9 sm:pt-6 pb-2 flex items-center gap-2">
        <button
          onClick={reset}
          title="홈으로"
          className="group relative w-8 h-8 shrink-0 active:scale-90 transition"
        >
          {/* 평소엔 다람이 로고, 마우스를 올리면 뒤로가기 화살표가 이미지를 대체 */}
          <img
            src={mainLogo}
            alt="다람이"
            className="w-8 h-8 object-contain group-hover:hidden"
          />
          <span className="hidden group-hover:flex absolute inset-0 items-center justify-center text-acorn text-xl font-bold">
            ←
          </span>
        </button>
        <div className="flex-1 min-w-0">
          <div className="text-lg font-extrabold text-acorn leading-tight truncate">
            {playbook ? `${playbook.title} 절차 안내` : ""}
          </div>
        </div>
      </div>

      {urgent && (
        <div className="mx-4 mb-2 rounded-xl bg-nut/10 border border-nut/30 px-3 py-2 flex items-center gap-2">
          <span
            className={`text-[10px] font-bold text-white rounded-md px-1.5 py-0.5 whitespace-nowrap shrink-0 ${
              urgent.dday < 0 ? "bg-red-500" : "bg-nut"
            }`}
          >
            {ddayLabel(urgent.dday)}
          </span>
          <span className="text-[11px] text-acorn/80 truncate">
            가장 임박: <b className="text-acorn">{urgent.p.name}</b> · {urgent.p.deadline}
          </span>
        </div>
      )}

      {/* 탭 */}
      <div className="flex px-4 gap-1">
        <TabButton active={tab === "chat"} onClick={() => setTab("chat")} label="대화" />
        <TabButton
          active={tab === "board"}
          onClick={() => setTab("board")}
          label="할 일"
          badge={activeCount}
        />
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  label,
  badge,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  badge?: number;
}) {
  return (
    <button
      onClick={onClick}
      className={`relative px-4 py-2 text-sm font-semibold rounded-t-lg transition ${
        active ? "text-nut" : "text-acorn/45"
      }`}
    >
      <span className="flex items-center gap-1.5">
        {label}
        {badge ? (
          <span className="text-[10px] font-bold text-white bg-nut rounded-full min-w-[16px] h-4 px-1 inline-flex items-center justify-center">
            {badge}
          </span>
        ) : null}
      </span>
      {active && <span className="absolute bottom-0 left-2 right-2 h-0.5 bg-nut rounded-full" />}
    </button>
  );
}
