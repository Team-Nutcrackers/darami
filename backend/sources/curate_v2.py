"""동적 플레이북 v2 — 이벤트 라우팅 + 이벤트별 API 매핑 기반 grounding.

입력(질문 + 페르소나)만 보고:
  1) classify : 50개 이벤트(playbooks/events.json) 중 관련 이벤트를 고른다(주 1 + 보조 최대 2).        [LLM]
  1-1) route  : 질의를 세 갈래로 라우팅한다.                                                           [LLM]
                - single      : 기존 그대로 — 주 이벤트 사례 10건 중 가장 비슷한 1건을 few-shot 으로.
                - multi_hop   : 한 이벤트 안에서 서로 다른 절차를 여럿 묻는 질의 → 부분 질문으로 분해해 각각 사례 검색.
                - multi_event : 여러 이벤트에 걸친 질의 → 이벤트별 부분 질문으로 분해 → 각 부분을 다시 이벤트 내 멀티홉 판단.
                판단 순서: 이벤트 단위(이벤트가 둘 이상 분류됐을 때만) → 이벤트 내. 홉마다 top-1 사례를 고른다(모두 LLM).
  2) plan     : 고른 이벤트의 API 매핑(apis)을 메뉴로 받아, 각 API에 무엇을 호출할지 정한다.            [LLM]
                - 고정 매핑(법령 조문·보조금24 서비스ID·복지로 servId·응급의료기관 종류)은 '해당 항목 선택 + 조건'
                - 검색형 API(보조금24 지역·지자체복지)는 검색어, 매핑에 없는 법령 절차는 법령명+조문 키워드
  3) execute  : 계획대로 API를 호출(grounding + 지역 후보 검색).                                       [코드]
  4) select   : 지역 후보 중 해당 서비스 선택, 매핑 밖 법령 조문이 맞는지 검증.                         [LLM]
  5) assemble : 검증된 절차로 플레이북 조립.                                                          [코드]

LLM 단계의 출력은 JSON 이고, curate_dynamic_v2() 는 LLM(json 호출자)으로, 사전 생성 단계에서는
사람이/Claude 가 작성한 결정 JSON 으로 같은 execute/assemble 을 탄다(sources/gen_playbooks.py).
"""

from __future__ import annotations

import json
from collections import defaultdict

from . import kb
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import graph
from .common import catalog, common_ref
from .curate import _SIDO, _apply_grounding, _apply_legal, _base_proc, _norm_name
from .emergency_api import ground_emergency
from .law_api import ground_legal, ground_legal_auto
from .local_welfare_api import ground_local_service, search_local
from .national_welfare_api import ground_national_service
from .welfare_api import _get as gov24_get, ground_service, search_services

EVENTS_PATH = Path(__file__).resolve().parent.parent / "playbooks" / "events.json"
MAX_EVENTS = 3
MAX_REGIONAL = 5


def load_events() -> dict[str, dict]:
    return {e["id"]: e for e in json.loads(EVENTS_PATH.read_text(encoding="utf-8"))["events"]}


EVENTS = load_events()


# ---------- 지역 ----------
# 시군구 → 시도 (이름이 전국에서 유일한 것만. 중구·동구·서구·남구·북구·강서구·고성군 등 중복 이름은 시도가 같이 나와야 한다)
_SGG_SIDO: dict[str, str] = {}
for _canon, _names in {
    "서울특별시": "종로구 용산구 성동구 광진구 동대문구 중랑구 성북구 강북구 도봉구 노원구 은평구 서대문구 마포구 양천구 구로구 금천구 영등포구 동작구 관악구 서초구 강남구 송파구 강동구",
    "부산광역시": "영도구 부산진구 동래구 해운대구 사하구 금정구 연제구 수영구 사상구 기장군",
    "대구광역시": "수성구 달서구 달성군 군위군",
    "인천광역시": "미추홀구 연수구 남동구 부평구 계양구 강화군 옹진군",
    "광주광역시": "광산구",
    "대전광역시": "유성구 대덕구",
    "울산광역시": "울주군",
    "세종특별자치시": "세종시",
    "경기도": "수원시 성남시 의정부시 안양시 부천시 광명시 평택시 동두천시 안산시 고양시 과천시 구리시 남양주시 오산시 시흥시 군포시 의왕시 하남시 용인시 파주시 이천시 안성시 김포시 화성시 광주시 양주시 포천시 여주시 연천군 가평군 양평군 분당구 장안구 권선구 팔달구 영통구 일산동구 일산서구 덕양구 수지구 기흥구 처인구 단원구 상록구 만안구 동안구 소사구 오정구 원미구",
    "강원": "춘천시 원주시 강릉시 동해시 태백시 속초시 삼척시 홍천군 횡성군 영월군 평창군 정선군 철원군 화천군 양구군 인제군 양양군",
    "충청북도": "청주시 충주시 제천시 보은군 옥천군 영동군 증평군 진천군 괴산군 음성군 단양군 상당구 서원구 흥덕구 청원구",
    "충청남도": "천안시 공주시 보령시 아산시 서산시 논산시 계룡시 당진시 금산군 부여군 서천군 청양군 홍성군 예산군 태안군 동남구 서북구",
    "전북": "전주시 군산시 익산시 정읍시 남원시 김제시 완주군 진안군 무주군 장수군 임실군 순창군 고창군 부안군 완산구 덕진구",
    "전라남도": "목포시 여수시 순천시 나주시 광양시 담양군 곡성군 구례군 고흥군 보성군 화순군 장흥군 강진군 해남군 영암군 무안군 함평군 영광군 장성군 완도군 진도군 신안군",
    "경상북도": "포항시 경주시 김천시 안동시 구미시 영주시 영천시 상주시 문경시 경산시 의성군 청송군 영양군 영덕군 청도군 고령군 성주군 칠곡군 예천군 봉화군 울진군 울릉군",
    "경상남도": "창원시 진주시 통영시 사천시 김해시 밀양시 거제시 양산시 의령군 함안군 창녕군 남해군 하동군 산청군 함양군 거창군 합천군 마산합포구 마산회원구 성산구 의창구 진해구",
    "제주": "제주시 서귀포시",
}.items():
    for _n in _names.split():
        _SGG_SIDO[_n] = _canon
_SGG_RE = re.compile(r"([가-힣]{1,5}(?:시|군|구))")


# 접미사 없는 시군구 이름 중 일반 낱말과 같은 것('예산 부족', '음성 안내', '보안 강화', '목표 달성') — 지명으로 보지 않는다
_STEM_COMMON_WORDS = {"광주", "고령", "영광", "보은", "진도", "음성", "장수", "영양", "예산", "부여", "강화", "달성", "동작",
                      "연수", "수영", "사상", "동해", "남해", "정선", "봉화", "거창", "장성", "성주"}
# 접미사 생략 지명 뒤에 붙는 조사('수원으로', '화성에서', '안산에')
_STEM_TAIL = r"(?=$|[^가-힣]|(?:으로|로|에서|에|쪽)(?:[^가-힣]|$))"
# 동네 이름으로 흔히 말하는 곳 → (시도, 시군구)
_AREA_ALIAS = {"일산": ("경기도", "고양시"), "송도": ("인천광역시", "연수구"), "판교": ("경기도", "성남시"),
               "동탄": ("경기도", "화성시"), "광교": ("경기도", "수원시"), "목동": ("서울특별시", "양천구"),
               "잠실": ("서울특별시", "송파구")}
# "A에서 B로 이사" / "A 살다가 B로 이사" — 목적지 B
_DEST_RE = re.compile(r"(?:에서|살다가|살다)\s*(.{1,20}?)\s*(?:으로|로)\s*(?:이사|이주|전입|옮기|옮겨|이전|가요|갑니다|가려|가는|가게|가서|내려가|올라가|귀촌|귀농|전학)")


