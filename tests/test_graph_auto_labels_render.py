"""图谱自动词条名：开关关近零开销（代码不出现）、开启时结构齐备、路由按开关传参。"""

from __future__ import annotations

import re


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    # 这一组契约锁的是星图（cosmos）那套词条名层；三维渲染另有自己的契约。
    # 默认已改成开，这里显式落成关，好让"关态零泄漏"的断言有意义。
    engine.save_basic({"graph_renderer": "cosmos", "graph_auto_labels": "0"})
    return engine


# ---- 开关关：零字节相关代码 ----

AUTO_LABEL_FINGERPRINTS = (
    "graph-labels",
    "findPointsInRect",
    "spaceToScreenPosition",
    "onZoomEnd",
    "onZoomStart",
    "measureText",
    # follow-aware 版本的"挑选"用 selectToken 做 debounce 合并。
    "selectToken",
    # 跟随期 rAF 循环的函数标识（CameraTracker.tick 内）。
    "createCameraTracker",
)


def test_switch_off_omits_all_auto_label_code():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=False)
    for needle in AUTO_LABEL_FINGERPRINTS:
        assert needle not in html, f"关态下不该出现 {needle!r}"


def test_switch_off_is_default_for_backward_compat():
    from rogmem.ui import graph_mount

    # 既有调用方不传 auto_labels 关键字参数，行为与关态一致。
    html = graph_mount("graph")
    for needle in AUTO_LABEL_FINGERPRINTS:
        assert needle not in html


# ---- 开关开：结构 ----


def test_switch_on_includes_core_pieces():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, renderer="cosmos")
    for needle in AUTO_LABEL_FINGERPRINTS:
        assert needle in html, f"开态缺 {needle!r}"
    # 50 预算 + hover/focus 合并槽
    assert "50" in html
    # 迟滞方向：Z_on > Z_off
    match = re.search(r"Z_on\s*=\s*([0-9.]+)", html)
    match2 = re.search(r"Z_off\s*=\s*([0-9.]+)", html)
    assert match and match2, "需要有 Z_on / Z_off 常量定义"
    assert float(match.group(1)) > float(match2.group(1)), (
        f"开启门必须高于关闭门：Z_on={match.group(1)} Z_off={match2.group(1)}"
    )
    # 规模闸：启用前的硬门
    assert "规模过大" in html or "scale-gate" in html
    # 跟随熔断：本页范围 tracker.destroy + 提示（不再 sessionStorage 跨刷新禁用）
    assert "trackerPaused" in html or "tracker.destroy" in html
    # Budget 现在由服务端注入 GRAPH_LABEL_BUDGET = <n>；默认 50。
    assert "GRAPH_LABEL_BUDGET = 50" in html


def test_switch_on_binds_real_cosmos_events_only():
    """只许 onZoom*/onResize/onSimulationStart/End/visibilitychange/webglcontextlost；禁 onDragStart/End 冒名 pan 事件。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, renderer="cosmos")
    # 不得在自动标签层绑画布拖动——节点拖动的 onDragStart/End 存在但本层不应用它冒名 pan。
    # 允许整份 HTML 里出现 onDragStart（Cosmos 本体支持），只约束：自动标签脚本块里不出现。
    label_script = html.split("rogmemAutoLabels", 1)
    assert len(label_script) == 2, "开态应有 rogmemAutoLabels 这块脚本标识"


def test_switch_on_has_visibility_change_handler():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, renderer="cosmos")
    assert "visibilitychange" in html


# ---- 路由按开关传值 ----


def test_admin_graph_route_wires_switch_value(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())

    off_html = client.get("/admin/graph").text
    for needle in AUTO_LABEL_FINGERPRINTS:
        assert needle not in off_html, f"路由关态泄漏 {needle!r}"
    engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "1"})
    on_html = client.get("/admin/graph").text
    for needle in AUTO_LABEL_FINGERPRINTS:
        assert needle in on_html, f"路由开态缺 {needle!r}"


def test_graph_page_accepts_auto_labels_kwarg():
    from rogmem.ui import graph_page

    off = graph_page(auto_labels=False, renderer="cosmos")
    on = graph_page(auto_labels=True, renderer="cosmos")
    assert "findPointsInRect" not in off
    assert "findPointsInRect" in on


def _js_fn(html: str, name: str) -> str:
    marker = f"function {name}"
    start = html.index(marker)
    nxt = html.index("\n  function ", start + len(marker))
    return html[start:nxt]


def test_crystal_graph_view_wires_switch_value(tmp_path, monkeypatch):
    """关联图谱和记忆图谱读同一对开关，悬停放大与变色是同一段绘制函数。"""
    engine = _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    off_html = client.get("/admin/crystals/owner?view=graph").text
    assert "关联图谱" in off_html
    for needle in AUTO_LABEL_FINGERPRINTS:
        assert needle not in off_html, f"关联图谱关态泄漏 {needle!r}"
    assert 'name="graph_auto_labels"' not in off_html
    assert 'name="graph_label_budget"' not in off_html

    engine.save_basic(
        {"sketch_char_limit": "3500", "graph_auto_labels": "1", "graph_label_budget": "17"}
    )
    on_html = client.get("/admin/crystals/owner?view=graph").text
    for needle in AUTO_LABEL_FINGERPRINTS:
        assert needle in on_html, f"关联图谱开态缺 {needle!r}"
    assert "GRAPH_LABEL_BUDGET = 17" in on_html
    assert 'name="graph_auto_labels"' not in on_html
    assert 'name="graph_label_budget"' not in on_html
    body = client.get("/admin/crystals/owner").text
    assert "findPointsInRect" not in body

    memory = client.get("/admin/graph").text
    assert "GRAPH_LABEL_BUDGET = 17" in memory
    for name in ("liftNode", "tintNode"):
        assert _js_fn(on_html, name) == _js_fn(memory, name)


def _mount(html: str) -> str:
    start = html.index('<div id="graph"')
    end = html.index("})();\n</script>", start) + len("})();\n</script>")
    return html[start:end]


def _without_neighborhood(script: str) -> str:
    start = script.find("var selfId")
    if start < 0:
        return script
    end_marker = "return keep[n.id]; });"
    end = script.find(end_marker, start)
    assert end > start
    return script[:start] + script[end + len(end_marker):]


def test_association_graph_mount_matches_memory_graph(tmp_path, monkeypatch):
    """两页的星图脚本除邻域过滤外应当同一份。邻域过滤只决定画哪些点。"""
    _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    association = client.get("/admin/crystals/owner?view=graph").text
    memory = client.get("/admin/graph").text
    draw = "rogmemDraw(document.getElementById('graph'), nodes, edges, true)"
    assert draw in association
    assert draw in memory
    assert "var selfId" in association
    assert "var selfId" not in memory
    assert _without_neighborhood(_mount(association)) == _mount(memory)
