import pytest

# Expose the shared browser fixtures via conftest discovery only when Playwright is actually
# installed. Importing `tests.conftest_browser` eagerly would call `pytest.importorskip` at
# module level; if Playwright is missing that still succeeds but then any attempt to use the
# `browser` fixture fails with a confusing collection error. The conditional import keeps
# non-browser tests (test_cost.py / test_cost_filters_*.py) fully runnable without Playwright.
try:
    import playwright  # noqa: F401
except Exception:
    pytest_plugins = []
else:
    pytest_plugins = ["tests.conftest_browser"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "queue_ingested_at: 档内/穿插等待最久用 ingested_at，不是 qid",
    )
    config.addinivalue_line(
        "markers",
        "ws_ur: WS-UR unresolved 真落库（engine/distill 闭环）",
    )
    config.addinivalue_line(
        "markers",
        "ws_leg: WS-LEG 双 legacy 删除 + statements/edges 同事务",
    )
    config.addinivalue_line(
        "markers",
        "ws_apply: WS-APPLY apply-first 共享 helper + sidecar 同 try 回滚",
    )
    config.addinivalue_line(
        "markers",
        "ws_prod: WS-PROD Distiller/resim/worker 真接 apply_integrate 统一管线",
    )
    config.addinivalue_line(
        "markers",
        "live_resim: live MiniMax dual-resume re-sim (mock-backed; no network/secrets)",
    )
    config.addinivalue_line(
        "markers",
        "true_live: True MiniMax live green (requires ROGMEM_LIVE_LLM + MiniMax secrets; skip≠通过)",
    )
    config.addinivalue_line(
        "markers",
        "ws_flip: 翻写开关 — overview / 再裁决 / 别名 / 投影 / 开篇 / 章节清扫 瘦路径真落薄；关开关 fail-closed",
    )
    config.addinivalue_line(
        "markers",
        "thick_sweep_fail_gate: Knife B 厚读清扫 Fail 闸（AB-B-INV / READ / SEAM / VOICE / PROMPT）",
    )
    config.addinivalue_line(
        "markers",
        "ab79_fail_gate: A/#79 残留 Fail 闸 — facts evidence 保留 + identity=new id 回填",
    )
    config.addinivalue_line(
        "markers",
        "match0_fail_gate: Match=0 冷启 Fail 闸 — identity=new 正路径；reject/deferred 仅负路径；禁 extract 假消化",
    )
    config.addinivalue_line(
        "markers",
        "mid_fanout_fail_gate: ≥3 mid 全量 fan-out Fail 闸 — Draft×1 Integrate×N；丢 mid=Fail",
    )
    config.addinivalue_line(
        "markers",
        "ba_promote_fail_gate: 多候选全文比对 — 不拿摘要卡片认人",
    )
    config.addinivalue_line(
        "markers",
        "live_multi_entity_fail_gate: LIVE 多实体硬卡 — 真 MiniMax ≥2 mid Integrate 落薄；skip≠绿",
    )
    config.addinivalue_line(
        "markers",
        "maybe_deferred_fail_gate: maybe→deferred Fail 闸 — decision=deferred；零 active/ops；retries 有界",
    )
    config.addinivalue_line(
        "markers",
        "nomid_extract_fail_gate: 无 mid 假消化 Fail 闸 — Match≥1+空 draft 禁 extract；#80 unwritten 同构",
    )
    config.addinivalue_line(
        "markers",
        "partial_compensate_fail_gate: 先成后败 — 整批撤回 / 重试不双建 / 泵非 done",
    )
    config.addinivalue_line(
        "markers",
        "v54_slot_chain_fail_gate: v5.4 P1-3/P1-4 — 槽位 SUPERSEDE 史链 + source_id→session 可点回",
    )


@pytest.fixture
def write_switch_off(monkeypatch):
    """Kill DISTILLER_STORE_WRITE_ENABLED so fail-closed paths stay fail-closed."""
    monkeypatch.setattr("rogmem.crystal_store.DISTILLER_STORE_WRITE_ENABLED", False)
