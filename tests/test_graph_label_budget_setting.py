"""基础设置新增"同屏最多名称数"：默认 50、范围 1..200、校验、不传不动、跨连接存活。"""

from __future__ import annotations

import re


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_default_is_50_when_unset(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.graph_label_budget() == 50
    assert engine.basic_config()["graph_label_budget"] == "50"


def test_default_is_50_when_garbage_stored(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    for raw in ("", "0", "201", "3.14", "true", "false", "1e2", "-5", "abc", "  ", "99999"):
        engine.set_setting("graph_label_budget", raw)
        engine.db().commit()
        assert engine.graph_label_budget() == 50, f"坏值 {raw!r} 应回落 50"


def test_save_basic_accepts_boundary_and_mid_values(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    for n in (1, 2, 50, 100, 199, 200):
        ok = engine.save_basic({"sketch_char_limit": "3500", "graph_label_budget": str(n)})
        assert ok["ok"] is True, f"n={n} 应合法"
        assert engine.graph_label_budget() == n


def test_save_basic_rejects_illegal_values(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_basic({"sketch_char_limit": "3500", "graph_label_budget": "50"})
    assert engine.graph_label_budget() == 50
    for raw in ("", "0", "201", "1000", "3.14", "true", "false", "1e2", "-5", "abc", " ", "99999"):
        got = engine.save_basic({"sketch_char_limit": "3500", "graph_label_budget": raw})
        assert got.get("ok") is False, f"{raw!r} 应被拒"
        assert engine.graph_label_budget() == 50, f"{raw!r} 失败后 settings 不得改"


def test_save_basic_without_field_leaves_value_intact(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_basic({"sketch_char_limit": "3500", "graph_label_budget": "99"})
    engine.save_basic({"sketch_char_limit": "4000"})  # 不带 graph_label_budget
    assert engine.graph_label_budget() == 99


def test_basic_page_renders_number_input_with_min_max(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.ui import basic_page

    html = basic_page(engine.basic_config())
    tag_match = re.search(r'<input[^>]*name="graph_label_budget"[^>]*>', html)
    assert tag_match, "basic_page 必须含 graph_label_budget 数字输入"
    tag = tag_match.group(0)
    assert 'type="number"' in tag
    assert 'min="1"' in tag
    assert 'max="200"' in tag
    assert 'step="1"' in tag
    assert 'value="50"' in tag  # 默认


def test_basic_page_value_reflects_setting(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_basic({"sketch_char_limit": "3500", "graph_label_budget": "17"})
    from rogmem.ui import basic_page

    html = basic_page(engine.basic_config())
    tag_match = re.search(r'<input[^>]*name="graph_label_budget"[^>]*>', html)
    assert tag_match
    assert 'value="17"' in tag_match.group(0)


def test_basic_form_submit_posts_graph_label_budget(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.ui import basic_page

    html = basic_page(engine.basic_config())
    # 表单 onsubmit 的 payload 必须带上 graph_label_budget 字段。
    assert "graph_label_budget" in html
    assert "this.graph_label_budget" in html


def test_two_connects_preserve_value(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_basic({"sketch_char_limit": "3500", "graph_label_budget": "150"})
    engine._DB.close()
    engine._LOCAL.con = None
    engine._DB = None
    engine.connect(tmp_path, tmp_path / "scan")
    assert engine.graph_label_budget() == 150


def test_save_basic_accepts_graph_auto_labels_and_budget_together(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    ok = engine.save_basic(
        {"sketch_char_limit": "3500", "graph_auto_labels": "1", "graph_label_budget": "20"}
    )
    assert ok["ok"] is True
    assert engine.graph_auto_labels() is True
    assert engine.graph_label_budget() == 20
