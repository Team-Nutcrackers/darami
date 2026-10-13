"""(호환 shim) 할 일 의존관계 엔진 → sources/graph.py 로 옮겨졌다.

예전 호출부(deps.apply / deps.names_of / deps.item_for_key)가 그대로 동작하도록 그래프 엔진에 위임한다.
새 코드는 `from . import graph` 를 쓸 것.
"""

from __future__ import annotations

from .graph import apply, item_for_key, names_of  # noqa: F401


def load() -> dict:
    """예전 dependencies.json 모양({edges, items})으로 돌려준다(검증 스크립트 호환)."""
    from . import graph
    return {"edges": graph.edges(), "items": {k: n.get("item") for k, n in graph.nodes().items() if n.get("item")}}
