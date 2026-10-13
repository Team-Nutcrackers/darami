"""이벤트별 질의 + 플레이북 쌍 자동 생성기 (검증용 데이터셋).

playbooks/events.json 의 각 이벤트마다:
1) LLM(기본 Solar Pro)이 그 이벤트를 겪은 사용자가 처음 보낼 법한 질의를 만든다.
2) 그 질의로 curate_dynamic(홈의 직접 입력 "?"와 같은 경로)을 돌려 플레이북을 만든다.
3) {query, playbook} 쌍을 generated/pairs/<event_id>/<event_id>-NN.json 으로 저장한다 → 이후 Claude로 검증.

이미 저장된 쌍은 건너뛰므로(이벤트별 개수 기준) 중간에 끊겨도 같은 명령으로 이어서 돌릴 수 있다.

실행 (backend/ 에서):
    .venv/bin/python -m sources.gen_pairs                       # 전 이벤트 × 10개
    .venv/bin/python -m sources.gen_pairs --limit 1             # 새 쌍 1개만 (테스트)
    .venv/bin/python -m sources.gen_pairs --events marriage,lease --per-event 3

주의: curate_dynamic 은 법령 grounding 성공 시 learned_common.json(자기학습 카탈로그)에 매핑을 누적한다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

from main import get_provider, make_json_caller
from prompts import today_str
from .curate import curate_dynamic

BACKEND_DIR = Path(__file__).resolve().parent.parent
EVENTS_PATH = BACKEND_DIR / "playbooks" / "events.json"
OUT_DIR = BACKEND_DIR / "generated" / "pairs"

QUERY_GEN_PROMPT = """너는 한국 공공서비스 AI 에이전트 '다람이'의 평가용 사용자 질의를 만드는 작성자다.
주어진 [생활 이벤트]를 겪고 있는 실제 한국인이 다람이에게 처음 보낼 법한 메시지를 하나 만든다.

[규칙]
1) 반드시 주어진 [생활 이벤트]에 해당하는 상황이어야 한다.
2) [이미 만든 질의]와 사용자 유형·상황·걱정거리가 겹치지 않게 새로운 사람을 만든다.
   나이·성별·지역(전국 시·군·구 골고루)·가족 구성·직업·소득 수준·시점(예정/진행 중/이미 지남)을 다양하게 바꾼다.
3) 질의는 사용자 1인칭 구어체 존댓말 2~4문장. 다음을 자연스럽게 담는다:
   - 무슨 일이 언제 있었는지/있을 예정인지 (상대 표현 가능: "지난주", "다음 달")
   - 거주지(시·구 수준), 가족 구성·나이·직업 등 절차 판단에 쓰일 만한 사실 1~3개
   - 구체적인 걱정이나 궁금한 점 1~2개
4) 행정 용어를 정확히 아는 사람처럼 쓰지 말고, 일반인이 쓰는 말투로. 제도 이름을 나열하지 말 것.
5) 반드시 아래 JSON 하나로만 응답.

[출력 스키마]
{
  "persona_summary": "질의 속 사용자 한 줄 요약 (예: 52세 · 대구 수성구 · 부친 사망, 형제 2명)",
  "query": "사용자가 보낼 첫 메시지"
}"""


def load_events() -> list[dict]:
    return json.loads(EVENTS_PATH.read_text(encoding="utf-8"))["events"]


def _existing_pairs(event_dir: Path) -> list[dict]:
    pairs = []
    for f in sorted(event_dir.glob("*.json")):
        try:
            pairs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return pairs


def generate_query(solar_json, event: dict, previous: list[str]) -> dict:
    """이벤트에 해당하는 사용자 질의 1건 생성. previous: 같은 이벤트에서 이미 만든 질의들(중복 회피)."""
    prev_block = "\n".join(f"- {q}" for q in previous) or "(없음)"
    user = (
        f"[오늘 날짜] {today_str()}\n"
        f"[생활 이벤트] {event['title']} — {event['description']}\n"
        f"(사용자들이 흔히 쓰는 표현: {', '.join(event.get('aliases', []))})\n"
        f"[이미 만든 질의]\n{prev_block}"
    )
    q = solar_json(QUERY_GEN_PROMPT, user, 0.9)
    if not (q.get("query") or "").strip():
        raise RuntimeError(f"질의 생성 실패: {q}")
    return {
        "event_id": event["id"],
        "event_title": event["title"],
        "persona_summary": q.get("persona_summary", ""),
        "query": q["query"].strip(),
    }


def _stats(playbook: dict) -> dict:
    procs = playbook.get("procedures", [])
    return {
        "procedures": len(procs),
        "by_status": dict(Counter(p.get("verification_status", "unverified") for p in procs)),
    }


def build_pair(query: dict, provider) -> dict:
    """질의 → curate_dynamic 플레이북 → 쌍(dict)."""
    t0 = time.time()
    playbook = curate_dynamic(query["query"], None, make_json_caller(provider))
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "generator": {"provider": provider.key, "model": provider.model, "curate": "curate_dynamic"},
        "query": query,
        "playbook": playbook,
        "stats": {**_stats(playbook), "curate_seconds": round(time.time() - t0, 1)},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="이벤트별 질의 + 플레이북 쌍 생성")
    ap.add_argument("--per-event", type=int, default=10, help="이벤트당 목표 쌍 개수(이미 있는 것 포함)")
    ap.add_argument("--events", help="쉼표로 구분한 이벤트 id만 (기본: 전체)")
    ap.add_argument("--limit", type=int, help="이번 실행에서 새로 만들 쌍의 최대 개수")
    ap.add_argument("--provider", default="solar", help="solar")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    provider = get_provider(args.provider)
    if provider.client is None:
        print(f"{provider.label} API 키가 설정되지 않았습니다. backend/.env를 확인하세요.", file=sys.stderr)
        return 1
    solar_json = make_json_caller(provider)

    events = load_events()
    if args.events:
        wanted = {e.strip() for e in args.events.split(",")}
        events = [e for e in events if e["id"] in wanted]

    made = failed = 0
    for event in events:
        event_dir = args.out / event["id"]
        event_dir.mkdir(parents=True, exist_ok=True)
        previous = [p["query"]["query"] for p in _existing_pairs(event_dir)]
        while len(previous) < args.per_event:
            if args.limit is not None and made >= args.limit:
                print(f"--limit {args.limit} 도달. 생성 {made}, 실패 {failed}")
                return 0
            idx = len(previous) + 1
            tag = f"{event['id']}-{idx:02d}"
            try:
                query = generate_query(solar_json, event, previous)
                pair = build_pair(query, provider)
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"[{tag}] 실패\n{traceback.format_exc()}", file=sys.stderr)
                if failed >= 5 and made == 0:
                    print("연속 실패로 중단합니다.", file=sys.stderr)
                    return 1
                continue
            path = event_dir / f"{tag}.json"
            path.write_text(json.dumps(pair, ensure_ascii=False, indent=2), encoding="utf-8")
            previous.append(query["query"])
            made += 1
            print(f"[{tag}] {query['query']}\n    → '{pair['playbook'].get('title')}' {pair['stats']}")
    print(f"완료. 생성 {made}, 실패 {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
