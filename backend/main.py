"""다람이 백엔드 — FastAPI + Upstage Solar Pro 프록시.

- 프론트엔드에서 /api/chat 으로 대화 메시지를 보내면
  선별된 이벤트 플레이북을 시스템 프롬프트에 주입해 Solar Pro 를 호출하고,
  결과(JSON)를 SSE로 흘려준다.
- Solar Pro : Upstage OpenAI 호환 chat completions (https://api.upstage.ai/v1, model=solar-pro2)
- API 키는 .env 에서만 읽고 프론트로 노출하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from openai import OpenAI

from prompts import build_system_prompt, build_generation_prompt, build_persona_prompt
from sources.curate import curate_event, legal_for, curate_dynamic
from sources.curate_v2 import curate_dynamic_v2
from sources import graph as procgraph
from sources import deadline
from sources import reldate

load_dotenv()

UPSTAGE_API_KEY = os.environ.get("UPSTAGE_API_KEY", "")
SOLAR_MODEL = os.environ.get("SOLAR_MODEL", "solar-pro2")

PLAYBOOK_DIR = Path(__file__).parent / "playbooks"
CURATE_VERSION = os.environ.get("DARAMI_CURATE", "v2")  # "v2"(기본) | "v1"(예전 키워드 검색 방식)


# ---------- LLM (Solar Pro 단일) ----------
@dataclass
class Provider:
    key: str               # 식별자(trace 기록용)
    label: str             # 공식 표기명
    model: str             # chat.completions 의 model 값
    client: Optional[OpenAI]

    @property
    def available(self) -> bool:
        return self.client is not None and bool(self.model)


SOLAR = Provider(
    key="solar",
    label="Solar Pro",
    model=SOLAR_MODEL,
    client=OpenAI(api_key=UPSTAGE_API_KEY, base_url="https://api.upstage.ai/v1") if UPSTAGE_API_KEY else None,
)


def get_provider(_key: Optional[str] = None) -> Provider:
    """항상 Solar Pro. (예전 요청의 provider 필드는 무시한다)"""
    return SOLAR


app = FastAPI(title="다람이 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 데이터 모델 ----------
class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatReq(BaseModel):
    event: Optional[str] = None        # "move" | "birth" | "resignation" | 동적 슬러그
    playbook: Optional[dict] = None    # 동적 이벤트는 생성된 플레이북을 직접 실어 보낸다
    persona: Optional[dict] = None     # 지금까지 파악한 사용자 페르소나(facts)
    messages: List[Message]
    board: Optional[List[dict]] = None  # 현재 '할 일' 보드 [{id, status}] — 완료 체크 상태를 모델이 알도록


class GenReq(BaseModel):
    description: str                    # 사용자가 자유롭게 입력한 생활 이벤트 설명
    persona: Optional[dict] = None      # 거주지 등(지자체 혜택 grounding에 사용)


class PersonaReq(BaseModel):
    persona: Optional[dict] = None     # 현재 페르소나(facts)
    messages: List[Message]            # 대화 기록(최신 사용자 입력 포함)


# ---------- 플레이북 ----------
def load_playbook(event: str | None) -> dict:
    if not event:
        return {"event": None, "title": "", "procedures": []}
    # on-demand 큐레이션(법령 grounding + 캐시). 실패 시 정적 JSON으로 fallback.
    try:
        curated = curate_event(event)
        if curated and curated.get("procedures"):  # 그라운딩 키가 없어 전부 걸러지면 정적 JSON 으로
            return deadline.enrich_playbook(curated)
    except Exception:  # noqa: BLE001
        pass
    path = PLAYBOOK_DIR / f"{event}.json"
    if not path.exists():
        return {"event": event, "title": "", "procedures": []}
    return deadline.enrich_playbook(json.loads(path.read_text(encoding="utf-8")))


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "model": SOLAR.label,
        "available": SOLAR.available,
    }


@app.get("/api/playbook/{event}")
def get_playbook(event: str, region: Optional[str] = None):
    """프론트가 절차 상세(서류·링크·출처)를 렌더하기 위한 전체 플레이북.
    region: 사용자 거주지(시도). 지자체/광역 혜택 grounding에 사용."""
    try:
        curated = curate_event(event, region=region)
        if curated and curated.get("procedures"):  # 그라운딩 키가 없어 전부 걸러지면 정적 JSON 으로
            return JSONResponse(deadline.enrich_playbook(curated))
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(load_playbook(event))


@app.get("/api/graph")
def graph_stats():
    """절차 그래프 현황(기본판 + 사용 중 학습된 확장). 학습판 삭제는 `python -m sources.graph reset-learned`."""
    b, l = procgraph.base(), procgraph.learned()
    return {
        "base": {"nodes": len(b.get("nodes", {})), "edges": len(b.get("edges", [])), "builtAt": b.get("built_at")},
        "learned": {"nodes": len(l.get("nodes", {})), "edges": len(l.get("edges", [])),
                    "rejected": len(l.get("rejected", [])), "updatedAt": l.get("updated_at"),
                    "edgesList": [{k: e.get(k) for k in ("before_name", "after_name", "relation", "guidance")} for e in l.get("edges", [])]},
    }


@app.get("/api/legal-basis/{procedure_id}")
def legal_basis(procedure_id: str):
    """절차의 법적 근거를 국가법령정보 OPEN API로 실시간 조회·교차검증.
    지원하지 않는 절차면 {"supported": false}."""
    return JSONResponse(legal_for(procedure_id))


# ---------- 핵심: LLM 호출 ----------
def _parse_json(raw: str, fallback: dict) -> dict:
    """모델 응답에서 JSON 객체를 파싱. 잡텍스트가 섞이면 중괄호 범위만 추출."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        return fallback


