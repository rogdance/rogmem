"""三维记忆图谱契约：默认走三维、静态资源现成、加载状态机照旧、星图仍可切回。

星图（cosmos）那套的细节契约在 test_design_port / test_graph_label_follow_contract /
test_graph_auto_labels_render 里，且都显式指定了 renderer="cosmos"。
"""

from __future__ import annotations

import re

# 只属于铺名层的指纹。三维核心模板里留了一行 `typeof rogmem3dLabels === 'function'` 的挂载守卫，
# 所以函数名本身不算关态泄漏；样式、预算常量与整段实现才是。
LABEL_FINGERPRINTS = ("graph-3d-labels", "graph-3d-label", "ROGMEM_3D_LABEL_BUDGET", "candidates.sort", "EVERY_MS")


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_default_renderer_constant_is_three_d():
    from rogmem.engine import GRAPH_RENDERER_DEFAULT, GRAPH_RENDERER_VALUES

    assert GRAPH_RENDERER_DEFAULT == "3d"
    assert set(GRAPH_RENDERER_VALUES) == {"3d", "cosmos"}


def test_graph_page_ships_the_three_d_module_by_default():
    from rogmem.ui import graph_page

    page = graph_page()
    assert "rogmemDraw3D" in page
    assert "__rogmemThree" in page
    assert "/admin/static/three/build/three.module.js" in page
    assert "three/addons/" in page
    # 三维页不再加载星图脚本。
    assert "/admin/static/cosmos-graph.min.js" not in page


def test_three_d_page_keeps_the_loader_state_machine():
    from rogmem.ui import graph_page

    page = graph_page()
    for needle in (
        "graph-loader",
        "正在铺开星图",
        "AbortController",
        "15000",
        "renderWatchdog",
        "图谱没有载入",
        "重试",
        "location.reload",
        "用系统浏览器打开同一页",
        "还没有可以铺开的星点",
        "setAttribute('aria-busy', 'false')",
    ):
        assert needle in page, f"三维页缺 {needle!r}"
    assert "prefers-reduced-motion" in page


def test_three_d_page_carries_none_of_the_star_map_only_bits():
    from rogmem.ui import graph_page

    page = graph_page()
    for needle in ("rogmemGlass", "pointDefaultShape: 8", "linkBlending", "setImageData", "rogmemGlow"):
        assert needle not in page, f"三维页泄漏星图专用代码 {needle!r}"


def test_star_map_renderer_is_still_available():
    from rogmem.ui import graph_page

    page = graph_page(renderer="cosmos")
    assert "/admin/static/cosmos-graph.min.js" in page
    assert "rogmemDraw3D" not in page


