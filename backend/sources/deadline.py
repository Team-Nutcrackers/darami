"""절차 기한 문구 → 구조화 기한 규칙(deadlineRule).

프론트(lib/dday.ts)가 이 규칙을 민법식 기간 계산(초일 불산입·역에 의한 월 계산·말일이 토요일/공휴일이면 익일)으로 평가해
실제 만료일과 D-day 를 만든다. 여기서는 '문구를 규칙으로 바꾸는' 결정적 파서와, 플레이북에 규칙을 채우는 enrich 만 한다.

규칙 종류(JSON, 프론트 types.DeadlineRule 과 동일)
  relative        {"kind":"relative","from":<anchor>,"amount":N,"unit":"day|week|month|year","fromNextDay":bool}
                  예) "출생 후 1개월 이내", "퇴사 다음날부터 12개월 이내", "취득일부터 60일 이내"
  month_offset    {"kind":"month_offset","from":<anchor>,"months":M,"day":N|"last"}
                  예) "사유 발생 달의 다음 달 15일까지", "다음 달 말일까지"
  annual          {"kind":"annual","month":M,"day":D,"yearOffset":Y|null}
                  예) "다음 해 5월 31일까지", "매년 5월 1일~31일"
  after_procedure {"kind":"after_procedure","proc":"<절차 이름 조각>","amount":N,"unit":...}
                  예) "전입신고 후 14일 이내", "출생신고 후 60일 이내"
anchor(from): 페르소나 날짜 key. birth_date / death_date / marriage_date / resignation_date / move_date,
  그 밖은 "event"(프론트가 이벤트 기본 기준일 또는 event_date 로 푼다).
"""

from __future__ import annotations

import re

UNIT = {"일": "day", "주": "week", "주일": "week", "개월": "month", "월": "month", "달": "month", "년": "year", "해": "year"}
UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

# 기산점 키워드 → 페르소나 날짜 key (앞에 있을수록 우선)
ANCHOR_WORDS: list[tuple[str, str]] = [
    ("출생", "birth_date"), ("태어", "birth_date"), ("출산", "birth_date"),
    ("사망", "death_date"),
    ("혼인", "marriage_date"), ("결혼", "marriage_date"),
    ("퇴사", "resignation_date"), ("퇴직", "resignation_date"), ("이직", "resignation_date"), ("실직", "resignation_date"),
    ("전입", "move_date"), ("이사", "move_date"), ("입주", "move_date"), ("이주", "move_date"),
]
ANCHOR_LABEL = {
    "birth_date": "출생일", "death_date": "사망일", "marriage_date": "혼인일", "resignation_date": "퇴사일",
    "move_date": "이사일", "event_date": "사건 발생일",
}
# 이벤트 기본 기준일 (프론트 DEFAULT_ANCHOR 와 동일)
EVENT_ANCHOR = {"move": "move_date", "birth": "birth_date", "resignation": "resignation_date",
                "marriage": "marriage_date", "death": "death_date", "bereavement": "death_date"}

_NUM = r"(\d+)"
_AFTER = r"(?:후|뒤|이후|완료\s*후|가\s*끝난\s*뒤|을\s*마친\s*뒤)"
_WITHIN = r"(?:이내|안에|내에|내|까지)"
_PAREN = r"(?:\([^)]*\))?\s*"  # "12개월(수급기간) 안에" 처럼 단위 뒤 괄호 설명 허용


# 사용자 페르소나로는 알 수 없는 기산점(서류·행정 사건 기준) — 이런 건 계산하지 않고 원문 기한을 그대로 보여 준다
_OTHER_ANCHOR = re.compile(r"납부기한|고지|통지|발급|확정|판결|심판|허가|승인|개시|만료|종료|취득|계약|입국|출국|신청일|등록일|접수|수령|지급|지정|선고|공고|안\s*날|알게\s*된")


def _anchor_of(text: str) -> str:
    """기산점 키워드는 '…부터/…후/…한 날' 바로 앞 짧은 구절에서만 찾는다(문장 어딘가의 '이사·감사' 같은 우연 일치 방지).
    돌려주는 값: 페르소나 날짜 key / "event"(이벤트 기본 기준일) / "other"(페르소나로 알 수 없는 기산점)."""
    snippets = [m.group(1) for m in re.finditer(r"([가-힣·()]{1,14})\s*(?:일|날|때)?\s*(?:부터|후|이후|다음\s*날|익일)", text)]
    explicit = bool(snippets)
    if not snippets:
        snippets = [text[:20]]
    for snip in snippets:
        if re.search(r"이사\s*[·회]", snip):  # 이사(理事)회·이사·감사
            continue
        for w, key in ANCHOR_WORDS:
            if w in snip:
                return key
    if explicit and any(_OTHER_ANCHOR.search(sn) for sn in snippets):
        return "other"
    return "event"


