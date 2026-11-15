"""Wiki marks follow titles and aliases of pages that still have a row."""

from __future__ import annotations


def test_a_mark_that_matches_no_active_label_keeps_the_words():
    from rogmem.wikilink import align_wikilinks

    index = {"青渚灯塔": "tower"}
    assert align_wikilinks("后来提到[[幽灵灯]]。", index, "self") == "后来提到幽灵灯。"
    assert "[[" not in align_wikilinks("后来提到[[幽灵灯]]。", index, "self")


def test_an_exact_mention_gains_brackets_and_a_near_spelling_does_not():
    from rogmem.wikilink import align_wikilinks

    index = {"青渚灯塔": "tower", "旧称澄禾": "tower"}
    text = "澄禾后来去了青渚灯塔，也提起旧称澄禾。见青渚灯台。"
    aligned = align_wikilinks(text, index, "self")
    assert aligned == "澄禾后来去了[[青渚灯塔]]，也提起[[旧称澄禾]]。见青渚灯台。"
    assert align_wikilinks(aligned, index, "self") == aligned
    assert align_wikilinks("青渚灯塔和青渚灯塔。", index, "self") == "[[青渚灯塔]]和[[青渚灯塔]]。"
    assert align_wikilinks("见 [[ 青渚灯塔 ]]。", index, "self") == "见 [[ 青渚灯塔 ]]。"


def test_this_page_does_not_gain_a_link_to_itself():
    from rogmem.wikilink import align_wikilinks

    index = {"澄禾": "self", "青渚灯塔": "tower"}
    text = "[[澄禾]]写下澄禾，去了青渚灯塔。"
    assert align_wikilinks(text, index, "self") == "[[澄禾]]写下澄禾，去了[[青渚灯塔]]。"


def test_a_one_character_label_is_not_filled_in():
    from rogmem.wikilink import align_wikilinks

    index = {"禾": "other", "青渚灯塔": "tower"}
    assert align_wikilinks("澄禾后来去了青渚灯塔。", index, "self") == "澄禾后来去了[[青渚灯塔]]。"
    assert align_wikilinks("单字[[禾]]留着。", index, "self") == "单字[[禾]]留着。"


def test_a_longer_label_covers_a_shorter_one():
    from rogmem.wikilink import align_wikilinks

    both = {"青渚灯塔": "tower", "灯塔": "lamp"}
    assert align_wikilinks("去青渚灯塔外的灯塔。", both, "self") == "去[[青渚灯塔]]外的[[灯塔]]。"
    assert align_wikilinks("去青渚灯塔。", {"灯塔": "lamp"}, "self") == "去青渚[[灯塔]]。"


def test_a_label_shared_by_two_pages_is_left_as_written():
    from rogmem.wikilink import align_wikilinks

    index = {"同名纸": None, "青渚灯塔": "tower"}
    text = "见同名纸。[[同名纸]]留着。去青渚灯塔。"
    assert align_wikilinks(text, index, "self") == "见同名纸。[[同名纸]]留着。去[[青渚灯塔]]。"


