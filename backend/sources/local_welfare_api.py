"""지자체복지서비스(복지로 지자체) API 클라이언트.

사용자 거주 지역의 지자체 복지(출산장려금·전세사기 이주비 등)를 찾아 grounding 한다.
보조금24 검색은 지역 필터 없이 서비스명 LIKE 상위 N건을 받은 뒤 걸러서 지자체 혜택을 놓치기 쉬운데,
이 API는 시도 필터(ctpvNm, 접두 매칭)와 서비스명+내용 검색(srchKeyCode=003)을 서버에서 해 준다.
- Base: https://apis.data.go.kr/B554287/LocalGovernmentWelfareInformations   (끝의 s 주의)
- 목록: /LcgvWelfarelist     (searchWrd, srchKeyCode, ctpvNm, sggNm, pageNo, numOfRows)
- 상세: /LcgvWelfaredetailed (servId)
- XML. 인증: serviceKey(.env DATA_GO_KR_KEY).
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

from .apicache import disk_cached

load_dotenv()

KEY = os.environ.get("DATA_GO_KR_KEY", "")
BASE = "https://apis.data.go.kr/B554287/LocalGovernmentWelfareInformations"
TIMEOUT = 15
NO_DEADLINE = 9999

# 데이터상 광주·전남은 '전남광주통합특별시'로 묶여 있다('광주광역시'로는 0건).
_CTPV_ALIAS = {"광주광역시": "전남광주", "전라남도": "전남광주"}

_cache: dict[str, dict] = {}


def _today() -> str:
    return date.today().isoformat()


def _get(ep: str, params: dict):
    try:
        r = requests.get(f"{BASE}/{ep}", params={"serviceKey": KEY, **params}, timeout=TIMEOUT)
        r.raise_for_status()
        return ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return None


def _clean(s: str | None) -> str:
    return (s or "").replace("\r\n", " ").replace("\n", " ").strip()


_AREA_PREFIX = re.compile(r"^[(\[]?([가-힣]{2,4}[시군구])")


def _area_of(s) -> str:
    """사업의 시군구. sggNm이 비어 있어도 담당부서('경기도 파주시 …')나 사업명('(파주시)…')에 드러나는 경우가 많다."""
    sgg = _clean(s.findtext("sggNm"))
    if sgg:
        return sgg
    dept = _clean(s.findtext("bizChrDeptNm")).split()
    if len(dept) >= 2 and re.fullmatch(r"[가-힣]+[시군구]", dept[1]):
        return dept[1]
    m = _AREA_PREFIX.match(_clean(s.findtext("servNm")))
    return m.group(1) if m else ""


def _sgg_mentioned(sgg: str, text: str) -> bool:
    """시군구명이 사용자 텍스트에 나오는지. '양평군'→'양평'처럼 접미사를 뗀 형태도 허용(2자 이상일 때만)."""
    if sgg in text:
        return True
    short = re.sub(r"[시군구]$", "", sgg)
    return len(short) >= 2 and short in text


@disk_cached("local_search")
def _search_one(kw: str, sido: str | None, text: str, per_kw: int) -> list[dict]:
    params = {"pageNo": 1, "numOfRows": 300, "searchWrd": kw, "srchKeyCode": "003"}
    if sido:
        params["ctpvNm"] = _CTPV_ALIAS.get(sido, sido)
    # 이 API는 간헐적으로 resultCode 99(UNKNOWN_ERROR)·타임아웃을 낸다 → 한 번 더 시도
    for _ in range(2):
        root = _get("LcgvWelfarelist", params)
        if root is not None and root.findtext("resultCode") in (None, "0", "00"):
            break
    if root is None or root.findtext("resultCode") not in (None, "0", "00"):
        return []
    out = []
    for s in root.findall("servList"):
        sgg = _area_of(s)
        if sgg:
            # 시군구 사업: 사용자가 그 시군구를 언급했을 때만. 시도를 모르면 '중구·서구' 같은 중의적 이름은 제외.
            if not _sgg_mentioned(sgg, text) or (not sido and len(sgg) <= 2):
                continue
        elif not sido:
            continue  # 광역 사업인데 사용자 시도를 모름 → 어느 광역인지 판단 불가
        ctpv = _clean(s.findtext("ctpvNm"))
        out.append({
            "service_id": s.findtext("servId"),
            "name": _clean(s.findtext("servNm")),
            "agency": _clean(s.findtext("bizChrDeptNm")) or f"{ctpv} {sgg}".strip(),
            "org_type": "지자체",
            "summary": _clean(s.findtext("servDgst"))[:120],
            "link": _clean(s.findtext("servDtlLink")),
            "source": "local",
        })
        if len(out) >= per_kw:
            break
    return out


def search_local(keywords: list[str], sido: str | None, text: str, per_kw: int = 8) -> list[dict]:
    """이벤트 키워드로 사용자 지역의 지자체 복지 후보를 모은다(search_services 와 같은 후보 스키마).

    sido: 사용자 시도(모르면 None → 전국 검색 후 텍스트에 언급된 시군구 사업만).
    text: 사용자 설명·페르소나 원문(시군구 언급 판정용).
    """
    if not KEY:
        return []
    kws = [k.strip() for k in keywords if k and k.strip()]
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(lambda k: _search_one(k, sido, text, per_kw), kws))
    out: dict[str, dict] = {}
    for rows in results:
        for c in rows:
            if c["service_id"] and c["service_id"] not in out:
                out[c["service_id"]] = c
    return list(out.values())


@disk_cached("local_welfare")
def ground_local_service(serv_id: str, list_fields: dict | None = None) -> dict:
    """지자체 복지 상세조회로 grounding (welfare_api 와 같은 결과 스키마). 실패 시 needs_review."""
    if serv_id in _cache:
        return _cache[serv_id]
    root = _get("LcgvWelfaredetailed", {"servId": serv_id})
    if root is None or not root.findtext("servNm"):
        return {"verification_status": "needs_review", "error": "지자체복지서비스 상세조회 실패"}
    svc = list_fields or {}

    def t(tag):
        return _clean(root.findtext(tag))

    def reld_names(list_tag):
        return [_clean(el.findtext("wlfareInfoReldNm")) for el in root.findall(list_tag) if el.findtext("wlfareInfoReldNm")]

    documents = [re.sub(r"\.(hwpx?|pdf|docx?)$", "", nm).strip()
                 for nm in reld_names("basfrmList") if "신청" in nm and "안내" not in nm]
    target = t("sprtTrgtCn")
    criteria = t("slctCritCn")
    content = t("alwServCn")
    result = {
        "verification_status": "verified",
        "source": "복지로(지자체복지서비스)",
        "service_id": serv_id,
        "service_name": t("servNm"),
        "agency": t("bizChrDeptNm") or f"{t('ctpvNm')} {t('sggNm')}".strip(),
        "documents": documents,
        "deadline_text": "",  # 이 API는 신청기한을 주지 않는다(사업기간 enfcEndYmd만 있음)
        "deadline_days": NO_DEADLINE,
        "summary": t("servDgst") or content[:200],
        "support_target": " ".join(x for x in (target, criteria) if x),
        "support_content": content,
        "apply_method": t("aplyMtdCn"),
        "legal": ", ".join(reld_names("baslawList")),
        "source_url": svc.get("link") or "",
        "apply_url": "",
        "evidence_span": (target or content)[:300],
        "fetched_at": _today(),
    }
    _cache[serv_id] = result
    return result