def _sgg_in_text(text: str) -> tuple[str | None, str | None]:
    """질문에 '고양시·마포구·안산'처럼 시군구만 나올 때 (시도, 시군구). 접미사 없는 '안산'·'수원으로'도 '안산시'·'수원시'로 본다."""
    for m in _SGG_RE.finditer(text):
        name = m.group(1)
        for cand in (name, name[1:] if len(name) > 3 else None):
            if cand and cand in _SGG_SIDO:
                return _SGG_SIDO[cand], cand
    for name, sido in _SGG_SIDO.items():  # 접미사 생략('안산 살아요', '고양 이사', '수원으로 이사')
        stem = name[:-1]
        if len(stem) >= 2 and stem not in _STEM_COMMON_WORDS and re.search(r"(?<![가-힣])" + stem + _STEM_TAIL, text):
            return sido, name
    for alias, (sido, sgg) in _AREA_ALIAS.items():
        if re.search(r"(?<![가-힣])" + alias + _STEM_TAIL, text):
            return sido, sgg
    return None, None


def _region_in_text(query: str) -> tuple[str | None, str | None]:
    hits = [(query.find(short), canon) for short, canon in _SIDO if short in query]
    if hits:
        sido = min(hits)[1]
        sgg_sido, sgg = _sgg_in_text(query)
        return sido, (sgg if sgg_sido == sido else None)
    return _sgg_in_text(query)


def resolve_region(persona: dict | None, query: str = "") -> tuple[str | None, str | None]:
    """(시도 표준명, 시군구). 페르소나 residence → 질문의 목적지("A에서 B로 이사"의 B) → 질문의 시도(가장 먼저 나온 것) → 질문의 시군구(사전) 순."""
    facts = (persona or {}).get("facts") or {}
    res = str(facts.get("residence") or "").strip()
    if res:
        head, *rest = res.split()
        for short, canon in _SIDO:
            if head.startswith(short):
                sgg = rest[0] if rest and re.fullmatch(r"[가-힣]+[시군구]", rest[0]) else None
                return canon, sgg
        sido, sgg = _sgg_in_text(res)
        if sido:
            return sido, sgg
    dest = _DEST_RE.search(query)
    if dest:
        sido, sgg = _region_in_text(dest.group(1))
        if sido:
            return sido, sgg
    return _region_in_text(query)


# ---------- 1) classify ----------
def event_catalog() -> str:
    """LLM 에 줄 이벤트 목록(id · 제목 — 설명 / 흔한 표현)."""
    return "\n".join(f"- {e['id']}: {e['title']} — {e['description']} (표현: {', '.join(e.get('aliases', []))})"
                     for e in EVENTS.values())


def normalize_events(ids, primary: str | None = None, limit: int | None = MAX_EVENTS) -> list[str]:
    """목록에 있는 id 만, 중복 없이. primary 가 있으면 맨 앞으로(LLM 이 언급 순서대로 나열하는 경우 대비)."""
    out = []
    for i in [primary, *(ids or [])]:
        if i and i in EVENTS and i not in out:
            out.append(i)
    return out[:limit] if limit else out


# ---------- 2) plan: API 메뉴 ----------
def build_menu(event_ids: list[str]) -> dict:
    """고른 이벤트들의 apis 를 합쳐 번호 붙인 메뉴로. 같은 법령 조문·서비스는 한 번만.
    ref 번호는 이벤트를 events.json 순서로 놓고 매기므로, 같은 이벤트 조합이면 고른 순서와 무관하게 같다."""
    event_ids = sorted(set(event_ids), key=list(EVENTS).index)
    menu = {"law": [], "gov24": [], "national_welfare": [], "emergency": [],
            "keywords": {"gov24": [], "local_welfare": []}}
    seen = set()
    for eid in event_ids:
        a = EVENTS[eid]["apis"]
        for it in a["law"]:
            key = ("law", it.get("common") or (it["law_query"], it["article_title"]))
            if key not in seen:
                seen.add(key)
                menu["law"].append({"ref": f"L{len(menu['law']) + 1}", "event": eid, **it})
        for kind in ("gov24", "national_welfare"):
            items = a["gov24"]["services"] if kind == "gov24" else a["national_welfare"]
            for it in items:
                key = (kind, it["service_id"])
                if key not in seen:
                    seen.add(key)
                    p = "G" if kind == "gov24" else "N"
                    menu[kind].append({"ref": f"{p}{len(menu[kind]) + 1}", "event": eid, **it})
        for it in a.get("emergency") or []:
            key = ("emergency", it["kind"])
            if key not in seen:
                seen.add(key)
                menu["emergency"].append({"ref": f"E{len(menu['emergency']) + 1}", "event": eid, **it})
        for kw in a["gov24"]["keywords"]:
            if kw not in menu["keywords"]["gov24"]:
                menu["keywords"]["gov24"].append(kw)
        for kw in a["local_welfare"]:
            if kw not in menu["keywords"]["local_welfare"]:
                menu["keywords"]["local_welfare"].append(kw)
    return menu


def _menu_key(kind: str, it: dict) -> str | None:
    """메뉴 항목 → 절차 그래프 노드 키(common:/law:/svc:)."""
    if kind == "law":
        return f"common:{it['common']}" if it.get("common") else f"law:{it['name']}"
    if kind in ("gov24", "national_welfare") and it.get("service_id"):
        return f"svc:{it['service_id']}"
    return None


