import { useEffect, useState } from "react";
import { useStore } from "../store";
import { fetchLegalBasis } from "../api/chat";
import type { LegalBasis, Procedure } from "../types";
import { deadlineInfo, ddayLabel, dueLabel, missingHint } from "../lib/dday";

/** 절차 상세 bottom sheet: 요약·기관·서류 체크리스트·바로가기·법적근거·출처 */
export default function ProcedureSheet() {
  const { openProcId, procIndex, board, openProc, toggleDone, persona, event } = useStore();
  const [legal, setLegal] = useState<LegalBasis | null>(null);

  // 시트가 열릴 때 해당 절차의 법적 근거를 국가법령정보 API로 실시간 조회
  useEffect(() => {
    if (!openProcId) {
      setLegal(null);
      return;
    }
    let cancelled = false;
    setLegal(null);
    fetchLegalBasis(openProcId).then((r) => {
      if (!cancelled) setLegal(r);
    });
    return () => {
      cancelled = true;
    };
  }, [openProcId]);

  if (!openProcId) return null;
  const p = procIndex[openProcId];
  if (!p) return null;
  const item = board.find((b) => b.id === openProcId);
  const isDone = item?.status === "done";
  const info = deadlineInfo(p, persona, event, board, procIndex);

  return (
    <div className="absolute inset-0 z-40 flex flex-col justify-end">
      {/* 딤 배경 */}
      <div className="absolute inset-0 bg-bark/40" onClick={() => openProc(null)} />

      <div className="relative bg-cream rounded-t-3xl max-h-[82%] flex flex-col sheet-enter">
        <div className="pt-3 pb-1 flex justify-center shrink-0">
          <div className="w-10 h-1.5 bg-acorn/20 rounded-full" />
        </div>

        <div className="overflow-y-auto no-scrollbar px-5 pb-6">
          {/* 헤더 */}
          <div className="flex items-start gap-2 pt-2">
            <div className="flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <h2 className="text-lg font-extrabold text-acorn">{p.name}</h2>
                <PriorityChip priority={p.priority} />
                {p.online && (
                  <span className="text-[10px] text-leaf border border-leaf/40 rounded px-1.5 py-0.5">
                    온라인 가능
                  </span>
                )}
              </div>
              {p.deadline?.trim() && (
                <p className="text-[11px] text-nut-deep font-semibold mt-1">⏰ {p.deadline}</p>
              )}
              {/* 계산된 만료일(민법식: 초일 불산입·역 계산·휴일이면 익일) 또는 기준일 누락 안내 */}
              {!isDone && info.due && (
                <p className="text-[11px] text-acorn/70 mt-0.5">
                  📅 <b className={info.days !== null && info.days < 0 ? "text-red-500" : "text-acorn"}>{dueLabel(info)}</b>
                  {" · "}{ddayLabel(info.days as number)}
                  <span className="text-acorn/45"> · {info.basis}{info.rolled ? ` · 말일이 ${info.rolled}라 다음 날로` : ""}</span>
                </p>
              )}
              {!isDone && !info.due && info.missing && (
                <p className="text-[11px] text-acorn/60 mt-0.5">📅 {missingHint(info)}</p>
              )}
            </div>
            <button
              onClick={() => openProc(null)}
              className="text-acorn/40 text-xl leading-none px-1"
            >
              ✕
            </button>
          </div>

          {/* 요약 (비어있으면 박스 자체를 숨김) */}
          {p.summary?.trim() && (
            <p className="text-[13px] text-acorn/80 leading-relaxed mt-3 bg-white rounded-xl border border-sand p-3">
              {p.summary}
            </p>
          )}

          {/* 담당 기관 · 어디서 */}
          {p.agency?.trim() && (
            <Section title="담당 기관">
              <p className="text-[13px] text-acorn">{p.agency}</p>
            </Section>
          )}
          {p.where?.trim() && (
            <Section title="어디서">
              <p className="text-[13px] text-acorn/80">{p.where}</p>
              {p.fee?.trim() && <p className="text-[12px] text-acorn/60 mt-1">수수료 · {p.fee}</p>}
            </Section>
          )}

          {/* 진행 순서 (지식베이스) */}
          {(p.steps?.length ?? 0) > 0 && (
            <Section title="진행 순서">
              <ol className="space-y-1.5 list-decimal pl-4">
                {p.steps!.map((st, i) => (
                  <li key={i} className="text-[13px] text-acorn/80">
                    {st}
                  </li>
                ))}
              </ol>
            </Section>
          )}

          {/* 필요 서류 (없으면 숨김) */}
          {p.documents.length > 0 && (
            <Section title="필요 서류">
              <ul className="space-y-1.5">
                {p.documents.map((d, i) => (
                  <li key={i} className="text-[13px] text-acorn/80">
                    {d}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {/* 주의점 */}
          {(p.pitfalls?.length ?? 0) > 0 && (
            <Section title="주의">
              <ul className="space-y-1.5">
                {p.pitfalls!.map((d, i) => (
                  <li key={i} className="text-[12.5px] text-nut-deep">
                    ⚠ {d}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {/* 조건 */}
          {p.condition && (
            <Section title="적용 조건">
              <p className="text-[12.5px] text-acorn/70">{p.condition}</p>
            </Section>
          )}

          {/* 출처 — 큐레이션 agent의 grounding 상태를 정직하게 표기 */}
          <SourceSection proc={p} />

          {/* 법적 근거 (국가법령정보 API 실시간 조회) */}
          {legal?.supported && <LegalBasisSection legal={legal} />}

          {/* 액션 */}
          <div className="mt-5 space-y-2">
            {p.link && (
              <a
                href={p.link}
                target="_blank"
                rel="noreferrer"
                className="block w-full text-center bg-nut text-white font-semibold rounded-xl py-3 text-sm active:scale-[0.98] transition"
              >
                온라인 바로가기 →
              </a>
            )}
            <button
              onClick={() => toggleDone(p.id)}
              className={`w-full rounded-xl py-3 text-sm font-semibold border transition active:scale-[0.98] ${
                isDone
                  ? "bg-white text-acorn/60 border-sand"
                  : "bg-leaf text-white border-leaf"
              }`}
            >
              {isDone ? "완료 취소" : "완료로 표시"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SourceSection({ proc }: { proc: Procedure }) {
  const status = proc.verification_status ?? "unverified";
  const map: Record<string, { label: string; cls: string }> = {
    verified: { label: "🛡️ 공식 출처 검증됨", cls: "bg-leaf/15 text-leaf" },
    partial: { label: "△ 근거 일부 확인", cls: "bg-nut/15 text-nut-deep" },
    needs_review: { label: "⚠️ 확인 필요", cls: "bg-red-100 text-red-600" },
    unverified: { label: "ℹ️ AI 생성 · 자동 검증 전", cls: "bg-acorn/10 text-acorn/60" },
  };
  const v = map[status] ?? map.unverified;
  const grounded = status === "verified" || status === "partial";
  return (
    <Section title="출처">
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`text-[11px] font-semibold rounded-md px-2 py-1 ${v.cls}`}>{v.label}</span>
        {proc.sourceUrl ? (
          <a href={proc.sourceUrl} target="_blank" rel="noreferrer" className="text-[11px] text-nut-deep underline">
            {proc.source.name}
          </a>
        ) : (
          <span className="text-[11px] text-acorn/60">{proc.source.name}</span>
        )}
        {grounded && proc.source.checkedAt && (
          <span className="text-[10px] text-acorn/40">· 조회 {proc.source.checkedAt}</span>
        )}
      </div>
      <p className="text-[11px] text-acorn/55 mt-1.5 leading-snug">
        {grounded
          ? proc.curated
            ? "창구·서류·기한은 공식 안내 페이지(정부24·기관 사이트)를 직접 확인해 정리한 값이에요. 신청 시점에 변동될 수 있어 담당 기관 확인을 권장합니다."
            : "담당기관·기한·서류는 공공 API(국가법령정보·보조금24)로 실시간 확인된 값이에요. 신청 시점에 변동될 수 있어 담당 기관 확인을 권장합니다."
          : "아직 공식 소스로 자동 검증되지 않은 안내값입니다. 신청 전 담당 기관에서 확인하세요."}
      </p>
    </Section>
  );
}

function LegalBasisSection({ legal }: { legal: LegalBasis }) {
  const status = legal.verification_status ?? "needs_review";
  const badge: Record<string, { label: string; cls: string }> = {
    verified: { label: "✅ 법령 교차검증 완료", cls: "bg-leaf/15 text-leaf" },
    partial: { label: "△ 일부 확인", cls: "bg-nut/15 text-nut-deep" },
    needs_review: { label: "⚠️ 확인 필요", cls: "bg-red-100 text-red-600" },
    stale: { label: "↻ 갱신 필요", cls: "bg-acorn/10 text-acorn/60" },
  };
  const b = badge[status] ?? badge.needs_review;
  return (
    <Section title="법적 근거 · 실시간 조회">
      <div className="bg-white rounded-xl border border-sand p-3 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-[11px] font-semibold rounded-md px-2 py-1 ${b.cls}`}>{b.label}</span>
          {legal.deadline_in_law_text && (
            <span className="text-[10px] text-acorn/55">법령상 {legal.deadline_in_law_text}</span>
          )}
        </div>
        <div className="text-[13px] text-acorn font-semibold">
          {legal.law_name} {legal.article}
        </div>
        <div className="text-[11px] text-acorn/55">
          시행 {legal.enforcement_date} · {legal.ministry}
        </div>
        {legal.evidence_span && (
          <blockquote className="text-[12px] text-acorn/75 leading-relaxed border-l-2 border-leaf/40 pl-2 mt-1">
            “{legal.evidence_span}”
          </blockquote>
        )}
        {legal.source_url && (
          <a
            href={legal.source_url}
            target="_blank"
            rel="noreferrer"
            className="inline-block text-[12px] font-semibold text-nut-deep mt-1"
          >
            법령 원문 보기 →
          </a>
        )}
        <div className="text-[10px] text-acorn/40">
          국가법령정보 OPEN API · 조회 {legal.fetched_at}
        </div>
      </div>
    </Section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-4">
      <h3 className="text-[11px] font-bold text-acorn/50 uppercase tracking-wide mb-1.5">
        {title}
      </h3>
      {children}
    </div>
  );
}

function PriorityChip({ priority }: { priority: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    must: { label: "필수", cls: "bg-nut/15 text-nut-deep" },
    nice: { label: "권장", cls: "bg-leaf/15 text-leaf" },
    qna: { label: "참고", cls: "bg-acorn/10 text-acorn/60" },
  };
  const c = map[priority] ?? map.qna;
  return (
    <span className={`text-[10px] font-bold rounded px-1.5 py-0.5 ${c.cls}`}>{c.label}</span>
  );
}
