"""사용자 페이즈 스모크 테스트 — "?" 경로(자유 입력, 페르소나 없음)를 Upstage LLM 으로 끝까지 돌린다.

generated/user_tests/<set>.json 에 {id, query, expect_event, expect_region?} 목록을 두고,
각 질문을 curate_dynamic_v2(query, None, upstage) 로 실행해 결과·판단 추적·자동 검사를 저장한다.

자동 검사
- classify: 주 이벤트가 expect_event 와 같은가(보조 이벤트 포함 여부도 기록)
- example : 같은 이벤트의 사전 생성 플레이북 중 하나를 골랐는가
- plan    : 메뉴에서 못 찾은 항목(unresolved)이 없는가, 절차가 1개 이상인가
- ground  : unverified 비율, must 절차 미검증 여부
- error   : 예외/빈 응답 없음

실행: .venv/bin/python -m sources.user_test generated/user_tests/set1.json [--provider solar] [--only id]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

from main import get_provider, make_json_caller
from .curate_v2 import curate_dynamic_v2, resolve_region

GEN = Path(__file__).resolve().parent.parent / "generated"


def check(case: dict, pb: dict, trace: dict) -> dict:
    procs = pb.get("procedures", [])
    events = pb.get("events") or []
    st = Counter(p.get("verification_status") for p in procs)
    problems = []
    if case.get("expect_open"):  # 맞는 이벤트가 없어도 처음부터 설계해 절차가 나와야 하는 질문
        if procs:
            return {"ok": True, "problems": [], "warnings": [], "events": events, "expect": "open", "example": None,
                    "n": len(procs), "status": dict(st), "region": pb.get("region"), "unresolved": trace.get("unresolved", []),
                    "mode": trace.get("mode")}
        return {"ok": False, "problems": ["open 모드인데 절차 0개"], "warnings": [], "events": events, "expect": "open",
                "example": None, "n": 0, "status": {}, "region": pb.get("region"), "unresolved": [], "mode": trace.get("mode")}
    expect_none = "expect_any" in case and not case["expect_any"] and not case.get("expect_event")
    if expect_none:  # 행정과 무관한 질문: 이벤트 없음·절차 없음이 정답
        if events or procs:
            problems.append(f"무관한 질문인데 이벤트 {events}/절차 {len(procs)}개 생성")
        return {"ok": not problems, "problems": problems, "warnings": [], "events": events, "expect": None,
                "example": None, "n": len(procs), "status": {}, "region": pb.get("region"), "unresolved": []}
    if not procs:
        problems.append("절차 0개")
    expect = case.get("expect_any") or ([case["expect_event"]] if case.get("expect_event") else [])
    if events and expect and events[0] not in expect:
        problems.append(f"주 이벤트 {events[0]} ∉ 기대 {expect}")
    if not events:
        problems.append("이벤트 미분류")
    if trace.get("unresolved"):
        problems.append(f"unresolved {trace['unresolved']}")
    warnings = []
    bad_must = [p["name"] for p in procs if p.get("priority") == "must" and p.get("verification_status") not in ("verified", "partial")]
    if bad_must:  # API 근거가 없는 'AI 제안'이 must 로 올라온 경우 — 실패는 아니고 품질 경고
        warnings.append(f"must 미검증(AI 제안) {bad_must}")
    ex = trace.get("example") or {}
    if events and ex.get("id") and not ex["id"].startswith(events[0] + "-"):
        problems.append(f"예시가 다른 이벤트 {ex.get('id')}")
    return {"ok": not problems, "problems": problems, "warnings": warnings, "events": events, "expect": case.get("expect_event"),
            "example": ex.get("id"), "n": len(procs), "status": dict(st),
            "region": pb.get("region"), "unresolved": trace.get("unresolved", [])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("set")
    ap.add_argument("--provider", default="solar")
    ap.add_argument("--only")
    a = ap.parse_args()
    cases = json.loads(Path(a.set).read_text(encoding="utf-8"))["cases"]
    if a.only:
        cases = [c for c in cases if c["id"] == a.only]
    provider = get_provider(a.provider)
    if provider.client is None:
        print(f"{provider.label} API 키 없음", file=sys.stderr); return 1
    llm = make_json_caller(provider)
    out_dir = GEN / "user_tests" / Path(a.set).stem
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for c in cases:
        t0 = time.time()
        rec = {"case": c, "provider": provider.key, "model": provider.model}
        try:
            pb, trace = curate_dynamic_v2(c["query"], None, llm)
            rec.update({"playbook": pb, "trace": trace, "seconds": round(time.time() - t0, 1), "check": check(c, pb, trace)})
        except Exception as e:  # noqa: BLE001
            rec.update({"error": repr(e), "traceback": traceback.format_exc(), "seconds": round(time.time() - t0, 1),
                        "check": {"ok": False, "problems": [f"예외 {e!r}"]}})
        (out_dir / f"{c['id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        ck = rec["check"]
        mark = "✓" if ck["ok"] else "✗"
        print(f"{mark} [{c['id']}] {rec['seconds']}s events={ck.get('events')} expect={c.get('expect_event')} "
              f"example={ck.get('example')} n={ck.get('n')} {ck.get('status')} region={(ck.get('region') or {}).get('sido')}")
        for p in ck["problems"]:
            print(f"    ! {p}")
        for w in ck.get("warnings", []):
            print(f"    ~ {w}")
        if "playbook" in rec:
            pb = rec["playbook"]
            print(f"    '{pb.get('title')}' — " + " | ".join(
                f"{p['name']}{'✓' if p['verification_status']=='verified' else '◐' if p['verification_status']=='partial' else '✗'}"
                for p in pb["procedures"][:8]))
        results.append(rec)
    ok = sum(r["check"]["ok"] for r in results)
    print(f"\n통과 {ok}/{len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