def expand_menu_with_graph(menu: dict, example: dict | list[dict] | None = None, max_items: int = 15) -> dict:
    """메뉴 항목(+예시가 쓴 절차)의 절차 그래프 1-hop 이웃을 '이어진 절차'로 메뉴에 덧붙인다. example 은 하나 또는 목록(멀티홉).
    메뉴판이 작아 선행·후속 절차가 빠진 경우(예: 전세사기 메뉴에 임차권등기명령은 있는데 그 요건인 대항력·확정일자가 없음)
    LLM 이 지어내지 않고 '고를' 수 있게 한다. 고르면 메뉴 항목과 같은 경로로 grounding 되고(검증 등급 동일),
    키가 그래프 노드와 일치해 선후 관계(dependsOn)가 그대로 걸린다.
    - 포함: law:/common: 노드, svc: 중 전국 단위(보조금24·복지로 중앙) 노드. 제외: x:(AI 제안·미검증), 지역 서비스.
    - 선행(requires/suggests 모두)과 후속을 다 보되, 선행을 먼저 둔다(체인이 끊기는 건 대개 선행이 빠져서).
    - 항목에 via(어느 메뉴 항목과 어떤 관계인지)를 적어 menu_text 가 별도 섹션으로 보여 준다."""
    kinds = ("law", "gov24", "national_welfare")
    have = {k for kind in kinds for it in menu[kind] if (k := _menu_key(kind, it))}
    seeds: dict[str, str] = {}  # key → 이름
    for kind in kinds:
        for it in menu[kind]:
            if (k := _menu_key(kind, it)):
                seeds[k] = it["name"]
    for ex in (example if isinstance(example, list) else ([example] if example else [])):
        for pr in (ex or {}).get("playbook", {}).get("procedures", []) or []:
            if pr.get("key") and not str(pr["key"]).startswith("x:"):
                seeds.setdefault(pr["key"], pr.get("name", pr["key"]))
    gnodes = graph.nodes()
    # 같은 후속을 향한 '대안 선행' 묶음(예: 건강보험 자격 변경 ← 출생신고/사망신고/혼인신고/전입신고 중 하나).
    # 소프트이면서 대안이 둘 이상이면 "이 중 하나면 된다"는 뜻이라 전부 끌어오면 잡음(퇴사 메뉴에 사망신고)이 되므로 선행 확장에서 뺀다.
    alt_count: dict[str, int] = defaultdict(int)
    for e in graph.edges():
        alt_count[e.get("group") or e.get("after")] += 1
    found: dict[str, tuple[int, str]] = {}  # key → (정렬 순위, via)
    for e in graph.edges():
        b, a = e.get("before"), e.get("after")
        if a in seeds and b not in have and b not in seeds:  # 선행
            if not e.get("hard") and alt_count[e.get("group") or a] > 1:
                continue
            rel = "필수 선행" if e.get("hard") else "권고 선행"
            via = f"{seeds[a]}의 {rel}" + (f" — {e['guidance']}" if e.get("guidance") else "")
            found.setdefault(b, (0, via))
        elif b in seeds and a not in have and a not in seeds:  # 후속
            rel = "다음 필수" if e.get("hard") else "다음 권고"
            via = f"{seeds[b]} {rel}" + (f" — {e['guidance']}" if e.get("guidance") else "")
            found.setdefault(a, (1, via))
    added = []
    for key, (rank_, via) in sorted(found.items(), key=lambda kv: (kv[1][0], kv[0])):
        n = gnodes.get(key)
        item = (n or {}).get("item")
        if not n or not item or key.startswith("x:"):
            continue
        kind = item.get("kind")
        if kind == "law":
            it = {k: v for k, v in item.items() if k != "kind"}
            it["ref"] = f"L{len(menu['law']) + 1}"
            it["via"] = via
            menu["law"].append(it)
        elif kind in ("gov24", "national_welfare") and item.get("service_id"):
            it = {k: v for k, v in item.items() if k != "kind"}
            it["ref"] = f"{'G' if kind == 'gov24' else 'N'}{len(menu[kind]) + 1}"
            it["via"] = via
            menu[kind].append(it)
        else:
            continue  # 지역 서비스·응급 등은 이웃으로 넣지 않는다
        have.add(key)
        added.append({"ref": it["ref"], "key": key, "name": it["name"], "via": via})
        if len(added) >= max_items:
            break
    menu["graph_refs"] = added
    return menu


def menu_text(menu: dict) -> str:
    """LLM 에 줄 메뉴 텍스트."""
    lines = ["[법령 절차 — 국가법령정보]"]
    def nm(it):  # 별칭이 있으면 같이 보여 준다(LLM 이 '실업급여'를 찾을 때 구직급여를 고르도록)
        al = it.get("aliases") or []
        return it["name"] + (f" (= {', '.join(al[:3])})" if al else "")
    def law_src(it):
        return f"공통:{it['common']}" if it.get("common") else f"{it['law_query']} / '{it['article_title']}'"
    for it in menu["law"]:
        if it.get("via"):
            continue
        lines.append(f"- {it['ref']} ({it['event']}) {nm(it)} — {law_src(it)}")
    lines.append("[전국 혜택 — 보조금24]")
    lines += [f"- {it['ref']} ({it['event']}) {nm(it)}" for it in menu["gov24"] if not it.get("via")] or ["- (없음)"]
    lines.append("[전국 혜택 — 복지로 중앙부처]")
    lines += [f"- {it['ref']} ({it['event']}) {nm(it)}" for it in menu["national_welfare"] if not it.get("via")] or ["- (없음)"]
    lines.append("[응급의료기관 — 국립중앙의료원 (사용자 지역 기준 목록)]")
    lines += [f"- {it['ref']} ({it['event']}) {it['name']}" for it in menu["emergency"]] or ["- (없음)"]
    linked = [(k, it) for k in ("law", "gov24", "national_welfare") for it in menu[k] if it.get("via")]
    if linked:
        lines.append("[이어진 절차 — 절차 그래프에서 가져온 위 항목들의 선행·후속. 사용자 상황에 해당하면 같은 방식(ref)으로 고른다]")
        for k, it in linked:
            src = f" — {law_src(it)}" if k == "law" else ""
            lines.append(f"- {it['ref']} ({it['event']}) {nm(it)}{src} · {it['via']}")
    lines.append(f"[지역 검색어 예시] 보조금24: {menu['keywords']['gov24'] or '(없음)'} / 지자체복지: {menu['keywords']['local_welfare'] or '(없음)'}")
    return "\n".join(lines)


# ---------- 3) execute ----------
def _gov24_regional(keywords: list[str], sido: str, sgg: str | None, per_kw: int = 100) -> list[dict]:
    """보조금24 지역(광역·시군구) 서비스 후보. 광역 직속 + 사용자 시군구만(타 시군구 제외)."""
    out: dict[str, dict] = {}
    for kw in keywords:
        data = gov24_get("serviceList", {"page": 1, "perPage": per_kw, "cond[서비스명::LIKE]": kw}) or {}
        for s in data.get("data") or []:
            org = (s.get("소관기관명") or "").strip()
            if s.get("소관기관유형") in ("중앙행정기관", "공공기관") or not org.startswith(sido):
                continue
            if " " in org and not (sgg and sgg in org):
                continue  # 다른 시군구 사업
            sid = s.get("서비스ID")
            if sid and sid not in out:
                out[sid] = {"source": "gov24", "service_id": sid, "name": s.get("서비스명"), "agency": org,
                            "summary": (s.get("서비스목적요약") or s.get("지원내용") or "").replace("\r\n", " ")[:120],
                            "상세조회URL": s.get("상세조회URL"), "신청기한": s.get("신청기한"), "지원대상": s.get("지원대상")}
    return list(out.values())


def _gov24_search(keywords: list[str], sido: str | None) -> list[dict]:
    """보조금24 전국(중앙행정기관) + 사용자 시도 서비스명 검색 — 이벤트 메뉴가 없을 때(open 모드) 후보 발굴용."""
    out = []
    for c in search_services(keywords, region=sido, per_kw=15):
        out.append({"source": "gov24", "service_id": c["service_id"], "name": c["name"], "agency": c["agency"], "summary": c.get("summary", "")})
    return out


def _legal(out: dict, g: dict) -> dict:
    """법령 grounding 반영. 기한은 기대 일수와 대조된(verified) 경우에만 쓴다 —
    partial 은 조문 속 첫 'N일 이내'(심의·유예·요양 기간 등)가 신청 기한이 아닐 수 있어 기한·근거문장을 비운다."""
    out = _apply_legal(out, g)
    if g.get("verification_status") == "partial" and g.get("article"):
        if out.get("deadline") == g.get("deadline_in_law_text"):
            out["deadline"] = ""
        if out.get("summary") == (g.get("evidence_span") or "").strip():
            out["summary"] = f"{g.get('law_name', '')} {g.get('article', '')}에 근거한 절차입니다.".strip()
    return out


def _norm_title(s: str) -> str:
    return re.sub(r"[\s·ㆍ()\[\]/\-_,.]|신청|신고|등록|지원|서비스|제도|사업", "", s or "")


def _closest_menu_item(name: str, menu: dict):
    """이름이 메뉴 항목과 거의 같은 경우만(핵심어 포함·유사도 0.75 이상) 그 항목을 돌려준다."""
    import difflib
    n = _norm_title(name)
    if len(n) < 2:
        return None
    best, best_r = None, 0.0
    for kind in ("law", "gov24", "national_welfare", "emergency"):
        for it in menu[kind]:
            m = _norm_title(it["name"])
            r = difflib.SequenceMatcher(None, n, m).ratio()
            if (n in m or m in n) and min(len(n), len(m)) >= 3:
                r = max(r, 0.8)
            for alias in it.get("aliases") or []:  # events.json 에 적어 둔 다른 이름('실업급여' → 구직급여)
                a = _norm_title(alias)
                if a == n or (len(a) >= 3 and (a in n or n in a)):
                    r = max(r, 0.9)
            if r > best_r:
                best, best_r = (kind, it), r
    return best if best_r >= 0.75 else None


