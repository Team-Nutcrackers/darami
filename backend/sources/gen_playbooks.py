"""v2 플레이북 사전 생성 드라이버 — LLM 단계의 결정을 JSON 으로 받아 curate_v2 를 실행한다.

generated/query.json 의 각 질의에 대해, LLM 역할(사전 생성 단계에서는 Claude 가 메인 에이전트로 판단)이
단계별 결정을 적고, 이 스크립트가 API 실행·조립을 맡는다.
결정 = {"classify": {events, reason}, "plan": {...}, "select": {...}} (스키마는 prompts.V2_*_PROMPT)
plan 의 메뉴 항목은 ref(L1/G2…) 또는 name 으로 지정할 수 있다(이름 우선).

실행 (backend/ 에서):
    .venv/bin/python -m sources.gen_playbooks show-event marriage            # 질의 10개 + API 메뉴
    .venv/bin/python -m sources.gen_playbooks menu birth,parental-leave     # 이벤트 조합의 메뉴
    .venv/bin/python -m sources.gen_playbooks plan-batch generated/batches/marriage.plan.json
        # {qid: {classify, plan}} → decisions/<qid>.json 저장 + API 실행 → 지역 후보·법령 매칭 출력
    .venv/bin/python -m sources.gen_playbooks select-batch generated/batches/marriage.select.json
        # {qid: select} → decisions 에 병합 + 플레이북 조립 → playbooks/examples/<event>/<qid>.json
    .venv/bin/python -m sources.gen_playbooks status
    (단건: show / run / finish <qid>)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import curate_v2 as cv

GEN = Path(__file__).resolve().parent.parent / "generated"
QUERIES = GEN / "query.json"
DECISIONS = GEN / "decisions"
WORK = GEN / "work"
OUT = GEN.parent / "playbooks" / "examples"
MARK = {"verified": "✓", "partial": "◐"}


def _queries() -> list[dict]:
    return json.loads(QUERIES.read_text(encoding="utf-8"))["queries"]


def _query(qid: str) -> dict:
    for q in _queries():
        if q["id"] == qid:
            return q
    raise SystemExit(f"질의 없음: {qid}")


def _decisions(qid: str) -> dict:
    p = DECISIONS / f"{qid}.json"
    if not p.exists():
        raise SystemExit(f"결정 파일 없음: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _short(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- 보기 ----------
def cmd_show(qid: str, events: str | None) -> None:
    q = _query(qid)
    print(f"[{qid}] {q['persona_summary']}\n질문: {q['query']}\n페르소나: {json.dumps(q['persona'], ensure_ascii=False)}")
    print(f"지역: {cv.resolve_region(q['persona'], q['query'])}")
    dp = DECISIONS / f"{qid}.json"
    if events:
        evs = cv.normalize_events(events.split(","))
    elif dp.exists():
        evs = json.loads(dp.read_text(encoding="utf-8"))["classify"]["events"]
    else:
        evs = [q["event_id"]]
    print(f"\n[API 메뉴: {evs}]\n" + cv.menu_text(cv.build_menu(evs)))


def cmd_show_event(eid: str) -> None:
    qs = [q for q in _queries() if q["event_id"] == eid]
    if not qs:
        raise SystemExit(f"이벤트 없음: {eid}")
    e = cv.EVENTS[eid]
    print(f"== {eid} {e['emoji']} {e['title']} — {e['description']}")
    for q in qs:
        sido, sgg = cv.resolve_region(q["persona"], q["query"])
        done = "✔" if (OUT / eid / f"{q['id']}.json").exists() else " "
        print(f"{done}[{q['id']}] ({sido}/{sgg}) {q['persona_summary']}\n   {q['query']}")
    print(f"\n[API 메뉴: {[eid]}]\n" + cv.menu_text(cv.build_menu([eid])))


def cmd_menu(events: str) -> None:
    evs = cv.normalize_events(events.split(","))
    print(f"[API 메뉴: {evs}]\n" + cv.menu_text(cv.build_menu(evs)))


# ---------- 실행 ----------
def _run_one(q: dict, d: dict) -> dict:
    plan = {**d["plan"], "events": d["classify"]["events"]}
    t0 = time.time()
    ex = cv.execute(plan, q["query"], q["persona"])
    ex["execute_seconds"] = round(time.time() - t0, 1)
    _dump(WORK / f"{q['id']}.json", ex)
    return ex


def _print_run(qid: str, ex: dict, plan: dict) -> None:
    r = ex["region"]
    print(f"[{qid}] events={ex['events']} region={r['sido']}/{r['sgg']} ({ex['execute_seconds']}s)"
          + (f"  ⚠ unresolved={ex['unresolved']}" if ex.get("unresolved") else ""))
    print("  procs: " + " | ".join(f"{p['ref']} {p['name']}{MARK.get(p['verification_status'], '✗')}" for p in ex["procedures"]))
    for c in ex["candidates"]:
        print(f"  {c['cid']} [{c['source'][0]}] {c['name']} ({_short(c['agency'].split()[-1] if c['agency'] else '', 12)}) — {_short(c['summary'], 48)}")
    xs = {x.get("id"): x for x in plan.get("extra_law") or []}
    for xid, g in ex["extra_law"].items():
        art = f"{g.get('law_name')} {g.get('article')}" if g.get("article") else "✗ 조문 못 찾음"
        print(f"  {xid} {xs.get(xid, {}).get('name', '')} → {art}")


def cmd_run(qid: str, force: bool = False) -> None:
    q, d = _query(qid), _decisions(qid)
    if d.get("select") and (WORK / f"{qid}.json").exists() and not force:
        raise SystemExit("이미 select 가 있는 질의입니다(후보가 바뀌면 선택이 어긋남). 다시 실행하려면 --force")
    ex = _run_one(q, d)
    _print_run(qid, ex, d["plan"])


def cmd_plan_batch(path: str) -> None:
    batch = json.loads(Path(path).read_text(encoding="utf-8"))
    classify_all = json.loads((GEN / "classify.json").read_text(encoding="utf-8"))
    for qid, d in batch.items():
        q = _query(qid)
        if not d.get("classify"):  # 분류는 generated/classify.json(메인 에이전트 분류)에서 채운다
            d["classify"] = classify_all[qid]
        assert d["classify"].get("events") and d.get("plan"), f"{qid}: classify.events/plan 필요"
        dp = DECISIONS / f"{qid}.json"
        prev = json.loads(dp.read_text(encoding="utf-8")) if dp.exists() else {}
        _dump(dp, {"classify": d["classify"], "plan": d["plan"], **({"select": prev["select"]} if prev.get("select") else {})})
        try:
            ex = _run_one(q, _decisions(qid))
            _print_run(qid, ex, d["plan"])
        except Exception as e:  # noqa: BLE001
            print(f"[{qid}] ✗ 실행 실패: {e!r}")


# ---------- 조립 ----------
def _finish_one(q: dict, d: dict) -> dict:
    wp = WORK / f"{q['id']}.json"
    if not wp.exists():
        raise SystemExit(f"{q['id']}: 먼저 run/plan-batch 를 실행하세요.")
    ex = json.loads(wp.read_text(encoding="utf-8"))
    plan = {**d["plan"], "events": d["classify"]["events"]}
    pb = cv.assemble(plan, ex, d.get("select") or {})
    procs = pb["procedures"]
    pair = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "generator": {"llm": "claude (main agent)", "curate": "curate_v2"},
        "query": q,
        "decisions": d,
        "playbook": pb,
        "stats": {"procedures": len(procs),
                  "by_status": dict(Counter(p["verification_status"] for p in procs)),
                  "candidates": len(ex["candidates"]), "unresolved": ex.get("unresolved", []),
                  "execute_seconds": ex.get("execute_seconds")},
    }
    _dump(OUT / q["event_id"] / f"{q['id']}.json", pair)
    return pair


def _print_finish(pair: dict) -> None:
    pb, st = pair["playbook"], pair["stats"]
    musts = [p for p in pb["procedures"] if p["priority"] == "must"]
    nices = [p for p in pb["procedures"] if p["priority"] != "must"]

    def fmt(p):
        return f"{p['name']}{MARK.get(p['verification_status'], '✗')}" + (f"[{_short(p['deadline'], 14)}]" if p.get("deadline") else "")

    print(f"[{pair['query']['id']}] '{pb['title']}' {st['procedures']}개 {st['by_status']}"
          + (f" ⚠ unresolved={st['unresolved']}" if st.get("unresolved") else ""))
    print("  must: " + " | ".join(fmt(p) for p in musts))
    print("  nice: " + " | ".join(fmt(p) for p in nices))


def cmd_finish(qid: str) -> None:
    _print_finish(_finish_one(_query(qid), _decisions(qid)))


def cmd_select_batch(path: str) -> None:
    batch = json.loads(Path(path).read_text(encoding="utf-8"))
    for qid, sel in batch.items():
        q, d = _query(qid), _decisions(qid)
        # 지역 후보 선택을 work 파일의 후보와 대조해 service_id 를 채운다(cid 또는 name 으로 지정 가능)
        wp = WORK / f"{qid}.json"
        cands = json.loads(wp.read_text(encoding="utf-8"))["candidates"] if wp.exists() else []
        by_cid, by_name = {c["cid"]: c for c in cands}, {c["name"]: c for c in cands}
        picks = []
        for r in sel.get("regional") or []:
            c = by_cid.get(r.get("cid")) or by_name.get(r.get("name"))
            if not c:
                print(f"[{qid}] ⚠ 지역 후보 없음: {r.get('cid')} {r.get('name')}")
                continue
            picks.append({**r, "cid": c["cid"], "name": c["name"], "service_id": c["service_id"]})
        sel = {**sel, "regional": picks}
        d["select"] = sel
        _dump(DECISIONS / f"{qid}.json", d)
        try:
            _print_finish(_finish_one(q, d))
        except Exception as e:  # noqa: BLE001
            print(f"[{qid}] ✗ 조립 실패: {e!r}")


def cmd_status() -> None:
    qs = _queries()
    by_event: dict[str, list] = {}
    for q in qs:
        by_event.setdefault(q["event_id"], []).append(q["id"])
    tot_d = tot_w = tot_p = 0
    for eid, ids in by_event.items():
        d = sum((DECISIONS / f"{i}.json").exists() for i in ids)
        w = sum((WORK / f"{i}.json").exists() for i in ids)
        p = sum((OUT / eid / f"{i}.json").exists() for i in ids)
        tot_d, tot_w, tot_p = tot_d + d, tot_w + w, tot_p + p
        if d or w or p:
            print(f"{eid:24} decisions {d:2}/10  work {w:2}/10  playbooks {p:2}/10")
    print(f"\n합계: decisions {tot_d}/{len(qs)}  work {tot_w}/{len(qs)}  playbooks {tot_p}/{len(qs)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="v2 플레이북 사전 생성 드라이버")
    ap.add_argument("cmd", choices=["show", "show-event", "menu", "run", "finish", "plan-batch", "select-batch", "status"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--events")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    {
        "show": lambda: cmd_show(a.arg, a.events),
        "show-event": lambda: cmd_show_event(a.arg),
        "menu": lambda: cmd_menu(a.arg),
        "run": lambda: cmd_run(a.arg, a.force),
        "finish": lambda: cmd_finish(a.arg),
        "plan-batch": lambda: cmd_plan_batch(a.arg),
        "select-batch": lambda: cmd_select_batch(a.arg),
        "status": cmd_status,
    }[a.cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
