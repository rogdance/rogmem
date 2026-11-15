"""结构契约：四职责拆分 + 关态零泄漏 + 跟随循环的硬约束。

静态断言只做必要的结构保证，细节行为留给真实浏览器脚本 scripts/verify_graph_label_follow.py。
"""

from __future__ import annotations

import re


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    # 这一组契约锁的是星图（cosmos）那套跟随层；三维渲染另有自己的契约。
    engine.save_basic({"graph_renderer": "cosmos"})
    return engine


FOLLOW_ONLY_FINGERPRINTS = (
    "createSnapshotLifecycle",
    "createSelector",
    "createCameraTracker",
    "__graphLabelsDebug",
    "getPointPositionsAsync",
    "pageSeed",
    "shuffleKey",
    "chainConfig",
    "onZoom:",
)


# ---- 关态：零字节相关代码 ----


def test_switch_off_emits_none_of_the_follow_code():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", auto_labels=False, renderer="cosmos")
    for needle in FOLLOW_ONLY_FINGERPRINTS:
        assert needle not in html, f"关态泄漏 {needle!r}"


def test_switch_off_backcompat_default_arg():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", renderer="cosmos")  # 默认 auto_labels=False
    for needle in FOLLOW_ONLY_FINGERPRINTS:
        assert needle not in html


# ---- 开态：四职责与跟随通路 ----


def test_switch_on_has_four_factories_and_chain_helper():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    for needle in ("createSnapshotLifecycle", "createSelector", "createCameraTracker", "chainConfig"):
        assert needle in html, f"开态缺 {needle!r}"


def test_switch_on_hooks_onZoom_in_setConfig():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # 7 个回调必须都在 hooks 批 patch 里（原子 setConfig batch，避免 per-call 竞态）。
    hooks = html.split("var hooks = {", 1)[1][: html.split("var hooks = {", 1)[1].find("};") + 1]
    for name in ("onZoomStart", "onZoom", "onZoomEnd", "onResize",
                 "onSimulationStart", "onSimulationEnd", "onDragEnd"):
        assert f"{name}: function" in hooks, f"hooks batch 缺 {name}"
    # 跟随语义：onZoomStart → beginFollow；onZoomEnd → syncFinal → endFollow → scheduleSelect。
    start_body = hooks.split("onZoomStart: function", 1)[1][:400]
    assert "beginFollow" in start_body
    assert "hideAll" not in start_body, "onZoomStart 不得 hideAll"
    assert "pause(" not in start_body, "onZoomStart 不得 pause"
    end_body = hooks.split("onZoomEnd: function", 1)[1][:400]
    assert end_body.index("syncFinal") < end_body.index("endFollow") < end_body.index("scheduleSelect")
    # 必须用一次 batch setConfigPartial（不是 per-name 多次调用），而且必须直接调 setConfigPartial
    # 没有 setConfig fallback：Cosmos 的 setConfig 会 reset 全部默认（b1(config)），回退不安全。
    assert "setConfigPartial" in html
    assert "graph.setConfigPartial(batchPatch)" in html
    # 缺 setConfigPartial 的 vendor 必须诚实停止，不能回退到 setConfig。
    assert "typeof graph.setConfigPartial !== 'function'" in html
    assert "(graph.setConfigPartial || graph.setConfig)" not in html, \
        "不得保留危险的 setConfig fallback"


def test_destroy_restores_callbacks_and_cleans_up():
    """destroy 必须恢复所有被我们覆盖的 Cosmos 回调，且清事件/layer/attached 标记，
    并精确拆掉自己那一层 window.rogmemOnGraphGone 不覆盖后来的第三方 wrapper。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # 第一个 restoreFns.push：batch callback restore（用 setConfigPartial）
    first_restore = html.split("restoreFns.push(function ()", 1)[1][:1000]
    assert "graph.config && graph.config[name]" in first_restore
    assert "restorePatch[name] = prevHooks[name]" in first_restore
    assert "setConfigPartial" in first_restore
    # window.rogmemOnGraphGone 的 wrapper 必须被登记还原
    assert "ourOnGoneWrapper" in html
    assert "window.rogmemOnGraphGone === ourOnGoneWrapper" in html
    # destroy 必须真的调所有 restoreFns 并完整清理
    destroy_region = html.split("function destroy(reason)", 1)[1][:1500]
    assert "restoreFns[" in destroy_region or "restoreFns.length" in destroy_region
    assert "snapshot.destroy()" in destroy_region
    assert "tracker.destroy()" in destroy_region
    assert "layer.remove()" in destroy_region
    assert "removeEventListener" in destroy_region
    assert "host.rogmemAutoLabelsAttached = false" in destroy_region


def test_selector_over_budget_uses_full_destroy():
    """selector 熔断必须走完整 destroy（恢复回调、解 listeners、unwrap onGraphGone），
    不能只 tracker.destroy + layer.remove。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # 找到 over-budget 分支
    over = html.split("consecutiveOverBudget >= BUDGET_CONSECUTIVE_BREAK", 1)[1][:800]
    assert "destroy('selector-over-budget')" in over, \
        "selector 熔断必须调 destroy() 统一清理"
    # 不得只 tracker.destroy + layer.remove 结束
    assert "tracker.destroy()\n          if (layer.parentNode) layer.remove()" not in over


