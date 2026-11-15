"""Opening packet: sketches, then recent excerpts, then an older page."""

from __future__ import annotations

from pathlib import Path

REPO = "https://github.com/yunlan/rogmem"
OTHER = "https://github.com/other/repo"


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _project(engine):
    engine.db().execute("UPDATE crystallizations SET sketch_md=? WHERE id='owner'", ("主人速写。",))
    engine._insert_crystal_page(engine.db(), "eng", "工程", "云岚核忆", holder="test")
    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='eng'").fetchone()
    from rogmem.crystal_store import CrystalStore, hydrate

    crystal = hydrate(row)
    crystal.body_md = f"仓库 {REPO}\n"
    crystal.sketch_md = "工程速写。"
    crystal.keys["仓库地址"] = REPO
    CrystalStore(engine.db()).put(crystal, holder="test")
    engine.db().commit()


def _turn(engine, sid, kind, body, event_at, project=REPO, device="dev-a"):
    engine.db().execute(
        """INSERT INTO sources(
             id,kind,device_id,project_ref,event_at,ingested_at,content_hash,body,status,lane,pair_ready
           ) VALUES(?,?,?,?,?,?,?,?,'已蒸馏','fast',1)""",
        (sid, kind, device, project, event_at, event_at, f"hash-{sid}", body),
    )


