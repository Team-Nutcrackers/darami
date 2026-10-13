"""지식베이스(playbooks/procedures.json)의 선행·후속 관계 → playbooks/kb_edges.json

항목마다 공식 페이지에서 확인한 prerequisites / successors(이름·hard·condition·guidance·pitfalls·evidence)를
그래프 노드 키로 해석한다. build_graph 가 이 파일을 카탈로그(build_deps.EDGES) 다음 재료로 읽어 기본판에 합친다.

실행: .venv/bin/python -m sources.kb_edges
- 이름 해석 순서: 그래프 노드 이름(graph.base.json) → 지식베이스 항목 이름 → events.json 메뉴 이름 → 유사도 0.85 이상
- 못 푼 이름은 unresolved 로 남기고(사람이 메뉴에 추가하거나 이름을 맞춘다) 간선은 만들지 않는다.
- 기존 간선과 방향이 반대이면 conflicts 로 남기고 간선은 만들지 않는다(사람 검토).
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from . import kb

ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = ROOT / "playbooks" / "graph.base.json"
DEPS_PATH = ROOT / "playbooks" / "dependencies.json"
EVENTS_PATH = ROOT / "playbooks" / "events.json"
OUT_PATH = ROOT / "playbooks" / "kb_edges.json"


# 지식베이스 항목끼리 반대 방향을 제안한 쌍 → 남길 방향 (before, after). 근거는 kb_edges.json conflicts 와 procedures.json evidence 참고.
KB_REVERSE_KEEP: dict[frozenset, tuple[str, str]] = {
    # 치매공공후견사업은 지자체장이 후견심판을 '청구'해 주는 지원 → 이용지원이 심판보다 앞
    frozenset(("law:치매 성년후견 이용지원", "law:성년후견개시 심판")): ("law:치매 성년후견 이용지원", "law:성년후견개시 심판"),
    # 선청구서는 수급권 발생 전에 내 두는 것 → 선청구가 분할연금 수급보다 앞
    frozenset(("x:분할연금 선청구", "law:분할연금")): ("x:분할연금 선청구", "law:분할연금"),
    # 연기는 노령연금을 청구하면서(또는 수급 중) 선택 → 청구가 앞
    frozenset(("law:노령연금 청구", "x:연기연금(노령연금 지급 연기)")): ("law:노령연금 청구", "x:연기연금(노령연금 지급 연기)"),
    # 재개명도 법원 허가가 먼저, 허가 후 1개월 내 개명신고
    frozenset(("x:가정법원 개명허가 신청(재개명)", "law:개명신고")): ("x:가정법원 개명허가 신청(재개명)", "law:개명신고"),
}


def _norm(s: str) -> str:
    return re.sub(r"[\s·ㆍ()\[\]/\-_,.'\"]", "", s or "")


def _name_index() -> dict[str, str]:
    """정규화 이름 → 키."""
    idx: dict[str, str] = {}
    if GRAPH_PATH.exists():
        g = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        for k, n in g["nodes"].items():
            idx.setdefault(_norm(n["name"]), k)
    for e in kb.load(force=True)["entries"]:
        idx.setdefault(_norm(e["name"]), e["key"])
        for a in e.get("aliases") or []:
            idx.setdefault(_norm(a), e["key"])
    data = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
    for ev in (data["events"] if isinstance(data, dict) else data):
        a = ev["apis"]
        for it in a["law"]:
            idx.setdefault(_norm(it["name"]), f"common:{it['common']}" if it.get("common") else f"law:{it['name']}")
        for s in a["gov24"]["services"] + a.get("national_welfare", []):
            idx.setdefault(_norm(s["name"]), f"svc:{s['service_id']}")
    return idx


def resolve(name: str, idx: dict[str, str]) -> tuple[str | None, str]:
    n = _norm(name)
    if not n:
        return None, "empty"
    if n in idx:
        return idx[n], "exact"
    best = difflib.get_close_matches(n, list(idx.keys()), n=1, cutoff=0.85)
    if best:
        return idx[best[0]], f"fuzzy:{best[0]}"
    return None, "unresolved"


def build() -> dict:
    idx = _name_index()
    # 비교 기준은 사람이 쓴 카탈로그(dependencies.json). graph.base.json 은 이미 지식베이스 간선이 섞여 있어 기준이 못 된다.
    existing: dict[tuple[str, str], dict] = {}
    for e in json.loads(DEPS_PATH.read_text(encoding="utf-8")).get("edges", []):
        for b in (e["before"] if isinstance(e["before"], list) else [e["before"]]):
            existing[(b, e["after"])] = {"guidance": e.get("why"), "hard": e.get("hard")}
    edges: dict[tuple[str, str], dict] = {}
    unresolved: dict[str, list[str]] = {}
    conflicts: list[dict] = []
    corrections: list[dict] = []

    def add(before: str, after: str, rel: dict, src_key: str, direction: str) -> None:
        if before == after:
            return
        k = (before, after)
        if (after, before) in existing:
            conflicts.append({"kb_before": before, "kb_after": after, "existing": existing[(after, before)].get("guidance"),
                              "from": src_key, "evidence": rel.get("evidence")})
            return
        hard = bool(rel.get("hard")) and not before.startswith("x:")  # 미검증(x:) 선행은 잠그지 않는다(graph.validate 와 같은 규칙)
        cur = edges.get(k)
        ed = {"before": before, "after": after, "hard": hard,
              "condition": (rel.get("condition") or "").strip() or None,
              "guidance": (rel.get("guidance") or "").strip() or None,
              "pitfalls": (rel.get("pitfalls") or "").strip() or None,
              "evidence": (rel.get("evidence") or "").strip() or None,
              "from": [f"{src_key}:{direction}"]}
        if cur:  # 양쪽 항목이 같은 간선을 말하면 hard 는 OR, 빈 속성은 보탠다
            cur["hard"] = cur["hard"] or hard
            for f in ("condition", "guidance", "pitfalls", "evidence"):
                cur[f] = cur[f] or ed[f]
            cur["from"].append(ed["from"][0])
        else:
            edges[k] = ed

    for e in kb.load()["entries"]:
        me = e["key"]
        for rel in e.get("prerequisites") or []:
            key, how = resolve(rel.get("name", ""), idx)
            if not key:
                unresolved.setdefault(rel.get("name", ""), []).append(f"{me} ← prerequisite")
                continue
            add(key, me, rel, me, "pre")
        for rel in e.get("successors") or []:
            key, how = resolve(rel.get("name", ""), idx)
            if not key:
                unresolved.setdefault(rel.get("name", ""), []).append(f"{me} → successor")
                continue
            add(me, key, rel, me, "succ")
        for c in e.get("edge_corrections") or []:
            corrections.append({"from": me, "note": c})

    # 지식베이스 항목끼리 반대 방향을 말한 쌍: 양쪽 근거를 읽고 정한 쪽만 남긴다(나머지는 conflicts 로 보고)
    for (b, a) in list(edges):
        if (a, b) in edges and (b, a) in edges:
            keep = KB_REVERSE_KEEP.get(frozenset((a, b)))
            drop = (a, b) if keep == (b, a) else (b, a) if keep == (a, b) else None
            if drop is None:  # 정하지 않은 쌍은 둘 다 빼고 보고
                for k in ((a, b), (b, a)):
                    if k in edges:
                        conflicts.append({"kb_before": k[0], "kb_after": k[1], "existing": "지식베이스 내부 반대 방향(미결)", "from": edges[k]["from"], "evidence": edges[k]["evidence"]})
                        del edges[k]
            elif drop in edges:
                del edges[drop]
    out_edges = sorted(edges.values(), key=lambda d: (d["after"], d["before"]))
    return {"_note": "sources/kb_edges.py 가 지식베이스에서 생성(손으로 고치지 말 것). build_graph 가 읽는다.",
            "stats": {"edges": len(out_edges), "hard": sum(d["hard"] for d in out_edges),
                      "new": sum((d["before"], d["after"]) not in existing for d in out_edges),
                      "unresolved_names": len(unresolved), "conflicts": len(conflicts), "corrections": len(corrections)},
            "edges": out_edges, "unresolved": unresolved, "conflicts": conflicts, "corrections": corrections}


def main() -> None:
    out = build()
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    s = out["stats"]
    print(f"kb edges {s['edges']} (hard {s['hard']}, new {s['new']}) · unresolved {s['unresolved_names']} · conflicts {s['conflicts']} · corrections {s['corrections']} → {OUT_PATH.name}")
    for n, srcs in list(out["unresolved"].items())[:40]:
        print(f"  ? {n}  ({srcs[0]}{' +%d' % (len(srcs) - 1) if len(srcs) > 1 else ''})")
    for c in out["conflicts"]:
        print(f"  ! 방향 충돌 {c['kb_before']} → {c['kb_after']} (기존: 반대 방향 — {c['existing']})")


if __name__ == "__main__":
    main()
