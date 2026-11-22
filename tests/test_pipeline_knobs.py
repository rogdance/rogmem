"""萃取成本与质量的旋钮：管线设置里改，运行时按设置走。"""

from datetime import datetime, timedelta, timezone

from rogmem import distill_triage, pipeline_knobs, sketch_basis


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_registry_defaults_match_the_module_constants():
    from rogmem import engine, pages_md

    d = pipeline_knobs.DEFAULTS
    assert engine.MERGE_PARTIAL_FROM_CHARS == d["merge_partial_from_chars"]
    assert engine.PAGE_BATCH_MIN_DRAFTS == d["page_batch_min_drafts"]
    assert engine.MERGE_BUDGET_BASE_TOKENS == d["merge_budget_base_tokens"]
    assert pages_md.SOURCE_MARK_CAP == d["provenance_mark_cap"]
    assert distill_triage.TEMPLATE_MIN_USER_CHARS == d["triage_template_min_chars"]
    assert abs(sketch_basis.REWRITE_BELOW_SIMILARITY - d["sketch_rewrite_below_similarity"] / 100) < 1e-9
    assert len({k.key for k in pipeline_knobs.KNOBS}) == len(pipeline_knobs.KNOBS)
    groups = {g for g, _t, _n in pipeline_knobs.GROUPS}
    assert all(k.group in groups for k in pipeline_knobs.KNOBS)
    assert all(k.low <= k.default <= k.high for k in pipeline_knobs.KNOBS)


def test_value_falls_back_on_blank_broken_or_out_of_range():
    store = {"page_batch_min_drafts": "12", "merge_partial_from_chars": "abc", "sketch_stale_hours": "999999"}
    get = store.get
    assert pipeline_knobs.value(get, "page_batch_min_drafts") == 12
    assert pipeline_knobs.value(get, "merge_partial_from_chars") == 6000
    assert pipeline_knobs.value(get, "sketch_stale_hours") == 24
    assert pipeline_knobs.value(get, "page_batch") == 1


def test_save_refuses_bad_values_and_keeps_blank_ones(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    bad = engine.save_pipeline_knobs({"page_batch_min_drafts": "0"})
    assert bad["ok"] is False and "攒够几份就整理" in bad["error"]
    saved = engine.save_pipeline_knobs(
        {"page_batch_min_drafts": "5", "merge_partial_from_chars": "", "page_batch": False, "horizon_gate": True}
    )
    assert saved["ok"] is True
    assert engine.knob("page_batch_min_drafts") == 5
    assert engine.knob("merge_partial_from_chars") == 6000
    assert engine.knob("page_batch") == 0
    assert engine._page_batched("owner", "正文") is False
    assert engine.knob("horizon_gate") == 1


def test_runtime_reads_the_knobs(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine._merge_partial_by_default("c1", "甲" * 7000, "乙") is True
    engine.save_pipeline_knobs({"merge_partial_from_chars": "8000"})
    assert engine._merge_partial_by_default("c1", "甲" * 7000, "乙") is False
    engine.save_pipeline_knobs({"merge_budget_base_tokens": "4096", "merge_budget_tokens_per_char": "3"})
    assert engine._merge_budget("https://api.minimax.chat/v1", "MiniMax-M3.1-Flash", "旧" * 10, "新" * 5, partial=False) == 4096 + 3 * 15
    engine.save_pipeline_knobs({"page_batch_from_chars": "1000"})
    assert engine._page_batched("c1", "字" * 1500) is True


def test_sketch_and_triage_thresholds_are_parameters():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    body = "".join(f"第{i}行：灯塔的值守表每周一更新。\n" for i in range(60))
    kept = sketch_basis.basis(body, now - timedelta(hours=2))
    small = body + "补一句：值守表改到周二。\n"
    assert sketch_basis.needs_rewrite(small, kept, has_sketch=True, now=now)[0] is False
    assert sketch_basis.needs_rewrite(small, kept, has_sketch=True, now=now, stale_after=timedelta(hours=1))[0] is True
    assert sketch_basis.needs_rewrite(small, kept, has_sketch=True, now=now, below=1.0)[0] is True
    assert distill_triage.template_key("字" * 150) == ""
    assert distill_triage.template_key("字" * 150, min_chars=100)


def test_pipeline_page_shows_the_knob_panel(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import ui

    html = ui.pipeline_page([], {}, {}, engine.pipeline_config(), engine.pipeline_knobs_view())
    assert "萃取成本与质量" in html and "/admin/api/settings/knobs" in html
    for title, *_ in [(t,) for _g, t, _n in pipeline_knobs.GROUPS]:
        assert title in html
    assert "name='page_batch_min_drafts'" in html and "name='page_batch'" in html