def test_recent_excerpts_follow_sketches_and_leave_other_material_out(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.CORE_BUDGET_DEFAULT == 15000
    _project(engine)
    _turn(engine, "new", "user", "这是较新的用户原话，应该完整留下。", "2026-02-02T00:00:00+00:00", device="dev-b")
    _turn(engine, "old", "user", "这是更早的一句。", "2026-01-01T00:00:00+00:00", device="dev-a")
    _turn(engine, "reply", "agent", "回" * 500, "2026-02-01T00:00:00+00:00")
    _turn(engine, "other", "user", "别的仓库的话。", "2026-03-01T00:00:00+00:00", project=OTHER)
    _turn(engine, "loose", "user", "没有仓库标记的一句。", "2026-03-03T00:00:00+00:00", project="")
    _turn(engine, "file", "file", "文件正文不该出现。", "2026-03-02T00:00:00+00:00")
    _turn(engine, "secret", "user", "钥匙是 sk-abc123DEF456 不要读出来。", "2026-01-15T00:00:00+00:00")
    engine.db().commit()

    bare = engine.core_memory()
    assert bare["loaded"][0]["name"] == "记忆主人"
    assert "云岚核忆" not in [item["name"] for item in bare["loaded"]]
    assert "没有工程速写" in bare["convention"]
    assert "不按仓库切开" in bare["convention"]
    bare_excerpts = [item for item in bare["loaded"] if item["name"] == "最近摘录"]
    assert bare_excerpts[0]["text"].startswith("用户：没有仓库标记的一句")
    assert bare_excerpts[0]["project"] == ""
    assert any(item["project"] == OTHER and item["text"].startswith("用户：别的仓库的话") for item in bare_excerpts)

    packed = engine.core_memory(project=REPO)
    names = [item["name"] for item in packed["loaded"]]
    assert names[:2] == ["记忆主人", "云岚核忆"]
    excerpts = [item for item in packed["loaded"] if item["name"] == "最近摘录"]
    assert excerpts[0]["text"].startswith("用户：没有仓库标记的一句")
    assert excerpts[0]["project"] == ""
    assert any(item["project"] == OTHER for item in excerpts)
    texts = [item["text"] for item in excerpts]
    assert any(item.startswith("用户：这是较新的用户原话") and "（此条已截断）" not in item for item in texts)
    assert any(item.startswith("回复：") and "（此条已截断）" in item and "回" * 201 not in item for item in texts)
    assert any("这是更早的一句" in item for item in texts)
    assert any("«令牌»" in item for item in texts)
    joined = "\n".join(texts)
    assert "sk-abc123DEF456" not in joined
    assert "文件正文不该出现" not in joined
    assert packed["rules_rev"] == 1
    assert packed["rules_block"] is None
    assert "历史包" in packed["convention"]
    assert packed["llm"] is False


def test_recent_pack_reads_the_budget_not_the_archive(tmp_path, monkeypatch):
    """The character budget is the read bound. Older transcripts stay on disk."""
    engine = _open(tmp_path, monkeypatch)
    for index in range(40):
        _turn(
            engine,
            f"old-{index:02d}",
            "agent",
            "旧" * 4000,
            f"2020-01-01T00:{index:02d}:00+00:00",
        )
    _turn(engine, "new", "user", "新的一句。", "2026-02-02T00:00:00+00:00")
    engine.db().commit()
    plan = " ".join(
        str(row[-1])
        for row in engine.db().execute(
            """EXPLAIN QUERY PLAN
               SELECT id, kind, event_at, project_ref, excerpt
               FROM source_openings
               WHERE kind IN ('user','agent')
               ORDER BY event_at DESC, id DESC
               LIMIT 32"""
        )
    )
    assert "source_openings_recent" in plan
    seen: list[str] = []
    engine.db().set_trace_callback(seen.append)
    loaded, truncated, _ = engine._pack_recent(80, "", "")
    engine.db().set_trace_callback(None)
    assert any("新的一句" in item["text"] for item in loaded)
    assert truncated is True
    joined = "\n".join(seen).lower()
    assert "from sources" not in joined
    assert "substr(" not in joined


def test_continuation_returns_only_older_excerpts(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _project(engine)
    _turn(engine, "new", "user", "较新的一句要被预算截断。", "2026-02-02T00:00:00+00:00")
    _turn(engine, "old", "user", "更早的一句在下一页。", "2026-01-01T00:00:00+00:00")
    engine.db().commit()
    owner = "主人速写。"
    project = "工程速写。"
    budget = len(owner) + len(project) + len("用户：") + len("（此条已截断）") + 2
    packed = engine.core_memory(budget=budget, project=REPO)
    excerpts = [item for item in packed["loaded"] if item["name"] == "最近摘录"]
    assert len(excerpts) == 1
    assert "（此条已截断）" in excerpts[0]["text"]
    assert packed["truncated"] is True
    assert packed["continuation"]["before"]
    assert "记忆主人" in [item["name"] for item in packed["loaded"]]

    older = engine.core_memory(project=REPO, before=packed["continuation"]["before"])
    assert [item["name"] for item in older["loaded"]] == ["最近摘录"]
    assert "更早的一句在下一页" in older["loaded"][0]["text"]
    assert "主人速写" not in older["loaded"][0]["text"]
    assert "更早的摘录" in older["convention"]
    assert "记忆主人" not in [item["name"] for item in older["loaded"]]


def test_global_rules_cover_every_installed_host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library" / "Application Support" / "Cursor").mkdir(parents=True)
    (home / ".hermes").mkdir()
    (home / ".codex").mkdir()
    (home / ".workbuddy").mkdir()
    (home / ".hermes" / "MEMORY.md").write_text("原记忆\n", encoding="utf-8")
    (home / ".hermes" / "USER.md").write_text("原用户\n", encoding="utf-8")
    (home / ".codex" / "AGENTS.md").write_text("保留我的说明\n", encoding="utf-8")
    from installer.rogmem_installer import install_global_rules, write_configs

    first = install_global_rules(home)
    assert first == ["Cursor", "Hermes", "Codex", "WorkBuddy"]
    cursor = (home / ".cursor" / "rules" / "rogmem.mdc").read_text(encoding="utf-8")
    hermes = (home / ".hermes" / "rogmem-rule.md").read_text(encoding="utf-8")
    work = (home / ".workbuddy" / "rules" / "rogmem.mdc").read_text(encoding="utf-8")
    codex = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert "alwaysApply: true" in cursor and "必须立刻调用 core" in cursor
    assert "MEMORY.md" in hermes and "必须立刻调用 core" in hermes
    assert "压缩摘要或本地笔记" in work
    assert codex.startswith("保留我的说明")
    assert "rogmem-rule:start" in codex and "必须立刻调用 core" in codex
    install_global_rules(home)
    again = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert again.count("rogmem-rule:start") == 1
    assert again.startswith("保留我的说明")
    assert (home / ".hermes" / "MEMORY.md").read_text(encoding="utf-8") == "原记忆\n"
    assert (home / ".hermes" / "USER.md").read_text(encoding="utf-8") == "原用户\n"

    written = write_configs(home, "http://127.0.0.1:18780", programs=["Cursor"])
    assert written == ["Cursor"]
    assert (home / ".hermes" / "rogmem-rule.md").is_file()

    from installer.rogmem_installer import install_global_rules as install

    assert install(Path.home()) == []


def test_hermes_prefetch_asks_for_the_packet_once_per_session(tmp_path):
    from rogmem.hermes_memory import RogmemMemoryProvider

    provider = RogmemMemoryProvider()
    provider._home = tmp_path
    calls = []

    def fake(name, arguments):
        calls.append((name, arguments))
        return {"result": {"content": [{"type": "text", "text": "历史包"}]}}

    provider._call_tool = fake
    assert provider.prefetch("第一问", session_id="s1") == "历史包"
    assert provider.prefetch("还在这场", session_id="s1") == "历史包"
    assert calls == [("core", {})]
    assert provider.prefetch("新的一场", session_id="s2") == "历史包"
    assert [name for name, _args in calls] == ["core", "core"]
    names = [item["name"] for item in provider.get_tool_schemas()]
    assert names == ["core", "search"]


def test_hermes_prompt_block_reads_the_written_rule(tmp_path):
    from rogmem.hermes_memory import RogmemMemoryProvider

    provider = RogmemMemoryProvider()
    provider._home = tmp_path
    builtin = provider.system_prompt_block()
    assert "MEMORY.md" in builtin and "必须立刻调用 core" in builtin
    (tmp_path / "rogmem-rule.md").write_text("来自文件的规则\n", encoding="utf-8")
    assert provider.system_prompt_block() == "来自文件的规则"


def test_a_new_session_uses_the_configured_core_budget(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.core_budget() == 15000
    assert engine.core_budget_for(0) == 15000
    assert engine.core_budget_for(4000) == 4000
    saved = engine.save_basic({"sketch_char_limit": "3500", "core_budget": "8000"})
    assert saved["ok"] is True
    assert engine.core_budget_for(0) == 8000
    assert engine.core_memory.__defaults__[0] == 15000
    refused = engine.save_basic({"sketch_char_limit": "3500", "core_budget": "12"})
    assert refused["ok"] is False
    assert engine.core_budget() == 8000
    store = engine.memory_store()
    assert store["path"].endswith("rogmem.sqlite")
    assert store["bytes"] > 0
    from rogmem.ui import basic_page

    page = basic_page(engine.basic_config())
    assert "新会话核心记忆预算" in page
    assert "主人称呼" in page
    assert 'name="owner_appellation"' in page
    assert 'name="core_budget" value="8000"' in page
    assert "readonly" in page
    assert store["path"] in page
    assert 'name="store_path"' not in page

def test_a_large_turn_is_not_opened_from_the_archive(tmp_path, monkeypatch):
    """Without the stored opening, core does not touch the archive. Backfill writes that opening off the request."""
    engine = _open(tmp_path, monkeypatch)
    _turn(engine, "huge", "user", "巨" * 1_500_000, "2026-02-02T00:00:00+00:00")
    engine.db().execute("DELETE FROM source_openings WHERE id='huge'")
    engine.db().commit()
    seen: list[str] = []
    engine.db().set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
    loaded, _, _ = engine._pack_recent(80, "", "")
    engine.db().set_trace_callback(None)
    joined = "\n".join(seen).lower()
    assert "from sources" not in joined
    assert "substr(" not in joined
    assert not any("巨" in item["text"] for item in loaded)
    assert engine.backfill_openings(limit=1) == 1
    seen.clear()
    engine.db().set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
    loaded, truncated, _ = engine._pack_recent(80, "", "")
    engine.db().set_trace_callback(None)
    assert any(item["text"].startswith("用户：巨") for item in loaded)
    assert truncated is True
    assert "from sources" not in "\n".join(seen).lower()


def test_health_snapshot_does_not_open_the_database(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "db", lambda: (_ for _ in ()).throw(AssertionError("health opened the database")))
    snap = engine.health_snapshot()
    assert snap["ok"] is True
    assert snap["service"] == "rogmem"
    assert snap["version"]


def test_opening_does_not_read_thick_leftover_columns(tmp_path, monkeypatch):
    """A history packet hydrates the thin page. chapters_json and summary_json stay on disk."""
    engine = _open(tmp_path, monkeypatch)
    thick = "厚" * 1_500_000
    engine.db().execute(
        "UPDATE crystallizations SET chapters_json=?, summary_json=?, sketch_md=? WHERE id='owner'",
        (thick, thick, "主人速写。"),
    )
    engine.db().commit()
    seen: list[str] = []
    engine.db().set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
    packed = engine.core_memory(200, "")
    engine.db().set_trace_callback(None)
    joined = "\n".join(seen).lower()
    assert "crystallizations" not in joined
    assert "chapters_json" not in joined
    assert "sketch_md" not in joined
    assert "body_md" not in joined
    assert "主人速写" in str(packed)
