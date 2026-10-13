"""국가법령정보 OPEN API 클라이언트 (PoC: 전입신고 근거 검증).

- 검색: GET http://www.law.go.kr/DRF/lawSearch.do  (target=law)
- 본문: GET http://www.law.go.kr/DRF/lawService.do  (target=law, MST=법령일련번호)
- 인증: OC 파라미터(가입 ID). 호출 서버 IP/도메인 사전 등록 필요.
- .env 의 LAW_OC 사용.

절차별로 "법령 + 조문 + 법정기한"을 실조회해 플레이북 값과 교차검증하고,
legal_basis / evidence / verification_status 를 만들어 돌려준다.
"""

from __future__ import annotations

import os
import re
from datetime import date

import requests
from dotenv import load_dotenv

from .apicache import disk_cached

load_dotenv()

LAW_OC = os.environ.get("LAW_OC", "")
BASE = "http://www.law.go.kr/DRF"
TIMEOUT = 8

_UNIT_DAYS = {"일": 1, "개월": 30, "년": 365}

# 프로세스 수명 캐시 ((법령명|조문) 키, 법령은 자주 바뀌지 않음)
_ground_cache: dict[str, dict] = {}


def _today() -> str:
    return date.today().isoformat()


def _fmt_date(yyyymmdd: str) -> str:
    s = str(yyyymmdd or "")
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