# 공통 절차를 LLM 이 다른 말로 부르는 경우(사전 생성·스모크 테스트에서 실제로 나온 표현)
_COMMON_ALIASES = {
    "transfer-report": ["주민등록 주소 변경", "주소지 변경", "주소 이전", "전입 신고", "이사 신고"],
    "birth-report": ["출생 신고", "출생신고 및 출산지원금 신청", "출생신고 및"],
    "death-report": ["사망 신고"],
    "marriage-report": ["혼인 신고"],
    "health-insurance-change": ["건강보험 주소 변경", "건강보험 피부양자 등록", "건강보험 자격 변동", "건강보험 지역가입"],
    "pension-change": ["국민연금 지역가입", "국민연금 자격 변경"],
    "car-address": ["자동차 변경등록", "차량 주소 변경", "자동차 등록 주소"],
    "business-registration": ["사업자 등록", "사업자등록 신청"],
    "online-business-report": ["통신판매업 신고", "통신판매업자 신고"],
    "unemployment-benefit": ["실업급여", "실업급여 신청", "구직급여", "구직급여 신청", "실업 신고"],
}


def _closest_common(name: str, cat: dict) -> str | None:
    """공통/학습 카탈로그에서 이름이 거의 같은 절차 key(유사도 0.75 이상 또는 별칭 일치)."""
    import difflib
    n = _norm_title(name)
    if len(n) < 2:
        return None
    for key, aliases in _COMMON_ALIASES.items():
        if key in cat and any(_norm_title(a) == n or (_norm_title(a) in n and len(_norm_title(a)) >= 3)
                              or (len(_norm_title(a)) >= 2 and n.startswith(_norm_title(a))) for a in aliases):
            return key
    best, best_r = None, 0.0
    for key, c in cat.items():
        m = _norm_title(c["name"])
        r = difflib.SequenceMatcher(None, n, m).ratio()
        if (n in m or m in n) and min(len(n), len(m)) >= 3:
            r = max(r, 0.8)
        if r > best_r:
            best, best_r = key, r
    return best if best_r >= 0.75 else None


def _law_proc(it: dict, pid: str, priority: str, condition: str) -> dict:
    out = _base_proc(pid, it["name"], priority, condition)
    out["key"] = f"common:{it['common']}" if it.get("common") else f"law:{it['name']}"  # 의존관계 카탈로그용 안정 키
    ref = common_ref(it["common"]) if it.get("common") else {"law_query": it["law_query"],
                                                              "article_title": it["article_title"], "expected_days": None}
    if ref is None:  # 그래프·의존관계가 참조하는 common 키가 이 환경의 카탈로그에 없음(common.py COMMON 승격 누락)
        raise KeyError(f"공통 카탈로그에 없는 절차 키: {it['common']} (sources/common.py COMMON 에 추가 필요)")
    return _legal(out, ground_legal(**ref))


def _auto_prereq(item: dict, why: str) -> dict | None:
    """의존관계 카탈로그의 hard 선행 절차가 플레이북에 없을 때 메뉴 출처로 만들어 넣는다."""
    import hashlib
    cond = f"선행 절차 — {why}" if why else "선행 절차"
    try:
        if item["kind"] == "law":
            pid = item.get("common") or f"law-{item['event']}-{hashlib.sha1(item['name'].encode('utf-8')).hexdigest()[:8]}"
            return _law_proc(item, pid, "must", cond)
        return _service_proc(item["kind"], item, "must", cond)
    except Exception:  # noqa: BLE001 — 선행 절차 grounding 실패는 플레이북 생성을 막지 않는다
        return None


def _service_proc(kind: str, it: dict, priority: str, condition: str) -> dict:
    sid = it["service_id"]
    if kind == "gov24":
        g, label = ground_service(sid), "보조금24"
    elif kind == "national_welfare":
        g, label = ground_national_service(sid), "복지로"
    elif it.get("source") == "local":
        g, label = ground_local_service(sid, it), "복지로"
    else:
        g, label = ground_service(sid, it), "보조금24"
    out = _apply_grounding(_base_proc(f"{kind}-{sid}", it["name"], priority, condition), g, label, set_name=True)
    out["key"] = f"svc:{sid}"
    # _apply_grounding 은 condition 을 공식 '지원대상'으로 덮어쓴다 → 공식 원문은 eligibility 로, condition 은 이 사용자 기준(LLM)으로
    official = out.get("condition", "")
    if condition and official != condition:
        out["eligibility"], out["condition"] = official, condition
    return out


def _emergency_proc(it: dict, sido: str | None, sgg: str | None, priority: str, condition: str) -> dict:
    """응급의료기관 목록을 하나의 안내 절차로. 실시간 병상은 넣지 않는다(캐시되는 플레이북)."""
    g = ground_emergency(it["kind"], sido, sgg)
    out = _base_proc(f"emergency-{it['kind']}", it["name"], priority, condition)
    out["key"] = f"emergency:{it['kind']}"
    out["verification_status"] = g.get("verification_status", "needs_review")
    if out["verification_status"] != "verified":
        return out
    fac = g["facilities"]
    out.update({
        "name": f"{it['name']} ({g['scope']})",
        "agency": "국립중앙의료원 중앙응급의료센터",
        "link": g["source_url"],
        "facilities": fac,
        "summary": " · ".join(f"{f['name']}({f['level']}, {f['tel']})" for f in fac)
                   + (f". {g['note']}" if g.get("note") else "")
                   + ". 실시간 응급실 병상은 e-gen에서 확인하고, 위급하면 119.",
        "source": {"name": g["source"], "tier": "official", "checkedAt": g["fetched_at"]},
    })
    return out


