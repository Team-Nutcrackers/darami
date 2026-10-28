"""사용자 문장 속 상대 날짜 표현 → 절대 날짜(결정적).

페르소나 관리자(LLM)가 "2주 뒤 토요일", "5일 전", "지난달 말"을 환산할 때 날짜 셈을 자주 틀리므로,
같은 문장을 여기서 규칙으로 한 번 더 계산해 LLM 값과 다르면 이 값으로 바로잡는다(/api/persona).
읽는 표현: 오늘/어제/그제/내일/모레, N일·주·개월·년 전/후/뒤, 지난·다음·이번·다다음·지지난 주/달/해(+말/초/중순),
"다음 달 3일", "N주 뒤 토요일"처럼 요일이 따라오면 그 주의 해당 요일, "10월 28일"·"10/28" 같은 절대 표기(앞의 내년·작년·올해 반영).
일부러 읽지 않는 것: "주말"(토·일 모름), "토요일에만·매주 토요일"(가능 시간), "매매가의 1/10"(분수) — 이런 건 LLM 값을 그대로 둔다.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_WD = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
_KNUM = {"하루": 1, "이틀": 2, "사흘": 3, "나흘": 4, "닷새": 5, "엿새": 6, "이레": 7, "열흘": 10, "보름": 15,
         "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
_NUMW = r"(\d+|하루|이틀|사흘|나흘|닷새|엿새|이레|열흘|보름|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)"


def _n(tok: str) -> int:
    return int(tok) if tok.isdigit() else _KNUM[tok]


def _add_months(d: date, months: int) -> date:
    y, m0 = d.year, d.month - 1 + months
    y, m = y + m0 // 12, m0 % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _weekday_in_week_of(base: date, wd: int) -> date:
    """base 가 속한 주(월~일)의 wd 요일"""
    return base - timedelta(days=base.weekday()) + timedelta(days=wd)


def parse_relative_date(text: str, today: date) -> tuple[date, str] | None:
    """문장에서 첫 번째 날짜 표현을 찾아 (날짜, 매칭 문구) 로. 없으면 None."""
    t = text.replace("２", "2")
    # 0) 절대 표기: 2026-10-28 / 10월 28일 / 10/28
    m = re.search(r"(\d{4})[-.년]\s*(\d{1,2})[-.월]\s*(\d{1,2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)
        except ValueError:
            pass
    for m in re.finditer(r"(?<![\d/])(\d{1,2})\s*월\s*(\d{1,2})\s*일|(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", t):
        if not m.group(1) and _FRACTION_CTX(t, m):  # "매매가의 1/10", "1/2 이상" 같은 분수
            continue
        mo, da = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4)))
        lead = t[max(0, m.start() - 8):m.start()]
        try:
            if re.search(r"내년|다음\s*해|이듬해", lead):
                d = date(today.year + 1, mo, da)
            elif re.search(r"작년|지난\s*해|지난해", lead):
                d = date(today.year - 1, mo, da)
            else:
                d = date(today.year, mo, da)
                if (d - today).days < -180 and not re.search(r"올해|금년", lead):  # 반년 넘게 과거면 다음 해 얘기
                    d = date(today.year + 1, mo, da)
            return d, m.group(0)
        except ValueError:
            pass
        break

    # 1) 오늘/어제/그제/내일/모레(+요일 무시)
    for word, off in (("그저께", -2), ("그제", -2), ("어제", -1), ("오늘", 0), ("내일", 1), ("모레", 2), ("글피", 3)):
        if re.search(rf"(?<![가-힣]){word}(?![가-힣])", t):
            return today + timedelta(days=off), word

    # 1-1) 하루·이틀·열흘·보름 처럼 단위가 붙은 날수 말 + 전/후/뒤
    m = re.search(r"(하루|이틀|사흘|나흘|닷새|엿새|이레|열흘|보름)\s*(전|후|뒤|뒷|있다가)", t)
    if m:
        n = _KNUM[m.group(1)]
        return today + timedelta(days=-n if m.group(2) == "전" else n), m.group(0)

    # 2) N(일|주|개월|년) 전/후/뒤/뒤에/이내  — "2주 뒤 토요일" 처럼 요일이 붙으면 그 주의 요일
    m = re.search(rf"{_NUMW}\s*(일|주일|주|달|개월|년)\s*(전|후|뒤|뒷|있다가|있으면|쯤\s*후|쯤\s*뒤)", t)
    if m:
        n, unit, dirw = _n(m.group(1)), m.group(2), m.group(3)
        sign = -1 if dirw == "전" else 1
        if unit == "일":
            base = today + timedelta(days=sign * n)
        elif unit in ("주", "주일"):
            base = today + timedelta(weeks=sign * n)
        elif unit in ("달", "개월"):
            base = _add_months(today, sign * n)
        else:
            base = _add_months(today, sign * 12 * n)
        wd = re.search(r"([월화수목금토일])요일", t[m.end():m.end() + 8])
        if wd and unit in ("주", "주일"):
            return _weekday_in_week_of(base, _WD[wd.group(1)]), t[m.start():m.end() + wd.end()]
        if wd:  # "10일 뒤 토요일" → 그 날 이후 가장 가까운 해당 요일
            d = base
            while d.weekday() != _WD[wd.group(1)]:
                d += timedelta(days=1)
            return d, t[m.start():m.end() + wd.end()]
        return base, m.group(0)

    # 3) 지난/다음/이번/다다음/지지난 (주|달|해) (+ 요일 | 초/중순/말 | 달 뒤 N일)
    m = re.search(r"(다다음|지지난|지난|저번|다음|이번|올)\s*(주|달|해|년)\s*(?:([월화수목금토일])요일|(초|중순|말)|(\d{1,2})\s*일(?!\s*(?:전|후|뒤|동안|간|째|만에)))?", t)
    if m:
        rel = {"다다음": 2, "지지난": -2, "지난": -1, "저번": -1, "다음": 1, "이번": 0, "올": 0}[m.group(1)]
        unit, wd, part, dnum = m.group(2), m.group(3), m.group(4), m.group(5)
        if unit == "주":
            if part == "말":  # '주말'은 토·일 어느 날인지 모른다 → 해석하지 않는다(LLM 값 유지)
                return None
            base = today + timedelta(weeks=rel)
            d = _weekday_in_week_of(base, _WD[wd]) if wd else base
            return d, m.group(0)
        if unit == "달":
            first = _add_months(today.replace(day=1), rel)
            last = calendar.monthrange(first.year, first.month)[1]
            if dnum:  # "다음 달 3일"
                return first.replace(day=min(int(dnum), last)), m.group(0)
            d = first.replace(day=last) if part == "말" else first.replace(day=15) if part == "중순" else first.replace(day=min(today.day, last)) if part is None else first
            return d, m.group(0)
        # 해/년
        y = today.year + rel
        d = date(y, 12, 31) if part == "말" else date(y, 1, 1) if part == "초" else date(y, 6, 30) if part == "중순" else date(y, today.month, min(today.day, calendar.monthrange(y, today.month)[1]))
        return d, m.group(0)

    # 4) 요일만: "토요일에 이사" → 오는 토요일(오늘 포함). "토요일에만 시간 돼요"·"매주 토요일" 같은 가능 시간 표현은 날짜가 아니다
    for m in re.finditer(r"(?:오는|이번)?\s*([월화수목금토일])요일", t):
        if re.match(r"\s*(?:에만|만|마다|밖에|빼고|제외)", t[m.end():]) or re.search(r"(?:매주|평일|주말|매번)\s*$", t[:m.start()]):
            continue
        d = today
        while d.weekday() != _WD[m.group(1)]:
            d += timedelta(days=1)
        return d, m.group(0)
    return None


def _FRACTION_CTX(t: str, m: re.Match) -> bool:
    """'N/M' 이 날짜가 아니라 분수로 쓰인 자리인지("매매가의 1/10", "1/2 이상", "3/4 정도")."""
    return bool(re.search(r"의\s*$", t[:m.start()]) or
                re.match(r"\s*(?:이상|이하|미만|초과|만큼|정도|수준|가량|씩|비율)", t[m.end():]))