def _create_json(provider: Provider, chat_messages: list[dict], temperature: float) -> str:
    """선택된 provider로 구조화 JSON 응답(raw 문자열)을 받는다."""
    completion = provider.client.chat.completions.create(
        model=provider.model,
        messages=chat_messages,
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    return completion.choices[0].message.content or "{}"


def call_chat(provider: Provider, system_prompt: str, messages: list[Message]) -> dict:
    """선택된 LLM을 호출해 대화용 구조화 JSON 응답을 받는다."""
    chat_messages = [{"role": "system", "content": system_prompt}]
    chat_messages += [{"role": m.role, "content": m.content} for m in messages]
    raw = _create_json(provider, chat_messages, temperature=0.3)
    return _parse_json(raw, {"reply": raw, "eventDetected": None, "askMissing": [], "quickReplies": [], "boardOps": []})


def make_json_caller(provider: Provider):
    """curate_dynamic / persona 가 쓰는 단발 JSON 호출자(provider 바인딩)."""
    def _json_call(system_prompt: str, user_content: str, temperature: float = 0.2) -> dict:
        raw = _create_json(
            provider,
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
            temperature,
        )
        return _parse_json(raw, {})

    return _json_call


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/generate")
def generate(req: GenReq):
    """자유 입력 생활 이벤트 → 동적 플레이북 (source-first 실데이터 큐레이션).

    보조금24를 이벤트 키워드로 검색해 실제 서비스에서 절차를 구성(grounding)하고,
    핵심 신고 절차는 법령 API로 보강. grounding 안 되는 건 'ai'(미검증)로 표기.
    """
    provider = get_provider()
    if provider.client is None:
        return JSONResponse({"error": f"{provider.label} API 키가 설정되지 않았습니다."}, status_code=500)
    try:
        if CURATE_VERSION == "v1":
            playbook = curate_dynamic(req.description.strip(), req.persona, make_json_caller(provider))
        else:
            # v2: 이벤트 분류 → 사전 생성 사례(few-shot) → 이벤트별 API 매핑으로 계획·grounding
            playbook, trace = curate_dynamic_v2(req.description.strip(), req.persona, make_json_caller(provider))
            _save_live_trace(req, provider.key, playbook, trace)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"동적 큐레이션 실패: {e}"}, status_code=500)
    return JSONResponse(deadline.enrich_playbook(playbook))


def _save_live_trace(req: GenReq, provider_key: str, playbook: dict, trace: dict) -> None:
    """실서비스 "?" 요청의 판단 추적을 generated/live/ 에 남긴다(이상한 절차가 나왔을 때 분류·계획·선택을 되짚기 위해)."""
    try:
        live = Path(__file__).resolve().parent / "generated" / "live"
        live.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        (live / f"{ts}_{playbook.get('event') or 'none'}.json").write_text(json.dumps({
            "query": req.description, "persona": req.persona, "provider": provider_key,
            "playbook": playbook, "trace": trace}, ensure_ascii=False, indent=2), encoding="utf-8")
        bad = [p["name"] for p in playbook.get("procedures", []) if p.get("verification_status") == "unverified"]
        print(f"[generate] event={playbook.get('event')} procs={len(playbook.get('procedures', []))} unverified={bad} → {live.name}/{ts}")
    except Exception as e:  # noqa: BLE001
        print(f"[generate] trace 저장 실패: {e!r}")