def test_citation_lines_fences_and_alias_lines_are_not_rewritten_into_new_links():
    from rogmem.wikilink import align_wikilinks

    index = {"青渚灯塔": "tower"}
    cite = "他在青渚灯塔。[^1]\n\n[^1]: 实 「他在青渚灯塔，[[幽灵灯]]照旧」\n"
    assert align_wikilinks(cite, index, "self") == (
        "他在[[青渚灯塔]]。[^1]\n\n[^1]: 实 「他在青渚灯塔，[[幽灵灯]]照旧」\n"
    )
    fenced = "他在青渚灯塔。\n```\n青渚灯塔\n[[幽灵灯]]\n```\n"
    assert align_wikilinks(fenced, index, "self") == "他在[[青渚灯塔]]。\n```\n青渚灯塔\n[[幽灵灯]]\n```\n"
    alias = "别名：[[青渚灯塔]]\n他经过青渚灯塔。\n"
    assert align_wikilinks(alias, index, "teller") == "别名：青渚灯塔\n他经过[[青渚灯塔]]。\n"


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_reconcile_aligns_links_without_deleting_pages(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.crystal_store import CrystalStore, ThinCrystal
    from rogmem.ui import crystal_page

    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id="tower",
            type="组织",
            name="青渚灯塔",
            status="active",
            revision=0,
            aliases=["旧称澄禾"],
        )
    )
    store.put(ThinCrystal(id="old", type="组织", name="旧称澄禾", status="active", revision=0))
    store.put(ThinCrystal(id="gone", type="产品", name="幽灵灯", status="active", revision=0))
    store.put(
        ThinCrystal(
            id="teller",
            type="人物",
            name="澄禾",
            status="active",
            revision=0,
            body_md="澄禾后来去了青渚灯塔，也提起[[旧称澄禾]]。看见[[幽灵灯]]。[[素未成页]]并不存在。见青渚灯台。",
        )
    )
    engine.db().execute("UPDATE crystallizations SET status='已合并' WHERE id='old'")
    engine.db().execute("UPDATE crystallizations SET status='已撤回' WHERE id='gone'")
    engine.db().commit()
    owner = store.get("owner")
    assert owner is not None
    owner.body_md = "主人页写到青渚灯塔，没有素未成页。"
    store.put(owner)

    before = [
        (row["id"], row["status"], row["name"])
        for row in engine.db().execute("SELECT id, status, name FROM crystallizations ORDER BY id")
    ]
    result = engine.reconcile_new_crystals()
    after = [
        (row["id"], row["status"], row["name"])
        for row in engine.db().execute("SELECT id, status, name FROM crystallizations ORDER BY id")
    ]
    assert result["links"] == 2
    assert result["folded"] == 0
    assert before == after
    assert "素未成页" not in {name for _cid, _status, name in after}
    assert "青渚灯台" not in {name for _cid, _status, name in after}

    expected = "澄禾后来去了[[青渚灯塔]]，也提起[[旧称澄禾]]。看见[[幽灵灯]]。素未成页并不存在。见青渚灯台。"
    detail = engine.crystal_detail("teller")
    assert detail["body_md"] == expected
    assert detail["out_links"] == [
        {"id": "gone", "name": "幽灵灯"},
        {"id": "tower", "name": "青渚灯塔"},
    ]
    live = engine.db().execute(
        "SELECT dst FROM edges WHERE src='teller' AND kind='提及' AND ended=0"
    ).fetchall()
    assert {row["dst"] for row in live} == {"tower", "gone"}
    html = crystal_page(detail, [], [], "", "active")
    assert "href='/admin/crystals/tower'" in html
    assert "href='/admin/crystals/gone'" in html
    assert "href='/admin/crystals/tower'>旧称澄禾</a>" in html
    assert "crystals/old" not in html
    assert ">旧称澄禾</a>" in html
    assert ">青渚灯塔</a>" in html
    assert ">幽灵灯</a>" in html
    assert "[[素未成页]]" not in html
    assert "素未成页" in html
    assert "青渚灯台" in html

    owner_body = engine.crystal_detail("owner")["body_md"]
    assert owner_body == "主人页写到[[青渚灯塔]]，没有素未成页。"
    assert {item["id"] for item in engine.crystal_detail("tower")["in_links"]} == {"teller", "owner"}
    assert [item["note"] for item in engine.revisions("teller")].count("链接") == 1
    note = engine.db().execute(
        "SELECT detail FROM decisions WHERE kind='词条整合' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert note is not None and "对齐链接 2 篇" in note["detail"]

    again = engine.reconcile_new_crystals()
    assert again["links"] == 0
    assert engine.crystal_detail("teller")["body_md"] == expected
    assert [item["note"] for item in engine.revisions("teller")].count("链接") == 1


def test_merged_and_withdrawn_titles_and_aliases_still_link(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.crystal_store import CrystalStore, ThinCrystal
    from rogmem.ui import crystal_page
    from rogmem.wikilink import title_index

    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id="folded",
            type="组织",
            name="北窗纸社",
            status="active",
            revision=0,
            aliases=["纸社旧称"],
        )
    )
    store.put(
        ThinCrystal(
            id="dim",
            type="产品",
            name="幽灵灯",
            status="active",
            revision=0,
            aliases=["灭掉的灯"],
        )
    )
    store.put(
        ThinCrystal(
            id="teller",
            type="人物",
            name="澄禾",
            status="active",
            revision=0,
            body_md="经过北窗纸社与纸社旧称，看见幽灵灯和灭掉的灯。[[素未成页]]并不存在。",
        )
    )
    engine.db().execute("UPDATE crystallizations SET status='已合并' WHERE id='folded'")
    engine.db().execute("UPDATE crystallizations SET status='已撤回' WHERE id='dim'")
    engine.db().commit()
    before = [
        (row["id"], row["status"], row["name"])
        for row in engine.db().execute("SELECT id, status, name FROM crystallizations ORDER BY id")
    ]
    index = title_index(engine.db())
    assert index["北窗纸社"] == "folded"
    assert index["纸社旧称"] == "folded"
    assert index["幽灵灯"] == "dim"
    assert index["灭掉的灯"] == "dim"
    assert "素未成页" not in index

    result = engine.reconcile_new_crystals()
    after = [
        (row["id"], row["status"], row["name"])
        for row in engine.db().execute("SELECT id, status, name FROM crystallizations ORDER BY id")
    ]
    assert result["links"] == 1
    assert before == after
    expected = "经过[[北窗纸社]]与[[纸社旧称]]，看见[[幽灵灯]]和[[灭掉的灯]]。素未成页并不存在。"
    detail = engine.crystal_detail("teller")
    assert detail["body_md"] == expected
    assert [item["id"] for item in detail["out_links"]] == ["folded", "dim"]
    html = crystal_page(detail, [], [], "", "active")
    assert "href='/admin/crystals/folded'" in html
    assert "href='/admin/crystals/dim'" in html
    assert ">纸社旧称</a>" in html
    assert ">灭掉的灯</a>" in html
    assert "[[素未成页]]" not in html
    assert engine.reconcile_new_crystals()["links"] == 0
    assert engine.crystal_detail("teller")["body_md"] == expected
