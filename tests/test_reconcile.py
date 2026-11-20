"""Scheduled fold of crystals opened in the same window."""

from pathlib import Path

from rogmem.crystal_store import CrystalStore, ThinCrystal
from rogmem.ui import basic_page, models_page, pipeline_page


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _put(engine, cid: str, ctype: str, name: str, aliases: list[str] | None = None) -> None:
    CrystalStore(engine.db()).put(
        ThinCrystal(id=cid, type=ctype, name=name, status="active", revision=0, aliases=aliases or []),
        commit=True,
    )


def _status(engine, cid: str) -> str:
    row = engine.db().execute("SELECT status FROM crystallizations WHERE id=?", (cid,)).fetchone()
    return row["status"]


def test_same_label_pages_fold_into_one(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "aa-keep", "组织", "北城制造")
    _put(engine, "zz-drop", "组织", "北城制造")
    _put(engine, "fold-person", "人物", "北城制造")
    result = engine.reconcile_new_crystals()
    assert result["folded"] == 1
    assert _status(engine, "aa-keep") == "active"
    assert _status(engine, "zz-drop") == "已合并"
    assert _status(engine, "fold-person") == "active"
    assert _status(engine, "owner") == "active"
    again = engine.reconcile_new_crystals()
    assert again["folded"] == 0
    from rogmem.version import product_version

    engine.set_setting("reconcile_build", product_version())
    engine.db().commit()
    assert engine.run_scheduled_reconcile() is None


def test_alias_overlap_folds(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "alias-early", "组织", "北城厂", ["北城制造"])
    _put(engine, "alias-late", "组织", "北城制造")
    engine.reconcile_new_crystals()
    statuses = {_status(engine, "alias-early"), _status(engine, "alias-late")}
    assert statuses == {"active", "已合并"}


def test_denied_pair_stays_split(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "deny-aa", "组织", "北城制造")
    _put(engine, "deny-zz", "组织", "北城制造")
    engine.db().execute(
        "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
        (engine.utcnow(), "不同实体", '{"a":"deny-aa","b":"deny-zz"}'),
    )
    engine.db().commit()
    result = engine.reconcile_new_crystals()
    assert result["folded"] == 0
    assert _status(engine, "deny-aa") == "active"
    assert _status(engine, "deny-zz") == "active"


def test_a_shared_prefix_does_not_merge_without_vectors(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "pre-short", "组织", "北城厂")
    _put(engine, "pre-long", "组织", "北城厂分部")
    monkeypatch.setattr(engine, "_judge_model", lambda role: ("http://example.test", "k", "m"))
    monkeypatch.setattr(engine, "_ask_same_entity", lambda *args, **kwargs: "是")
    result = engine.reconcile_new_crystals()
    assert result["asked"] == 0
    assert _status(engine, "pre-short") == "active"
    assert _status(engine, "pre-long") == "active"


def test_crystal_vectors_are_reused_for_the_same_text(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "aa-han", "产品", "北城")
    calls = {"n": 0}

    def embed_many(texts):
        calls["n"] += 1
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(engine, "_EMBED", "fastembed")
    monkeypatch.setattr(engine, "_MODEL", object())
    monkeypatch.setattr(engine, "_embed_many", embed_many)
    engine._CRYSTAL_VEC_CACHE.clear()
    first = engine._crystal_vector_rows()
    second = engine._crystal_vector_rows()
    assert calls["n"] == 1
    assert {item["id"] for item in first} == {item["id"] for item in second}
    assert "aa-han" in {item["id"] for item in first}


def test_scheduled_reconcile_waits_for_the_embedder(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    result = engine.run_scheduled_reconcile()
    assert engine.get_setting("reconcile_build") == ""
    assert (result or {}).get("asked", 0) == 0
    assert (result or {}).get("merged", 0) == 0


def test_scheduled_fold_same_name_without_embedder(tmp_path, monkeypatch):
    """Identical written names are a program check. They do not wait on vectors."""
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "dock-keep", "工程", "青渚船坞")
    _put(engine, "dock-new", "工程", "青渚船坞")
    result = engine.run_scheduled_reconcile()
    assert result["folded"] == 1
    assert engine.get_setting("reconcile_build") == ""
    assert {_status(engine, "dock-keep"), _status(engine, "dock-new")} == {"active", "已合并"}


