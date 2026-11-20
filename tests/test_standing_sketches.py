"""P1-3: cross-page standing sketches (question → answer) for core."""

from __future__ import annotations

REPO = "https://github.com/example/memory"
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


def _put_owner_sketch(engine, text: str) -> None:
    from rogmem.crystal_store import CrystalStore, hydrate

    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
    crystal = hydrate(row)
    crystal.sketch_md = text
    CrystalStore(engine.db()).put(crystal, holder="test")


def _put_eng(engine, cid: str, name: str, repo: str, sketch: str) -> None:
    engine._insert_crystal_page(engine.db(), cid, "工程", name, holder="test")
    row = engine.db().execute("SELECT * FROM crystallizations WHERE id=?", (cid,)).fetchone()
    from rogmem.crystal_store import CrystalStore, hydrate

    crystal = hydrate(row)
    crystal.body_md = f"仓库 {repo}\n生产端口 18080，不得改端口，不得删除卷 rogmem-data。\n"
    crystal.sketch_md = sketch
    crystal.keys["仓库地址"] = repo
    CrystalStore(engine.db()).put(crystal, holder="test")
    engine.db().commit()


def _add_statement(engine, crystal_id: str, sid: str, text: str, quote: str) -> None:
    engine.db().execute(
        """INSERT INTO statements(
             id, crystal_id, field, chapter, text, quote, source_id, trust, rank, created_at
           ) VALUES(?,?, '约定','协作',?,?, 'src', '实','主', '2026-01-01T00:00:00+00:00')""",
        (sid, crystal_id, text, quote),
    )
    engine.db().commit()


def test_registry_seeds_and_core_reads_without_crystallizations(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import standing_sketches as ss

    rows = engine.db().execute("SELECT id, question FROM standing_sketches ORDER BY id").fetchall()
    ids = [r["id"] for r in rows]
    assert "owner-collaboration" in ids
    assert "engineering-invariants" in ids

    _put_owner_sketch(
        engine,
        "协作约定：交付须真机核验，不得伪造测试。\n\n别的履历段落没有协作字样。",
    )
    packed = engine.core_memory(budget=8000, project="")
    names = [item["name"] for item in packed["loaded"]]
    assert "记忆主人" in names
    assert "记忆主人协作约定" in names
    collab = next(item for item in packed["loaded"] if item["name"] == "记忆主人协作约定")
    assert "真机核验" in collab["text"]
    assert "来自「" in collab["text"]
    assert "跨页常驻速写" in packed["convention"]

    seen: list[str] = []
    engine.db().set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
    again = ss.pack_for_core(engine.db(), project="")
    engine.db().set_trace_callback(None)
    joined = "\n".join(seen).lower()
    assert "from crystallizations" not in joined
    assert "from statements" not in joined
    assert any(item["name"] == "记忆主人协作约定" for item in again)


def test_empty_without_citable_sources(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import standing_sketches as ss

    _put_owner_sketch(engine, "只有履历与早年经历。")
    ss.ensure_fresh(engine.db(), force=True, commit=True)
    ready = {item["id"]: item for item in ss.list_ready(engine.db())}
    assert ready["owner-collaboration"]["ready"] is False
    packed = engine.core_memory(budget=4000)
    assert "记忆主人协作约定" not in [item["name"] for item in packed["loaded"]]


def test_statement_quote_required(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import standing_sketches as ss

    _put_owner_sketch(engine, "无关。")
    _add_statement(engine, "owner", "s1", "不得伪造测试结果。", "不得伪造测试结果。")
    _add_statement(engine, "owner", "s2", "无引文的胡说约定", "")
    ss.refresh_one(engine.db(), "owner-collaboration")
    engine.db().commit()
    item = next(i for i in ss.list_ready(engine.db()) if i["id"] == "owner-collaboration")
    assert item["ready"] is True
    assert "不得伪造测试结果" in item["answer_md"]
    assert "引文连续原文" in item["answer_md"]
    assert "无引文的胡说约定" not in item["answer_md"]


def test_project_overlay_is_cross_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import standing_sketches as ss

    _put_owner_sketch(
        engine,
        "生产端口 18080 不得改端口。\n\n协作约定：先核验再交付。",
    )
    _put_eng(
        engine,
        "eng",
        "云岚核忆",
        REPO,
        "不可改：卷 rogmem-data 与 rogmem-scan，项目 id 固定 rogmem。",
    )
    _put_eng(
        engine,
        "eng-other",
        "别的工程",
        OTHER,
        "不可改：别的端口 9999。",
    )
    ss.ensure_fresh(engine.db(), project=REPO, force=True, commit=True)
    overlay = ss.overlay_id("engineering-invariants", REPO)
    row = engine.db().execute(
        "SELECT answer_md, source_crystal_ids FROM standing_sketches WHERE id=?",
        (overlay,),
    ).fetchone()
    assert row is not None
    answer = row["answer_md"] or ""
    assert "18080" in answer
    assert "rogmem-data" in answer
    assert "9999" not in answer
    import json

    sources = json.loads(row["source_crystal_ids"])
    assert "owner" in sources
    assert "eng" in sources
    assert "eng-other" not in sources

    packed = engine.core_memory(budget=12000, project=REPO)
    names = [item["name"] for item in packed["loaded"]]
    assert names[:2] == ["记忆主人", "云岚核忆"]
    assert "当前工程不可改约束" in names
    assert "记忆主人协作约定" in names
    inv = next(item for item in packed["loaded"] if item["name"] == "当前工程不可改约束")
    assert "rogmem-data" in inv["text"]
    assert "9999" not in inv["text"]


def test_crystal_change_marks_and_refreshes(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import standing_sketches as ss

    _put_owner_sketch(engine, "协作约定：第一版。")
    first = engine.db().execute(
        "SELECT answer_md, stale FROM standing_sketches WHERE id='owner-collaboration'"
    ).fetchone()
    assert int(first["stale"]) == 0
    assert "第一版" in (first["answer_md"] or "")

    _put_owner_sketch(engine, "协作约定：第二版，禁止静默覆盖。")
    second = engine.db().execute(
        "SELECT answer_md, stale FROM standing_sketches WHERE id='owner-collaboration'"
    ).fetchone()
    assert int(second["stale"]) == 0
    assert "第二版" in (second["answer_md"] or "")
    assert "第一版" not in (second["answer_md"] or "")
    packed = ss.pack_for_core(engine.db())
    text = next(item["text"] for item in packed if item["name"] == "记忆主人协作约定")
    assert "第二版" in text


def test_assemble_cap_keeps_question(tmp_path, monkeypatch):
    from rogmem.standing_sketches import assemble_answer

    cites = [
        {
            "kind": "sketch",
            "crystal_id": "owner",
            "crystal_name": "记忆主人",
            "text": "协作" + ("长" * 5000),
        }
    ]
    text = assemble_answer("记忆主人协作约定", cites, cap=200)
    assert text.startswith("记忆主人协作约定")
    assert text.endswith("…")
    assert len(text) <= 200
