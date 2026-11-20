"""段级注入的 ops 作用域护栏：只允许改本次注入过的既有小节。"""

from __future__ import annotations

from rogmem import engine

OLD_BODY = "## 隐私边界\n\n旧隐私\n\n## 打包与依赖边界\n\n旧打包\n\n## 兼容性调研结论\n\n旧兼容"


def _sectioned_pack():
    # 只注入了「隐私边界」，其余两段只出现在目录里。
    body = "## 隐私边界\n\n旧隐私\n\n## 其余小节（只给目录）\n\n- 打包与依赖边界\n- 兼容性调研结论"
    return {"id": "cry_1", "body_md": body, "body_injection": "sectioned"}


def test_ops_on_injected_section_kept():
    ops = [{"op": "update", "path": "body_md##隐私边界", "value": "新隐私"}]
    kept = engine._filter_ops_to_injected_sections(ops, [_sectioned_pack()], "cry_1", OLD_BODY, None)
    assert kept == ops


def test_ops_on_uninjected_existing_section_dropped():
    ops = [
        {"op": "update", "path": "body_md##隐私边界", "value": "新隐私"},
        {"op": "update", "path": "body_md##兼容性调研结论", "value": "偷改"},
    ]
    kept = engine._filter_ops_to_injected_sections(ops, [_sectioned_pack()], "cry_1", OLD_BODY, None)
    assert [op["path"] for op in kept] == ["body_md##隐私边界"]


def test_ops_on_new_section_kept():
    ops = [{"op": "add", "path": "body_md##全新小节", "value": "新的"}]
    kept = engine._filter_ops_to_injected_sections(ops, [_sectioned_pack()], "cry_1", OLD_BODY, None)
    assert kept == ops


def test_full_injection_is_not_filtered():
    pack = {"id": "cry_1", "body_md": OLD_BODY, "body_injection": "full"}
    ops = [{"op": "update", "path": "body_md##兼容性调研结论", "value": "改"}]
    kept = engine._filter_ops_to_injected_sections(ops, [pack], "cry_1", OLD_BODY, None)
    assert kept == ops


def test_other_candidate_id_is_not_filtered():
    ops = [{"op": "update", "path": "body_md##兼容性调研结论", "value": "改"}]
    kept = engine._filter_ops_to_injected_sections(ops, [_sectioned_pack()], "cry_other", OLD_BODY, None)
    assert kept == ops


def test_full_body_write_over_cap_dropped():
    ops = [
        {"op": "update", "path": "body_md", "value": "字" * 3000},
        {"op": "update", "path": "body_md##隐私边界", "value": "新隐私"},
    ]
    kept = engine._filter_ops_to_injected_sections(ops, [_sectioned_pack()], "cry_1", OLD_BODY, None)
    assert [op["path"] for op in kept] == ["body_md##隐私边界"]


def test_full_body_write_within_cap_kept():
    ops = [{"op": "update", "path": "body_md", "value": "整篇重排但不太长"}]
    kept = engine._filter_ops_to_injected_sections(ops, [_sectioned_pack()], "cry_1", OLD_BODY, None)
    assert kept == ops


def test_full_body_write_kept_when_not_sectioned():
    pack = {"id": "cry_1", "body_md": OLD_BODY, "body_injection": "full"}
    ops = [{"op": "update", "path": "body_md", "value": "字" * 99999}]
    kept = engine._filter_ops_to_injected_sections(ops, [pack], "cry_1", OLD_BODY, None)
    assert kept == ops


def test_ops_on_outline_container_dropped():
    # 「其余小节（只给目录）」是注入用的目录容器，不是可改小节：给它落 op 等于凭空多一节。
    body = "## 隐私边界\n\n旧隐私\n\n## 其余小节（只给目录）\n\n- 打包与依赖边界"
    pack = {"id": "cry_1", "body_md": body, "body_injection": "sectioned"}
    ops = [{"op": "add", "path": "body_md##其余小节（只给目录）", "value": "垃圾小节"}]
    kept = engine._filter_ops_to_injected_sections(ops, [pack], "cry_1", OLD_BODY, None)
    assert kept == []


def test_peel_section_scope_round_trips():
    pack = _sectioned_pack()
    raw = {"identity": "same", engine._SECTION_SCOPE: [pack]}
    cleaned, scope = engine._peel_section_scope(raw)
    assert engine._SECTION_SCOPE not in cleaned
    assert scope == [pack]
    assert engine._peel_section_scope({"identity": "same"}) == ({"identity": "same"}, None)


