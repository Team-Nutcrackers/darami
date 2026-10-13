"""사전 생성 플레이북(playbooks/examples) 전수 검증.

검사 항목
- 분류: classify.events[0](주 이벤트) == query.event_id(정답 라벨) / 어느 하나라도 일치
- 구조: 절차 0개, 이름 중복, 필수 필드 누락, 메뉴에서 못 찾은 항목(unresolved)
- grounding: verified/partial/unverified 비율, must 절차가 unverified 인 경우
- 지역: 페르소나 거주지가 있는데 지역을 못 잡은 경우
- 응급: emergency 매핑이 있는 이벤트에서 '지금 치료가 필요'한 질의인데 응급 절차가 없는 경우는 수동 확인 대상으로만 표시

실행: .venv/bin/python -m sources.verify_playbooks [--event birth] [--verbose]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from . import curate_v2 as cv

OUT = Path(__file__).resolve().parent.parent / "playbooks" / "examples"
REQUIRED = ("id", "name", "priority", "deadline", "deadlineDays", "agency", "online", "documents", "link",
            "source", "condition", "summary", "verification_status")


def check_pair(pair: dict) -> dict:
    q, d, pb = pair["query"], pair["decisions"], pair["playbook"]
    procs = pb["procedures"]
    events = d["classify"]["events"]
    problems: list[str] = []
    if not procs:
        problems.append("절차 0개")
    names = [p["name"] for p in procs]
    for n, c in Counter(names).items():
        if c > 1:
            problems.append(f"이름 중복: {n}")
    for p in procs:
        missing = [k for k in REQUIRED if k not in p]
        if missing:
            problems.append(f"{p.get('id')}: 필드 누락 {missing}")
    if pair["stats"].get("unresolved"):
        problems.append(f"unresolved {pair['stats']['unresolved']}")
    st = Counter(p["verification_status"] for p in procs)
    bad_must = [p["name"] for p in procs if p["priority"] == "must" and p["verification_status"] not in ("verified", "partial")]
    if bad_must:
        problems.append(f"must 미검증: {bad_must}")
    facts = (q.get("persona") or {}).get("facts") or {}
    if facts.get("residence") and not pb.get("region", {}).get("sido"):
        problems.append("거주지 있는데 지역 미해석")
    if not pb.get("title"):
        problems.append("제목 없음")
    return {
        "id": q["id"], "label": q["event_id"], "events": events,
        "primary_ok": bool(events) and events[0] == q["event_id"],
        "any_ok": q["event_id"] in events,
        "n": len(procs), "must": sum(p["priority"] == "must" for p in procs),
        "status": dict(st), "regional": sum(p["id"].startswith(("regional-", "local-")) for p in procs),
        "emergency": sum(p["id"].startswith("emergency-") for p in procs),
        "problems": problems,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    files = sorted(OUT.glob(f"{a.event or '*'}/*.json"))
    rows = [check_pair(json.loads(f.read_text(encoding="utf-8"))) for f in files]
    if not rows:
        print("플레이북 없음"); return 1
    n = len(rows)
    prim = sum(r["primary_ok"] for r in rows); anym = sum(r["any_ok"] for r in rows)
    st = Counter(); [st.update(r["status"]) for r in rows]
    tot = sum(st.values())
    print(f"플레이북 {n}개 · 절차 {tot}개 (평균 {tot / n:.1f}) · verified {st['verified']} ({st['verified'] / tot:.0%}) "
          f"partial {st['partial']} ({st['partial'] / tot:.0%}) unverified {st.get('unverified', 0)}")
    print(f"분류: 주 이벤트 일치 {prim}/{n} ({prim / n:.0%}), 포함 일치 {anym}/{n}")
    print(f"지역 절차 포함 {sum(r['regional'] > 0 for r in rows)}/{n} · 응급 포함 {sum(r['emergency'] > 0 for r in rows)}/{n} "
          f"· 절차 수 min/max {min(r['n'] for r in rows)}/{max(r['n'] for r in rows)}")
    multi = [r for r in rows if len(r["events"]) > 1]
    print(f"복수 이벤트 {len(multi)}개: " + ", ".join(f"{r['id']}{r['events']}" for r in multi[:30]) + (" …" if len(multi) > 30 else ""))
    mism = [r for r in rows if not r["primary_ok"]]
    if mism:
        print("주 이벤트 불일치: " + ", ".join(f"{r['id']}→{r['events']}" for r in mism))
    probs = [r for r in rows if r["problems"]]
    print(f"\n문제 있는 플레이북 {len(probs)}개")
    for r in probs:
        print(f"  {r['id']}: {'; '.join(r['problems'])}")
    if a.verbose:
        for r in rows:
            print(f"  {r['id']:26} n={r['n']:2} must={r['must']} {r['status']} reg={r['regional']} em={r['emergency']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
