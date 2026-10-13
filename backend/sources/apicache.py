"""공공 API 응답 디스크 캐시 (generated/cache/<이름>/<해시>.json).

- 사전 생성 500건은 같은 조문·서비스를 수백 번 조회하므로 캐시가 없으면 느리고,
  복지로 중앙부처 API(개발계정 일 100건)는 한도를 넘긴다.
- 성공 결과만 저장(verified / 비어 있지 않은 목록). 실패는 다음 호출에서 다시 시도.
- TTL(기본 3일)을 넘긴 파일은 무시 → 서비스 경로에서도 너무 오래된 값은 쓰지 않는다.
- 끄려면 DARAMI_API_CACHE=0.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import time
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "generated" / "cache"
TTL_SECONDS = 3 * 24 * 3600
ENABLED = os.environ.get("DARAMI_API_CACHE", "1") != "0"


def _key(args, kwargs) -> str:
    raw = json.dumps([args, kwargs], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _ok(result) -> bool:
    if isinstance(result, dict):
        return result.get("verification_status") == "verified" or ("verification_status" not in result and bool(result))
    if isinstance(result, list):
        return bool(result)
    return result is not None


def disk_cached(name: str):
    """함수 결과를 (name, 인자) 기준으로 디스크에 캐시. 성공 결과만 저장."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not ENABLED:
                return fn(*args, **kwargs)
            path = CACHE_DIR / name / f"{_key(args, kwargs)}.json"
            try:
                if path.exists() and time.time() - path.stat().st_mtime < TTL_SECONDS:
                    return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            result = fn(*args, **kwargs)
            if _ok(result):
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            return result
        return wrapper
    return deco
