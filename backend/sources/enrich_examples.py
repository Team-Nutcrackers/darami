"""사전 생성 플레이북(playbooks/examples/*/*.json) 500건에 절차 지식베이스를 일괄 반영한다.

실행: .venv/bin/python -m sources.enrich_examples [--dry]
- 절차마다 kb.enrich 를 적용하고 stats.by_status 를 다시 센다.
- 서비스 경로(curate_v2)도 같은 kb.apply 를 쓰므로, 예시와 실시간 결과의 서류·링크·검증 상태가 같아진다.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

from . import graph, kb

EXAMPLES = Path(__file__).resolve().parent.parent / "playbooks" / "examples"


def main(dry: bool = False) -> None:
    kb.load(force=True)
    files = sorted(EXAMPLES.glob("*/*.json"))
    hit = collections.Counter()
    miss: collections.Counter = collections.Counter()
    status = collections.Counter()
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        procs = d["playbook"]["procedures"]
        for p in procs:
            before = json.dumps(p, ensure_ascii=False, sort_keys=True)
            kb.enrich(p)
            k = (p.get("key") or "").split(":")[0]
            if json.dumps(p, ensure_ascii=False, sort_keys=True) != before:
                hit[k] += 1
            elif k not in ("svc", "emergency-er", "emergency-trauma"):
                miss[p.get("key") or p["name"]] += 1
            status[p.get("verification_status")] += 1
        # 간선·추천 순서는 고친 그래프 기본판으로 다시 계산한다(옛 dependsOn 은 버림. 자동 추가는 하지 않음)
        for p in procs:
            p.pop("dependsOn", None)
            p.pop("suggestedAfter", None)
        procs, _ = graph.apply(procs)
        d["playbook"]["procedures"] = procs
        d.setdefault("stats", {})["by_status"] = dict(collections.Counter(p.get("verification_status") for p in procs))
        if not dry:
            f.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"files={len(files)} enriched={dict(hit)} status={dict(status)}")
    if miss:
        print(f"no KB entry ({len(miss)} keys):", ", ".join(k for k, _ in miss.most_common(30)))


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
