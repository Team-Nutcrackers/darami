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
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from openai import OpenAI

from ai_log import AiRun
from prompts import build_system_prompt, build_generation_prompt, build_persona_prompt, DANGER_NOTE
from sources.curate import curate_event, legal_for, curate_dynamic
from sources.curate_v2 import curate_dynamic_v2, EVENTS
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
# CORS 는 열지 않는다 — 프론트는 Vite 프록시로 같은 출처(/api)에서 부른다. 다른 사이트가 브라우저로 이 API 를 쓰지 못하게.

# 입력 상한(비용·악용 방지). 실제 대화는 이보다 훨씬 짧다.
MAX_MSG_CHARS = 4000      # 메시지 한 개
MAX_HISTORY = 40          # 대화 이력(최근 N개만 모델에 보낸다)
MAX_DESC_CHARS = 2000     # 자유 입력 이벤트 설명
CHAT_ROLES = ("user", "assistant")  # system 역할 등은 받지 않는다(시스템 프롬프트는 서버만 만든다)


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
    deadlines: Optional[List[dict]] = None  # 화면 D-day 와 같은 계산의 절차별 만료일 [{id, due, days}] — 모델이 날짜를 직접 셈하지 않도록


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


def _create_json(provider: Provider, chat_messages: list[dict], temperature: float, run: Optional[AiRun] = None) -> str:
    """선택된 provider로 구조화 JSON 응답(raw 문자열)을 받는다.
    모든 LLM 호출이 여기를 지난다 → 호출 1건씩 AI API 호출 로그(ai_log)에 남는다.
    run: 이 호출이 속한 기능 실행. 없으면(오프라인 스크립트 등) 스크립트 단위 기본 실행에 누적."""
    body = {
        "model": provider.model,
        "messages": chat_messages,
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }
    completion = (run or AiRun.default()).call(provider.client, body)
    return completion.choices[0].message.content or "{}"


def _clean_history(messages: list[Message], limit: int = MAX_HISTORY) -> list[dict]:
    """모델에 보낼 대화 이력: user/assistant 만, 최근 limit 개, 메시지당 MAX_MSG_CHARS 자."""
    msgs = [m for m in messages if m.role in CHAT_ROLES][-limit:]
    return [{"role": m.role, "content": m.content[:MAX_MSG_CHARS]} for m in msgs]


def call_chat(provider: Provider, system_prompt: str, messages: list[Message], run: Optional[AiRun] = None) -> dict:
    """선택된 LLM을 호출해 대화용 구조화 JSON 응답을 받는다."""
    chat_messages = [{"role": "system", "content": system_prompt}]
    chat_messages += _clean_history(messages)
    raw = _create_json(provider, chat_messages, temperature=0.3, run=run)
    return _parse_json(raw, {"reply": raw, "eventDetected": None, "askMissing": [], "quickReplies": [], "boardOps": []})