@app.post("/api/persona")
def persona(req: PersonaReq):
    """대화 입력으로 사용자 페르소나를 갱신하고 충돌을 검사한다(턴당 1차 호출)."""
    provider = get_provider()
    if provider.client is None:
        return JSONResponse({"error": f"{provider.label} API 키가 설정되지 않았습니다."}, status_code=500)
    # 최근 대화를 한 덩어리로 만들어 최신 입력 중심으로 갱신
    prior_facts = (req.persona or {}).get("facts") or {}
    convo = "\n".join(f"{m.role}: {m.content}" for m in req.messages[-8:])
    result = make_json_caller(provider)(build_persona_prompt(req.persona or {}), convo, temperature=0.1)
    facts = result.get("facts")
    if not isinstance(facts, dict):
        facts = prior_facts
    conflict = result.get("conflict")
    if not (isinstance(conflict, dict) and conflict.get("question")):
        conflict = None
    # ★ conflict는 '이미 저장된 facts'와의 모순일 때만 성립한다(룰 3a를 코드로 강제 · 모델 무관).
    #   대조할 기존 facts가 없으면(첫 입력 등) 모델이 confirm성 conflict를 만들어도 무시 → 빈 facts 루프 차단.
    if not prior_facts:
        conflict = None
    # ★ 기존 facts 의 어떤 값도 바뀌지 않았으면 모순이 아니다(모델이 같은 날짜를 두고 "맞나요?" 하던 가짜 conflict 차단).
    #   내부 key 이름이나 'facts' 를 사용자 질문에 노출하는 conflict 도 버린다.
    if conflict:
        changed_keys = [k for k, v in prior_facts.items() if k in facts and facts[k] != v]
        q = str(conflict.get("question", ""))
        if not changed_keys or "facts" in q or re.search(r"\b[a-z]+_[a-z_]+\b", q):
            conflict = None
    facts = _fix_relative_dates(facts, prior_facts, req)
    return JSONResponse({"facts": facts, "conflict": conflict})


_DATE_KEYS = ("move_date", "birth_date", "resignation_date", "marriage_date", "death_date", "event_date")


def _fix_relative_dates(facts: dict, prior: dict, req: PersonaReq) -> dict:
    """LLM 이 환산한 기준일을 결정적 상대 날짜 파서(sources/reldate)로 교차 검증한다.
    - 이번 턴에 새로 생기거나 바뀐 날짜 key 가 하나이고, 최신 사용자 문장에서 날짜 표현이 읽히는데 값이 다르면 파서 값으로 바로잡는다.
    - 날짜 key 가 하나도 안 생겼는데 문장에 날짜 표현 + 기준일 키워드(출생·이사·퇴사…)가 있으면 그 key 를 채운다."""
    try:
        latest = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        parsed = reldate.parse_relative_date(latest, date.today())
        if not parsed:
            # 이번 문장에 날짜 표현이 없는데 이미 저장된 기준일이 바뀌었다면 모델이 멋대로 재계산한 것 → 기존 값 유지
            for k in _DATE_KEYS:
                if prior.get(k) and facts.get(k) and facts[k] != prior[k]:
                    print(f"[persona] {k}: 날짜 표현 없이 {prior[k]} → {facts[k]} 로 바뀜 → 기존 값 유지")
                    facts = {**facts, k: prior[k]}
            return facts
        iso, phrase = parsed[0].isoformat(), parsed[1]
        changed = [k for k in _DATE_KEYS if facts.get(k) and facts.get(k) != prior.get(k)]
        if len(changed) == 1:
            if str(facts[changed[0]])[:10] != iso:
                print(f"[persona] {changed[0]}: LLM {facts[changed[0]]} → 파서 {iso} ('{phrase}')")
                facts = {**facts, changed[0]: iso}
        elif not changed:
            key = None
            for w, k in deadline.ANCHOR_WORDS:
                if w in latest:
                    key = k
                    break
            if key is None and (req.persona or {}).get("event") in deadline.EVENT_ANCHOR:
                key = deadline.EVENT_ANCHOR[(req.persona or {}).get("event")]
            if key and not facts.get(key) and not prior.get(key):
                print(f"[persona] {key} 비어 있음 → 파서 {iso} ('{phrase}')")
                facts = {**facts, key: iso}
    except Exception as e:  # noqa: BLE001 — 교차 검증 실패가 대화를 막지 않도록
        print(f"[persona] 날짜 교차 검증 실패: {e!r}")
    return facts


