"""On-demand 플레이북 큐레이션 agent (Layer 0).

이벤트가 처음 요청될 때, 사람이 검수한 thin seed(절차 목록 + 법령 ref)를 바탕으로
allowlist 소스(현재: 국가법령정보 OPEN API)에서 근거·기한을 실시간 grounding 하고,
검증 상태와 출처를 붙인 playbook 을 만들어 캐시한다. 이후 호출은 캐시 재사용.

- 절차 목록/조건/서류 같은 'seed content'는 playbooks/*.json 을 seed/fallback 으로 재사용한다.
- deadline·담당부처·출처·법적근거는 agent 가 law API 로 grounding 한다(내가 손으로 적지 않음).
- grounding 안 되는 필드(서류·운영채널 등)는 'unverified'로 정직하게 표기.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import json as _json

from .law_api import ground_legal, ground_legal_auto
from .welfare_api import ground_welfare, search_services, ground_service
from .national_welfare_api import ground_national_welfare
from .local_welfare_api import search_local, ground_local_service
from . import kb
from .common import COMMON, CROSSCUT_KEYS, common_ref, catalog, learn
from prompts import build_dynamic_plan_prompt, build_dynamic_curate_prompt

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "playbooks"

# 사람이 검수한 thin seed: 절차 → 법령 근거(allowlist 신뢰 경계).
# expected_days=None 이면 '근거 조문만' 확인(partial). 라이브 API로 실재 확인된 매핑만 등록.
SEED_LEGAL: dict[str, dict] = {
    # 공통 카탈로그 참조(단일 소스): 신고/등록 + cross-cutting
    "transfer-report": common_ref("transfer-report"),
    "birth-report": common_ref("birth-report"),
    "car-address": common_ref("car-address"),
    "health-insurance-change": common_ref("health-insurance-change"),
    "pension-change": common_ref("pension-change"),
    # 시나리오 고유 법령 절차
    "school-transfer": {"law_query": "초·중등교육법 시행령", "article_title": "초등학교의 전학절차", "expected_days": None},
    "severance-pay": {"law_query": "근로자퇴직급여 보장법", "article_title": "퇴직금의 지급", "expected_days": 14},
}

# 복지/혜택 절차 → 보조금24 서비스명.
# 값이 str  : 중앙행정기관(국가) 혜택.
# 값이 dict : {"query": 서비스명, "region": 소관기관 접두(지자체/광역)}.
SEED_WELFARE: dict[str, object] = {
    # 출산 (중앙)
    "first-meet": "첫만남이용권",
    "child-allowance": "아동수당",
    "child-care-allowance": "가정양육수당",
    # 출산 (광역/지자체 — 사용자 거주지로 동적 grounding. region 모르면 미노출)
    "seoul-maternity-transport": {"query": "임산부 교통비", "scope": "local"},
    # 퇴사 (중앙)
    "unemployment-benefit": "구직급여",
    "tomorrow-learning-card": "국민내일배움카드",
    "early-reemployment": "조기재취업수당",
}

# 중앙부처복지서비스(복지로) → 보조금24에 없는 중앙 복지(예: 부모급여)
SEED_NATIONAL: dict[str, str] = {
    "parent-allowance": "부모급여",
}

_play_cache: dict[str, dict] = {}


def _load_seed(event: str) -> dict | None:
    path = PLAYBOOK_DIR / f"{event}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _enrich(proc: dict, region: str | None = None) -> dict:
    """절차 하나를 grounding 으로 보강 (법령 또는 복지 출처)."""
    pid = proc.get("id")
    if pid in SEED_LEGAL:
        return _enrich_legal(dict(proc), SEED_LEGAL[pid])
    if pid in SEED_WELFARE:
        return _enrich_welfare(dict(proc), SEED_WELFARE[pid], region)
    if pid in SEED_NATIONAL:
        return _enrich_national(dict(proc), SEED_NATIONAL[pid])
    out = dict(proc)
    out["verification_status"] = "unverified"
    return out


def _apply_grounding(out: dict, g: dict, source_label: str, set_name: bool = False) -> dict:
    """복지 grounding 결과(공통 스키마)를 절차에 반영."""
    status = g.get("verification_status", "needs_review")
    out["verification_status"] = status
    if status != "verified":
        return out
    if set_name and g.get("service_name"):
        out["name"] = g["service_name"]
    if g.get("agency"):
        out["agency"] = g["agency"]
    if g.get("documents"):
        out["documents"] = g["documents"]
    out["deadline"] = g.get("deadline_text") or out.get("deadline")
    out["deadlineDays"] = g.get("deadline_days", out.get("deadlineDays"))
    if g.get("summary"):
        out["summary"] = g["summary"]
    # 적용 조건: grounding된 '지원대상'을 넣어 LLM이 정확히 판단하게 한다(예: 0~23개월, 출산 후 6개월)
    target = (g.get("support_target") or "").replace("\r\n", " ").replace("○", "").strip()
    if target:
        out["condition"] = target[:140]
    out["link"] = g.get("apply_url") or g.get("source_url") or out.get("link")
    out["source"] = {
        "name": f"{source_label} · {g.get('agency', '')}".rstrip(" ·"),
        "tier": "official",
        "checkedAt": g.get("fetched_at", ""),
    }
    return out


def _enrich_welfare(out: dict, spec, region: str | None = None) -> dict:
    """보조금24로 복지 절차를 grounding.
    spec: str(중앙) 또는 {"query", region|scope}(지자체/광역)."""
    is_local = isinstance(spec, dict) and spec.get("scope") == "local"
    if isinstance(spec, dict):
        if is_local:
            if not region:
                out["verification_status"] = "needs_review"  # 거주지 모르면 제외
                return out
            g = ground_welfare(spec["query"], region=region)
        else:
            g = ground_welfare(spec["query"], region=spec.get("region"))
    else:
        g = ground_welfare(spec)
    return _apply_grounding(out, g, "보조금24", set_name=is_local)


def _enrich_national(out: dict, service_name: str) -> dict:
    """중앙부처복지서비스(복지로)로 grounding (보조금24에 없는 중앙 복지)."""
    g = ground_national_welfare(service_name)
    return _apply_grounding(out, g, "복지로")


def _apply_legal(out: dict, g: dict) -> dict:
    """법령 grounding 결과(g)를 절차에 반영(_enrich_legal/auto 공용)."""
    status = g.get("verification_status", "needs_review")
    out["verification_status"] = status
    if status in ("verified", "partial") and g.get("article"):
        ministry = g.get("ministry")
        if ministry:
            channel = out.get("agency", "").strip()
            out["agency"] = f"{ministry} · {channel}".rstrip(" ·") if channel else ministry
        out["source"] = {
            "name": f"{g['law_name']} {g['article']}",
            "tier": "official" if status == "verified" else "reference",
            "checkedAt": g.get("fetched_at", ""),
        }
        if status == "verified" and g.get("deadline_days_in_law"):
            out["deadlineDays"] = g["deadline_days_in_law"]
        # 동적 절차는 deadline/summary가 비어 있으므로 법령 데이터로 채운다(seed가 있으면 유지)
        if not (out.get("deadline") or "").strip() and g.get("deadline_in_law_text"):
            out["deadline"] = g["deadline_in_law_text"]
        if not (out.get("summary") or "").strip():
            ev = (g.get("evidence_span") or "").strip()
            out["summary"] = ev or f"{g.get('law_name', '')} {g.get('article', '')}에 근거한 절차입니다.".strip()
    return out


def _enrich_legal(out: dict, ref: dict) -> dict:
    return _apply_legal(out, ground_legal(**ref))


def curate_event(event: str, region: str | None = None, force: bool = False) -> dict | None:
    """이벤트 플레이북을 on-demand 로 큐레이션(grounding)하고 캐시.
    region: 사용자 거주지(시도). 지자체/광역 혜택 grounding에 사용. 모르면 지자체 절차 미노출."""
    cache_key = f"{event}|{region or ''}"
    if not force and cache_key in _play_cache:
        return _play_cache[cache_key]
    seed = _load_seed(event)
    if not seed:
        return None
    # 큐레이션 agent가 grounding 가능한(법령/복지 seed 등록된) 절차만 내보낸다.
    # 손으로 만든 미검증 절차는 보드에 띄우지 않는다(정직성).
    groundable = {p.get("id") for p in seed.get("procedures", [])} & (
        SEED_LEGAL.keys() | SEED_WELFARE.keys() | SEED_NATIONAL.keys()
    )
    grounded = [_enrich(p, region) for p in seed.get("procedures", []) if p.get("id") in groundable]
    grounded = [p for p in grounded if p.get("verification_status") in ("verified", "partial")]
    pb = dict(seed)
    pb["procedures"] = kb.apply(grounded)
    _play_cache[cache_key] = pb
    return pb


def legal_for(procedure_id: str) -> dict:
    """절차의 법적 근거 단건 조회(/api/legal-basis 용). 미지원이면 supported:false."""
    ref = SEED_LEGAL.get(procedure_id)
    if not ref:
        return {"supported": False}
    g = ground_legal(**ref)
    return {"supported": True, "procedureId": procedure_id, **g}


# ---------- 동적("?") 이벤트 source-first 큐레이션 ----------
_SIDO = [
    ("서울", "서울특별시"), ("부산", "부산광역시"), ("대구", "대구광역시"), ("인천", "인천광역시"),
    ("광주", "광주광역시"), ("대전", "대전광역시"), ("울산", "울산광역시"), ("세종", "세종특별자치시"),
    ("경기", "경기도"), ("강원", "강원"), ("충북", "충청북도"), ("충남", "충청남도"),
    ("전북", "전북"), ("전남", "전라남도"), ("경북", "경상북도"), ("경남", "경상남도"), ("제주", "제주"),
]


def _region_from_text(*texts: str) -> str | None:
    blob = " ".join(t for t in texts if t)
    for short, canon in _SIDO:
        if short in blob:
            return canon
    return None


def _region_from_persona(persona: dict) -> str | None:
    blob = " ".join(str(v) for v in ((persona or {}).get("facts") or {}).values() if isinstance(v, str))
    return _region_from_text(blob)


def _norm_name(name: str) -> str:
    return re.sub(r"[\s()·ㆍ'\"]", "", name or "")


def _base_proc(pid: str, name: str, priority: str, condition: str) -> dict:
    return {
        "id": pid, "name": name or "절차", "priority": priority if priority in ("must", "nice") else "nice",
        "condition": condition or "", "deadline": "", "deadlineDays": 9999, "agency": "",
        "online": True, "documents": [], "link": "",
        "source": {"name": "", "tier": "reference", "checkedAt": ""}, "summary": "",
        "verification_status": "unverified",
    }


def _judge_article(solar_json, name: str, law_name: str, article: str) -> bool:
    """검증자: 찾은 조문이 정말 그 절차의 '직접 근거 조문'인지 LLM이 yes/no 판정.
    키워드 검색의 mis-grounding(엉뚱한 조문 매칭)을 막아 잘못된 학습을 방지."""
    try:
        r = solar_json(
            "너는 한국 법령 검증자다. 주어진 조문이 해당 행정 절차의 '직접적 근거 조문'이 맞는지만 판단한다."
            ' 반드시 JSON {"match": true|false} 하나로만 답한다.',
            f"절차: {name}\n조문: {law_name} {article}", 0.0)
        return bool(r.get("match"))
    except Exception:  # noqa: BLE001
        return False


def _proc_from_common(key: str) -> dict:
    """공통/학습 카탈로그 절차를 고정 ref로 결정론적 grounding."""
    c = catalog()[key]
    out = _base_proc(key, c["name"], c.get("priority", "nice"), c.get("condition", ""))
    return _enrich_legal(out, common_ref(key))


def curate_dynamic(description: str, persona: dict, solar_json) -> dict:
    """자유 입력 이벤트를 source-first로 grounding.
    solar_json(system, user, temperature) → dict (main의 _solar_json 주입)."""
    # 거주지는 페르소나 + 설명 텍스트 둘 다에서 추출(생성 시점엔 페르소나가 비어있을 수 있음)
    persona_blob = " ".join(str(v) for v in ((persona or {}).get("facts") or {}).values() if isinstance(v, str))
    region = _region_from_text(persona_blob, description)
    ctx = f"[이벤트] {description}\n[페르소나] {_json.dumps(persona or {}, ensure_ascii=False)}"

    # 1) 계획: 검색 키워드 + 핵심 법령 절차 (공통+학습 카탈로그를 프롬프트에 주입)
    cat = catalog()
    common_block = "\n".join(
        f"- {k}: {v['name']}{' (crosscut)' if v.get('crosscut') else ''} — {v.get('condition', '')}"
        for k, v in cat.items()
    )
    try:
        plan = solar_json(build_dynamic_plan_prompt(common_block), ctx, 0.2)
    except Exception:  # noqa: BLE001
        plan = {}
    keywords = [k for k in (plan.get("search_keywords") or []) if isinstance(k, str)][:10]

    procedures: list[dict] = []
    used_common: set[str] = set()

    # 2) source-first: 보조금24 + 지자체복지서비스 검색 → 후보 → LLM 선별 → grounding
    candidates = search_services(keywords, region=region) if keywords else []
    if keywords:
        # 같은 사업이 보조금24에도 있으면 보조금24 쪽(서류·기한이 더 풍부)을 남긴다
        seen = {_norm_name(c["name"]) for c in candidates}
        candidates += [c for c in search_local(keywords, region, f"{persona_blob} {description}")
                       if _norm_name(c["name"]) not in seen]
    if candidates:
        brief = [{"service_id": c["service_id"], "name": c["name"], "agency": c["agency"], "summary": c["summary"]}
                 for c in candidates]
        try:
            sel = solar_json(build_dynamic_curate_prompt(),
                             ctx + "\n[후보 서비스]\n" + _json.dumps(brief, ensure_ascii=False), 0.2)
        except Exception:  # noqa: BLE001
            sel = {}
        by_id = {c["service_id"]: c for c in candidates}
        for s in (sel.get("selected") or [])[:6]:
            sid = s.get("service_id")
            if sid not in by_id:
                continue
            if by_id[sid].get("source") == "local":
                g, label = ground_local_service(sid, by_id[sid]), "복지로"
            else:
                g, label = ground_service(sid, by_id[sid]), "보조금24"
            if g.get("verification_status") != "verified":
                continue
            out = _base_proc(s.get("id") or sid, by_id[sid]["name"], s.get("priority"), s.get("condition"))
            procedures.append(_apply_grounding(out, g, label, set_name=True))

    # 3) 핵심 법령/신고 절차 — 카탈로그(공통+학습) 매칭은 결정론적 grounding,
    #    아니면 LLM이 준 법령으로 '키워드 검색 grounding' 시도 → 성공 시 학습 카탈로그에 영속, 실패 시 ai
    for lp in (plan.get("legal_procedures") or [])[:6]:
        ck = lp.get("common")
        if ck in cat:
            procedures.append(_proc_from_common(ck))
            used_common.add(ck)
            continue
        name = lp.get("name") or "절차"
        out = _base_proc(lp.get("id") or "proc", name, lp.get("priority"), lp.get("condition"))
        law_q = lp.get("law_query")
        if law_q:
            # 절차명·LLM힌트를 키워드로 조문 검색(정확한 조문명 불필요, 기한은 LLM 추측 안 믿고 법령값 사용)
            kws = [k for k in (lp.get("article_title"), name,
                               name.replace(" 신청", "").replace(" 신고", "").strip()) if k]
            g = ground_legal_auto(law_q, kws, None)
            # 검증자: 찾은 조문이 정말 이 절차의 근거인가? (mis-grounding·잘못된 학습 방지)
            if g.get("article") and _judge_article(solar_json, name, g.get("law_name"), g.get("article")):
                out = _apply_legal(out, g)
                lk = lp.get("id") or name
                learn(lk, name, {"law_query": law_q, "article_title": g.get("article_title") or kws[0],
                                 "expected_days": None},
                      condition=lp.get("condition", ""), priority=lp.get("priority", "nice"))
        if out.get("verification_status") not in ("verified", "partial"):
            out["verification_status"] = "unverified"  # AI 생성·자동 검증 전 (또는 검증자 거부)
            if not (out.get("summary") or "").strip():
                out["summary"] = "AI가 제안한 절차입니다. 정확한 요건·서류·기한은 담당 기관에서 확인하세요."
        procedures.append(out)

    # 4) cross-cutting 공통 (해당 시 후보) — 결정론적 grounding
    for ck in (plan.get("applicable_common") or []):
        if ck in cat and ck in CROSSCUT_KEYS and ck not in used_common:
            procedures.append(_proc_from_common(ck))
            used_common.add(ck)

    return {
        "event": plan.get("event") or "custom",
        "title": plan.get("title") or "내 생활 이벤트",
        "emoji": plan.get("emoji") or "🌰",
        "intro": plan.get("intro") or "",
        "procedures": procedures,
    }