def execute(plan: dict, query: str, persona: dict | None, menu: dict | None = None) -> dict:
    """plan(LLM 결정) → API 호출. 반환: 확정 절차 + 지역 후보 + 매핑 밖 법령 후보. menu 를 주면(open 모드) 그걸 쓴다."""
    events = normalize_events(plan.get("events"))
    menu = menu if menu is not None else build_menu(events)
    kinds = ("law", "gov24", "national_welfare", "emergency")
    by_ref = {it["ref"]: (kind, it) for kind in kinds for it in menu[kind]}
    by_name = {(kind, it["name"]): (kind, it) for kind in kinds for it in menu[kind]}
    sido, sgg = resolve_region(persona, query)
    unresolved: list[str] = []

    jobs = []
    for kind in kinds:
        for c in plan.get(kind) or []:
            # 메뉴 항목은 name 또는 ref(L1/G2…)로 지정. 이름이 있으면 이름 우선(이벤트 조합이 바뀌어도 번호 밀림에 안전)
            hit = by_name.get((kind, c.get("name"))) if c.get("name") else None
            hit = hit or by_ref.get(c.get("ref"))
            if not hit or hit[0] != kind:
                unresolved.append(f"{kind}:{c.get('name') or c.get('ref')}")
                continue
            _, it = hit
            pr, cond = c.get("priority", "nice"), c.get("condition", "")
            if kind == "emergency":
                jobs.append(("proc", it["ref"], lambda it=it, pr=pr, cond=cond: _emergency_proc(it, sido, sgg, pr, cond)))
            elif kind == "law":
                pid = it.get("common") or f"law-{it['event']}-{it['ref'].lower()}"
                jobs.append(("proc", it["ref"], lambda it=it, pid=pid, pr=pr, cond=cond: _law_proc(it, pid, pr, cond)))
            else:
                jobs.append(("proc", it["ref"], lambda kind=kind, it=it, pr=pr, cond=cond: _service_proc(kind, it, pr, cond)))
    # 메뉴에 이미 있는 절차를 '메뉴 밖 법령'으로 다시 쓴 경우(예: '건강보험 주소 변경' ≈ '건강보험 자격·주소 변경') → 메뉴 항목으로 흡수
    absorbed: list[str] = []
    for x in list(plan.get("extra_law") or []):
        hit = _closest_menu_item(x.get("name", ""), menu)
        if hit:
            kind, it = hit
            if not any(j[1] == it["ref"] for j in jobs):
                pr, cond = x.get("priority", "nice"), x.get("condition", "")
                if kind == "law":
                    pid = it.get("common") or f"law-{it['event']}-{it['ref'].lower()}"
                    jobs.append(("proc", it["ref"], lambda it=it, pid=pid, pr=pr, cond=cond: _law_proc(it, pid, pr, cond)))
                elif kind == "emergency":
                    jobs.append(("proc", it["ref"], lambda it=it, pr=pr, cond=cond: _emergency_proc(it, sido, sgg, pr, cond)))
                else:
                    jobs.append(("proc", it["ref"], lambda kind=kind, it=it, pr=pr, cond=cond: _service_proc(kind, it, pr, cond)))
            absorbed.append(x.get("id"))
            plan["extra_law"] = [y for y in plan["extra_law"] if y is not x]
    # 메뉴엔 없지만 공통 카탈로그(전입신고·출생신고·사업자등록 등)에 있는 절차 → 고정 ref 로 결정론적 grounding
    cat = catalog()
    for x in list(plan.get("extra_law") or []):
        ck = _closest_common(x.get("name", ""), cat)
        if ck and not any(j[1] == ck for j in jobs):
            c = cat[ck]
            it = {"ref": ck, "event": "common", "name": c["name"], "common": ck}
            pr, cond = x.get("priority", "nice"), x.get("condition", "")
            jobs.append(("proc", ck, lambda it=it, ck=ck, pr=pr, cond=cond: _law_proc(it, ck, pr, cond)))
            absorbed.append(x.get("id"))
            plan["extra_law"] = [y for y in plan["extra_law"] if y is not x]
    for x in plan.get("extra_law") or []:
        if x.get("law_query") and x.get("keywords"):
            jobs.append(("extra", x.get("id"), lambda x=x: ground_legal_auto(x["law_query"], x["keywords"], None)))
    skws = [k.strip() for k in (plan.get("search_keywords") or []) if isinstance(k, str) and k.strip()][:4]
    if skws:  # open 모드: 전국 서비스명 검색
        jobs.append(("cand", "search", lambda: _gov24_search(skws, sido)))
    reg = plan.get("regional") or {}
    if sido and reg.get("gov24_keywords"):
        jobs.append(("cand", "gov24", lambda: _gov24_regional(reg["gov24_keywords"], sido, sgg)))
    if sido and reg.get("local_keywords"):
        text = " ".join([str(((persona or {}).get("facts") or {}).get("residence") or ""), query])
        # 지자체복지 검색(srchKeyCode=003)은 본문까지 매칭해 잡음이 많다 → 이름·요약에 검색어가 보이는 것만
        kws = [k.replace(" ", "") for k in reg["local_keywords"]]
        jobs.append(("cand", "local", lambda: [
            c for c in search_local(reg["local_keywords"], sido, text)
            if any(k in (c["name"] + c["summary"]).replace(" ", "") for k in kws)]))

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda j: (j[0], j[1], j[2]()), jobs))

    procs, extra, cands = [], {}, []
    for typ, ref, r in results:
        if typ == "proc":
            procs.append({"ref": ref, **r})
        elif typ == "extra":
            extra[ref] = r
        else:
            cands += r
    # 지역 후보: 같은 사업이 보조금24·지자체 둘 다 있으면 보조금24(서류·기한이 더 풍부) 우선, 이미 확정된 서비스 제외
    fixed = {_norm_name(p["name"]) for p in procs}
    uniq, seen = [], set(fixed)
    for c in sorted(cands, key=lambda c: c["source"] != "gov24"):
        n = _norm_name(c["name"])
        if n not in seen:
            seen.add(n)
            uniq.append(c)
    for i, c in enumerate(uniq, 1):
        c["cid"] = f"R{i}"
    return {"events": events, "region": {"sido": sido, "sgg": sgg}, "procedures": procs,
            "extra_law": extra, "candidates": uniq, "unresolved": unresolved, "absorbed": absorbed}


def candidates_text(ex: dict, plan: dict) -> str:
    """select 단계 LLM 입력: 지역 후보 + 매핑 밖 법령 조문 매칭 결과."""
    lines = ["[지역·검색 후보]"]
    lines += [f"- {c['cid']} [{c['source']}] {c['name']} ({c['agency']}) — {c['summary']}" for c in ex["candidates"]] or ["- (없음)"]
    lines.append("[매핑 밖 법령 절차 — 찾은 조문]")
    xs = {x.get("id"): x for x in plan.get("extra_law") or []}
    for xid, g in ex["extra_law"].items():
        art = f"{g.get('law_name')} {g.get('article')}" if g.get("article") else "조문 못 찾음"
        if g.get("article") and g.get("match_level") == "loose":
            art += " ⚠ 제목의 일부 낱말만 일치 — 이 절차의 직접 근거 조문인지 확인"
        lines.append(f"- {xid} {xs.get(xid, {}).get('name', '')} → {art}")
    if not ex["extra_law"]:
        lines.append("- (없음)")
    return "\n".join(lines)


# ---------- 5) assemble ----------
def assemble(plan: dict, ex: dict, select: dict) -> dict:
    procs = [p for p in ex["procedures"]]
    # 지역 후보 grounding
    # 후보는 cid 로 고르되, service_id 가 같이 오면 그걸로 대조한다(재실행으로 후보 순서가 바뀌어도 안전)
    by_cid = {c["cid"]: c for c in ex["candidates"]}
    by_sid = {c["service_id"]: c for c in ex["candidates"]}
    picks = []
    for s in (select.get("regional") or [])[:MAX_REGIONAL]:
        c = by_sid.get(s.get("service_id")) if s.get("service_id") else by_cid.get(s.get("cid"))
        if c:
            picks.append((c, s))
    with ThreadPoolExecutor(5) as pool:
        procs += list(pool.map(lambda cs: {"ref": cs[0]["cid"], **_service_proc(
            "local" if cs[0]["source"] == "local" else "regional", cs[0],
            cs[1].get("priority", "nice"), cs[1].get("condition", ""))}, picks))
    # 매핑 밖 법령: select 단계에서 '직접 근거 조문'으로 승인된 것만 grounding 결과 반영, 나머지는 미검증
    accepted = set(select.get("extra_law_accept") or [])
    for x in plan.get("extra_law") or []:
        out = _base_proc(x.get("id") or "extra", x.get("name"), x.get("priority"), x.get("condition"))
        out["key"] = f"x:{x.get('name')}"
        g = ex["extra_law"].get(x.get("id"))
        if g and g.get("article") and x.get("id") in accepted:
            out = _legal(out, g)
        if out.get("verification_status") not in ("verified", "partial"):
            out["verification_status"] = "unverified"
            out["summary"] = out.get("summary") or "AI가 제안한 절차입니다. 정확한 요건·서류·기한은 담당 기관에서 확인하세요."
        procs.append({"ref": x.get("id"), **out})
    # grounding 실패(API 오류 등)한 고정 매핑은 미검증으로 표기
    for p in procs:
        if p.get("verification_status") not in ("verified", "partial"):
            p["verification_status"] = "unverified"
    # must 먼저, must 안에서는 응급의료 안내를 맨 앞(지금 치료가 급한 경우), 그다음 기한 순
    procs.sort(key=lambda p: (p.get("priority") != "must", not p["id"].startswith("emergency-"), p.get("deadlineDays", 9999)))
    # 의존관계: 카탈로그 간선으로 dependsOn 을 채우고, 빠진 hard 선행 절차(예: 아동수당 ← 출생신고)는 자동 추가, 선행이 앞에 오게 정렬
    for p in procs:  # 예전 work 파일(키 없음)로 재조립할 때도 의존관계가 걸리도록 id 에서 키를 복원
        if not p.get("key"):
            pid = str(p.get("id", ""))
            if pid in catalog():
                p["key"] = f"common:{pid}"
            elif pid.startswith(("gov24-", "national_welfare-", "local-", "regional-")):
                p["key"] = "svc:" + pid.split("-", 1)[1]
            elif pid.startswith("law-"):
                p["key"] = f"law:{p.get('name')}"
    procs, _auto = graph.apply(procs, make_proc=_auto_prereq)  # 추천 순서(order)·unlocks 도 여기서 붙는다
    primary = ex["events"][0] if ex["events"] else "custom"
    return {
        "event": primary,
        "events": ex["events"],
        "title": plan.get("title") or EVENTS.get(primary, {}).get("title", "내 생활 이벤트"),
        "emoji": plan.get("emoji") or EVENTS.get(primary, {}).get("emoji", "🌰"),
        "intro": plan.get("intro") or "",
        "region": ex["region"],
        "procedures": procs,
    }