def test_pipeline_interval_defaults_and_rejects_zero(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.pipeline_config()["reconcile_hours"] == "3"
    assert engine.reconcile_due() is True
    saved = engine.save_pipeline(
        {"scan_interval": "90", "probe_interval": "600", "reconcile_hours": "3", "sweep_at": "04:15"}
    )
    assert saved["ok"] is True
    assert engine.get_setting("probe_interval") == "600"
    assert engine.probe_interval_seconds() == 600
    refused = engine.save_pipeline(
        {"scan_interval": "90", "probe_interval": "600", "reconcile_hours": "0", "sweep_at": "04:15"}
    )
    assert refused["ok"] is False
    engine.reconcile_new_crystals()
    assert engine.reconcile_due() is False


def _near(engine, monkeypatch):
    def embed(text):
        if "北城" in text or "Beicheng" in text:
            return [1.0, 0.0, 0.0]
        if "青石" in text:
            return [0.0, 1.0, 0.0]
        if "Marbletown" in text:
            return [0.0, 0.0, 1.0]
        return [0.0, 0.0, 0.0]

    monkeypatch.setattr(engine, "_EMBED", "hash")
    monkeypatch.setattr(engine, "_embed_one", embed)


def test_different_spellings_stay_apart_until_vectors_say_they_are_close(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "aa-han", "产品", "北城")
    _put(engine, "zz-lat", "产品", "Beicheng")
    result = engine.reconcile_new_crystals()
    assert result["folded"] == 0
    assert result["asked"] == 0
    assert _status(engine, "aa-han") == "active"
    assert _status(engine, "zz-lat") == "active"


def test_vector_neighbor_is_merged_only_when_the_model_says_yes(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "aa-han", "产品", "北城")
    _put(engine, "zz-lat", "产品", "Beicheng")
    _near(engine, monkeypatch)
    monkeypatch.setattr(engine, "_judge_model", lambda role: ("http://example.test", "k", "m"))
    monkeypatch.setattr(engine, "_ask_same_entity", lambda *args, **kwargs: "是")
    result = engine.reconcile_new_crystals()
    assert result["asked"] == 1
    assert result["merged"] == 1
    assert {_status(engine, "aa-han"), _status(engine, "zz-lat")} == {"active", "已合并"}
    kept = "aa-han" if _status(engine, "aa-han") == "active" else "zz-lat"
    other = "Beicheng" if kept == "aa-han" else "北城"
    assert other in engine.crystal_detail(kept)["summary"]["别名"]


def test_distant_vectors_are_not_asked(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "aa-han", "组织", "青石厂")
    _put(engine, "zz-lat", "组织", "Marbletown")
    _near(engine, monkeypatch)

    def refuse(*args, **kwargs):
        raise AssertionError("远的一对不该问模型")

    monkeypatch.setattr(engine, "_judge_model", lambda role: ("http://example.test", "k", "m"))
    monkeypatch.setattr(engine, "_ask_same_entity", refuse)
    result = engine.reconcile_new_crystals()
    assert result["asked"] == 0
    assert _status(engine, "aa-han") == "active"
    assert _status(engine, "zz-lat") == "active"


def test_distill_match_ignores_a_close_vector_when_the_name_misses(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _put(engine, "aa-han", "产品", "北城")
    _near(engine, monkeypatch)
    hits = engine._match_integrate_parts("Beicheng shipped")
    ids = [item["id"] for item in hits.get("library_candidates") or []]
    assert "aa-han" not in ids


def test_pipeline_page_holds_workers_and_schedules():
    html = pipeline_page(
        [],
        {},
        {"source": "none", "model": ""},
        {"scan_interval": "60", "probe_interval": "300", "reconcile_hours": "3", "sweep_at": "03:00"},
    )
    assert "<title>ROGMEM · 管线设置</title>" in html
    assert 'class="brand"' in html and 'aria-label="ROGMEM"' in html
    assert "linearGradient id=\"rogmem-grad\"" in html
    assert "#FF3763" in html and "#FF7038" in html
    assert "header .brand svg" in html
    assert "管线设置" in html
    assert "工人设置" in html and "定时任务" in html
    assert "草稿" in html and "身份合并" in html
    assert "结晶润色" not in html
    assert "冲突裁决" not in html
    distill = html.split("草稿", 1)[1].split("身份合并", 1)[0]
    assert "模型选择" in distill and "推理强度" in distill and "class='worker-controls'" in distill
    embed = html.split("向量嵌入", 1)[1].split("定时任务", 1)[0]
    assert "模型选择" in embed and "推理强度" not in embed
    assert ".workers { display: flex; flex-direction: column; }" in html
    assert "每个工人单独设推理强度" not in html
    assert "词条整合" in html and 'name="reconcile_hours"' in html and "3" in html
    assert "世界时间" in html
    models = models_page([], {}, {"source": "none"})
    assert "name='model_distill'" not in models
    basic = basic_page({"scan_path": "/scan", "active_scan": "/scan", "file_types": "md", "turn_wait": "30"})
    assert "name=\"scan_interval\"" not in basic
    assert "管线设置" in basic
    assert "采集管理" not in basic and "采集设置" not in basic
    assert "<h2>配色模式</h2>" in basic
    assert 'data-theme-pick="light"' in basic and 'data-theme-pick="dark"' in basic
    assert "<h2>自动采集方案</h2>" in basic
    assert "<h2>记忆预算上限</h2>" in basic
    assert "<h2>记忆图谱视效</h2>" in basic
    assert '<p class="setting-name">定时扫描目录</p>' in basic
    assert '<p class="setting-name">支持文件格式</p>' in basic
    assert '<p class="setting-name">会话回合等待时长</p>' in basic
    assert 'aria-label="定时扫描目录"' in basic
    assert 'aria-label="会话回合等待时长"' in basic
    assert "<h2>采集</h2>" not in basic and "<h2>字数</h2>" not in basic and "<h2>图谱</h2>" not in basic
    assert '<p class="setting-name">扫描目录</p>' not in basic
    assert '<p class="setting-name">接受的文件类型</p>' not in basic
    assert '<p class="setting-name">回合等待</p>' not in basic
    assert 'aria-label="扫描目录"' not in basic and 'aria-label="回合等待"' not in basic
    assert "name='file_type' value='md' checked" in basic
    assert "name='file_type' value='pdf' checked" not in basic
    assert "内容哈希去重" not in basic and "设备显示名" not in basic
    assert 'name="turn_wait"' in basic and 'value="30"' in basic
    assert "默认全部勾选" not in basic and "不要用「本机」" not in basic
    assert "正在扫描" not in basic
    differ = basic_page({"scan_path": "/scan", "active_scan": "/data/scan", "file_types": "md", "turn_wait": "30"})
    assert "正在扫描 /data/scan" in differ
    from rogmem.engine import FILE_TYPES as stored_types
    from rogmem.ui import FILE_TYPES as shown_types

    assert stored_types == shown_types


def test_same_bytes_stay_one_file(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    first_path = tmp_path / "scan" / "a.txt"
    second_path = tmp_path / "scan" / "b.txt"
    first_path.parent.mkdir(parents=True, exist_ok=True)
    first_path.write_text("同一段正文，用来核对去重。", encoding="utf-8")
    second_path.write_text("同一段正文，用来核对去重。", encoding="utf-8")
    first = engine.ingest_file(first_path)
    again = engine.ingest_file(second_path)
    assert again["duplicate"] is True and again["id"] == first["id"]
    saved = engine.save_collect(
        {"scan_path": str(tmp_path / "scan"), "file_types": "txt,md", "turn_wait": "30", "hash_dedup": "0"}
    )
    assert saved["ok"] is True and "hash_dedup" not in saved["config"]
    assert engine.hash_dedup_enabled() is True
    third = engine.ingest_file(second_path)
    assert third.get("duplicate") is True and third["id"] == first["id"]
    page = basic_page(engine.basic_config())
    assert "内容哈希去重" not in page and 'name="hash_dedup"' not in page


def test_turn_wait_defaults_to_600(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.turn_wait_seconds() == 600
    page = basic_page({})
    assert 'name="turn_wait"' in page and 'value="600"' in page
    saved = engine.save_collect({"scan_path": str(tmp_path / "scan"), "file_types": "txt", "turn_wait": ""})
    assert saved["ok"] is True and saved["config"]["turn_wait"] == "600"