def make_json_caller(provider: Provider, run: Optional[AiRun] = None):
    """curate_dynamic / persona 가 쓰는 단발 JSON 호출자(provider · 기능 실행 바인딩)."""
    def _json_call(system_prompt: str, user_content: str, temperature: float = 0.2) -> dict:
        raw = _create_json(
            provider,
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
            temperature,
            run,
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
    description = req.description.strip()[:MAX_DESC_CHARS]
    with AiRun("generate") as run:  # 이 요청 동안의 LLM 호출(분류·라우팅·계획·선택·그래프 확장)을 한 실행으로 기록
        try:
            if CURATE_VERSION == "v1":
                playbook = curate_dynamic(description, req.persona, make_json_caller(provider, run))
            else:
                # v2: 이벤트 분류 → 사전 생성 사례(few-shot) → 이벤트별 API 매핑으로 계획·grounding
                playbook, trace = curate_dynamic_v2(description, req.persona, make_json_caller(provider, run))
                _save_live_trace(req, provider.key, playbook, trace)
        except Exception as e:  # noqa: BLE001
            run.fail(e)
            print(f"[generate] 실패: {e!r}")  # 내부 오류 내용은 로그에만(사용자 화면에 예외 문자열을 내보내지 않는다)
            return JSONResponse({"error": "절차를 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."}, status_code=500)
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
    # 최근 대화를 한 덩어리로 만들어 최신 입력 중심으로 갱신.
    # 옛 사용자 발화 원문("2주 뒤", "5일 전" 같은 상대 표현)이 매 턴 다시 보이면 모델이 날짜를 재환산한다 →
    # 사용자 발화는 최신 것만 원문으로 주고 그 전 것은 가린다(이미 facts 에 반영됨). 어시스턴트 발화는 '직전에 되물었는지' 판단용으로 남긴다.
    prior_facts = (req.persona or {}).get("facts") or {}
    recent = [m for m in req.messages if m.role in CHAT_ROLES][-8:]
    last_user = max((i for i, m in enumerate(recent) if m.role == "user"), default=-1)
    convo = "\n".join(
        f"{m.role}: {m.content[:MAX_MSG_CHARS]}" if m.role != "user" or i == last_user else "user: (이전 입력 — 이미 facts 에 반영됨)"
        for i, m in enumerate(recent)
    )
    with AiRun("persona") as run:
        latest = next((m.content for m in reversed(recent) if m.role == "user"), "")
        result = make_json_caller(provider, run)(build_persona_prompt(req.persona or {}, guard=_needs_guard(latest)), convo, temperature=0.1)
    facts = result.get("facts")
    if not isinstance(facts, dict):
        facts = prior_facts
    conflict = result.get("conflict")
    if not (isinstance(conflict, dict) and conflict.get("question")):
        conflict = None
    # ★ 날짜 key 는 결정론적으로 먼저 정리한다 — 최신 입력에 날짜 표현이 있을 때만 파서 값으로 인정, 없으면 저장값 유지.
    #   (모델의 재환산은 여기서 되돌아가므로 아래 충돌 판정의 근거에서도 빠진다)
    facts, date_changed = _fix_relative_dates(facts, prior_facts, req)
    # ★ conflict는 '이미 저장된 facts'와의 모순일 때만 성립한다(룰 3a를 코드로 강제 · 모델 무관).
    #   대조할 기존 facts가 없으면(첫 입력 등) 모델이 confirm성 conflict를 만들어도 무시 → 빈 facts 루프 차단.
    if not prior_facts:
        conflict = None
    # ★ 기존 facts 의 어떤 값도 바뀌지 않았으면 모순이 아니다(모델이 같은 날짜를 두고 "맞나요?" 하던 가짜 conflict 차단).
    #   날짜 key 는 최신 입력에 날짜 표현이 있어 실제로 바뀐 경우에만 근거로 친다. 내부 key 이름이나 'facts' 를 노출하는 conflict 도 버린다.
    if conflict:
        changed_keys = [k for k, v in prior_facts.items() if k in facts and facts[k] != v and (k not in _DATE_KEYS or k in date_changed)]
        q = str(conflict.get("question", ""))
        if not changed_keys or "facts" in q or re.search(r"\b[a-z]+_[a-z_]+\b", q):
            conflict = None
    return JSONResponse({"facts": facts, "conflict": conflict})


_DATE_KEYS = ("move_date", "birth_date", "resignation_date", "marriage_date", "death_date", "event_date")


def _fix_relative_dates(facts: dict, prior: dict, req: PersonaReq) -> tuple[dict, set[str]]:
    """기준일(날짜 key)을 결정론적으로 정리한다. 돌려주는 집합 = 최신 입력의 날짜 표현으로 실제로 바뀐(인정된) 날짜 key.
    - 최신 사용자 문장에 날짜 표현이 없으면: 이미 저장된 날짜 key 는 모델이 바꿨어도 저장값으로 되돌린다(재환산 금지를 코드로 강제).
    - 날짜 표현이 있으면: 이번 턴에 새로 생기거나 바뀐 날짜 key 가 하나일 때 파서 값으로 맞추고, 하나도 없으면
      문장의 기준일 키워드(출생·이사·퇴사…) 또는 이벤트 기본 key 를 채운다."""
    changed_ok: set[str] = set()
    try:
        latest = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        parsed = reldate.parse_relative_date(latest, date.today())
        if not parsed:
            for k in _DATE_KEYS:
                if prior.get(k) and facts.get(k) and facts[k] != prior[k]:
                    print(f"[persona] {k}: 날짜 표현 없이 {prior[k]} → {facts[k]} 로 바뀜 → 기존 값 유지")
                    facts = {**facts, k: prior[k]}
            return facts, changed_ok
        iso, phrase = parsed[0].isoformat(), parsed[1]
        changed = [k for k in _DATE_KEYS if facts.get(k) and facts.get(k) != prior.get(k)]
        if len(changed) == 1:
            if str(facts[changed[0]])[:10] != iso:
                print(f"[persona] {changed[0]}: LLM {facts[changed[0]]} → 파서 {iso} ('{phrase}')")
                facts = {**facts, changed[0]: iso}
            if prior.get(changed[0]) and prior[changed[0]] != iso:
                changed_ok.add(changed[0])
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
        else:
            # 둘 이상 바뀌면 어느 것이 이번 문장 것인지 모른다 → 저장값이 있던 key 는 되돌린다
            for k in changed:
                if prior.get(k):
                    facts = {**facts, k: prior[k]}
    except Exception as e:  # noqa: BLE001 — 교차 검증 실패가 대화를 막지 않도록
        print(f"[persona] 날짜 교차 검증 실패: {e!r}")
    return facts, changed_ok


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
        if op["id"] not in deps and op["id"] not in board_status:
            continue  # 플레이북에도 보드에도 없는 id(지어낸 절차·스키마 예시 '<플레이북 절차 id>')는 카드로 만들지 않는다
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


_CRISIS_RE = re.compile(r"죽고\s*싶|자살|극단적\s*(?:인\s*)?(?:선택|생각)|살기\s*싫|살고\s*싶지\s*않|사는\s*게\s*(?:너무\s*|정말\s*)?(?:힘들|괴롭|지옥)|"
                        r"삶을\s*포기|생을\s*마감|목숨을\s*끊|사라지고\s*싶")
_HANGUL_RE = re.compile(r"[가-힣]")
# 주입·주제 이탈 신호 — 이런 턴에만 대화 프롬프트 맨 앞에 [지켜야 할 원칙]을 넣는다
_GUARD_TRIGGER_RE = re.compile(
    r"무시\s*(?:하고|해|하라|해라|하세요)|(?:대화|지시|내용|규칙|설정)\S*\s*(?:을|를)?\s*(?:모두\s*|전부\s*|다\s*)?(?:잊|무시)|"
    r"(?:너|다람이)(?:의|는|가)?\s*(?:어떤\s*)?(?:규칙|지시|설정|프롬프트)|규칙\s*(?:번호|내용|전부|그대로)|시스템\s*(?:공지|지시|메시지|프롬프트)|"
    r"\[\s*(?:시스템|관리자|system|admin)|관리자\s*모드|개발자\s*모드|이제부터\s*(?:너|넌|다람이)|너는\s*이제|역할\s*(?:을|를)?\s*(?:바꿔|맡아)|"
    r"레시피|요리법|코드\s*(?:를\s*)?(?:짜|작성|만들어)|코딩|파이썬|python|자바스크립트|javascript|"
    r"시\s*(?:한\s*편|를\s*써|써\s*줘)|소설\s*(?:을\s*)?써|가사\s*(?:를\s*)?써|(?:종목|주식)\s*추천|반말로|"
    r"ignore\s+(?:all\s+)?(?:the\s+)?previous|system\s*prompt|instructions?\b", re.I)


_DETAIL_Q_RE = re.compile(r"서류|준비물|필요한\s*(?:게|것|서류)|뭐가\s*필요|뭘\s*챙|챙겨|지참|구비|어디서|어디로|어디에|어디\s*가|온라인|인터넷|정부24|방문|"
                          r"신청\s*방법|어떻게\s*신청|신청하려면|발급|세대주|대리|위임|document|online|where", re.I)


def _asks_details(text: str) -> bool:
    """서류·장소·온라인·신청 방법을 묻는 턴 — 그때만 플레이북의 담당기관·온라인 여부·서류를 프롬프트에 넣는다."""
    return bool(_DETAIL_Q_RE.search(text or ""))


def _needs_guard(text: str) -> bool:
    return bool(text) and (bool(_GUARD_TRIGGER_RE.search(text)) or not _HANGUL_RE.search(text))


def _strip_ids(reply: str, playbook: dict) -> str:
    """답에 섞인 내부 절차 id('(law-foreign-resident-l1)', 'transfer-report')를 정리한다 — 괄호째 지우거나 절차 이름으로."""
    for p in sorted((p for p in playbook.get("procedures", []) or [] if isinstance(p, dict) and p.get("id")), key=lambda p: -len(str(p["id"]))):
        pid, name = str(p["id"]), str(p.get("name") or "")
        reply = re.sub(r"\s*\(\s*" + re.escape(pid) + r"\s*\)", "", reply)
        if "-" in pid and name:
            reply = re.sub(r"(?<![\w-])" + re.escape(pid) + r"(?![\w-])", name, reply)
    return reply


_GENERIC_PROC = {"건강보험 자격·주소 변경"}
_AMBIG_ALIAS = {"계약 만료"}


def _other_event(user_msgs: list[str], playbook: dict) -> str | None:
    """마지막 사용자 말이 지금 플레이북과 별개인 다른 생활 이벤트를 묻는지 — 그 이벤트 id.
    첫 턴(플레이북을 만든 바로 그 질문), 플레이북 절차 이름·조건·설명에 이미 나오는 낱말, 그 이벤트의 절차가 이미 있는 경우는 제외."""
    if len(user_msgs) < 2:
        return None
    text = user_msgs[-1]
    procs = [p for p in playbook.get("procedures", []) or [] if isinstance(p, dict)]
    names = {str(p.get("name", "")) for p in procs}
    blob = " ".join(f"{p.get('name', '')} {p.get('condition', '')} {p.get('summary', '')}" for p in procs)
    covered = {playbook.get("event")} | set(playbook.get("events") or [])
    for eid, e in EVENTS.items():
        if eid in covered:
            continue
        hit = [a for a in e.get("aliases", []) if a not in _AMBIG_ALIAS and a in text]
        if not hit or any(a in blob for a in hit):
            continue
        if ({x["name"] for x in e.get("apis", {}).get("law", [])} - _GENERIC_PROC) & names:
            continue
        return eid
    return None


_DANGER_RE = re.compile(r"때려|때리|폭행|가정\s*폭력|데이트\s*폭력|스토킹|협박|위협|학대")


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
    # (폭력·위기 상황을 말한 턴에는 날짜를 묻지 않는다 — 안전 안내가 먼저)
    latest_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    danger = bool(_CRISIS_RE.search(latest_user) or _DANGER_RE.search(latest_user))
    try:
        missing = "" if danger else deadline.missing_anchor_block(playbook, req.persona, req.board)
    except Exception:  # noqa: BLE001
        missing = ""
    # 한국어가 아닌 말로 물으면(한글 없이 영문 등) 이번 턴 reply 언어를 지정한다 — 규칙만으로는 한국어로 답하는 경우가 많다
    lang = ("[Response language — this turn]\nThe user is not writing in Korean. Write the entire \"reply\" in the user's language (e.g. English), "
            "not in Korean. Keep each procedure's Korean name with a short gloss in parentheses, e.g. 전입신고 (move-in report)."
            if latest_user and not _HANGUL_RE.search(latest_user) and len(re.findall(r"[A-Za-z]{2,}", latest_user)) >= 3 else "")
    # 지금 플레이북에 없는 다른 생활 이벤트를 물었으면(이사 대화 중 출산 등) 이번 턴에 새로 찾도록 안내하게 한다 — 규칙만으로는 절반쯤 지켜진다
    other = _other_event([m.content for m in req.messages if m.role == "user"], playbook or {})
    other_note = (f"[이번 턴 — 다른 생활 이벤트]\n사용자가 지금 할 일 목록에 없는 다른 생활 이벤트({EVENTS[other]['title']})를 물었다. "
                  f"그 일의 절차·기한·날짜는 말하지 말고, '처음 화면의 입력란에 {EVENTS[other]['title']} 상황을 적으면 맞춤 절차를 찾아 드려요'라고 안내한다. "
                  "boardOps 는 [] 로 둔다." if other else "")
    tail = "\n\n".join(x for x in (other_note, lang, DANGER_NOTE if danger else "") if x)
    system_prompt = build_system_prompt(playbook, req.persona, req.board, "\n\n".join(x for x in (guidance, missing) if x), req.deadlines, tail,
                                        guard=_needs_guard(latest_user), details=_asks_details(latest_user))
    provider = get_provider()
    board_status = {b.get("id"): b.get("status") for b in (req.board or []) if isinstance(b, dict) and b.get("id")}

    async def gen():
        if provider.client is None:
            yield sse("error", {"message": f"{provider.label} API 키가 설정되지 않았습니다. backend/.env를 확인하세요."})
            return

        # LLM 호출은 동기 → 스레드로 빼서 이벤트 루프를 막지 않음
        run = AiRun("chat")
        try:
            result = await asyncio.to_thread(call_chat, provider, system_prompt, req.messages, run)
        except Exception as e:  # noqa: BLE001
            run.finish(e)
            print(f"[chat] {provider.label} 호출 실패: {e!r}")  # 내부 오류 내용은 로그에만
            yield sse("error", {"message": "답변을 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."})
            return
        run.finish()

        reply = str(result.get("reply", "")).strip()
        reply = _strip_ids(reply, playbook or {})

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
        #    빠른답변 칩은 '부족한 정보(askMissing)를 고르게 하는' 용도 — 물을 것이 없으면 칩도 비운다(예시 베끼기·억지 칩 차단)
        ask_missing = [a for a in (result.get("askMissing") or []) if isinstance(a, str) and a.strip()]
        quick = _sanitize_quick_replies(result.get("quickReplies", [])) if ask_missing else []
        yield sse(
            "meta",
            {
                "eventDetected": result.get("eventDetected"),
                "askMissing": ask_missing,
                "quickReplies": quick,
                "boardOps": _sanitize_board_ops(result.get("boardOps", []), board_status, playbook),
                "userTodos": result.get("userTodos", []),
            },
        )
        yield sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream")