@disk_cached("law_search")
def search_law(query: str) -> dict | None:
    """법령명을 검색해 현행 최상위 1건의 메타데이터를 반환."""
    try:
        r = requests.get(
            f"{BASE}/lawSearch.do",
            params={"OC": LAW_OC, "target": "law", "type": "JSON", "query": query, "display": "50"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    laws = (data.get("LawSearch") or {}).get("law")
    if not laws:
        return None
    if isinstance(laws, dict):
        laws = [laws]
    # 정확히 같은 법령명을 우선(가운뎃점·공백 변형 무시), 없으면 첫 현행
    def norm(s: str) -> str:
        return re.sub(r"[ㆍ·‧\s]", "", s or "")
    nq = norm(query)
    exact = next((l for l in laws if norm(l.get("법령명한글", "")) == nq), None)
    # 정확 일치 → 약칭 일치(예: '전자상거래법') → 이름에 질의 포함. 모두 없으면 실패(엉뚱한 법령에 grounding 하지 않음)
    law = (exact
           or next((l for l in laws if norm(l.get("법령약칭명", "")) == nq), None)
           or next((l for l in laws if nq in norm(l.get("법령명한글", ""))), None))
    if not law:
        # 개명된 법령('환경분쟁 조정법' → '환경분쟁 조정 및 환경피해 구제 등에 관한 법률'): 접미사(법·법률)를 뗀 핵심어(3자 이상)가
        # 이름에 들어 있는 법률을 고른다. 질의가 시행령·시행규칙이 아니면 시행령·시행규칙은 제외.
        core = re.sub(r"(시행령|시행규칙|법률|법)$", "", nq)
        if len(core) >= 3:
            is_rule = nq.endswith(("시행령", "시행규칙"))
            law = next((l for l in laws if core in norm(l.get("법령명한글", ""))
                        and (is_rule or not norm(l.get("법령명한글", "")).endswith(("시행령", "시행규칙")))), None)
    if not law:
        return None
    return {
        "law_name": law.get("법령명한글", "").strip(),
        "mst": law.get("법령일련번호", ""),
        "law_id": law.get("법령ID", ""),
        "enforcement_date": _fmt_date(law.get("시행일자", "")),
        "promulgation_date": _fmt_date(law.get("공포일자", "")),
        "ministry": law.get("소관부처명", ""),
        "kind": law.get("법령구분명", ""),
    }


def _collect_articles(obj) -> list:
    """응답 구조에 상관없이 '조문단위' 리스트를 찾아낸다."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "조문단위":
                return v if isinstance(v, list) else [v]
            found = _collect_articles(v)
            if found:
                return found
    elif isinstance(obj, list):
        for it in obj:
            found = _collect_articles(it)
            if found:
                return found
    return []


def _article_text(article: dict) -> str:
    """조문 내 모든 '...내용' 텍스트(조문내용·항내용·호내용 등)를 이어붙인다."""
    parts: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k.endswith("내용") and isinstance(v, str):
                    parts.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)
        elif isinstance(o, str):
            return

    walk(article)
    # 중복 제거하며 순서 유지
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "\n".join(out)


@disk_cached("law_articles")
def _fetch_articles(mst: str) -> list | None:
    """법령 본문(조문단위 리스트). 법령 하나가 수 MB 라 디스크 캐시."""
    try:
        r = requests.get(
            f"{BASE}/lawService.do",
            params={"OC": LAW_OC, "target": "law", "MST": mst, "type": "JSON"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    return _collect_articles(data) or None


_TITLE_CONNECT = re.compile(r"에\s*(대한|관한|따른|의한)|의한|따른|관한|대한|경우|또는|및")
_TITLE_PARTICLE = re.compile(r"(에서|에|의|등|을|를|과|와)(?=\s|$|[()\[\]「」,.·ㆍ])")  # 낱말 끝의 조사만('등록'의 '등'은 보존)
_TITLE_PUNCT = re.compile(r"\s|[·ㆍ()\[\]「」,.]")


# 조문 제목엔 잘 안 쓰이는 일상적 절차어 — 낱말 매칭에서 뺀다
_GENERIC_WORDS = {"신청", "신고", "통지", "통보", "절차", "방법", "제출", "서류", "접수", "발급", "처리", "안내", "확인", "요청", "요구"}


def _norm_article_title(s: str) -> str:
    """조문 제목 비교용 정규화 — 띄어쓰기·낱말 끝 조사·연결어 제거('묵시적 갱신의 경우 계약의 해지' → '묵시적갱신계약해지')."""
    s = _TITLE_CONNECT.sub(" ", s or "")
    s = _TITLE_PARTICLE.sub("", s)
    return _TITLE_PUNCT.sub("", s)


def _article_record(art: dict, match_level: str = "exact") -> dict:
    """match_level: exact(제목 일치) | contains(제목에 키워드 포함) | loose(낱말 일부만 일치 — 직접 근거인지 확인 필요)."""
    title = str(art.get("조문제목", ""))
    no = str(art.get("조문번호", "")).strip()
    branch = str(art.get("조문가지번호", "")).strip()
    label = f"제{no}조" + (f"의{branch}" if branch and branch != "0" else "")
    if title:
        label += f"({title})"
    return {"label": label, "title": title, "text": _article_text(art), "match_level": match_level}


def find_article(mst: str, keywords: list[str]) -> dict | None:
    """여러 키워드로 조문을 찾는다. 엄격한 단계부터 모든 키워드를 시도하고, 안 되면 다음 단계로:
    1) 제목 정확 일치  1-1) 띄어쓰기·조사만 다른 정확 일치  2) 제목에 키워드 포함
    3) 띄어쓰기·조사를 뺀 뒤 포함('계약해지' ⊂ '묵시적갱신계약해지')
    4) 키워드의 낱말(2자 이상, 신청·통지 같은 일상어 제외)이 전부 제목에 들어 있음 — 낱말이 하나뿐이면 조문이 유일할 때만.
    한 단계 안에서는 키워드 순서대로 본다."""
    arts = _fetch_articles(mst) or []
    kws = [str(k or "").strip() for k in keywords if str(k or "").strip()]
    if not arts or not kws:
        return None
    titles = [str(a.get("조문제목", "")) for a in arts]
    for kw in kws:
        for a, t in zip(arts, titles):
            if t.strip() == kw:
                return _article_record(a, "exact")
    norm_titles = [_norm_article_title(t) for t in titles]
    for kw in kws:  # 띄어쓰기·조사만 다른 정확 일치('1세대 1주택의 특례' = '1세대1주택의 특례')를 부분 일치보다 먼저
        nk = _norm_article_title(kw)
        for a, nt in zip(arts, norm_titles):
            if nt and nt == nk:
                return _article_record(a, "exact")
    def contains_level(nk: str, nt: str) -> str:  # 키워드가 제목의 절반 미만이면('장부' ⊂ '장부의 기록ㆍ보관 불성실 가산세') 느슨한 일치로 표시
        return "contains" if len(nk) * 2 >= len(nt) else "loose"
    for kw in kws:
        for a, t, nt in zip(arts, titles, norm_titles):
            if t and kw in t:
                return _article_record(a, contains_level(_norm_article_title(kw), nt))
    for kw in kws:
        nk = _norm_article_title(kw)
        if len(nk) < 2:
            continue
        for a, nt in zip(arts, norm_titles):
            if nt and nk in nt:
                return _article_record(a, contains_level(nk, nt))
    for kw in kws:
        toks = [_norm_article_title(w) for w in re.split(r"[\s·ㆍ,/]+", kw)]
        toks = [w for w in toks if len(w) >= 2 and w not in _GENERIC_WORDS]  # '해지 통보' → ['해지']
        if not toks:
            continue
        hits = [a for a, nt in zip(arts, norm_titles) if nt and all(w in nt for w in toks)]
        if len(hits) == 1 or (len(hits) > 1 and len(toks) >= 2):
            return _article_record(hits[0], "loose")  # 낱말 하나뿐이면 조문이 유일할 때만(예: '갱신'은 3개 조문에 걸려 실패)
    for kw in kws:  # 5) 긴 낱말(3자 이상)만으로 조문이 하나로 정해지면 채택('거주불명 등록' → '거주불명자에 대한 …' 유일)
        toks = [_norm_article_title(w) for w in re.split(r"[\s·ㆍ,/]+", kw)]
        toks = [w for w in toks if len(w) >= 3 and w not in _GENERIC_WORDS]
        if not toks:
            continue
        hits = [a for a, nt in zip(arts, norm_titles) if nt and all(w in nt for w in toks)]
        if len(hits) == 1:
            return _article_record(hits[0], "loose")
    return None


def get_article(mst: str, title_contains: str) -> dict | None:
    """법령 본문에서 제목에 키워드가 든 조문을 찾아 텍스트로 반환.
    제목이 키워드와 정확히 같은 조문을 우선하고(예: '휴직' → 제71조(휴직)), 없으면 부분일치, 그다음 띄어쓰기·조사 무시 일치."""
    return find_article(mst, [title_contains])


def _find_deadline(text: str) -> tuple[str | None, str | None, int | None]:
    """'N(일|개월|년) 이내'를 포함한 문장·문구·환산일수를 추출."""
    for sent in re.split(r"(?<=다\.)\s*|\n", text):
        m = re.search(r"(\d+)\s*(일|개월|년)\s*이내", sent)
        if m:
            num, unit = int(m.group(1)), m.group(2)
            return sent.strip(), f"{num}{unit} 이내", num * _UNIT_DAYS[unit]
    return None, None, None


def _build_legal_result(law: dict, art: dict | None, expected_days: int | None) -> dict:
    span, deadline_text, found_days = (None, None, None)
    if art:
        span, deadline_text, found_days = _find_deadline(art["text"])
    if not art:
        status = "needs_review"
    elif expected_days is None:
        status = "partial"
    elif found_days is not None and found_days == expected_days:
        status = "verified"
    else:
        status = "partial"
    return {
        "verification_status": status,
        "law_name": law["law_name"],
        "article": art["label"] if art else None,
        "article_title": art["title"] if art else None,  # 매칭된 조문제목(학습 카탈로그 영속용)
        "match_level": art.get("match_level") if art else None,
        "enforcement_date": law["enforcement_date"],
        "promulgation_date": law["promulgation_date"],
        "ministry": law["ministry"],
        "deadline_in_law_text": deadline_text,
        "deadline_days_in_law": found_days,
        "deadline_days_expected": expected_days,
        "evidence_span": span,
        "source_url": f"https://www.law.go.kr/법령/{law['law_name']}",
        "fetched_at": _today(),
    }


def ground_legal(law_query: str, article_title: str, expected_days: int | None = None) -> dict:
    """주어진 (법령·조문 제목 키워드)을 실시간 조회해 근거·기한을 grounding + 교차검증."""
    key = f"{law_query}|{article_title}"
    if key in _ground_cache:
        return _ground_cache[key]
    if not LAW_OC:
        return {"verification_status": "needs_review", "error": "LAW_OC 미설정"}
    law = search_law(law_query)
    if not law:
        return {"verification_status": "needs_review", "error": "법령 조회 실패(키/IP 등록 또는 네트워크)"}
    result = _build_legal_result(law, get_article(law["mst"], article_title), expected_days)
    _ground_cache[key] = result
    return result


def ground_legal_auto(law_query: str, keywords: list[str], expected_days: int | None = None) -> dict:
    """법령명 + 여러 키워드로 조문을 '검색'해 grounding.
    정확한 조문 제목을 몰라도 절차 키워드(예: '사업자등록')로 매칭 → 자기학습 카탈로그용.
    """
    if not LAW_OC:
        return {"verification_status": "needs_review", "error": "LAW_OC 미설정"}
    # LLM 이 '민법 제750조(불법행위)'처럼 조문까지 붙여 주면 법령명만 검색하고, 조문 번호·제목은 검색 단서로 쓴다
    m = re.search(r"\s*제\s*(\d+)\s*조(?:의\s*(\d+))?\s*(?:\(([^)]*)\))?\s*$", law_query or "")
    art_no = art_branch = None
    keywords = list(keywords or [])
    if m:
        law_query = law_query[:m.start()].strip()
        art_no, art_branch = m.group(1), (m.group(2) or "0")
        if m.group(3):
            keywords = [m.group(3).strip(), *keywords]
    law = search_law(law_query)
    if not law:
        return {"verification_status": "needs_review", "error": "법령 조회 실패"}
    art = None
    if art_no:
        for a in _fetch_articles(law["mst"]) or []:
            if str(a.get("조문번호", "")).strip() == art_no and (str(a.get("조문가지번호", "")).strip() or "0") == art_branch:
                art = _article_record(a, "exact")
                break
    art = art or find_article(law["mst"], keywords)
    return _build_legal_result(law, art, expected_days)
