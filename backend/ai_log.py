"""AI API 호출 로그 — 「국내 AI API 호출 로그 제출 가이드」 형식.

한 건(record) = 요청(요청 시각 · 엔드포인트 · 요청 모델 · 요청 내용) + 응답(응답 시각 · 응답 본문).
응답 본문은 API 가 돌려준 그대로(응답 ID · 모델명 · 답변 · 토큰 사용량 포함)다.
기능 1회 실행(AiRun) 동안의 호출을 누적해 generated/ai_logs/<기능ID>_<시작시각>_<실행ID>.jsonl 에 한 줄씩 쌓는다.
  {"type": "run", ...}     기능 · 시작 시각
  {"type": "record", ...}  호출 1건 (요청 직전에 요청 정보를 잡고, 응답 수신 직후 응답 정보를 붙여 기록. 실패하면 error)
  {"type": "end", ...}     종료 시각 · 상태
시각은 모두 KST. API 키 · Authorization 헤더는 기록하지 않는다(본문만 남김).
본문 속 개인정보 패턴(주민등록번호 · 휴대전화 · 이메일 · 카드번호)과 비밀값은 그 부분만 마스킹하고 구조는 유지한다.

제출 파일 만들기 — 기능별 최근 정상 실행 1회를 <기능명>.json 으로, 전체를 zip 하나로:
  python3 ai_log.py list                                   # 실행 목록
  python3 ai_log.py export --run <실행ID> [--run ...]      # 특정 실행을 골라서 (권장: 데모 시나리오로 실행한 것)
  python3 ai_log.py export --out ai_api_logs.zip           # 서버 기능(chat · persona · generate) 각각 가장 최근 1회
가장 최근 실행은 실사용자 대화일 수 있다 — 자동 마스킹은 정해진 패턴만 지우므로(이름·주소는 못 지움) 제출 전 내용을 확인한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9), "KST")
LOG_DIR = Path(__file__).resolve().parent / "generated" / "ai_logs"
ENABLED = os.environ.get("DARAMI_AI_LOG", "1") != "0"
LOG_SINCE = datetime(2026, 10, 14, tzinfo=KST)  # 가이드: 10월 14일 이후 호출만 인정

# 기능 ID → (기능명 = 제출 파일명, 이 기능을 부르는 곳). 최종 결과 보고서의 기능명과 맞춘다.
FEATURES = {
    "chat": ("대화 응답 생성", "POST /api/chat"),
    "persona": ("세션 페르소나 추출", "POST /api/persona"),
    "generate": ("자유 입력 이벤트 절차 구성", "POST /api/generate"),
    "gen_pairs": ("사전 생성 사례 생성", "python -m sources.gen_pairs"),
    "user_test": ("사용자 테스트", "python -m sources.user_test"),
}
SERVER_FEATURES = ("chat", "persona", "generate")

FIELD_GUIDE = {
    "request_time": "요청 시각 (KST)",
    "endpoint": "엔드포인트 (도메인 포함)",
    "model": "요청 모델",
    "request_body": "요청 내용 — AI 모델에 보낸 전체 메시지 데이터",
    "response_time": "응답 시각 (KST)",
    "response_body": "응답 본문 — 응답 ID(id) · 모델명(model) · 답변(choices) · 토큰 사용량(usage)",
    "latency_ms": "(추가) 응답까지 걸린 시간",
    "error": "(추가) 호출 실패 시 오류 내용",
}


# ---------- 마스킹 ----------
_PATTERNS = [
    (re.compile(r"(?<!\d)\d{6}\s?-\s?[1-4]\d{6}(?!\d)"), "[주민등록번호]"),
    (re.compile(r"(?<!\d)01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)"), "[휴대전화]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[이메일]"),
    (re.compile(r"(?<!\d)\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}(?!\d)"), "[카드번호]"),
    (re.compile(r"\b(?:up|flp)_[A-Za-z0-9]{16,}\b"), "[API키]"),
]
_SECRET_ENVS = ("UPSTAGE_API_KEY", "KEXAONE_API_KEY", "LAW_OC", "DATA_GO_KR_KEY")


def _mask_str(s: str) -> str:
    for env in _SECRET_ENVS:
        v = os.environ.get(env, "")
        if len(v) >= 6 and v in s:
            s = s.replace(v, "[비밀값]")
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


def mask(obj):
    """dict/list 구조는 그대로 두고 문자열 속 민감정보만 치환."""
    if isinstance(obj, str):
        return _mask_str(obj)
    if isinstance(obj, dict):
        return {k: mask(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask(v) for v in obj]
    return obj


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="milliseconds")


# ---------- 기록 ----------
class AiRun:
    """기능 1회 실행 동안의 AI API 호출을 한 파일에 누적한다. 스레드 안전(병렬 호출도 같은 실행으로 묶임)."""

    _defaults: dict[str, "AiRun"] = {}
    _defaults_lock = threading.Lock()

    def __init__(self, feature: str):
        self.feature = feature
        self.name, self.trigger = FEATURES.get(feature, (feature, None))
        self.run_id = uuid.uuid4().hex[:12]
        self.started_at = _now()
        stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
        self.path = LOG_DIR / f"{feature}_{stamp}_{self.run_id}.jsonl"
        self._lock = threading.Lock()
        self._calls = 0
        self._failed = 0
        self._opened = False
        self._error: BaseException | None = None

    @classmethod
    def default(cls) -> "AiRun":
        """실행을 넘기지 않은 호출(오프라인 배치 스크립트 등): 스크립트 이름을 기능 ID 로, 프로세스당 한 실행에 누적."""
        stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
        feature = stem if stem and stem not in ("__main__", "uvicorn") else "other"
        with cls._defaults_lock:
            if feature not in cls._defaults:
                cls._defaults[feature] = cls(feature)
            return cls._defaults[feature]

    def call(self, client, body: dict):
        """OpenAI 호환 chat.completions 호출 1건: 요청 직전 요청 정보 → 호출 → 응답 수신 직후 응답 정보 → 한 건으로 저장."""
        if not ENABLED:
            return client.chat.completions.create(**body)
        rec = {
            "type": "record",
            "run_id": self.run_id,
            "feature": self.name,
            "seq": None,
            "request_time": None,
            "endpoint": f"{str(client.base_url).rstrip('/')}/chat/completions",
            "model": body.get("model"),
            "request_body": mask(body),
        }
        rec["request_time"] = _now()
        t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(**body)
        except Exception as e:
            rec["response_time"] = _now()
            rec["latency_ms"] = round((time.monotonic() - t0) * 1000)
            rec["error"] = mask({"type": type(e).__name__, "message": str(e),
                                 "status_code": getattr(e, "status_code", None), "body": getattr(e, "body", None)})
            self._append(rec, failed=True)
            raise
        rec["response_time"] = _now()
        rec["latency_ms"] = round((time.monotonic() - t0) * 1000)
        rec["response_body"] = mask(resp.model_dump(mode="json"))
        self._append(rec)
        return resp

    def fail(self, error: BaseException) -> None:
        """기능이 실패로 끝났음을 표시(예외를 기능 쪽에서 잡아 처리하는 경우)."""
        self._error = error

    def finish(self, error: BaseException | None = None) -> None:
        if error is not None:
            self._error = error
        if not (ENABLED and self._opened):
            return
        status = "error" if (self._error or self._failed) else "ok"
        self._write({"type": "end", "run_id": self.run_id, "finished_at": _now(), "calls": self._calls,
                     "failed_calls": self._failed, "status": status,
                     **({"error": mask(f"{type(self._error).__name__}: {self._error}")} if self._error else {})})

    def __enter__(self) -> "AiRun":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finish(exc)

    def _append(self, rec: dict, failed: bool = False) -> None:
        with self._lock:
            self._calls += 1
            self._failed += failed
            rec["seq"] = self._calls
            if not self._opened:
                self._write({"type": "run", "run_id": self.run_id, "feature_id": self.feature, "feature": self.name,
                             "trigger": self.trigger, "started_at": self.started_at}, locked=True)
                self._opened = True
            self._write(rec, locked=True)

    def _write(self, obj: dict, locked: bool = False) -> None:
        """로그 기록 실패가 기능을 막지 않도록 OSError 는 경고만 남긴다."""
        def write():
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        try:
            if locked:
                write()
            else:
                with self._lock:
                    write()
        except OSError as e:
            print(f"[ai_log] 기록 실패: {e!r}", file=sys.stderr)


# ---------- 조회 · 제출 파일 ----------
def load_runs() -> list[dict]:
    runs = []
    for path in sorted(LOG_DIR.glob("*.jsonl")):
        head, records, end = None, [], None
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind = d.get("type")
            if kind == "run":
                head = d
            elif kind == "record":
                records.append(d)
            elif kind == "end":
                end = d
        if not (head and records):
            continue
        records.sort(key=lambda r: r["request_time"])
        ok = not any("error" in r for r in records) and (end is None or end.get("status") == "ok")
        runs.append({"path": path, "head": head, "records": records, "end": end, "ok": ok})
    return runs


def _submission(run: dict) -> dict:
    head, end = run["head"], run["end"] or {}
    records = []
    for i, r in enumerate(run["records"], 1):
        r = {k: v for k, v in r.items() if k != "type"}
        r["seq"] = i
        records.append(mask(r))
    return {
        "feature": head["feature"],
        "feature_id": head["feature_id"],
        "trigger": head.get("trigger"),
        "run_id": head["run_id"],
        "started_at": head["started_at"],
        "finished_at": end.get("finished_at"),
        "call_count": len(records),
        "field_guide": FIELD_GUIDE,
        "records": records,
    }


def _preview(run: dict) -> str:
    """첫 호출의 마지막 메시지 앞부분 — 어떤 입력으로 실행됐는지 보고 제출할 실행을 고르기 위해."""
    try:
        text = run["records"][0]["request_body"]["messages"][-1]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return " ".join(str(text).split())[:50]


def cmd_list(_args) -> None:
    for r in load_runs():
        h = r["head"]
        print(f"{h['run_id']}  {h['started_at'][:19]}  {h['feature_id']:<9} 호출 {len(r['records']):>3}  "
              f"{'정상' if r['ok'] else '실패포함'}  {_preview(r)}")


def cmd_export(args) -> None:
    runs = [r for r in load_runs() if datetime.fromisoformat(r["head"]["started_at"]) >= LOG_SINCE]
    if args.run:
        chosen = [r for r in runs if r["head"]["run_id"] in args.run]
        missing = set(args.run) - {r["head"]["run_id"] for r in chosen}
        if missing:
            sys.exit(f"실행 ID 없음: {', '.join(sorted(missing))}")
    else:
        features = args.feature or SERVER_FEATURES
        chosen = []
        for f in features:
            cands = [r for r in runs if r["head"]["feature_id"] == f and r["ok"]]
            if cands:
                chosen.append(cands[-1])
            else:
                print(f"경고: '{FEATURES.get(f, (f,))[0]}'({f}) 의 정상 실행 기록이 없음 — 기능을 한 번 실행한 뒤 다시 내보내세요", file=sys.stderr)
    if not chosen:
        sys.exit("내보낼 실행이 없습니다")
    out = Path(args.out)
    names = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for r in chosen:
            name = r["head"]["feature"].replace("/", "_")
            if name in names:
                name = f"{name}_{r['head']['run_id']}"
            names.add(name)
            z.writestr(f"{name}.json", json.dumps(_submission(r), ensure_ascii=False, indent=2))
            print(f"{name}.json  ← 실행 {r['head']['run_id']} ({r['head']['started_at'][:19]}, 호출 {len(r['records'])}건)")
    print(f"→ {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="darami AI API 호출 로그 조회 · 제출 파일 생성")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="실행 목록").set_defaults(fn=cmd_list)
    ex = sub.add_parser("export", help="제출용 zip 만들기 (기능별 1회)")
    ex.add_argument("--out", default="ai_api_logs.zip")
    ex.add_argument("--run", action="append", help="내보낼 실행 ID (여러 번 지정 가능)")
    ex.add_argument("--feature", action="append", help=f"기능 ID (기본: {', '.join(SERVER_FEATURES)})")
    ex.set_defaults(fn=cmd_export)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