def _has_anchor_clue(text: str) -> bool:
    """기한 문구에 기산점 단서('…한 날부터', '…후', '개시·만료·허가' 등)가 있는지 — 있으면 이름으로 기산점을 추측하지 않는다."""
    return bool(re.search(r"([가-힣·()]{1,14})\s*(?:일|날|때)?\s*(?:부터|후|이후|다음\s*날|익일)", text) or _OTHER_ANCHOR.search(text))


def _anchor_of_name(name: str) -> str | None:
    """절차 이름의 기산점 키워드 → 페르소나 날짜 key. 없으면 None."""
    if re.search(r"이사\s*[·회]", name):
        return None
    for w, key in ANCHOR_WORDS:
        if w in name:
            return key
    return None


def parse_rule(text: str | None, name: str = "") -> dict | None:
    """기한 문구 하나를 규칙으로. 못 읽으면 None. 여러 절이 있으면 첫 절('/' ';' 앞)을 본다."""
    if not text or not str(text).strip():
        return None
    t = str(text).strip()
    first = re.split(r"\s*[/;]\s*", t)[0]
    anchor = _anchor_of(first)
    next_day = bool(re.search(r"다음\s*날|익일|다음날", first))

    # 1) 선행 절차 기준: "전입신고 후 14일 이내", "출생신고를 마친 뒤 60일 안에"
    m = re.search(rf"([가-힣·]+(?:신고|등록|신청|허가|개시))\s*(?:를|을)?\s*{_AFTER}\s*{_NUM}\s*(일|주|개월|월|년)\s*{_WITHIN}", first)
    if m and not (name and m.group(1) in name):  # 자기 자신 이름이면 선행이 아니다
        return {"kind": "after_procedure", "proc": m.group(1), "amount": int(m.group(2)), "unit": UNIT[m.group(3)]}

    # 2) 월 오프셋: "사유 발생 달의 다음 달 15일까지", "다음 달 말일까지", "그 달 말일까지"
    m = re.search(r"(다음\s*달|익월|다다음\s*달|그\s*달|해당\s*월|당월)\s*(말일|(\d+)\s*일)", first)
    if m:
        months = 2 if "다다음" in m.group(1) else (1 if m.group(1).replace(" ", "") in ("다음달", "익월") else 0)
        day: int | str = "last" if m.group(2).startswith("말일") else int(m.group(3))
        return {"kind": "month_offset", "from": anchor, "months": months, "day": day}

    # 3) 고정 연간일: "다음 해 5월 31일까지", "이듬해 5월", "매년 5월 1일~31일", "5월 31일까지"
    m = re.search(r"(다음\s*해|이듬해|익년|다음\s*연도|매년|해당\s*연도|그\s*해)?\s*(\d{1,2})\s*월\s*(?:(\d{1,2})\s*일(?:\s*[~～-]\s*(\d{1,2})\s*일)?|말일)", first)
    if m and re.search(r"월\s*(\d{1,2}\s*일|말일)", first):
        lead = (m.group(1) or "").replace(" ", "")
        month = int(m.group(2))
        day: int | str = int(m.group(4) or m.group(3)) if (m.group(3) or m.group(4)) else "last"
        year_off = 1 if lead in ("다음해", "이듬해", "익년", "다음연도") else (0 if lead in ("해당연도", "그해") else None)
        return {"kind": "annual", "month": month, "day": day, "yearOffset": year_off, "from": anchor}

    # 4) 상대 기간: "출생 후 1개월 이내", "퇴사 다음날부터 12개월 이내", "취득일부터 60일 이내", "3일 전까지"(음수)
    m = re.search(rf"{_NUM}\s*(일|주일|주|개월|월|년)\s*{_PAREN}(이내|안에|내에|내|후까지|전까지|이전까지)", first)
    if m:
        amount = int(m.group(1))
        unit = UNIT[m.group(2)]
        if m.group(3) in ("전까지", "이전까지"):
            return {"kind": "relative", "from": anchor, "amount": -amount, "unit": unit}
        return {"kind": "relative", "from": anchor, "amount": amount, "unit": unit, "fromNextDay": next_day}
    return None


