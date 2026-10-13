"""중앙부처복지서비스(복지로) API 클라이언트.

보조금24에 없는 중앙정부 복지(예: 부모급여)를 grounding 한다.
- Base: https://apis.data.go.kr/B554287/NationalWelfareInformationsV001
- 목록: /NationalWelfarelistV001   (srchKeyCode=003 서비스명 + searchWrd)
- 상세: /NationalWelfaredetailedV001 (servId)
- XML. 개발계정 100건/일 → 프로세스 캐시 필수.
- 인증: serviceKey(.env DATA_GO_KR_KEY).
"""

from __future__ import annotations

import os
import re
from datetime import date
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

from .apicache import disk_cached

load_dotenv()

KEY = os.environ.get("DATA_GO_KR_KEY", "")
BASE = "https://apis.data.go.kr/B554287/NationalWelfareInformationsV001"
TIMEOUT = 12
NO_DEADLINE = 9999

_cache: dict[str, dict] = {}


def _today() -> str:
    return date.today().isoformat()


def _get(ep: str, params: dict):
    try:
        r = requests.get(f"{BASE}/{ep}", params={"serviceKey": KEY, **params}, timeout=TIMEOUT)
        r.raise_for_status()
        return ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError):
        return None


def _find_service(name: str) -> dict | None:
    root = _get("NationalWelfarelistV001", {"pageNo": 1, "numOfRows": 5, "srchKeyCode": "003", "searchWrd": name})
    if root is None:
        return None
    for s in root.findall(".//servList"):
        return {"servId": s.findtext("servId"), "servNm": s.findtext("servNm"),
                "link": s.findtext("servDtlLink"), "onap": s.findtext("onapPsbltYn")}
    return None


def _detail_groups(root) -> list[tuple[str, str, str]]:
    """(servSeCode, servSeDetailNm, servSeDetailLink) 그룹을 모은다."""
    out = []
    for el in root.iter():
        if el.find("servSeCode") is not None:
            out.append((el.findtext("servSeCode") or "", el.findtext("servSeDetailNm") or "",
                        el.findtext("servSeDetailLink") or ""))
    return out


def ground_national_welfare(service_name: str) -> dict:
    """중앙부처복지서비스로 복지 절차를 grounding. 실패 시 needs_review."""
    if service_name in _cache:
        return _cache[service_name]
    if not KEY:
        return {"verification_status": "needs_review", "error": "DATA_GO_KR_KEY 미설정"}

    svc = _find_service(service_name)
    if not svc or not svc.get("servId"):
        return {"verification_status": "needs_review", "error": f"'{service_name}' 검색 결과 없음"}
    result = ground_national_service(svc["servId"], svc)
    if result.get("verification_status") == "verified":
        _cache[service_name] = result
    return result


@disk_cached("national_welfare")
def ground_national_service(serv_id: str, svc: dict | None = None) -> dict:
    """servId로 직접 grounding (이벤트 API 매핑에 고정된 servId 용). 실패 시 needs_review."""
    if serv_id in _cache:
        return _cache[serv_id]
    if not KEY:
        return {"verification_status": "needs_review", "error": "DATA_GO_KR_KEY 미설정"}
    svc = svc or {}
    root = _get("NationalWelfaredetailedV001", {"servId": serv_id})
    if root is None or not root.findtext(".//servNm"):
        return {"verification_status": "needs_review", "error": "상세조회 실패"}

    def t(tag):
        return (root.findtext(f".//{tag}") or "").replace("\r\n", " ").strip()

    groups = _detail_groups(root)
    # 서류(040 중 '신청서'류), 근거법령(030)
    documents = []
    for code, nm, _ in groups:
        if code == "040" and "신청" in nm and "안내" not in nm:
            documents.append(re.sub(r"\.(hwpx?|pdf|docx?)$", "", nm).strip())
    legal = next((nm for code, nm, _ in groups if code == "030" and nm), "")

    agency = t("jurMnofNm") or t("jurOrgNm")
    target = t("tgtrDtlCn")
    content = t("alwServCn")
    outline = t("wlfareInfoOutlCn")

    result = {
        "verification_status": "verified",
        "source": "복지로(중앙부처복지서비스)",
        "service_id": serv_id,
        "service_name": svc.get("servNm") or t("servNm"),
        "agency": agency,
        "documents": documents,
        "deadline_text": "상시신청",
        "deadline_days": NO_DEADLINE,
        "summary": outline or content[:200],
        "support_target": target,
        "support_content": content,
        "legal": legal,
        "source_url": svc.get("link") or t("servDtlLink")
        or f"https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId={serv_id}",
        "evidence_span": (target or content)[:300],
        "fetched_at": _today(),
    }
    _cache[serv_id] = result
    return result