def test_produce_carries_the_injected_scope_to_the_landing_step(monkeypatch):
    """段级范围要随 payload 一路带到落库那一步；中间被丢掉，护栏就等于没跑。"""
    pack = _sectioned_pack()
    raw = {"identity": "same", "matched_id": "cry_1", "ops": []}
    raw[engine._SECTION_SCOPE] = [pack]

    monkeypatch.setattr(engine, "_align_project_candidates", lambda text, kw: kw)
    monkeypatch.setattr(engine, "_is_match_zero", lambda kw: False)
    monkeypatch.setattr(
        engine, "_with_approved_draft", lambda text, kind, **kw: dict(kw, draft_crystal={"name": "甲"})
    )
    monkeypatch.setattr(engine, "_complete_integrate_mid", lambda *a, **k: dict(raw))
    monkeypatch.setattr(engine, "_produce_integrate", lambda _raw: {"payload_kind": "integrate", "ops": []})

    out = engine._produce_distill_payload("正文", "file", draft_crystal={"name": "甲"})
    assert out.get(engine._SECTION_SCOPE) == [pack]


def test_apply_integrate_hands_the_scope_to_the_guard(tmp_path, monkeypatch):
    """落库时护栏要拿到「本次注入过的小节」，而不是空的 matched 列表——否则护栏不跑。"""
    import sqlite3
    from pathlib import Path

    from rogmem import distill_gate
    from rogmem.crystal_store import CrystalStore, ThinCrystal, migrate_crystal_store

    schema = (Path(engine.__file__).resolve().parents[1] / "rogmem" / "schema.sql").read_text(encoding="utf-8")
    con = sqlite3.connect(tmp_path / "c.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(schema)
    migrate_crystal_store(con)
    store = CrystalStore(con)
    store.put(ThinCrystal(id="cry_1", type="概念", name="甲", status="active", revision=0, body_md=OLD_BODY))
    store.acquire_lease("cry_1", "h")

    class _Accepted:
        decision = "accept"
        identity = "same"
        matched_id = "cry_1"
        payload = {"ops": [{"op": "update", "path": "body_md##隐私边界", "value": "新隐私"}]}
        issues = []

    monkeypatch.setattr(distill_gate, "evaluate_integrate", lambda *a, **k: _Accepted())
    monkeypatch.setattr(engine, "check_core_module_complete", lambda *a, **k: [])

    seen: list = []
    real = engine._filter_ops_to_injected_sections

    def spy(ops, candidates, target_id, existing_body, store):
        seen.append(candidates)
        return real(ops, candidates, target_id, existing_body, store)

    monkeypatch.setattr(engine, "_filter_ops_to_injected_sections", spy)

    payload = {"identity": "same", "matched_id": "cry_1", "ops": []}
    payload[engine._SECTION_SCOPE] = [_sectioned_pack()]
    try:
        engine.apply_integrate(payload, candidates=[], crystal_id="cry_1", holder="h", store=store, commit=True)
    except Exception:
        pass
    assert seen and seen[0] and seen[0][0].get("id") == "cry_1"


def test_guard_events_are_recorded_not_swallowed(tmp_path):
    """护栏的两条事件都要真的落库；事件 kind 不在白名单会被静默吞掉，观测就瞎了。"""
    import sqlite3
    from pathlib import Path

    from rogmem.crystal_store import EVENT_KINDS, CrystalStore, migrate_crystal_store

    assert "full_body_write_sectioned" in EVENT_KINDS  # 曾经不在白名单，record 抛错被吞
    schema = (Path(engine.__file__).resolve().parents[1] / "rogmem" / "schema.sql").read_text(encoding="utf-8")
    con = sqlite3.connect(tmp_path / "e.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(schema)
    migrate_crystal_store(con)
    store = CrystalStore(con)

    engine._filter_ops_to_injected_sections(
        [{"op": "update", "path": "body_md", "value": "整篇重排但不太长"}], [_sectioned_pack()], "cry_1", OLD_BODY, store
    )
    engine._filter_ops_to_injected_sections(
        [{"op": "update", "path": "body_md##兼容性调研结论", "value": "偷改"}], [_sectioned_pack()], "cry_1", OLD_BODY, store
    )
    kinds = {r["kind"] for r in con.execute("SELECT DISTINCT kind FROM crystal_events")}
    assert "full_body_write_sectioned" in kinds
    assert "gate_drop" in kinds