def rule_days(rule: dict | None) -> int | None:
    """규칙의 대략적 길이(일수) — 정렬·레거시 deadlineDays 용. 날짜가 없으면 None."""
    if not rule:
        return None
    k = rule.get("kind")
    if k in ("relative", "after_procedure"):
        return int(rule["amount"]) * UNIT_DAYS[rule["unit"]]
    if k == "month_offset":
        return 15 + 30 * int(rule.get("months", 0))
    return None


def enrich_procedure(p: dict, event: str | None = None) -> dict:
    """절차 한 건에 deadlineRule 을 채운다(이미 있으면 유지). 문구에서 못 읽으면 deadlineDays 로 상대 규칙을 만든다."""
    if p.get("deadlineRule"):
        return p
    rule = parse_rule(p.get("deadline"), p.get("name", ""))
    days = p.get("deadlineDays")
    if rule is None and isinstance(days, int) and 0 < days < 9999:
        rule = {"kind": "relative", "from": _anchor_of(str(p.get("deadline") or "")), "amount": days, "unit": "day"}
    if rule is None:
        return p
    if rule.get("from") == "event" and not _has_anchor_clue(str(p.get("deadline") or "")):
        # 문구에 기산점이 전혀 없으면('14일 이내') 절차 이름으로 — '전입신고'는 이사일, '사망신고'는 사망일
        rule["from"] = _anchor_of_name(p.get("name", "")) or "event"
    p["deadlineRule"] = rule
    # 레거시 숫자(정렬·그래프 순서용)가 비어 있으면 규칙 길이로 채운다
    if not isinstance(days, int) or days in (0, 9999):
        d = rule_days(rule)
        if d and d > 0:
            p["deadlineDays"] = d
    return p


def enrich_playbook(pb: dict | None) -> dict | None:
    if not pb or not isinstance(pb, dict):
        return pb
    for p in pb.get("procedures", []) or []:
        if isinstance(p, dict):
            enrich_procedure(p, pb.get("event"))
    return pb


def resolve_anchor(rule: dict, event: str | None) -> str | None:
    """규칙이 기대는 페르소나 날짜 key. 선행 절차 기준·고정일·페르소나로 알 수 없는 기산점(other)은 None."""
    k = rule.get("kind")
    if k == "after_procedure" or rule.get("from") == "other":
        return None
    if k == "annual" and rule.get("yearOffset") is None:
        return None
    a = rule.get("from") or "event"
    if a == "event":
        return EVENT_ANCHOR.get(event or "", "event_date")
    return a


def missing_anchor_block(playbook: dict | None, persona: dict | None, board=None) -> str:
    """플레이북 절차의 기한 계산에 필요한데 페르소나에 없는 기준일 → 모델에게 한 번 물어보라고 알리는 블록.
    보드에 올라간 절차가 있으면 그것들만, 아니면 must 절차 기준."""
    if not playbook:
        return ""
    facts = (persona or {}).get("facts") or {}
    on_board = {b.get("id") for b in (board or []) if isinstance(b, dict)}
    procs = [p for p in playbook.get("procedures", []) if isinstance(p, dict)]
    target = [p for p in procs if p.get("id") in on_board] or [p for p in procs if p.get("priority") == "must"]
    need: dict[str, list[str]] = {}
    for p in target:
        rule = p.get("deadlineRule")
        if not rule:
            continue
        key = resolve_anchor(rule, playbook.get("event"))
        if key and not facts.get(key):
            need.setdefault(key, []).append(p.get("name", p.get("id", "")))
    if not need:
        return ""
    lines = [f"- {ANCHOR_LABEL.get(k, k)}({k}): {', '.join(v[:3])}{' 외' if len(v) > 3 else ''} 의 기한 계산에 필요" for k, v in need.items()]
    first_label = ANCHOR_LABEL.get(next(iter(need)), "기준일")
    first_proc = next(iter(need.values()))[0]
    return ("[기준일 누락 — 반드시 처리]\n" + "\n".join(lines) +
            f"\n→ 이번 reply 마지막 문장에서 {first_label}이 언제인지 꼭 물어라(예: \"{first_label}이 언제인지 알려 주시면 {first_proc} 기한을 날짜로 계산해 드릴게요\")."
            f" askMissing 에 \"{first_label}\" 을 넣어라. 직전 assistant 턴에서 이미 같은 날짜를 물었거나 사용자가 모른다고 했으면 다시 묻지 않는다."
            " quickReplies 로는 날짜를 받을 수 없으니 날짜는 글로 답하게 한다(예: '10월 말에요', '지난주 금요일').")