def test_snapshot_refresh_has_dirty_queue():
    """R1 修复：snapshot.refresh 必须支持 generation-aware coalesce；
    inflight 期间的新调用不复用 inflight（它可能过期），而是登记 dirty + 返回"下一轮" Promise。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    body = html.split("snapshot.refresh = function", 1)[1][:500]
    # 旧的 "if (inflight) return inflight" 是 R1 bug 核心
    assert "return inflight;" not in body, "inflight 期间不得直接返回 inflight（可能过期）"
    # 必须有 dirty / nextWaiters 之类的 queue 机制
    assert "dirty" in body or "nextWaiters" in body
    assert "startRefresh" in html
    # destroy 必须 flush waiters 防止悬挂
    sd = html.split("snapshot.destroy = function", 1)[1][:300]
    assert "refreshDestroyed = true" in sd
    assert "flushWaiters" in sd


def test_no_250ms_polling_main_path():
    """不靠 250ms setInterval 轮询，rogmemPaint 建完就 window.rogmemOnGraphReady(host) 单播。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    assert "window.rogmemOnGraphReady = function" in html
    # rogmemPaint 必须真正 call 它（关态下 rogmemPaint 存在，所以这个 call 存在于通用部分）
    assert "window.rogmemOnGraphReady(el)" in html, "rogmemPaint 必须单播通知"
    # setInterval 轮询不能是主路径；若仍存在只作为"晚加载"兜底，不得是每 250ms 检查 rogmemReady。
    assert "setInterval" not in html.split("rogmemAutoLabels", 1)[1], \
        "attach IIFE 内不再使用 setInterval 轮询（用 rogmemOnGraphReady 单播 + 一次性 findMount 兜底）"


def test_snapshot_gated_by_sim_ended():
    """snapshot.refresh 的首次触发必须等 rogmemSimEnded === true 或 chainConfig 的 onSimulationEnd。
    不得在 attach 结束时无条件 snapshot.refresh（这是 PR110 的 bug，会把移动中的坐标标 ready）。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # attach 尾部不得出现无条件 snapshot.refresh() 调用
    attach_tail = html.split("// ——— 启动 ———", 1)[1][:800]
    assert "host.rogmemSimEnded === true" in attach_tail
    assert "onSimEndedTrigger()" in attach_tail
    # rogmemPaint 必须把 el.rogmemSimEnded 的 true 写在 onSimulationEnd 入口（在 framed 检查之前）
    sim_end = html.split("onSimulationEnd: function () {", 1)[1][:400]
    first_lines = sim_end.split("\n")[:5]
    joined = " ".join(first_lines)
    assert "rogmemSimEnded = true" in joined, "rogmemPaint 的 onSimulationEnd 必须先把 rogmemSimEnded 置 true"


def test_hover_not_hidden_preemptively():
    """rogmemOnHover 不得无条件 host.rogmemHoverTip.hidden = true；只在 pool 实际展示同一 nodeIdx 时才隐。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    hover_block = html.split("host.rogmemOnHover = function (pointIndex) {", 1)[1][:600]
    assert "rogmemHoverTip.hidden = true" not in hover_block, \
        "rogmemOnHover 不得无条件隐 #graph-tip（P1 hover 回归修复）"
    # 真正隐藏必须由 dedupHoverTipIfInPool 做（检查 pool 命中）
    assert "function dedupHoverTipIfInPool" in html
    dedup = html.split("function dedupHoverTipIfInPool", 1)[1][:600]
    assert "placements[i].nodeIdx === hoverNode" in dedup
    assert "rogmemHoverTip.hidden = true" in dedup


def test_focus_feeds_selector_and_counts_budget():
    """focus 选中必须纳入 Selector 预算且去重（否则 PR 的'含 hover/focus 总预算'只是文案）。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # Selector.select 签名含 focusNodeIdx
    sel = html.split("function select(graph, snapshot, W, H, budget, hoverNodeIdx, focusNodeIdx)", 1)
    assert len(sel) == 2, "Selector.select 必须接受 focusNodeIdx 参数"
    # focus 的 tryPlace 真正被调（且是去重的）
    assert "tryPlace(focusNodeIdx, false, true)" in html
    # runSelectOnce 必须把 focus 传给 selector
    run = html.split("function runSelectOnce()", 1)[1][:1200]
    assert "rogmemGetFocusNode" in run
    assert "hoverNode, focusNode" in run


def test_degrade_does_not_sessionStorage_persist():
    """P2 需求：低性能降级只影响本页，不 sessionStorage 跨刷新持续禁用。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    assert "sessionStorage" not in html, "不得再 sessionStorage 持久化降级状态"
    assert "rogmem-graph-auto-labels-degraded" not in html


