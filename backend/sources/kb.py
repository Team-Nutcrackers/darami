"""절차 지식베이스(playbooks/procedures.json) — 법령·공통·AI제안 절차의 '어디서·무슨 서류로·어떤 순서로'.

공공 API(보조금24·복지로)에서 온 절차(svc:)는 서류·링크가 이미 공식 데이터로 채워지지만,
법령(law:)·공통(common:)·AI제안(x:) 절차는 조문 텍스트뿐이라 창구·서류·사이트가 비어 있었다.
이 파일은 그 빈칸을 사람(메인 에이전트)이 공식 페이지를 직접 확인해 채운 항목(검증일·출처 포함)으로 메운다.

- 키 일치(key) 우선, 없으면 정규화한 이름으로 대조한다.
- confidence=verified(서류·창구·기한을 공식 페이지에서 확인) → verification_status 를 verified 로, source.tier 를 official 로 올린다.
- confidence=partial → 찾은 필드만 채우고 상태는 partial 이상으로만 올린다.
- svc: 절차는 공식 데이터가 우선이라 비어 있는 필드(steps·pitfalls 등)만 보탠다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

KB_PATH = Path(__file__).resolve().parent.parent / "playbooks" / "procedures.json"
_kb: dict | None = None
_kb_mtime: float = 0.0  # procedures.json 이 바뀌면(배치 병합 등) 서버 재시작 없이 다시 읽는다


def _norm(s: str) -> str:
    return re.sub(r"[\s·ㆍ()\[\]/\-_,.'\"]", "", s or "")


def load(force: bool = False) -> dict:
    """{'by_key': {key: entry}, 'by_name': {norm(name): entry}, 'entries': [...]}"""
    global _kb, _kb_mtime
    try:
        mtime = KB_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _kb is not None and not force and mtime == _kb_mtime:
        return _kb
    _kb_mtime = mtime
    entries: list[dict] = []
    if KB_PATH.exists():
        try:
            data = json.loads(KB_PATH.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else data
        except (OSError, ValueError):
            entries = []
    by_key = {e["key"]: e for e in entries if e.get("key")}
    by_name: dict[str, dict] = {}
    for e in entries:
        for nm in [e.get("name")] + list(e.get("aliases") or []):
            if nm and _norm(nm) not in by_name:
                by_name[_norm(nm)] = e
    _kb = {"by_key": by_key, "by_name": by_name, "entries": entries}
    return _kb


def lookup(proc: dict) -> dict | None:
    kb = load()
    key = proc.get("key") or ""
    if key in kb["by_key"]:
        return kb["by_key"][key]
    n = _norm(proc.get("name", ""))
    if n and n in kb["by_name"]:
        return kb["by_name"][n]
    if key.startswith("x:"):  # 'x:이름' 은 이름이 곧 키
        return kb["by_name"].get(_norm(key[2:]))
    return None


def _nonempty(v) -> bool:
    return bool(v) and (not isinstance(v, str) or v.strip() != "")


def enrich(proc: dict) -> dict:
    """절차 한 건에 지식베이스 항목을 덧입힌다(제자리 수정 후 반환). 항목이 없으면 그대로."""
    e = lookup(proc)
    if not e:
        return proc
    is_svc = str(proc.get("key") or "").startswith("svc:")
    verified = e.get("confidence") == "verified"
    # 창구·서류·링크 — 공공 서비스 데이터가 있으면(svc) 그쪽이 우선, 나머지는 지식베이스가 채운다
    if not is_svc or not _nonempty(proc.get("documents")):
        if _nonempty(e.get("documents")):
            proc["documents"] = list(e["documents"])
    if not _nonempty(proc.get("link")) and str(e.get("link") or "").startswith("http"):
        proc["link"] = e["link"]
    if not is_svc:
        if _nonempty(e.get("agency")):
            proc["agency"] = e["agency"]
        if isinstance(e.get("online"), bool):
            proc["online"] = e["online"]
        if _nonempty(e.get("summary")):
            proc["summary"] = e["summary"]
    if not _nonempty(proc.get("deadline")) and _nonempty(e.get("deadline")):
        proc["deadline"] = e["deadline"]
    if (proc.get("deadlineDays") in (None, 9999)) and isinstance(e.get("deadlineDays"), int):
        proc["deadlineDays"] = e["deadlineDays"]
    for f in ("where", "fee", "steps", "pitfalls"):
        if _nonempty(e.get(f)):
            proc[f] = e[f]
    # 출처·검증 상태 — 법령 조문 출처는 이름에 같이 남긴다
    srcs = [s for s in (e.get("sources") or []) if isinstance(s, dict) and s.get("name")]
    if not is_svc and srcs:
        law_src = (proc.get("source") or {}).get("name") or ""
        name = srcs[0]["name"]
        while law_src and _norm(law_src).startswith(_norm(name)):  # 다시 돌려도 이름이 겹겹이 붙지 않게
            law_src = law_src[len(name):].lstrip(" ·")
        if law_src:
            name = f"{name} · {law_src}"
        proc["source"] = {"name": name, "tier": "official" if verified else "reference",
                          "checkedAt": e.get("checkedAt") or (proc.get("source") or {}).get("checkedAt", "")}
        proc["sourceUrl"] = srcs[0].get("url", "")
    if not is_svc:
        cur = proc.get("verification_status")
        if verified:
            proc["verification_status"] = "verified"
        elif cur not in ("verified", "partial"):
            proc["verification_status"] = "partial"
        if cur != proc["verification_status"] or srcs:
            proc["curated"] = True
    return proc


def apply(procs: list[dict]) -> list[dict]:
    for p in procs or []:
        try:
            enrich(p)
        except Exception:  # noqa: BLE001 — 지식베이스 결함이 플레이북 생성을 막으면 안 된다
            pass
    return procs


def stats() -> dict:
    kb = load()
    es = kb["entries"]
    return {"entries": len(es), "verified": sum(e.get("confidence") == "verified" for e in es),
            "partial": sum(e.get("confidence") == "partial" for e in es),
            "with_documents": sum(bool(e.get("documents")) for e in es), "with_link": sum(bool(e.get("link")) for e in es)}


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False))