def _sanitize_board_ops(ops, board_status: dict, playbook: dict | None = None) -> list:
    """모델의 boardOps 중 보드 상태를 망가뜨리는 것을 거르고, 의존관계를 코드로 보강한다.
    - 이미 보드에 있는 절차를 다시 add → 상태(done 등)가 waiting 으로 초기화되므로 버린다.
    - done 인 절차를 update 로 waiting/in_progress 로 되돌리는 것도 버린다(완료 해제는 사용자가 직접 체크로 한다).
    - 어떤 절차가 done 이 되면 그 선행 절차(dependsOn, 재귀)도 끝난 것이므로 done 으로 같이 올린다(모델이 빠뜨려도)."""
    out, seen = [], set()
    deps = {p.get("id"): list(p.get("dependsOn") or []) for p in (playbook or {}).get("procedures", [])}
    for op in ops or []:
        if not isinstance(op, dict) or not op.get("id"):
            continue
        cur = board_status.get(op["id"])
        if op.get("op") == "add" and cur is not None:
            continue
        if cur == "done" and op.get("status") != "done":
            continue
        out.append(op); seen.add(op["id"])
        if op.get("status") == "done":
            stack = list(deps.get(op["id"], []))
            while stack:
                d = stack.pop()
                if d in seen or board_status.get(d) == "done":
                    continue
                seen.add(d)
                out.append({"op": "update" if d in board_status else "add", "id": d, "status": "done"})
                stack += deps.get(d, [])
    return out


_DATE_CHIP = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}월\s*\d{1,2}일|이전입니다|이후입니다")


def _sanitize_quick_replies(items) -> list[str]:
    """날짜를 고르게 하는 칩("퇴사일이 2026-10-14 이후입니다")은 뺀다 — 날짜는 글로 받는다(기준일 누락 블록 참고)."""
    out = []
    for q in items or []:
        if not isinstance(q, str) or not q.strip():
            continue
        if _DATE_CHIP.search(q):
            continue
        out.append(q.strip())
    return out


@app.post("/api/chat")
async def chat(req: ChatReq):
    # 동적 이벤트는 요청에 실린 플레이북을 우선 사용, 없으면 디스크에서 로드
    playbook = deadline.enrich_playbook(req.playbook) or load_playbook(req.event)
    # 절차 그래프: 보드의 완료 상태로 '현재 위치'를 잡고 지금 할 수 있는 것·막힌 것·순서를 모델에 알린다
    try:
        guidance = procgraph.guidance_block(playbook, req.board)
    except Exception:  # noqa: BLE001 — 안내 생성 실패가 대화를 막지 않도록
        guidance = ""
    # 기한 계산에 필요한 기준일(출생일·이사일 등)이 페르소나에 없으면 모델이 한 번 묻도록 알린다
    try:
        missing = deadline.missing_anchor_block(playbook, req.persona, req.board)
    except Exception:  # noqa: BLE001
        missing = ""
    system_prompt = build_system_prompt(playbook, req.persona, req.board, "\n\n".join(x for x in (guidance, missing) if x))
    provider = get_provider()
    board_status = {b.get("id"): b.get("status") for b in (req.board or []) if isinstance(b, dict) and b.get("id")}

    async def gen():
        if provider.client is None:
            yield sse("error", {"message": f"{provider.label} API 키가 설정되지 않았습니다. backend/.env를 확인하세요."})
            return

        # LLM 호출은 동기 → 스레드로 빼서 이벤트 루프를 막지 않음
        try:
            result = await asyncio.to_thread(call_chat, provider, system_prompt, req.messages)
        except Exception as e:  # noqa: BLE001
            yield sse("error", {"message": f"{provider.label} 호출 실패: {e}"})
            return

        reply = str(result.get("reply", "")).strip()

        # 1) reply를 짧게 끊어 타이핑 효과로 흘려준다
        buf = ""
        for ch in reply:
            buf += ch
            if len(buf) >= 2 or ch in " .,!?\n":
                yield sse("token", {"text": buf})
                buf = ""
                await asyncio.sleep(0.012)
        if buf:
            yield sse("token", {"text": buf})

        # 2) 보드 갱신·빠른답변 등 구조화 메타를 마지막에 전달
        yield sse(
            "meta",
            {
                "eventDetected": result.get("eventDetected"),
                "askMissing": result.get("askMissing", []),
                "quickReplies": _sanitize_quick_replies(result.get("quickReplies", [])),
                "boardOps": _sanitize_board_ops(result.get("boardOps", []), board_status, playbook),
                "userTodos": result.get("userTodos", []),
            },
        )
        yield sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream")