# ---------- 사전 생성 플레이북 = few-shot 예시 ----------
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "playbooks" / "examples"
_examples: dict[str, list[dict]] = {}


def load_examples(event_id: str) -> list[dict]:
    """해당 이벤트의 사전 생성 쌍(질의+결정+플레이북) 목록. 서비스 경로에서 '가장 비슷한 사례'로 쓴다."""
    if event_id in _examples:
        return _examples[event_id]
    out = []
    for f in sorted((EXAMPLES_DIR / event_id).glob("*.json")) if (EXAMPLES_DIR / event_id).exists() else []:
        try:
            pair = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": pair["query"]["id"], "query": pair["query"]["query"],
                        "persona_summary": pair["query"].get("persona_summary", ""),
                        "events": pair["decisions"]["classify"]["events"], "plan": pair["decisions"]["plan"],
                        "select": pair["decisions"].get("select") or {}, "playbook": pair["playbook"]})
        except (OSError, ValueError, KeyError):
            continue
    _examples[event_id] = out
    return out


def examples_text(pairs: list[dict]) -> str:
    return "\n".join(f"{i}. [{p['id']}] ({p['persona_summary']}) {p['query']}" for i, p in enumerate(pairs, 1))


def example_block(pair: dict, menu: dict, label: str = "", hop: str | None = None) -> str:
    """고른 사례의 결정을 '현재 메뉴 번호'로 다시 매겨 계획 프롬프트에 넣는다.
    (번호는 이벤트 조합에 따라 달라지므로 이름으로 대조해 메뉴에 있는 항목만 보여준다.)
    label: 예시가 여럿일 때 '1/3' 같은 번호, hop: 이 예시가 대응하는 부분 질문(멀티홉일 때)."""
    kinds = ("law", "gov24", "national_welfare", "emergency")
    by_name = {(k, it["name"]): it["ref"] for k in kinds for it in menu[k]}
    plan = pair["plan"]
    lines = [f"[참고 예시{(' ' + label) if label else ''} — 비슷한 질문에 만든 플레이북 ({pair['id']})]"]
    if hop:
        lines.append(f"대응 부분 질문: {hop}")
    lines += [f"질문: {pair['query']}", f"제목: {plan.get('title', '')} / 안내: {plan.get('intro', '')}", "고른 항목:"]
    shown = 0
    for k in kinds:
        for c in plan.get(k) or []:
            ref = by_name.get((k, c.get("name")))
            if ref:
                lines.append(f"- {ref} {c.get('name')} ({c.get('priority', 'nice')}) — {c.get('condition', '')}")
                shown += 1
    if not shown:
        lines.append("- (현재 메뉴와 겹치는 항목 없음)")
    reg = plan.get("regional") or {}
    lines.append(f"지역 검색어: 보조금24 {reg.get('gov24_keywords', [])} / 지자체 {reg.get('local_keywords', [])}")
    for x in plan.get("extra_law") or []:
        lines.append(f"메뉴 밖 법령: {x.get('name')} — {x.get('law_query')} / {x.get('keywords')} ({x.get('priority', 'nice')}) — {x.get('condition', '')}")
    picks = [r.get("name") for r in (pair.get("select") or {}).get("regional") or [] if r.get("name")]
    if picks:
        lines.append(f"그때 고른 지역 혜택(거주지가 달라 참고만): {picks}")
    lines.append("※ 예시는 참고용이며 상한이 아니다. 이 사용자의 상황·지역·시점에 맞게 항목을 가감하고 condition 을 새로 쓸 것. "
                 "예시에 없어도 선행 절차·사용자가 아직 안 한 기본 절차는 extra_law 로 추가할 것.")
    return "\n".join(lines)


def examples_block(examples: list[dict], menu: dict, mode: str) -> str:
    """라우팅 결과에 따라 예시 블록을 만든다. single 이면 기존과 같은 블록 하나, 멀티홉이면 번호·대응 부분 질문을 붙여 여러 개."""
    if not examples:
        return ""
    if mode == "single" and len(examples) == 1:
        return example_block(examples[0], menu)
    n = len(examples)
    return "\n\n".join(example_block(ex, menu, label=f"{i}/{n}", hop=ex.get("hop_question")) for i, ex in enumerate(examples, 1))


def example_picks(examples: list[dict]) -> list[str]:
    """예시들이 그때 고른 지역 혜택 이름(select 단계 힌트용, 중복 제거)."""
    out: list[str] = []
    for ex in examples:
        for r in (ex.get("select") or {}).get("regional") or []:
            if r.get("name") and r["name"] not in out:
                out.append(r["name"])
    return out


# ---------- 라우팅: single / multi_hop(이벤트 내) / multi_event(이벤트 단위) ----------
ROUTE_MAX_HOPS = 4


def _event_title(event_id: str) -> str:
    return (load_events().get(event_id) or {}).get("title", event_id)


def pick_example(event_id: str, ctx: str, llm_json) -> tuple[dict | None, dict]:
    """해당 이벤트의 사전 생성 사례 중 ctx 의 [질문]과 가장 비슷한 것 하나를 LLM 이 고른다(top-1).
    반환: (사례 또는 None, trace 용 {id, reason})."""
    from prompts import build_v2_example_prompt
    pairs = load_examples(event_id)
    if not pairs:
        return None, {"id": None, "reason": "사례 없음"}
    pick = llm_json(build_v2_example_prompt(), ctx + "\n[예시 질문 목록]\n" + examples_text(pairs), 0.0)
    idx = pick.get("index")
    if isinstance(idx, int) and 1 <= idx <= len(pairs):
        ex = pairs[idx - 1]
    else:
        ex = next((p for p in pairs if p["id"] == pick.get("id")), None)
    return ex, {"id": ex["id"] if ex else None, "reason": pick.get("reason", "")}


