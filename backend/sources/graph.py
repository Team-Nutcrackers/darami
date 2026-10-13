"""절차 그래프(Procedural Graph) 런타임 엔진.

기본판 playbooks/graph.base.json(sources/build_graph.py 가 500건+카탈로그로 생성)과
학습판 playbooks/graph.learned.json(사용 중 새 절차가 나오면 LLM 제안 → 구조 검증 → 여기에만 추가)을 합쳐 쓴다.
학습판은 언제든 지워도 된다(기본판과 동작 동일, `python -m sources.graph reset-learned`).

하는 일
  apply(procs)      : 플레이북 절차에 dependsOn 을 채우고, requires(hard) 선행이 없으면 메뉴 출처로 자동 추가
  rank(procs)       : 선행→후속 순서를 지키는 추천 순서(위상 정렬 + must·기한·unlocks 우선). 각 절차에 order 를 붙인다
  guidance_block()  : 보드 상태(완료 노드)로 '현재 위치'를 잡고 지금 할 수 있는 것·막힌 것·간선 안내를 프롬프트 텍스트로 만든다
                      (논문 3.2 Generative Guidance 의 localize → extract(이웃) → 텍스트화. 생성은 대화 LLM 이 한다)
  learn(pb, llm)    : 그래프에 없는 새 절차가 플레이북에 있으면 LLM 에게 간선을 제안받아 구조 검증(끝점·중복·역방향·순환) 후
                      학습판에 커밋. 거절된 제안은 rejected 에 남겨 다음 제안에서 금지한다(논문 3.3 의 구조 검사 + rejection memory.
                      검증 집합 점수 게이트는 없다 — 대신 사용자가 학습판을 통째로 지울 수 있다)
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_PATH = ROOT / "playbooks" / "graph.base.json"
LEARNED_PATH = ROOT / "playbooks" / "graph.learned.json"

_base: dict | None = None
_learned: dict | None = None


# ---------- 로드 ----------
def _read(path: Path, empty: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(empty)


def base() -> dict:
    global _base
    if _base is None:
        _base = _read(BASE_PATH, {"nodes": {}, "edges": []})
    return _base


def learned() -> dict:
    global _learned
    if _learned is None:
        _learned = _read(LEARNED_PATH, {"nodes": {}, "edges": [], "rejected": []})
        _learned.setdefault("nodes", {}); _learned.setdefault("edges", []); _learned.setdefault("rejected", [])
    return _learned


def _save_learned() -> None:
    l = learned()
    l["_note"] = ("사용 중 학습된 절차 그래프 확장(LLM 제안 → 구조 검증). 기본판 graph.base.json 과 합쳐 쓴다. "
                  "지우면 기본판만으로 동작한다(python -m sources.graph reset-learned).")
    l["updated_at"] = datetime.now().isoformat(timespec="seconds")
    LEARNED_PATH.write_text(json.dumps(l, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def reload() -> None:
    global _base, _learned
    _base = None
    _learned = None


def nodes() -> dict[str, dict]:
    return {**base().get("nodes", {}), **learned().get("nodes", {})}


def edges() -> list[dict]:
    return list(base().get("edges", [])) + list(learned().get("edges", []))


def _by_after() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for e in edges():
        out[e["after"]].append(e)
    return out


def _is_soft_only(e: dict) -> bool:
    """선행이 AI 제안(x:) 절차면 잠그는 선행(dependsOn)이 아니라 권고(suggestedAfter)로만 적용한다."""
    return str(e.get("before", "")).startswith("x:")


def item_for_key(key: str) -> dict | None:
    """자동 추가용 메뉴 항목(kind/event/name + law_query·article_title 또는 service_id). 학습 노드는 출처가 없어 None."""
    n = nodes().get(key)
    return n.get("item") if n else None


# ---------- 1) 플레이북에 간선 적용 ----------
def apply(procs: list[dict], make_proc=None, max_depth: int = 2) -> tuple[list[dict], list[dict]]:
    """procs 에 dependsOn 을 채우고, requires 선행이 없으면 make_proc(item, why) 로 만들어 추가한다.
    반환: (추천 순서로 정렬된 절차, 자동 추가된 절차)"""
    by_after = _by_after()
    procs = [dict(p) for p in procs]
    added: list[dict] = []

    def present(key: str) -> dict | None:
        return next((p for p in procs if p.get("key") == key), None)

    frontier = list(procs)
    for _ in range(max_depth):
        new_items: list[dict] = []
        for p in frontier:
            # any-of 묶음(group)은 후보 중 하나라도 있으면 그걸로 연결하고 자동 추가는 하지 않는다
            # group 이 없는 간선은 각각 독립(둘 다 필요할 수 있음), group 이 있으면 후보 중 하나면 된다
            groups: list[list[dict]] = []
            grouped: dict[str, list[dict]] = defaultdict(list)
            for e in by_after.get(p.get("key") or "", []):
                if e.get("group"):
                    grouped[e["group"]].append(e)
                else:
                    groups.append([e])
            groups += list(grouped.values())
            for ges in groups:
                hits = [(e, q) for e in ges if (q := present(e["before"])) and q["id"] != p["id"]]
                if hits:
                    for e, q in hits:
                        # AI 제안(x:) 절차에서 나가는 간선은 '권고'로만 둔다 — 미검증 절차가 검증된 절차를 잠그지 않도록
                        field = "suggestedAfter" if _is_soft_only(e) else "dependsOn"
                        ids = p.setdefault(field, [])
                        if q["id"] not in ids:
                            ids.append(q["id"])
                    continue
                if len(ges) > 1 or make_proc is None or not ges[0]["hard"]:
                    continue
                e = ges[0]
                dup = next((n for n in new_items if n.get("key") == e["before"]), None)
                if dup:
                    ids = p.setdefault("dependsOn", [])
                    if dup["id"] not in ids:
                        ids.append(dup["id"])
                    continue
                item = item_for_key(e["before"])
                if not item:
                    continue
                q = make_proc(item, e.get("guidance") or "")
                if q:
                    q["key"] = e["before"]
                    q["autoAdded"] = True
                    q["autoAddedFor"] = p["id"]
                    new_items.append(q)
                    p.setdefault("dependsOn", []).append(q["id"])
        if not new_items:
            break
        procs += new_items
        added += new_items
        frontier = new_items
    return rank(procs), added


# ---------- 2) 추천 순서 ----------
def _prio_key(p: dict) -> tuple:
    return (p.get("priority") != "must", not str(p.get("id", "")).startswith("emergency-"),
            p.get("deadlineDays") if isinstance(p.get("deadlineDays"), int) else 9999)


def rank(procs: list[dict]) -> list[dict]:
    """선행→후속(dependsOn + 권고 suggestedAfter)을 지키는 위상 정렬. 같은 층에서는 must → 응급 → 기한 짧은 것 → 여는 절차가 많은 것(unlocks) 순.
    각 절차에 order(1부터)와 unlocks(이 플레이북 안에서 열어 주는 후속 수)를 붙인다."""
    by_id = {p["id"]: p for p in procs}
    succ: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = {p["id"]: 0 for p in procs}
    for p in procs:
        seen_d: set[str] = set()
        for d in (p.get("dependsOn") or []) + (p.get("suggestedAfter") or []):
            if d in by_id and d != p["id"] and d not in seen_d:
                seen_d.add(d)
                succ[d].append(p["id"])
                indeg[p["id"]] += 1

    def descendants(i: str, seen: set) -> set:
        for s in succ.get(i, ()):
            if s not in seen:
                seen.add(s)
                descendants(s, seen)
        return seen

    for p in procs:
        p["unlocks"] = len(descendants(p["id"], set()))
    pos = {p["id"]: i for i, p in enumerate(procs)}
    ready = [p for p in procs if indeg[p["id"]] == 0]
    out: list[dict] = []
    while ready:
        ready.sort(key=lambda p: (*_prio_key(p), -p["unlocks"], pos[p["id"]]))
        p = ready.pop(0)
        out.append(p)
        for s in succ.get(p["id"], ()):
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(by_id[s])
    if len(out) < len(procs):  # 순환(있어서는 안 되지만) → 남은 것은 원래 순서로 뒤에
        done = {p["id"] for p in out}
        out += [p for p in procs if p["id"] not in done]
    for i, p in enumerate(out, 1):
        p["order"] = i
    return out


def names_of(procs: list[dict]) -> dict[str, list[str]]:
    """{절차 id: [선행 절차 이름]} — 검증 출력용."""
    by_id = {p["id"]: p.get("name", p["id"]) for p in procs}
    return {p["id"]: [by_id.get(d, d) for d in p.get("dependsOn") or []] for p in procs if p.get("dependsOn")}


# ---------- 3) 프롬프트용 가이던스(현재 위치 → 다음 할 일) ----------
def _edge_lookup(procs: list[dict]) -> dict[tuple[str, str], dict]:
    key_of = {p["id"]: p.get("key") for p in procs}
    want = {(key_of.get(d), key_of.get(p["id"])) for p in procs
            for d in (p.get("dependsOn") or []) + (p.get("suggestedAfter") or [])}
    out = {}
    for e in edges():
        k = (e["before"], e["after"])
        if k in want:
            out[k] = e
    return out


def guidance_block(playbook: dict, board) -> str:
    """보드(완료 상태)를 그래프에 대응시켜 '지금 할 수 있는 절차 / 선행 때문에 막힌 절차 / 간선 안내'를 만든다.
    보드가 비어 있으면 플레이북 전체를 추천 순서로 요약한다."""
    procs = [dict(p) for p in playbook.get("procedures", [])]
    if not procs:
        return ""
    procs = rank(procs)
    by_id = {p["id"]: p for p in procs}
    status = {b.get("id"): b.get("status") for b in (board or []) if isinstance(b, dict) and b.get("id")}
    done = {i for i, s in status.items() if s == "done"}
    el = _edge_lookup(procs)
    key_of = {p["id"]: p.get("key") for p in procs}
    lines = ["[절차 순서 안내 — 그래프 기준]",
             "추천 순서: " + " → ".join(f"{p['order']}.{p['name']}" for p in procs)]
    ready, blocked = [], []
    for p in procs:
        if p["id"] in done:
            continue
        pend = [d for d in p.get("dependsOn") or [] if d in by_id and d not in done]
        (blocked if pend else ready).append((p, pend))
    if status:
        lines.append("지금 할 수 있는 절차(선행 완료): " + (", ".join(p["name"] for p, _ in ready) or "(없음)"))
    for p, pend in blocked:
        why = []
        for d in pend:
            e = el.get((key_of.get(d), key_of.get(p["id"])))
            rel = "필수 선행" if (e and e.get("hard")) else "권고 선행"
            g = f" — {e['guidance']}" if e and e.get("guidance") else ""
            why.append(f"{by_id[d]['name']}({rel}{g})")
        lines.append(f"- {p['name']} 은(는) 먼저 {', '.join(why)} 을(를) 마쳐야 한다")
    for p in procs:  # AI 제안 절차 뒤에 오는 권고(잠그지 않음)
        soft = [d for d in p.get("suggestedAfter") or [] if d in by_id and d not in done]
        if soft and p["id"] not in done:
            lines.append(f"- {p['name']} 은(는) {', '.join(by_id[d]['name'] for d in soft)} 뒤에 하는 것을 권한다(필수 아님, AI 제안 기준)")
    pits = {e["pitfalls"] for e in el.values() if e.get("pitfalls")}
    if pits:
        lines.append("주의: " + " / ".join(sorted(pits)))
    return "\n".join(lines)


# ---------- 4) 학습(새 절차 → 간선 제안 → 구조 검증 → 학습판 커밋) ----------
def new_keys(procs: list[dict]) -> list[str]:
    known = nodes()
    return [p["key"] for p in procs if p.get("key") and p["key"] not in known]


def _would_cycle(before: str, after: str, extra: list[tuple[str, str]]) -> bool:
    succ: dict[str, set] = defaultdict(set)
    for e in edges():
        succ[e["before"]].add(e["after"])
    for b, a in extra:
        succ[b].add(a)
    succ[before].add(after)
    # after 에서 before 로 돌아오면 순환
    stack, seen = [after], set()
    while stack:
        u = stack.pop()
        if u == before:
            return True
        for v in succ.get(u, ()):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return False


def validate(proposals: list[dict], procs: list[dict]) -> tuple[list[dict], list[dict]]:
    """제안 간선 {before_id, after_id, hard, condition, guidance, pitfalls} → (채택, 거절[+reason]).
    규칙: 두 끝점이 플레이북에 있고 서로 다름 · 기존 간선(정/역방향) 아님 · 거절 이력 아님 · 순환 없음 · 두 끝점 모두 key 보유."""
    by_id = {p["id"]: p for p in procs}
    existing = {(e["before"], e["after"]) for e in edges()}
    rejected_pairs = {(r["before"], r["after"]) for r in learned().get("rejected", [])}
    ok, bad, extra = [], [], []
    for pr in proposals or []:
        if not isinstance(pr, dict):
            continue
        b, a = by_id.get(str(pr.get("before_id", ""))), by_id.get(str(pr.get("after_id", "")))
        reason = None
        if not b or not a:
            reason = "플레이북에 없는 절차 id"
        elif b["id"] == a["id"]:
            reason = "자기 자신"
        elif not b.get("key") or not a.get("key"):
            reason = "안정 키 없음"
        else:
            kb, ka = b["key"], a["key"]
            if (kb, ka) in existing or (kb, ka) in {(x, y) for x, y in extra}:
                reason = "이미 있는 간선"
            elif (ka, kb) in existing:
                reason = "기존 간선과 반대 방향"
            elif (kb, ka) in rejected_pairs:
                reason = "이전에 거절된 제안"
            elif _would_cycle(kb, ka, extra):
                reason = "순환 발생"
        if reason:
            bad.append({**pr, "reason": reason})
            continue
        extra.append((b["key"], a["key"]))
        # AI 제안(x:) 절차가 선행이면 hard 를 받지 않는다(미검증 절차가 검증된 절차를 잠그면 안 된다)
        hard = bool(pr.get("hard")) and not b["key"].startswith("x:")
        ok.append({"before": b["key"], "after": a["key"], "before_name": b["name"], "after_name": a["name"],
                   "relation": "requires" if hard else "suggests", "hard": hard,
                   "condition": (str(pr.get("condition")).strip() or None) if pr.get("condition") else None,
                   "guidance": (str(pr.get("guidance")).strip() or None) if pr.get("guidance") else None,
                   "pitfalls": (str(pr.get("pitfalls")).strip() or None) if pr.get("pitfalls") else None,
                   "group": None, "support": 0, "origin": "learned"})
    return ok, bad


def commit(accepted: list[dict], rejected: list[dict], procs: list[dict], meta: dict) -> None:
    """채택 간선·새 노드를 학습판에 쓰고, 거절은 rejection memory 에 남긴다."""
    l = learned()
    known = nodes()
    stamp = {"at": datetime.now().isoformat(timespec="seconds"), **meta}
    for p in procs:
        k = p.get("key")
        if k and k not in known:
            l["nodes"][k] = {"key": k, "name": p.get("name"), "kind": k.split(":")[0], "events": [meta.get("event")],
                             "item": None, "count": 1, "priority": p.get("priority"),
                             "deadlineDays": p.get("deadlineDays") if 0 < (p.get("deadlineDays") or 0) < 9999 else None,
                             "agency": p.get("agency") or None, "online": p.get("online"), "unlocks": 0, "origin": stamp}
    for e in accepted:
        l["edges"].append({**e, "origin": stamp})
    for r in rejected:
        if r.get("reason") in ("플레이북에 없는 절차 id", "자기 자신", "안정 키 없음"):
            continue  # 형식 오류는 기억할 가치가 없다
        by_id = {p["id"]: p for p in procs}
        b, a = by_id.get(str(r.get("before_id"))), by_id.get(str(r.get("after_id")))
        if b and a and b.get("key") and a.get("key"):
            l["rejected"].append({"before": b["key"], "after": a["key"], "reason": r["reason"], **stamp})
    _save_learned()


def learn(pb: dict, llm_json, query: str = "", trace: dict | None = None) -> dict:
    """플레이북에 그래프 밖 절차가 있으면 LLM 에게 간선을 제안받아 검증·커밋하고, 플레이북에 반영한다.
    llm_json(system, user, temperature) → dict. 반환: 요약(trace 용). LLM 실패는 플레이북 생성을 막지 않는다."""
    from prompts import build_graph_edge_prompt
    procs = pb.get("procedures", [])
    fresh = new_keys(procs)
    summary: dict = {"new_nodes": fresh, "proposed": 0, "accepted": [], "rejected": []}
    if not fresh or llm_json is None:
        if trace is not None:
            trace["graph"] = summary
        return summary
    known_edges = [(p["id"], d) for p in procs for d in (p.get("dependsOn") or []) + (p.get("suggestedAfter") or [])]
    by_id = {p["id"]: p for p in procs}
    rej = {(r["before"], r["after"]) for r in learned().get("rejected", [])}
    key_of = {p["id"]: p.get("key") for p in procs}
    banned = [f"{by_id[a]['name']} → {by_id[b]['name']}" for a in by_id for b in by_id
              if (key_of[a], key_of[b]) in rej]
    user = "\n".join([
        f"[질문]\n{query}",
        "[절차 목록] (id · 이름 · 우선순위 · 기한 · 조건)  ※ '신규' = 그래프에 아직 없는 절차",
        *[f"- {p['id']} · {p['name']} · {p.get('priority')} · {p.get('deadline') or '기한 없음'} · {p.get('condition') or ''}"
          + (" · 신규" if p.get("key") in fresh else "") for p in procs],
        "[이미 있는 선행 관계] (선행 → 후속)",
        *([f"- {by_id[d]['name']} → {by_id[a]['name']}" for a, d in known_edges if d in by_id] or ["- (없음)"]),
        "[제안 금지 — 이전에 거절됨]",
        *(["- " + x for x in banned] or ["- (없음)"]),
    ])
    try:
        res = llm_json(build_graph_edge_prompt(), user, 0.0)
    except Exception as e:  # noqa: BLE001
        summary["error"] = repr(e)
        if trace is not None:
            trace["graph"] = summary
        return summary
    proposals = [x for x in (res.get("edges") or []) if isinstance(x, dict)]
    # 신규 절차가 한쪽 끝에 있는 제안만 받는다(기존 절차끼리의 관계는 카탈로그 EDGES 가 정답)
    proposals = [x for x in proposals if key_of.get(str(x.get("before_id"))) in fresh or key_of.get(str(x.get("after_id"))) in fresh]
    summary["proposed"] = len(proposals)
    ok, bad = validate(proposals, procs)
    if ok or bad:
        commit(ok, bad, procs, {"kind": "llm", "query": query[:120], "event": pb.get("event")})
    else:
        commit([], [], procs, {"kind": "llm", "query": query[:120], "event": pb.get("event")})  # 새 노드만 기록
    summary["accepted"] = [f"{e['before_name']} → {e['after_name']}" for e in ok]
    summary["rejected"] = [f"{b.get('before_id')} → {b.get('after_id')}: {b['reason']}" for b in bad]
    if ok:
        pb["procedures"], _ = apply(procs)  # 새 간선 반영(자동 추가 없음)
    if trace is not None:
        trace["graph"] = summary
    return summary


# ---------- CLI ----------
def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "stats"
    if cmd == "stats":
        b, l = base(), learned()
        print(f"base: nodes {len(b.get('nodes', {}))} · edges {len(b.get('edges', []))} (built {b.get('built_at')})")
        print(f"learned: nodes {len(l['nodes'])} · edges {len(l['edges'])} · rejected {len(l['rejected'])}"
              + (f" (updated {l.get('updated_at')})" if l.get("updated_at") else ""))
        for e in l["edges"]:
            print(f"  + {e.get('before_name', e['before'])} → {e.get('after_name', e['after'])} [{e['relation']}] {e.get('guidance') or ''}")
        for r in l["rejected"]:
            print(f"  - {r['before']} → {r['after']}: {r['reason']}")
        return 0
    if cmd == "check":
        from .build_graph import check_structure
        probs = check_structure(nodes(), edges())
        print("문제 없음" if not probs else "\n".join(probs))
        return 0 if not probs else 1
    if cmd == "reset-learned":
        if LEARNED_PATH.exists():
            LEARNED_PATH.unlink()
            print(f"삭제: {LEARNED_PATH.name} — 기본판만으로 동작")
        else:
            print("학습판 없음")
        reload()
        return 0
    if cmd == "show" and len(argv) > 1:
        k = argv[1]
        n = nodes().get(k)
        print(json.dumps(n, ensure_ascii=False, indent=1) if n else f"노드 없음: {k}")
        for e in edges():
            if k in (e["before"], e["after"]):
                print(f"  {e['before']} → {e['after']} [{e['relation']}] {e.get('guidance') or ''}")
        return 0
    print("usage: python -m sources.graph [stats|check|reset-learned|show <key>]")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