def test_renderer_setting_round_trips_and_rejects_junk(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.graph_renderer() == "3d"
    assert engine.save_basic({"graph_renderer": "cosmos"})["ok"] is True
    assert engine.graph_renderer() == "cosmos"
    # 2d 是星图的别名，收下并归一。
    assert engine.save_basic({"graph_renderer": "2d"})["ok"] is True
    assert engine.graph_renderer() == "cosmos"
    assert engine.save_basic({"graph_renderer": "webgl"})["ok"] is False
    assert engine.basic_config()["graph_renderer"] == "cosmos"


def test_route_serves_three_d_by_default_and_the_star_map_on_demand(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    three = client.get("/admin/graph").text
    assert "rogmemDraw3D" in three
    assert "/admin/static/cosmos-graph.min.js" not in three
    engine.save_basic({"graph_renderer": "cosmos"})
    flat = client.get("/admin/graph").text
    assert "rogmemDraw3D" not in flat
    assert "/admin/static/cosmos-graph.min.js" in flat


def test_basic_page_offers_the_renderer_switch():
    from rogmem.ui import basic_page

    chosen = basic_page({"graph_renderer": "cosmos"})
    assert 'name="graph_renderer"' in chosen
    assert '<option value="3d"' in chosen
    assert '<option value="cosmos" selected' in chosen
    default = basic_page({})
    assert '<option value="3d" selected' in default


def test_auto_labels_default_to_on_everywhere():
    from rogmem.engine import GRAPH_AUTO_LABELS_DEFAULT
    from rogmem.ui import basic_page

    assert GRAPH_AUTO_LABELS_DEFAULT is True
    tag = re.search(r'<input[^>]*name="graph_auto_labels"[^>]*>', basic_page({})).group(0)
    assert "checked" in tag


def test_three_d_off_state_omits_the_label_layer():
    from rogmem.ui import graph_page

    page = graph_page(auto_labels=False)
    for needle in LABEL_FINGERPRINTS:
        assert needle not in page, f"关态不该出现 {needle!r}"


def test_three_d_on_state_carries_the_label_layer_and_the_budget():
    from rogmem.ui import graph_page

    page = graph_page(auto_labels=True, label_budget=17)
    for needle in LABEL_FINGERPRINTS:
        assert needle in page, f"开态缺 {needle!r}"
    assert "ROGMEM_3D_LABEL_BUDGET = 17" in page
    # 迟滞方向：Z_on > Z_off，免得在临界点来回闪。
    on = re.search(r"Z_on = ([0-9.]+)", page)
    off = re.search(r"Z_off = ([0-9.]+)", page)
    assert on and off, "需要有 Z_on / Z_off 常量定义"
    assert float(on.group(1)) > float(off.group(1))


def test_two_d_star_map_does_not_carry_the_three_d_label_layer():
    from rogmem.ui import graph_page

    page = graph_page(auto_labels=True, renderer="cosmos")
    for needle in ("rogmem3dLabels", "graph-3d-labels"):
        assert needle not in page


def test_sphere_layout_gives_depth_not_a_plane():
    from rogmem.engine import _sphere_positions

    count = 40
    nodes = [{"id": f"c{i}", "name": f"结晶 {i}", "type": "概念"} for i in range(count)]
    nodes[0]["id"] = "owner"
    edges = [{"src": nodes[i]["id"], "dst": nodes[i + 1]["id"], "kind": "提及"} for i in range(count - 1)]
    _sphere_positions(nodes, edges)
    zs = [node["z"] for node in nodes]
    radii = [(node["x"] ** 2 + node["y"] ** 2 + node["z"] ** 2) ** 0.5 for node in nodes]
    # 根在球心、末梢在球面：半径随跳数变大，z 不能全落在同一个平面上。
    assert max(zs) - min(zs) > 1.0
    assert radii[0] < radii[-1]


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


def test_both_graphs_ship_the_same_three_d_body(tmp_path, monkeypatch):
    """关联图谱和记忆图谱的三维脚本除邻域过滤外应当同一份。"""
    _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    association = client.get("/admin/crystals/owner?view=graph").text
    memory = client.get("/admin/graph").text
    draw = "rogmemDraw3D(document.getElementById('graph'), nodes, edges, true)"
    assert draw in association
    assert draw in memory
    assert "var selfId" in association
    assert "var selfId" not in memory
    assert _without_neighborhood(_mount(association)) == _mount(memory)


def test_three_d_canvas_follows_the_container_not_the_window():
    """画布尺寸必须跟容器走。容器在页面里只占剩余高度（顶部还有标题与说明），
    用窗口高度会把渲染中心推到可见区下方：球心看着偏下、拖拽像没绕球心转，球还会被裁掉一截。"""
    from rogmem.ui import graph_page

    page = graph_page()
    assert "function rogmem3dViewport(el)" in page
    assert "el.clientWidth" in page
    assert "el.clientHeight" in page
    assert "var viewport = rogmem3dViewport(el);" in page
    assert "renderer.setSize(viewport[0], viewport[1])" in page
    assert "new THREE.PerspectiveCamera(62, viewport[0] / Math.max(1, viewport[1])" in page
    # 容器自身变化（不只是窗口 resize）也要重新贴合。
    assert "new ResizeObserver(fitViewport)" in page
    # 不得再拿窗口尺寸当画布尺寸。
    assert "renderer.setSize(window.innerWidth" not in page
    assert "composer.setSize(window.innerWidth" not in page
    assert "camera.aspect = window.innerWidth" not in page
    # 铺名层的屏幕坐标同样按容器换算，否则名字会整体偏移。
    labelled = graph_page(auto_labels=True, label_budget=17)
    assert "var size = box();" in labelled
