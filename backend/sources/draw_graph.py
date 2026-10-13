"""절차 그래프 전체를 3차원 한 장 그림으로 그린다 (노드·간선만, 글자 없음).

입력: playbooks/graph.base.json + playbooks/graph.learned.json (실제 구조 그대로, 노드 808 · 간선 643)
출력: docs/figure/procedural-graph-3d.png

배치(실제 구조를 반영)
  - 연결된 노드(간선이 하나라도 있는 502개)는 3차원 스프링 레이아웃(힘 기반). 거대 성분(372노드)이 중심을 차지하고
    작은 성분 25개는 서로 밀려 주변에 놓인다.
  - 고립 노드 306개(간선 없음)는 바깥 구면에 흩어 놓는다 — '연결되지 않았다'는 사실 자체가 구조다.
  - 노드 크기 = 차수(연결 수). 색 = 종류(법령·공통 / 공공서비스 / AI 제안).
  - 간선: requires(hard) 는 진하고 굵게, suggests(soft) 는 얇고 흐리게. 깊이(z)에 따라 투명도·크기를 줄여 입체감을 준다.

실행 (백엔드 venv 에는 그림 라이브러리가 없다 — 별도 환경에서):
  python -m venv /tmp/vizenv && /tmp/vizenv/bin/pip install networkx matplotlib numpy scipy
  /tmp/vizenv/bin/python -m sources.draw_graph [--views] [--gif] [--json]
    --views : 다른 각도 3장도 저장
    --gif   : 카메라가 그래프를 한 바퀴 돌며 가까운 절차 이름이 떠오르는 GIF(docs/figure/procedural-graph-3d.gif, README 용)
    --json  : 같은 배치 좌표를 docs/graph/graph.json 으로 내보낸다(docs/graph/index.html 의 WebGL 인터랙티브 뷰가 읽는다)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
import networkx as nx
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Line3DCollection  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT.parent / "docs" / "figure"
SEED = 7

# 색: 검증된 다크 서피스용 범주 팔레트 3색(all-pairs CVD ΔE 9.4, 정상시 20.9, 대비 ≥3:1)
SURFACE = "#0d0f14"
COLOR = {"law": "#3987e5", "svc": "#199e70", "x": "#d95926"}  # 법령·공통 / 공공서비스 / AI 제안
EDGE_HARD = np.array([0.93, 0.95, 1.00])  # 거의 흰색(푸른 기)
EDGE_SOFT = np.array([0.62, 0.66, 0.78])


def _kind(key: str) -> str:
    p = key.split(":")[0]
    return "law" if p in ("law", "common") else ("svc" if p == "svc" else "x")


def load() -> tuple[dict, list[dict]]:
    b = json.loads((ROOT / "playbooks" / "graph.base.json").read_text(encoding="utf-8"))
    try:
        l = json.loads((ROOT / "playbooks" / "graph.learned.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        l = {"nodes": {}, "edges": []}
    return {**b["nodes"], **l.get("nodes", {})}, b["edges"] + l.get("edges", [])


def _fib_sphere(n: int, rng: np.random.Generator, jitter: float = 0.0) -> np.ndarray:
    """구면 위 n 개 방향(피보나치 격자)."""
    i = np.arange(n) + 0.5
    phi = np.arccos(1 - 2 * i / max(n, 1))
    theta = np.pi * (1 + 5**0.5) * i
    v = np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=1)
    if jitter:
        v += rng.normal(0, jitter, v.shape)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v


def _spring3d(H: nx.Graph, seed: int, k_scale: float = 1.0, iters: int = 500) -> np.ndarray:
    pos = nx.spring_layout(H, dim=3, seed=seed, k=k_scale / np.sqrt(max(H.number_of_nodes(), 2)), iterations=iters)
    P = np.array([pos[n] for n in H])
    P -= P.mean(axis=0)
    P /= (np.linalg.norm(P, axis=1).max() or 1.0)
    return np.nan_to_num(P)


def layout(nodes: dict, edges: list[dict]) -> tuple[nx.DiGraph, dict[str, np.ndarray]]:
    """세 겹 배치: 중심 = 거대 연결 성분(힘 기반 3D), 중간 껍질 = 작은 성분들(각각 힘 기반으로 배치해 방향별로 놓음),
    바깥 껍질 = 고립 노드. 성분 크기·연결 여부라는 실제 구조가 그대로 반지름이 된다."""
    G = nx.DiGraph()
    G.add_nodes_from(nodes)
    G.add_edges_from((e["before"], e["after"]) for e in edges if e["before"] in nodes and e["after"] in nodes)
    U = G.to_undirected()
    comps = sorted((list(c) for c in nx.connected_components(U)), key=len, reverse=True)
    giant, small, isolated = comps[0], [c for c in comps[1:] if len(c) > 1], [c[0] for c in comps if len(c) == 1]
    rng = np.random.default_rng(SEED)
    pos: dict[str, np.ndarray] = {}
    # 1) 거대 성분: Kamada-Kawai 3D(전역 거리 보존 → 고르게 펴짐) 뒤 바깥으로 튄 노드는 반지름을 부드럽게 눌러 반지름 ~1.0 안에
    Gg = U.subgraph(giant)
    kk = nx.kamada_kawai_layout(Gg, dim=3, pos=nx.spring_layout(Gg, dim=3, seed=SEED, iterations=200))
    P = np.array([kk[n] for n in giant]); P -= P.mean(axis=0)
    r = np.linalg.norm(P, axis=1); r90 = np.percentile(r, 85)
    P = P / r90  # 85% 노드가 반지름 1 안
    r = np.linalg.norm(P, axis=1)
    P = P * (np.tanh(r) / np.maximum(r, 1e-9))[:, None]  # 바깥 노드 압축(1→0.76, 2→0.96)
    P = np.nan_to_num(P)
    for n, p in zip(giant, P):
        pos[n] = p
    # 2) 작은 성분: 각자 작은 구슬로 만들어 반지름 1.25~1.4 껍질에 고르게 — 큰 성분일수록 구슬이 크다
    dirs = _fib_sphere(len(small), rng, jitter=0.15)
    for c, d in zip(small, dirs):
        Q = _spring3d(U.subgraph(c), SEED, k_scale=1.5, iters=300) * (0.05 + 0.028 * np.sqrt(len(c)))
        center = d * rng.uniform(1.22, 1.38)
        for n, q in zip(c, Q):
            pos[n] = center + q
    # 3) 고립 노드: 반지름 1.6~1.85 의 바깥 껍질
    shell = _fib_sphere(len(isolated), rng, jitter=0.08) * rng.uniform(1.45, 1.68, (len(isolated), 1))
    for n, p in zip(isolated, shell):
        pos[n] = p
    return G, pos


def _depth(xyz: np.ndarray, elev: float, azim: float) -> np.ndarray:
    """카메라(elev/azim) 기준 깊이. 1 = 가장 앞, 0 = 가장 뒤."""
    a, e = np.radians(azim), np.radians(elev)
    view = np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])
    d = xyz @ view
    return (d - d.min()) / (d.max() - d.min() + 1e-9)


def render(G: nx.DiGraph, pos: dict, edges: list[dict], out: Path, elev: float = 22, azim: float = 38, size_px: int = 3200) -> None:
    keys = list(G.nodes)
    XYZ = np.array([pos[k] for k in keys])
    depth = _depth(XYZ, elev, azim)
    deg = np.array([G.degree(k) for k in keys], dtype=float)
    kinds = [_kind(k) for k in keys]
    isolated = deg == 0

    dpi = 200
    fig = plt.figure(figsize=(size_px / dpi, size_px / dpi), dpi=dpi, facecolor=SURFACE)
    ax = fig.add_axes([0, 0, 1, 1], projection="3d", facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.view_init(elev=elev, azim=azim)
    ax.set_axis_off()
    try:
        ax.set_box_aspect((1, 1, 1), zoom=1.75)  # 3D 축 기본 여백을 없애고 화면을 채운다
    except Exception:  # noqa: BLE001 — 구버전 matplotlib
        ax.set_box_aspect((1, 1, 1))
    lim = 1.8
    ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim); ax.set_zlim(-lim, lim)

    # ---- 간선: 깊이별로 투명도. hard 는 굵고 밝게, soft 는 얇고 흐리게 ----
    idx = {k: i for i, k in enumerate(keys)}
    segs, cols, lws = [], [], []
    for e in edges:
        if e["before"] not in idx or e["after"] not in idx:
            continue
        a, b = idx[e["before"]], idx[e["after"]]
        d = (depth[a] + depth[b]) / 2
        hard = bool(e.get("hard"))
        base = EDGE_HARD if hard else EDGE_SOFT
        alpha = (0.30 + 0.55 * d) if hard else (0.14 + 0.36 * d)
        segs.append([XYZ[a], XYZ[b]])
        cols.append((*base, alpha))
        lws.append((1.1 + 1.1 * d) if hard else (0.5 + 0.6 * d))
    lc = Line3DCollection(segs, colors=cols, linewidths=lws, zorder=1)
    ax.add_collection3d(lc)

    # ---- 노드: 글로우(크고 흐린 원) + 코어(작고 밝은 원). 크기 = 차수, 깊이에 따라 축소 ----
    rgb = np.array([matplotlib.colors.to_rgb(COLOR[k]) for k in kinds])
    size_core = np.where(isolated, 10.0, 22.0 + 14.0 * np.sqrt(deg)) * (0.55 + 0.75 * depth)
    alpha_core = np.where(isolated, 0.35 + 0.35 * depth, 0.55 + 0.45 * depth)
    glow_rgba = np.column_stack([rgb, np.where(isolated, 0.05 + 0.08 * depth, 0.10 + 0.22 * depth)])
    core_rgba = np.column_stack([rgb, alpha_core])
    order = np.argsort(depth)  # 뒤에서 앞으로 그린다
    ax.scatter(XYZ[order, 0], XYZ[order, 1], XYZ[order, 2], s=size_core[order] * 5.5, c=glow_rgba[order], linewidths=0, depthshade=False, zorder=2)
    ax.scatter(XYZ[order, 0], XYZ[order, 1], XYZ[order, 2], s=size_core[order], c=core_rgba[order], linewidths=0, depthshade=False, zorder=3)
    # 허브(차수 상위)에는 아주 작은 흰 점을 얹어 반짝이게
    hub = deg >= 10
    ax.scatter(XYZ[hub, 0], XYZ[hub, 1], XYZ[hub, 2], s=size_core[hub] * 0.18, c=[(1, 1, 1, 0.9)], linewidths=0, depthshade=False, zorder=4)

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi, facecolor=SURFACE)
    plt.close(fig)


def export_json(G: nx.DiGraph, pos: dict, nodes: dict, edges: list[dict], out: Path) -> None:
    """인터랙티브 뷰(docs/graph/index.html)용 데이터. 절차 이름·종류·차수·좌표와 간선(hard/guidance)만 — 키·개인정보 없음."""
    keys = list(G.nodes)
    idx = {k: i for i, k in enumerate(keys)}
    data = {
        "nodes": [{"i": idx[k], "name": nodes[k].get("name") or k, "kind": _kind(k), "deg": G.degree(k),
                   "p": [round(float(v), 4) for v in pos[k]]} for k in keys],
        "links": [{"s": idx[e["before"]], "t": idx[e["after"]], "hard": bool(e.get("hard")),
                   "g": (e.get("guidance") or "")[:120]} for e in edges if e["before"] in idx and e["after"] in idx],
        "meta": {"nodes": len(keys), "edges": G.number_of_edges(), "isolated": sum(1 for k in keys if G.degree(k) == 0)},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def render_gif(G: nx.DiGraph, pos: dict, nodes: dict, edges: list[dict], out: Path, frames: int = 72, size_px: int = 800, labels: int = 7) -> None:
    """카메라가 한 바퀴(방위각 360°, 고도는 천천히 오르내림) 돌며, 카메라에 가장 가까운 연결 노드 몇 개에 이름을 띄운다."""
    from matplotlib.animation import PillowWriter
    import matplotlib.font_manager as fm
    have = {f.name for f in fm.fontManager.ttflist}
    plt.rcParams["font.family"] = [n for n in ("Apple SD Gothic Neo", "AppleGothic", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR", "Malgun Gothic") if n in have] + ["DejaVu Sans"]
    keys = list(G.nodes)
    XYZ = np.array([pos[k] for k in keys])
    deg = np.array([G.degree(k) for k in keys], dtype=float)
    kinds = [_kind(k) for k in keys]
    isolated = deg == 0
    rgb = np.array([matplotlib.colors.to_rgb(COLOR[k]) for k in kinds])
    idx = {k: i for i, k in enumerate(keys)}
    pairs = [(idx[e["before"]], idx[e["after"]], bool(e.get("hard"))) for e in edges if e["before"] in idx and e["after"] in idx]
    dpi = 100
    fig = plt.figure(figsize=(size_px / dpi, size_px / dpi), dpi=dpi, facecolor=SURFACE)
    ax = fig.add_axes([0, 0, 1, 1], projection="3d", facecolor=SURFACE)
    writer = PillowWriter(fps=12)
    out.parent.mkdir(parents=True, exist_ok=True)
    with writer.saving(fig, str(out), dpi):
        for f in range(frames):
            t = f / frames
            azim, elev = 360 * t, 18 + 14 * np.sin(2 * np.pi * t)
            ax.cla(); ax.set_facecolor(SURFACE); ax.set_axis_off(); ax.view_init(elev=elev, azim=azim)
            try:
                ax.set_box_aspect((1, 1, 1), zoom=1.75)
            except Exception:  # noqa: BLE001
                pass
            lim = 1.8
            ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim); ax.set_zlim(-lim, lim)
            depth = _depth(XYZ, elev, azim)
            segs = [[XYZ[a], XYZ[b]] for a, b, _ in pairs]
            d = np.array([(depth[a] + depth[b]) / 2 for a, b, _ in pairs])
            hard = np.array([h for _, _, h in pairs])
            cols = [(*(EDGE_HARD if h else EDGE_SOFT), (0.30 + 0.55 * dd) if h else (0.14 + 0.36 * dd)) for h, dd in zip(hard, d)]
            lws = np.where(hard, 1.0 + 1.0 * d, 0.45 + 0.55 * d)
            ax.add_collection3d(Line3DCollection(segs, colors=cols, linewidths=lws))
            size = np.where(isolated, 7.0, 16.0 + 10.0 * np.sqrt(deg)) * (0.55 + 0.75 * depth)
            glow = np.column_stack([rgb, np.where(isolated, 0.05 + 0.08 * depth, 0.10 + 0.22 * depth)])
            core = np.column_stack([rgb, np.where(isolated, 0.35 + 0.35 * depth, 0.55 + 0.45 * depth)])
            order = np.argsort(depth)
            ax.scatter(XYZ[order, 0], XYZ[order, 1], XYZ[order, 2], s=size[order] * 5.5, c=glow[order], linewidths=0, depthshade=False)
            ax.scatter(XYZ[order, 0], XYZ[order, 1], XYZ[order, 2], s=size[order], c=core[order], linewidths=0, depthshade=False)
            # 카메라에 가까운 연결 노드에 이름 — 가까움 + 허브(차수) 가중, 화면 좌표로 겹침 회피
            from mpl_toolkits.mplot3d import proj3d
            cand = np.where(deg >= 2)[0]
            score = depth[cand] + 0.02 * np.minimum(deg[cand], 15)
            px_all, py_all, _ = proj3d.proj_transform(XYZ[cand, 0], XYZ[cand, 1], XYZ[cand, 2], ax.get_proj())
            px_all, py_all = np.asarray(px_all), np.asarray(py_all)
            span_x, span_y = np.ptp(px_all) or 1.0, np.ptp(py_all) or 1.0  # 화면 가로·세로 폭(투영 좌표)
            proj = {int(i): (float(x), float(y)) for i, x, y in zip(cand, px_all, py_all)}
            placed: list[tuple[float, float]] = []
            shown = 0
            for i in cand[np.argsort(-score)]:
                if shown >= labels:
                    break
                sx, sy = proj[int(i)]
                if any(abs(sx - px) < 0.16 * span_x and abs(sy - py) < 0.035 * span_y for px, py in placed):
                    continue  # 화면상 가까운 자리에 이미 라벨이 있으면 건너뛴다(라벨 폭 ≈ 화면의 16%, 높이 ≈ 3.5%)
                placed.append((sx, sy)); shown += 1
                a = 0.5 + 0.5 * depth[i]
                name = nodes[keys[i]].get("name") or keys[i]
                ax.text(XYZ[i, 0], XYZ[i, 1], XYZ[i, 2] + 0.035, name if len(name) <= 18 else name[:17] + "…",
                        color=(0.96, 0.96, 0.98, a), fontsize=7.2, ha="center", va="bottom",
                        bbox=dict(boxstyle="round,pad=0.25", fc=(0.05, 0.06, 0.09, 0.6 * a), ec="none"), zorder=10)
            writer.grab_frame(facecolor=SURFACE)
    plt.close(fig)


def main(argv: list[str]) -> int:
    nodes, edges = load()
    G, pos = layout(nodes, edges)
    main_out = OUT_DIR / "procedural-graph-3d.png"
    render(G, pos, edges, main_out)
    print(f"saved {main_out.relative_to(ROOT.parent)}  (nodes {G.number_of_nodes()}, edges {G.number_of_edges()}, "
          f"isolated {sum(1 for n in G if G.degree(n) == 0)})")
    if "--json" in argv:
        jp = ROOT.parent / "docs" / "graph" / "graph.json"
        export_json(G, pos, nodes, edges, jp)
        print(f"saved {jp.relative_to(ROOT.parent)}")
    if "--gif" in argv:
        gp = OUT_DIR / "procedural-graph-3d.gif"
        render_gif(G, pos, nodes, edges, gp)
        print(f"saved {gp.relative_to(ROOT.parent)}  ({gp.stat().st_size / 1e6:.1f} MB)")
    if "--views" in argv:
        for i, (el, az) in enumerate([(10, 120), (35, -60), (60, 20)], 1):
            p = OUT_DIR / f"procedural-graph-3d-view{i}.png"
            render(G, pos, edges, p, elev=el, azim=az, size_px=2000)
            print(f"saved {p.relative_to(ROOT.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
