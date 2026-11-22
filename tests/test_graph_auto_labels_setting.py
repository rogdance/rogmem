"""基础设置"图谱自动展示词条名"开关：默认开、可保存、校验、既有调用方不受影响。"""

from __future__ import annotations


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_default_is_on(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.graph_auto_labels() is True
    assert engine.basic_config()["graph_auto_labels"] == "1"


def test_save_basic_turns_it_on_and_off(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    ok = engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "1"})
    assert ok["ok"] is True
    assert engine.graph_auto_labels() is True
    assert engine.basic_config()["graph_auto_labels"] == "1"
    ok = engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "0"})
    assert ok["ok"] is True
    assert engine.graph_auto_labels() is False


def test_save_basic_accepts_on_off_true_false(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    for raw in ("on", "true", "1"):
        engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": raw})
        assert engine.graph_auto_labels() is True
    for raw in ("off", "false", "0", ""):
        engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": raw})
        assert engine.graph_auto_labels() is False


def test_save_basic_rejects_garbage(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "1"})
    assert engine.graph_auto_labels() is True
    bad = engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "maybe"})
    assert bad["ok"] is False
    # 上一轮已经写过 1，坏值被拒绝后不应把值改掉。
    assert engine.graph_auto_labels() is True


def test_save_basic_without_field_leaves_value_intact(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "1"})
    # 既有调用方不传 graph_auto_labels，不能被意外清零。
    engine.save_basic({"sketch_char_limit": "4000"})
    assert engine.graph_auto_labels() is True


def test_saving_the_switch_leaves_the_other_plates(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_basic({"sketch_char_limit": "3500", "owner_appellation": "甲"})
    ok = engine.save_basic({"graph_auto_labels": "1", "graph_label_budget": "12"})
    assert ok["ok"] is True
    assert engine.sketch_char_limit() == 3500
    assert engine.get_setting("owner_appellation") == "甲"
    assert engine.graph_auto_labels() is True
    assert engine.graph_label_budget() == 12


def test_basic_page_renders_checkbox(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.ui import basic_page

    html = basic_page(engine.basic_config())
    assert "自动展示词条名" in html
    assert "<h2>记忆星图视效</h2>" in html
    assert "<h2>图谱</h2>" not in html
    assert 'name="graph_auto_labels"' in html or "name='graph_auto_labels'" in html
    assert 'type="checkbox"' in html or "type='checkbox'" in html


def test_basic_page_checkbox_reflects_state(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.ui import basic_page

    off = basic_page(engine.basic_config())
    off_tag = _checkbox_tag(off)
    assert "checked" in off_tag, f"默认开态 checkbox 应带 checked: {off_tag!r}"
    engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "0"})
    shut = basic_page(engine.basic_config())
    shut_tag = _checkbox_tag(shut)
    assert "checked" not in shut_tag, f"关态 checkbox 不该带 checked: {shut_tag!r}"
    engine.save_basic({"sketch_char_limit": "3500", "graph_auto_labels": "1"})
    on = basic_page(engine.basic_config())
    on_tag = _checkbox_tag(on)
    assert "checked" in on_tag, f"开态 checkbox 应带 checked: {on_tag!r}"


def _checkbox_tag(html: str) -> str:
    import re

    match = re.search(r'<input[^>]*name="graph_auto_labels"[^>]*>', html)
    assert match, "没找到 graph_auto_labels input 标签"
    return match.group(0)