def route_and_retrieve(query: str, persona: dict | None, events: list[str], ctx: str, llm_json, trace: dict) -> tuple[list[dict], str]:
    """질의를 세 갈래로 라우팅하고 홉마다 사례를 검색한다. 반환: (예시 목록(중복 제거, hop_question 포함), mode).
    1단계 이벤트 단위: 이벤트가 둘 이상 분류됐을 때만 '여러 이벤트에 걸친 질의'인지 판단 → 이벤트별 부분 질문(parts).
    2단계 이벤트 내 : 부분 질문(또는 전체 질문)마다 '서로 다른 절차를 여럿 묻는지' 판단 → 홉(hops).
    3단계 검색      : 홉마다 그 이벤트 사례 10건 중 가장 비슷한 1건. single 이면 기존과 똑같이 전체 질문으로 1건.
    모든 판단은 LLM(solar). trace["route"] 에 단계별 결정을 남긴다."""
    from prompts import build_v2_route_event_prompt, build_v2_route_hop_prompt
    persona_txt = json.dumps(persona or {}, ensure_ascii=False)

    def hop_ctx(q: str) -> str:  # 전체 질문이면 기존 ctx 그대로(single 경로 동작 불변), 부분 질문이면 전체를 맥락으로 붙인다
        return ctx if q == query else f"[질문]\n{q}\n[전체 질문(맥락)]\n{query}\n[페르소나]\n{persona_txt}"

    # 1) 이벤트 단위
    parts = [{"event": events[0], "question": query}]
    ev_route = None
    if len(events) >= 2:
        ev_list = "\n".join(f"- {e}: {_event_title(e)}" for e in events)
        ev_route = llm_json(build_v2_route_event_prompt(), ctx + "\n[이벤트 목록]\n" + ev_list, 0.0)
        if ev_route.get("mode") == "multi_event":
            merged: dict[str, list[str]] = {}
            for pt in ev_route.get("parts") or []:
                if isinstance(pt, dict) and pt.get("event") in events and str(pt.get("question") or "").strip():
                    merged.setdefault(pt["event"], []).append(str(pt["question"]).strip())
            if len(merged) >= 2:  # 주 이벤트가 먼저 오게 분류 순서로 정렬
                parts = [{"event": e, "question": " ".join(merged[e])} for e in events if e in merged]

    # 2) 이벤트 내 (부분 질문별 병렬)
    def split_hops(part: dict) -> dict:
        head = f"[이벤트]\n{part['event']}: {_event_title(part['event'])}\n"
        r = llm_json(build_v2_route_hop_prompt(), head + hop_ctx(part["question"]), 0.0)
        hops = [str(h).strip() for h in (r.get("hops") or []) if str(h).strip()][:ROUTE_MAX_HOPS] if r.get("mode") == "multi_hop" else []
        if len(hops) < 2:
            hops = [part["question"]]
        return {**part, "hop_mode": "multi_hop" if len(hops) >= 2 else "single", "hop_reason": r.get("reason", ""),
                "hops": [{"question": h} for h in hops]}

    with ThreadPoolExecutor(4) as pool:
        parts = list(pool.map(split_hops, parts))

    # 3) 홉별 사례 검색 (병렬)
    jobs = [(pt, h) for pt in parts for h in pt["hops"]]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda j: pick_example(j[0]["event"], hop_ctx(j[1]["question"]), llm_json), jobs))
    examples: list[dict] = []
    seen: set[str] = set()
    for (pt, h), (ex, info) in zip(jobs, results):
        h.update(info)
        if ex and ex["id"] not in seen:
            seen.add(ex["id"])
            examples.append({**ex, "hop_question": h["question"]})

    mode = "multi_event" if len(parts) >= 2 else ("multi_hop" if parts[0]["hop_mode"] == "multi_hop" else "single")
    trace["route"] = {"mode": mode, "event_level": ev_route, "parts": parts}
    trace["examples"] = [{"id": e["id"], "hop": e["hop_question"]} for e in examples]
    first = next((h for pt in parts for h in pt["hops"] if h.get("id")), None)  # 기존 trace["example"] 호환(user_test 등)
    trace["example"] = {"id": first["id"], "reason": first.get("reason", "")} if first else {"id": None, "reason": ""}
    return examples, mode


# ---------- LLM 오케스트레이션 (서비스 경로: Upstage 등) ----------
def build_open_menu(related: list[str]) -> dict:
    """open 모드 메뉴: 조금 관련된 이벤트의 항목 + 공통 행정 절차 카탈로그(전입신고·사업자등록·구직급여 …)."""
    # 공통 카탈로그(전입신고·사업자등록 …)는 넣지 않는다 — 관련 이벤트 메뉴에 필요한 건 이미 있고,
    # 층간소음 질문에 전입신고가 끼는 식의 잡음만 늘었다(스모크 u36·u37). LLM 이 꼭 필요하다고 보면 extra_law 로 적고 흡수된다.
    return build_menu(related)


def _curate_open(query: str, persona: dict | None, llm_json, ctx: str, related: list[str], trace: dict) -> tuple[dict, dict]:
    """목록에 맞는 이벤트가 없을 때: 공통 절차 + 관련 이벤트 메뉴 + 보조금24 전국 검색 + 법령 검색으로 처음부터 설계한다(참고 예시 없음).
    행정과 무관한 질문이면 LLM 이 목록을 비워 절차 0개가 된다(프론트가 '찾지 못했어요' 안내)."""
    from prompts import build_v2_open_plan_prompt, build_v2_select_prompt
    menu = build_open_menu(related)
    trace.update({"mode": "open", "related": related})
    plan = llm_json(build_v2_open_plan_prompt(), ctx + "\n[API 메뉴]\n" + menu_text(menu), 0.2)
    plan["events"] = related
    ex = execute(plan, query, persona, menu=menu)
    # LLM 이 제안한 공공기관 절차(근거 없음)는 select 단계가 '질문을 직접 해결하는 것'만 승인한다
    suggested = [sg for sg in (plan.get("suggested") or [])[:3] if isinstance(sg, dict) and str(sg.get("name") or "").strip()]
    for i, sg in enumerate(suggested, 1):
        sg["id"] = f"S{i}"
    select = {}
    if ex["candidates"] or ex["extra_law"] or suggested:
        stext = "\n[AI 제안 절차 — 법령·서비스 근거 없음]\n" + ("\n".join(
            f"- {sg['id']} {str(sg['name']).strip()} ({sg.get('agency') or '기관 미상'})" for sg in suggested) or "- (없음)")
        select = llm_json(build_v2_select_prompt(), ctx + "\n" + candidates_text(ex, plan) + stext, 0.0)
    pb = assemble(plan, ex, select)
    import difflib
    have = [_norm_title(p["name"]) for p in pb["procedures"]]
    def dup(n: str) -> bool:  # '금융감독원 분쟁조정 신청' ≈ '금융분쟁조정 신청' 같은 중복 제거
        return any(n == h or (len(min(n, h, key=len)) >= 4 and (n in h or h in n)) or difflib.SequenceMatcher(None, n, h).ratio() >= 0.7 for h in have)
    accepted = set(select.get("suggested_accept") or [])
    for sg in suggested:
        name = str(sg["name"]).strip()
        if sg["id"] not in accepted or dup(_norm_title(name)):
            continue
        out = _base_proc(sg["id"], name, sg.get("priority", "nice"), str(sg.get("condition") or ""))
        out["key"] = f"x:{name}"  # 그래프 노드 키(학습판에 새 노드로 등록된다)
        out["verification_status"] = "unverified"
        out["summary"] = "AI가 제안한 절차입니다. 정확한 요건·서류·기한은 담당 기관에서 확인하세요."
        if sg.get("agency"):
            out["agency"] = str(sg["agency"])
        if str(sg.get("link") or "").startswith("http"):
            out["link"] = sg["link"]
        pb["procedures"].append({"ref": sg["id"], **out})
        have.append(_norm_title(name))
    kb.apply(pb["procedures"])
    pb["procedures"], _ = graph.apply(pb["procedures"])  # AI 제안 절차까지 포함해 간선·추천 순서 재계산
    pb.update({"event": "custom", "events": related, "title": plan.get("title") or "내 생활 이벤트",
               "emoji": plan.get("emoji") or "🌰", "intro": plan.get("intro") or ""})
    trace.update({"plan": plan, "select": select, "unresolved": ex.get("unresolved", []), "absorbed": ex.get("absorbed", []),
                  "candidates": [{"cid": c["cid"], "name": c["name"], "source": c["source"]} for c in ex["candidates"]]})
    graph.learn(pb, llm_json, query, trace)  # 그래프에 없는 새 절차가 있으면 간선 제안 → 검증 → 학습판에 확장
    return pb, trace