def test_tick_duration_ring_bounded():
    """tickDurationsMs 不得无界 push；生产每帧写，会话长了内存无界。改有界 ring。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    assert "TICK_RING_SIZE" in html
    push = html.split("function pushTick", 1)[1][:400]
    assert "TICK_RING_SIZE" in push
    assert "splice" in push, "pushTick 必须截断到 TICK_RING_SIZE"


def test_budget_window_not_called_p95():
    """BUDGET_TICK_WINDOW 之前被注释成 P95，但实际是'连续 N 帧'。诚实命名。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # 必须有连续超预算计数（新名 BUDGET_TICK_OVERBUDGET_RUN）
    assert "BUDGET_TICK_OVERBUDGET_RUN" in html
    # 旧的歧义 P95 用法不得保留在注释里
    assert "P95" not in html


def test_tracker_destroyed_prevents_revival():
    """destroy 后 beginFollow 不得复活 rAF（僵尸循环）。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # beginFollow 入口必须先检查 destroyed
    bf = html.split("beginFollow: function () {", 1)[1][:200]
    assert "destroyed" in bf
    tick = html.split("function tick() {", 1)[1][:500]
    assert "if (destroyed) return" in tick


def test_switch_on_exposes_sizes_from_paint():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # rogmemPaint 必须暴露 sizes 给 label 层。
    assert "sizes: sizes" in html or "sizes:sizes" in html


def test_switch_on_budget_is_injected_not_hardcoded():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=17, renderer="cosmos")
    assert "GRAPH_LABEL_BUDGET = 17" in html
    # 旧的硬编 50 常量赋值不得再在自动标签 IIFE 里出现。
    assert "LABEL_BUDGET = 50" not in html
    # 确认只出现"作为注入"的那一次。
    assert html.count("GRAPH_LABEL_BUDGET = ") == 1


def test_tracker_tick_body_forbids_full_graph_ops():
    """CameraTracker.tick 内部不得调用 findPointsInRect / getPointPositionsAsync / sort / setConfig。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    tracker = html.split("function createCameraTracker", 1)[1]
    # 切出 tick 函数体（粗略：直到下一次出现 "function " 定义）
    tick_start = tracker.find("function tick(")
    assert tick_start >= 0, "CameraTracker 必须有 tick 函数"
    tick_body = tracker[tick_start : tick_start + 2000]
    forbidden = ("findPointsInRect", "getPointPositionsAsync", "getPointPositionsArray", ".sort(", "setConfig")
    for needle in forbidden:
        assert needle not in tick_body, f"tick 内不得出现 {needle!r}（§2.4 不变量）"


def test_selector_select_can_use_findPointsInRect_and_spaceToScreen():
    """Selector 允许用 findPointsInRect + spaceToScreenPosition（事件触发期）。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    selector = html.split("function createSelector", 1)[1]
    sel_body = selector[: selector.find("function createCameraTracker")]
    assert "findPointsInRect" in sel_body
    assert "spaceToScreenPosition" in sel_body


def test_snapshot_uses_async_with_generation_token():
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    snap = html.split("function createSnapshotLifecycle", 1)[1]
    snap_body = snap[: snap.find("function createSelector")]
    assert "getPointPositionsAsync" in snap_body
    assert "generation" in snap_body
    # 失败路径不得自动走同步 fallback：
    assert "getPointPositionsArray" not in snap_body


def test_page_seed_is_fresh_per_page_not_derived_only_from_id():
    """pageSeed 必须每页独立；不应仅依 id 生成."""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    assert "crypto.getRandomValues" in html


def test_admin_graph_route_wires_budget(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "1", "graph_label_budget": "33"})
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    html = client.get("/admin/graph").text
    assert "GRAPH_LABEL_BUDGET = 33" in html


def test_graph_page_accepts_label_budget_kwarg():
    from rogmem.ui import graph_page

    on = graph_page(auto_labels=True, label_budget=7, renderer="cosmos")
    assert "GRAPH_LABEL_BUDGET = 7" in on


def test_hover_dedup_is_pool_hit_gated():
    """P1 hover 回归修正后：#graph-tip 只在 pool 真展示同一 nodeIdx 时才隐；snapshot 未 ready /
    zoom<=Zoff / 边缘 tryPlace 失败等任意"pool 不显示"场景必须保留原 Cosmos tip。"""
    from rogmem.ui import graph_mount

    html = graph_mount("graph", focus=True, auto_labels=True, label_budget=50, renderer="cosmos")
    # 1) rogmemOnHover 本身不得无条件隐 tip
    hover_block = html.split("host.rogmemOnHover = function", 1)[1][:600]
    assert "rogmemHoverTip.hidden = true" not in hover_block
    # 2) 必须存在 pool-命中-才隐的函数
    assert "function dedupHoverTipIfInPool" in html
    dedup = html.split("function dedupHoverTipIfInPool", 1)[1][:600]
    assert "placements[i].nodeIdx === hoverNode" in dedup
    assert "rogmemHoverTip.hidden = true" in dedup
    # 3) runSelectOnce 在 installPlacements 后调 dedupHoverTipIfInPool
    run = html.split("function runSelectOnce()", 1)[1][:1500]
    assert run.index("installPlacements") < run.index("dedupHoverTipIfInPool")
