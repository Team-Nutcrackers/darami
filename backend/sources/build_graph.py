"""절차 그래프(Procedural Graph) 기본판 생성기 → playbooks/graph.base.json

논문 "Procedural Graphs: Self-Evolving Execution Structures for LLM Agents"(Lu et al., 2026)의 표현을 따른다.
  G = (V, R, E, Φ): 노드 = 절차, 간선 = (선행 절차, relation, 후속 절차), Φ = 간선 속성 {condition, guidance, pitfalls}
  relation 어휘(논문 LEADS_TO / PROVIDES_INPUT_FOR 에 대응):
    - requires : 선행 절차가 끝나야(증명·확인서가 있어야) 후속 절차를 할 수 있다 (hard)
    - suggests : 순서 권고. 둘 다 있을 때 '먼저' 표시만 (soft, any-of 포함)

재료
  1) sources/build_deps.py 의 EDGES → playbooks/dependencies.json (사람이 쓴 관계 + 자동 추가용 메뉴 항목)
  1') sources/kb_edges.py → playbooks/kb_edges.json (절차 지식베이스에서 공식 페이지로 확인한 선행·후속 — 근거 문장 포함)
  2) playbooks/examples/*/*.json 사전 생성 500건 → 노드 메타(이름·이벤트·빈도·우선순위·기한)와 간선 support(실제로 걸린 횟수)

실행: .venv/bin/python -m sources.build_graph
  (dependencies.json 이 바뀌면 build_deps 먼저 돌린다. 사용자 사용으로 늘어나는 간선은 graph.learned.json 에 따로 쌓인다 — 이 파일은 건드리지 않는다)
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPS_PATH = ROOT / "playbooks" / "dependencies.json"
KB_EDGES_PATH = ROOT / "playbooks" / "kb_edges.json"  # sources/kb_edges.py — 지식베이스에서 확인한 선행·후속
OUT_PATH = ROOT / "playbooks" / "graph.base.json"
EXAMPLES_DIR = ROOT / "playbooks" / "examples"


def _load_pairs() -> list[dict]:
    out = []
    for f in sorted(EXAMPLES_DIR.glob("*/*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        pb = d.get("playbook") or d
        if pb.get("procedures"):
            out.append({"file": str(f.relative_to(ROOT)), "event": pb.get("event"), "procedures": pb["procedures"]})
    return out


def _edge_attrs(e: dict) -> dict:
    """dependencies.json 간선 → 논문식 속성. any-of 는 '해당하는 선행 하나'가 조건."""
    any_of = isinstance(e["before"], list)
    cond = None
    if any_of:
        cond = "선행 후보 중 사용자 상황에 해당하는 절차가 끝났을 때"
    return {
        "relation": "requires" if e.get("hard") else "suggests",
        "hard": bool(e.get("hard")),
        "condition": cond,
        "guidance": e.get("why") or None,
        "pitfalls": ("선행 절차가 끝나기 전에는 신청해도 반려·보류될 수 있다" if e.get("hard") else None),
    }


def build() -> dict:
    deps = json.loads(DEPS_PATH.read_text(encoding="utf-8"))
    pairs = _load_pairs()

    # ---- 노드: 메뉴 항목(자동 추가 출처) + 500건에서 관측된 메타 ----
    nodes: dict[str, dict] = {}
    for key, item in deps.get("items", {}).items():
        nodes[key] = {"key": key, "name": item["name"], "kind": item["kind"], "events": [item["event"]],
                      "item": item, "count": 0, "priority": None, "deadlineDays": None, "agency": None, "online": None}
    seen_ev: dict[str, set] = defaultdict(set)
    prio: dict[str, Counter] = defaultdict(Counter)
    dl: dict[str, list] = defaultdict(list)
    agency: dict[str, Counter] = defaultdict(Counter)
    online: dict[str, Counter] = defaultdict(Counter)
    support: Counter = Counter()
    observed_edges: dict[tuple[str, str], dict] = {}
    for pr in pairs:
        by_id = {p["id"]: p for p in pr["procedures"]}
        for p in pr["procedures"]:
            k = p.get("key")
            if not k:
                continue
            n = nodes.setdefault(k, {"key": k, "name": p.get("name"), "kind": k.split(":")[0], "events": [],
                                     "item": None, "count": 0, "priority": None, "deadlineDays": None,
                                     "agency": None, "online": None})
            n["count"] += 1
            if pr["event"]:
                seen_ev[k].add(pr["event"])
            prio[k][p.get("priority") or "nice"] += 1
            if isinstance(p.get("deadlineDays"), int) and 0 < p["deadlineDays"] < 9999:
                dl[k].append(p["deadlineDays"])
            if p.get("agency"):
                agency[k][p["agency"]] += 1
            online[k][bool(p.get("online"))] += 1
            for d in p.get("dependsOn") or []:
                q = by_id.get(d)
                if q and q.get("key"):
                    support[(q["key"], k)] += 1
                    observed_edges.setdefault((q["key"], k), {"before": q["key"], "after": k})
    for k, n in nodes.items():
        evs = set(n["events"]) | seen_ev.get(k, set())
        n["events"] = sorted(e for e in evs if e)
        if prio[k]:
            n["priority"] = prio[k].most_common(1)[0][0]
        if dl[k]:
            n["deadlineDays"] = min(dl[k])
        if agency[k]:
            n["agency"] = agency[k].most_common(1)[0][0]
        if online[k]:
            n["online"] = online[k].most_common(1)[0][0]

    # ---- 간선: 카탈로그(any-of 는 후보별로 펼친다) + support ----
    edges: list[dict] = []
    idx: dict[tuple[str, str], dict] = {}
    for e in deps.get("edges", []):
        befores = e["before"] if isinstance(e["before"], list) else [e["before"]]
        attrs = _edge_attrs(e)
        for b in befores:
            if b == e["after"]:
                continue
            k = (b, e["after"])
            if k in idx:
                continue
            ed = {"before": b, "after": e["after"], **attrs,
                  "group": e["after"] if len(befores) > 1 else None,  # any-of 묶음(같은 후속의 선행 후보들)
                  "support": support.get(k, 0), "origin": "catalog"}
            idx[k] = ed
            edges.append(ed)
    # ---- 지식베이스 간선: 카탈로그 간선이면 근거·속성 보강, 새 간선이면 순환이 생기지 않을 때만 추가 ----
    kb_dropped: list[str] = []
    if KB_EDGES_PATH.exists():
        def reaches(src: str, dst: str) -> bool:
            stack, seen = [src], set()
            while stack:
                u = stack.pop()
                if u == dst:
                    return True
                for ed in edges:
                    if ed["before"] == u and ed["after"] not in seen:
                        seen.add(ed["after"]); stack.append(ed["after"])
            return False
        for e in json.loads(KB_EDGES_PATH.read_text(encoding="utf-8")).get("edges", []):
            k = (e["before"], e["after"])
            if k in idx:
                ed = idx[k]
                for f in ("condition", "guidance", "pitfalls"):
                    if not ed.get(f) and e.get(f):
                        ed[f] = e[f]
                if e.get("hard") and not ed["hard"] and not ed.get("group"):  # any-of 묶음은 soft 유지
                    ed["hard"], ed["relation"] = True, "requires"
                ed["evidence"], ed["verified"] = e.get("evidence"), True
                continue
            if (e["after"], e["before"]) in idx or reaches(e["after"], e["before"]):
                kb_dropped.append(f"{e['before']} → {e['after']}")  # 방향 충돌·순환 — kb_edges.json conflicts 와 함께 사람 검토
                continue
            ed = {"before": e["before"], "after": e["after"], "relation": "requires" if e.get("hard") else "suggests",
                  "hard": bool(e.get("hard")), "condition": e.get("condition"), "guidance": e.get("guidance"),
                  "pitfalls": e.get("pitfalls") or ("선행 절차가 끝나기 전에는 신청해도 반려·보류될 수 있다" if e.get("hard") else None),
                  "group": None, "support": support.get(k, 0), "origin": "kb", "evidence": e.get("evidence"), "verified": True}
            idx[k] = ed
            edges.append(ed)
    for k, ob in observed_edges.items():  # 카탈로그 밖에서 관측된 간선(정적 플레이북 등)은 suggests 로 보존
        if k not in idx and (k[1], k[0]) not in idx:  # 카탈로그·지식베이스가 반대 방향으로 고친 간선은 옛 관측을 버린다
            ed = {**ob, "relation": "suggests", "hard": False, "condition": None, "guidance": None, "pitfalls": None,
                  "group": None, "support": support[k], "origin": "observed"}
            idx[k] = ed
            edges.append(ed)
    for ed in edges:
        for k in (ed["before"], ed["after"]):
            nodes.setdefault(k, {"key": k, "name": k.split(":", 1)[-1], "kind": k.split(":")[0], "events": [],
                                 "item": None, "count": 0, "priority": None, "deadlineDays": None, "agency": None, "online": None})

    # ---- 구조 검증: 모든 간선 끝점 존재, 순환 없음(논문 PrepareCandidate 의 구조 검사) ----
    problems = check_structure(nodes, edges)

    # unlocks = 후손 수(이 절차가 끝나면 열리는 절차의 수) — 우선순위 산정에 쓴다
    succ: dict[str, set] = defaultdict(set)
    for ed in edges:
        succ[ed["before"]].add(ed["after"])

    def desc(k: str, seen: set) -> set:
        for s in succ.get(k, ()):
            if s not in seen:
                seen.add(s)
                desc(s, seen)
        return seen

    for k, n in nodes.items():
        n["unlocks"] = len(desc(k, set()))

    return {
        "_note": "절차 그래프 기본판. sources/build_graph.py 가 생성(손으로 고치지 말 것). "
                 "사용 중 학습된 간선은 graph.learned.json 에 따로 쌓인다(지우면 기본판으로 복귀).",
        "built_at": date.today().isoformat(),
        "relations": {"requires": "선행이 끝나야 가능(hard, 없으면 자동 추가)", "suggests": "순서 권고(soft)"},
        "stats": {"nodes": len(nodes), "edges": len(edges), "hard": sum(e["hard"] for e in edges),
                  "supported_edges": sum(1 for e in edges if e["support"] > 0),
                  "kb_edges": sum(1 for e in edges if e.get("verified")), "kb_dropped": kb_dropped,
                  "playbooks_scanned": len(pairs), "problems": problems},
        "nodes": nodes,
        "edges": edges,
    }


def check_structure(nodes: dict, edges: list[dict]) -> list[str]:
    """끝점 존재 + 비순환(DFS) 검사. 문제 목록을 돌려준다(빈 리스트 = 정상)."""
    problems: list[str] = []
    succ: dict[str, list] = defaultdict(list)
    for e in edges:
        if e["before"] not in nodes or e["after"] not in nodes:
            problems.append(f"끝점 없음: {e['before']} → {e['after']}")
        succ[e["before"]].append(e["after"])
    color: dict[str, int] = {}

    def dfs(u: str, path: list) -> None:
        color[u] = 1
        path.append(u)
        for v in succ.get(u, ()):
            if color.get(v) == 1:
                problems.append("순환: " + " → ".join(path[path.index(v):] + [v]))
            elif color.get(v) is None:
                dfs(v, path)
        path.pop()
        color[u] = 2

    for k in list(nodes):
        if color.get(k) is None:
            dfs(k, [])
    return problems


def main() -> None:
    g = build()
    OUT_PATH.write_text(json.dumps(g, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    s = g["stats"]
    print(f"nodes {s['nodes']} · edges {s['edges']} (hard {s['hard']}, 500건에서 관측 {s['supported_edges']}, 지식베이스 확인 {s['kb_edges']}, 탈락 {len(s['kb_dropped'])}) "
          f"· scanned {s['playbooks_scanned']} → {OUT_PATH.name}")
    for p in s["problems"]:
        print("  !", p)


if __name__ == "__main__":
    main()
