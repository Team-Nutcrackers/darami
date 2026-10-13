"""국립중앙의료원 전국 응급의료기관 정보 API 클라이언트.

교통사고·산재·재난·중증질환 등 '지금 치료가 급한' 이벤트에, 사용자 지역의 응급실·권역외상센터를 붙인다.
- Base: https://apis.data.go.kr/B552657/ErmctInfoInqireService
- 응급의료기관 목록: /getEgytListInfoInqire   (Q0=시도, Q1=시군구)
- 외상센터 목록   : /getStrmListInfoInqire    (Q0=시도)
- XML. 인증: serviceKey(.env DATA_GO_KR_KEY).

실시간 가용병상(/getEmrrmRltmUsefulSckbdInfoInqire)은 분 단위로 바뀌므로 캐시되는 플레이북에는 넣지 않는다
(기관 목록·종별·연락처·주소만). 실시간 병상은 e-gen 또는 화면 조회 시점에 별도로 보여줘야 한다.
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
BASE = os.environ.get("EMERGENCY_API_BASE", "https://apis.data.go.kr/B552657/ErmctInfoInqireService")
TIMEOUT = 12
EGEN_URL = "https://www.e-gen.or.kr/egen/search_emergency_room.do"

# curate._SIDO 표준명 → 이 API의 시도명. (광주·전남은 데이터상 '전남광주통합특별시'로 묶여 있다)
_SIDO_NAME = {
    "강원": "강원특별자치도", "전북": "전북특별자치도", "제주": "제주특별자치도",
    "광주광역시": "전남광주통합특별시", "전라남도": "전남광주통합특별시",
}
# 응급의료기관 종별: 권역센터 > 지역센터 > 지역기관 > 운영신고기관
_LEVEL = {"G001": 0, "G006": 1, "G007": 2, "G009": 3}

_cache: dict[str, dict] = {}


def _today() -> str:
    return date.today().isoformat()


def _get(ep: str, params: dict):
    try:
        r = requests.get(f"{BASE}/{ep}", params={"serviceKey": KEY, "pageNo": 1, "numOfRows": 100, **params},
                         timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return None
    return root if root.findtext(".//resultCode") in ("00", "0") else None


def _items(root) -> list[dict]:
    out = []
    for it in root.iter("item"):
        out.append({
            "name": (it.findtext("dutyName") or "").strip(),
            "level": (it.findtext("dutyEmclsName") or "").strip(),
            "level_code": (it.findtext("dutyEmcls") or "").strip(),
            "tel": (it.findtext("dutyTel3") or it.findtext("dutyTel1") or "").strip(),
            "addr": (it.findtext("dutyAddr") or "").strip(),
            "hpid": (it.findtext("hpid") or "").strip(),
        })
    return sorted(out, key=lambda x: _LEVEL.get(x["level_code"], 9))


def _sgg_for_api(sgg: str | None) -> str | None:
    """'수원시 영통구' 같은 구는 시 단위로만 조회된다."""
    if not sgg:
        return None
    m = re.match(r"([가-힣]+시)\s*[가-힣]+구$", sgg)
    return m.group(1) if m else sgg


def _result(kind: str, scope: str, items: list[dict], note: str = "") -> dict:
    return {
        "verification_status": "verified" if items else "needs_review",
        "source": "국립중앙의료원 응급의료기관 정보",
        "kind": kind,
        "scope": scope,
        "facilities": items,
        "note": note,
        "source_url": EGEN_URL,
        "fetched_at": _today(),
    }


@disk_cached("emergency")
def ground_emergency(kind: str, sido: str | None, sgg: str | None = None, limit: int = 5) -> dict:
    """kind='er'(지역 응급실) | 'trauma'(권역외상센터). sido 는 curate._SIDO 표준명."""
    if not KEY:
        return {"verification_status": "needs_review", "error": "DATA_GO_KR_KEY 미설정"}
    if not sido:
        return {"verification_status": "needs_review", "error": "거주 시도 미상"}
    q0, q1 = _SIDO_NAME.get(sido, sido), _sgg_for_api(sgg)
    key = f"{kind}|{q0}|{q1 or ''}"
    if key in _cache:
        return _cache[key]
    if kind == "trauma":
        root = _get("getStrmListInfoInqire", {"Q0": q0})
        items = _items(root)[:limit] if root is not None else []
        for f in items:  # 이 목록의 종별은 병원 응급실 등급이라, 외상센터 운영 기관임을 명시
            f["level"] = "권역외상센터"
        res = _result(kind, q0, items)
    else:
        root = _get("getEgytListInfoInqire", {"Q0": q0, "Q1": q1}) if q1 else None
        items, scope, note = (_items(root) if root is not None else []), f"{q0} {q1 or ''}".strip(), ""
        if not items:  # 시군구에 응급실이 없으면(군 지역 등) 시도 전체의 상위 기관으로
            root = _get("getEgytListInfoInqire", {"Q0": q0})
            items, scope = (_items(root) if root is not None else []), q0
            note = f"{q1} 안에 응급의료기관이 없어 {q0} 기준으로 안내" if q1 else ""
        res = _result(kind, scope, items[:limit], note)
    if res["verification_status"] == "verified":
        _cache[key] = res
    return res
