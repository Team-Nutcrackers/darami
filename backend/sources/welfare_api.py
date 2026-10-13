"""보조금24(행정안전부 대한민국 공공서비스) API 클라이언트.

복지/혜택 절차(첫만남이용권·부모급여·아동수당 등)의 담당기관·구비서류·신청기한·
지원내용·근거법령·출처를 실시간 grounding 한다.

- Base: https://api.odcloud.kr/api/gov24/v3
- serviceList  : 서비스 목록(서비스명 LIKE 검색) — 소관기관/지원대상/지원내용/신청기한/상세URL
- serviceDetail: 서비스 상세(서비스ID EQ) — 구비서류/법령/신청방법/소관기관/신청기한 등
- 인증: serviceKey 쿼리파라미터(.env DATA_GO_KR_KEY), 일 50만건
"""

from __future__ import annotations

import os
import re
from datetime import date

import requests
from dotenv import load_dotenv

from .apicache import disk_cached

load_dotenv()

KEY = os.environ.get("DATA_GO_KR_KEY", "")
BASE = "https://api.odcloud.kr/api/gov24/v3"
TIMEOUT = 10
NO_DEADLINE = 9999  # '상시신청' 등 마감일 없음 sentinel

_cache: dict[str, dict] = {}


def _today() -> str:
    return date.today().isoformat()


@disk_cached("gov24")
def _get(ep: str, params: dict) -> dict | None:
    try:
        r = requests.get(f"{BASE}/{ep}", params={"serviceKey": KEY, **params}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def _find_service(name: str, region: str | None = None) -> dict | None:
    """서비스명으로 검색.
    region=None  → 중앙행정기관(국가 혜택)만.
    region 지정  → 소관기관명이 region으로 시작하는 지자체/광역 서비스만(예: '서울특별시', '서울특별시 양천구').
    """
    data = _get("serviceList", {"page": 1, "perPage": 30, "cond[서비스명::LIKE]": name})
    rows = (data or {}).get("data") or []
    if region:
        # 정확매칭 우선(예: region='서울특별시' → 광역 직속만, 산하 시군 제외)
        exact = [r for r in rows if (r.get("소관기관명") or "").strip() == region]
        if exact:
            return exact[0]
        # 시군구까지 지정된 region(예: '서울특별시 양천구')은 접두 매칭 허용
        if " " in region:
            local = [r for r in rows if (r.get("소관기관명") or "").startswith(region)]
            return local[0] if local else None
        return None
    central = [r for r in rows if r.get("소관기관유형") == "중앙행정기관"]
    return central[0] if central else None


def _parse_docs(raw: str) -> list[str]:
    if not raw or raw.strip() in ("해당없음", "-"):
        return []
    out = []
    for line in re.split(r"[\r\n]+", raw):
        line = line.strip().lstrip("-·•").strip()
        if line and line != "해당없음":
            out.append(line)
    return out


def _parse_deadline(raw: str) -> tuple[str, int]:
    """신청기한 문자열 → (표시 텍스트, 환산일수 또는 sentinel)."""
    raw = (raw or "").strip()
    if not raw or "상시" in raw:
        return (raw or "상시신청", NO_DEADLINE)
    m = re.search(r"(\d+)\s*(일|개월|년)", raw)
    if m:
        unit = {"일": 1, "개월": 30, "년": 365}[m.group(2)]
        return (raw, int(m.group(1)) * unit)
    return (raw, NO_DEADLINE)


def _assemble(sid: str, list_fields: dict, detail: dict) -> dict:
    """serviceDetail(+list 필드)로 grounding 스키마를 조립."""
    d, svc = detail, (list_fields or {})
    deadline_text, deadline_days = _parse_deadline(d.get("신청기한") or svc.get("신청기한"))
    target = (d.get("지원대상") or svc.get("지원대상") or "").strip()
    content = (d.get("지원내용") or svc.get("지원내용") or "").strip()
    purpose = (d.get("서비스목적") or svc.get("서비스목적요약") or "").strip()
    return {
        "verification_status": "verified",  # 보조금24(공식 정부 출처)에서 확인됨
        "source": "보조금24",
        "service_id": sid,
        "service_name": d.get("서비스명") or svc.get("서비스명"),
        "agency": d.get("소관기관명") or svc.get("소관기관명") or "",
        "documents": _parse_docs(d.get("구비서류", "")),
        "deadline_text": deadline_text,
        "deadline_days": deadline_days,
        "summary": purpose or content[:200],
        "support_target": target,
        "support_content": content,
        "apply_method": (d.get("신청방법") or "").strip(),
        "legal": (d.get("법령") or "").strip(),
        "source_url": svc.get("상세조회URL") or f"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/{sid}",
        "apply_url": (d.get("온라인신청사이트URL") or "").strip(),
        "evidence_span": (target or content)[:300],
        "fetched_at": _today(),
    }


def ground_service(service_id: str, list_fields: dict | None = None) -> dict:
    """서비스ID로 직접 grounding (동적 큐레이션이 검색으로 찾은 servId에 사용)."""
    if not service_id:
        return {"verification_status": "needs_review", "error": "servId 없음"}
    if service_id in _cache:
        return _cache[service_id]
    detail = _get("serviceDetail", {"page": 1, "perPage": 1, "cond[서비스ID::EQ]": service_id})
    drows = (detail or {}).get("data") or []
    if not drows:
        return {"verification_status": "needs_review", "error": "상세조회 실패"}
    result = _assemble(service_id, list_fields or {}, drows[0])
    _cache[service_id] = result
    return result


def ground_welfare(service_name: str, region: str | None = None) -> dict:
    """복지 서비스명을 grounding. region 지정 시 해당 지자체/광역 서비스. 실패 시 needs_review."""
    cache_key = f"{service_name}|{region or ''}"
    if cache_key in _cache:
        return _cache[cache_key]
    if not KEY:
        return {"verification_status": "needs_review", "error": "DATA_GO_KR_KEY 미설정"}
    svc = _find_service(service_name, region=region)
    if not svc:
        scope = f"({region}) " if region else ""
        return {"verification_status": "needs_review", "error": f"{scope}'{service_name}' 검색 결과 없음"}
    result = ground_service(svc.get("서비스ID"), svc)
    result["region_scope"] = region or "중앙"
    _cache[cache_key] = result
    return result


def search_services(keywords: list[str], region: str | None = None, per_kw: int = 8) -> list[dict]:
    """이벤트 키워드들로 보조금24를 검색해 후보 서비스 목록을 모은다(중앙 + 사용자 지역만).

    동적 이벤트 source-first 디스커버리용. 반환: [{service_id, name, agency, org_type, summary}]
    """
    if not KEY:
        return []
    out: dict[str, dict] = {}
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        data = _get("serviceList", {"page": 1, "perPage": per_kw, "cond[서비스명::LIKE]": kw})
        for s in (data or {}).get("data") or []:
            org = s.get("소관기관명") or ""
            org_type = s.get("소관기관유형") or ""
            # 중앙행정기관 OR 사용자 거주지(시도/시군구) 소관만 — 타 지역 노이즈 제거
            keep = org_type == "중앙행정기관" or (region and org.startswith(region))
            if not keep:
                continue
            sid = s.get("서비스ID")
            if sid and sid not in out:
                out[sid] = {
                    "service_id": sid,
                    "name": s.get("서비스명"),
                    "agency": org,
                    "org_type": org_type,
                    "summary": (s.get("서비스목적요약") or s.get("지원내용") or "").replace("\r\n", " ")[:120],
                }
    return list(out.values())