def curate_dynamic_v2(query: str, persona: dict | None, llm_json, use_examples: bool = True) -> tuple[dict, dict]:
    """llm_json(system, user, temperature) → dict. 반환: (playbook, 단계별 결정 trace).
    단계: 분류 → 라우팅(single / multi_hop / multi_event, 홉마다 가장 비슷한 사례 1건) → 계획(사례들을 few-shot 으로) → 실행 → 선택·검증 → 조립.
    분류가 상한(3개)을 넘는 이벤트를 놓쳤으면(결혼+퇴사+이사+임신) 3개로 만든 플레이북은 그대로 두고, 빠진 이벤트의 절차만 따로 만들어 덧붙인다."""
    from prompts import build_v2_classify_prompt
    ctx = f"[질문]\n{query}\n[페르소나]\n{json.dumps(persona or {}, ensure_ascii=False)}"
    cls = llm_json(build_v2_classify_prompt(event_catalog()), ctx, 0.0)
    all_events = normalize_events(cls.get("events"), cls.get("primary"), limit=None)
    events = all_events[:MAX_EVENTS]
    trace: dict = {"classify": cls}
    extra_event = all_events[MAX_EVENTS] if len(all_events) > MAX_EVENTS else None  # 모델이 4개를 냈으면 4번째
    if extra_event is None and len(events) == MAX_EVENTS:  # 상한까지 찼으면 빠진 이벤트가 있을 수 있다 → 하나만 더 묻는다
        from prompts import build_v2_classify_extra_prompt
        extra = llm_json(build_v2_classify_extra_prompt(event_catalog()), ctx + "\n[고른 이벤트]\n" + ", ".join(events), 0.0)
        trace["classify_extra"] = extra
        if extra.get("event") in EVENTS and extra["event"] not in events:
            extra_event = extra["event"]
    if not events:  # 맞는 이벤트가 없음 → 예시 없이 처음부터 설계(open 모드)
        related = normalize_events(cls.get("related"))
        return _curate_open(query, persona, llm_json, ctx, related, trace)
    pb = _curate_events(query, persona, llm_json, ctx, events, trace, use_examples)
    if pb is None:  # 이벤트는 골랐는데 계획이 비었거나 전부 못 찾음 → 그 이벤트를 힌트로 처음부터 다시 설계
        trace["fallback"] = "open"
        return _curate_open(query, persona, llm_json, ctx, events, trace)
    if extra_event:
        sub_trace: dict = {}
        sub = _curate_events(query, persona, llm_json, ctx, [extra_event], sub_trace, use_examples)
        trace["extra_event"] = {"event": extra_event, "trace": sub_trace}
        if sub:  # 검증된 절차(법령·공통·보조금24)만 — AI 제안(x:)은 앞 플레이북 항목과 이름만 다른 중복이 되기 쉽다
            _append_procedures(pb, [p for p in sub["procedures"] if not str(p.get("key", "")).startswith("x:")])
            pb["events"] = list(pb.get("events") or []) + [extra_event]
    return pb, trace


def _curate_events(query: str, persona: dict | None, llm_json, ctx: str, events: list[str], trace: dict,
                   use_examples: bool = True) -> dict | None:
    """고른 이벤트들로 메뉴 → 라우팅·사례 → 계획 → 실행 → 선택 → 조립. 절차가 하나도 없으면 None."""
    from prompts import build_v2_plan_prompt, build_v2_select_prompt
    menu = build_menu(events)
    examples: list[dict] = []
    mode = "single"
    if use_examples:
        examples, mode = route_and_retrieve(query, persona, events, ctx, llm_json, trace)
    menu = expand_menu_with_graph(menu, examples)  # 메뉴·예시 절차의 그래프 1-hop 이웃을 '이어진 절차'로 추가
    trace["graph_menu"] = [{"ref": a["ref"], "name": a["name"], "via": a["via"]} for a in menu.get("graph_refs", [])]
    ex_text = examples_block(examples, menu, mode)
    plan_input = ctx + "\n[API 메뉴]\n" + menu_text(menu) + (("\n\n" + ex_text) if ex_text else "")
    plan = llm_json(build_v2_plan_prompt(), plan_input, 0.2)
    plan["events"] = events
    ex = execute(plan, query, persona, menu=menu)
    select = {}
    if ex["candidates"] or ex["extra_law"]:
        hint = ""
        picks = example_picks(examples)
        if picks:
            hint = f"\n[참고] 비슷한 사례에서 고른 지역 혜택(지역이 다르니 성격만 참고): {picks}"
        select = llm_json(build_v2_select_prompt(), ctx + "\n" + candidates_text(ex, plan) + hint, 0.0)
    trace.update({"plan": plan, "select": select, "unresolved": ex.get("unresolved", []), "absorbed": ex.get("absorbed", []),
                  "candidates": [{"cid": c["cid"], "name": c["name"], "source": c["source"]} for c in ex["candidates"]]})
    pb = assemble(plan, ex, select)
    if not pb["procedures"]:
        return None
    kb.apply(pb["procedures"])  # 법령·AI제안 절차에 창구·서류·링크(지식베이스) 덧입히기
    graph.learn(pb, llm_json, query, trace)  # 그래프에 없는 새 절차(extra_law 등)가 있으면 간선 제안 → 검증 → 학습판에 확장
    return pb


def _append_procedures(pb: dict, extra: list[dict]) -> None:
    """다른 이벤트로 만든 절차를 기존 플레이북 뒤에 덧붙인다. 같은 절차(key 또는 이름)는 건너뛰고,
    id 가 겹치면 바꾸며, 선행 관계(dependsOn·suggestedAfter)는 바뀐 id·기존 절차 id 로 고쳐 잇는다. 기존 절차는 건드리지 않는다."""
    procs = pb["procedures"]
    by_key = {p["key"]: p["id"] for p in procs if p.get("key")}
    by_name = {p.get("name"): p["id"] for p in procs}
    ids = {p["id"] for p in procs}
    rename: dict[str, str] = {}
    adds = []
    for p in extra:
        same = by_key.get(p.get("key")) if p.get("key") else None
        same = same or by_name.get(p.get("name"))
        if same:
            rename[p["id"]] = same
            continue
        nid = p["id"]
        while nid in ids:
            nid = "e-" + nid
        rename[p["id"]] = nid
        ids.add(nid)
        adds.append(p)
    order = max((p.get("order") or 0 for p in procs), default=0)
    for p in adds:
        q = dict(p, id=rename[p["id"]])
        for f in ("dependsOn", "suggestedAfter"):
            if q.get(f):
                q[f] = [rename[d] for d in q[f] if d in rename]
        if "order" in q:
            order += 1
            q["order"] = order
        procs.append(q)
