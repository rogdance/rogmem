"""A crystal page is findable from the words on the page, without a claim row."""

from __future__ import annotations

from rogmem.crystal_store import CrystalStore, ThinCrystal


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_search_returns_a_page_that_has_no_statements(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    CrystalStore(engine.db()).put(
        ThinCrystal(
            id="lamp-page",
            type="概念",
            name="北港灯塔",
            status="active",
            revision=0,
            body_md="北港灯塔在雾季用 LIGHTHOUSEMARK 为过往船只指路。",
        )
    )
    found = engine.search("LIGHTHOUSEMARK", limit=5)
    hits = [item for item in found["crystals"] if item.get("crystal") == "北港灯塔"]
    assert hits, found
    assert "LIGHTHOUSEMARK" in hits[0]["text"]


def test_search_drops_a_page_when_its_wording_changes(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id="lamp-page",
            type="概念",
            name="北港灯塔",
            status="active",
            revision=0,
            body_md="旧词 OLDWORDMARK 只属于上一稿。",
        )
    )
    current = store.get("lamp-page")
    current.body_md = "新稿只保留 NEWWORDMARK。"
    store.put(current)
    found = engine.search("OLDWORDMARK", limit=5)
    assert all(item.get("crystal") != "北港灯塔" for item in found["crystals"])
    again = engine.search("NEWWORDMARK", limit=5)
    assert any(item.get("crystal") == "北港灯塔" for item in again["crystals"])


def test_read_section_uses_the_page_heading_not_a_preset_name(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        "UPDATE crystallizations SET body_md=? WHERE id='owner'",
        ("## 任职经过\n\n2018 年在甲公司任职。",),
    )
    engine.db().commit()
    missed = engine.read_section("记忆主人", "事业经历")
    assert missed["missing"] is True
    assert missed["sections"] == ["任职经过"]
    found = engine.read_section("记忆主人", "任职经过")
    assert found["missing"] is False
    assert found["text"] == "2018 年在甲公司任职。"


def test_read_chain_returns_earlier_wording_for_that_heading(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = engine.utcnow()
    engine.db().execute(
        "UPDATE crystallizations SET body_md=? WHERE id='owner'",
        ("## 口味\n\n现在吃素。",),
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,chapter,text,quote,source_id,trust,rank,event_at,created_at,invalid_at)
           VALUES('meat','owner','口味','喜欢吃肉','喜欢吃肉','src-meat','written','user','2026-05-01T00:00:00+00:00',?,?)""",
        (now, now),
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,chapter,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('veg','owner','口味','更喜欢吃素','更喜欢吃素','src-veg','written','user','2026-08-01T00:00:00+00:00',?)""",
        (now,),
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,statement_id,basis)
           VALUES('owner',?,'口味','veg','觉得自己胖了，不想再吃肉')""",
        (now,),
    )
    engine.db().commit()
    chain = engine.read_chain("记忆主人", "口味")
    assert [item["text"] for item in chain["rings"]] == ["喜欢吃肉", "更喜欢吃素"]
    assert chain["rings"][0]["current"] is False
    assert chain["rings"][0]["basis"] == ""
    assert chain["rings"][1]["current"] is True
    assert chain["rings"][1]["basis"] == "觉得自己胖了，不想再吃肉"
    assert chain["sections"] == ["口味"]


def test_search_returns_the_page_opened_by_that_name_ahead_of_mentions(tmp_path, monkeypatch):
    """A long page is still the hit when the query is the name used to open it."""
    engine = _open(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id="lamp-page",
            type="概念",
            name="北港灯塔",
            status="active",
            revision=0,
            body_md=("这页大部分在讲航线、潮汐和补给。" * 80) + "\n结尾才提到北港灯塔一次。",
        )
    )
    for index in range(12):
        store.put(
            ThinCrystal(
                id=f"mention-{index}",
                type="概念",
                name=f"旁注{index}",
                status="active",
                revision=0,
                body_md="北港灯塔 " * 40,
            )
        )
    found = engine.search("北港灯塔", limit=5)
    assert found["crystals"], found
    assert found["crystals"][0]["crystal"] == "北港灯塔"


def test_search_finds_a_page_by_a_published_alias_absent_from_the_body(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id="lamp-page",
            type="概念",
            name="北港灯塔",
            status="active",
            revision=0,
            aliases=["雾季航标"],
            body_md="正文只写灯塔如何为船只指路，没有别名那几个字。",
        )
    )
    for index in range(6):
        store.put(
            ThinCrystal(
                id=f"mention-{index}",
                type="概念",
                name=f"旁注{index}",
                status="active",
                revision=0,
                body_md="雾季航标 " * 20,
            )
        )
    found = engine.search("雾季航标", limit=5)
    assert found["crystals"], found
    assert found["crystals"][0]["crystal"] == "北港灯塔"
    assert "北港灯塔" in found["crystals"][0]["text"]


def test_search_opens_owner_by_the_reader_name_when_the_title_is_a_personal_name(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    owner = store.get("owner")
    owner.name = "云岚甲"
    owner.body_md = "云岚甲在甲处任职。" * 40
    store.put(owner)
    for index in range(8):
        store.put(
            ThinCrystal(
                id=f"mention-{index}",
                type="概念",
                name=f"旁注{index}",
                status="active",
                revision=0,
                body_md="记忆主人 " * 20,
            )
        )
    found = engine.search("记忆主人", limit=5)
    assert found["crystals"], found
    assert found["crystals"][0]["crystal"] == "云岚甲"


def test_a_name_mentioned_inside_a_question_does_not_become_the_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id="mark-page",
            type="概念",
            name="航标",
            status="active",
            revision=0,
            body_md="这一页只解释航标是什么，没有今晚的安排。",
        )
    )
    question = "今晚去航标看海"
    for index in range(8):
        store.put(
            ThinCrystal(
                id=f"note-{index}",
                type="概念",
                name=f"旁注{index}",
                status="active",
                revision=0,
                body_md=(question + " ") * 12,
            )
        )
    found = engine.search(question, limit=5)
    assert found["crystals"], found
    assert found["crystals"][0]["crystal"] != "航标"


def test_open_indexes_a_page_that_was_stored_before_the_index_existed(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        "UPDATE crystallizations SET body_md=? WHERE id='owner'",
        ("记忆主人页里写着 OWNERPAGEMARK，没有任何主张行。",),
    )
    engine.db().commit()
    engine.connect(tmp_path, tmp_path / "scan")
    found = engine.search("OWNERPAGEMARK", limit=5)
    assert any(item.get("crystal") == "记忆主人" for item in found["crystals"]), found
