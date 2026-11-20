"""A file that introduces a person keeps that person's facts, even when they are not the owner."""

import json

from rogmem.crystal_store import CrystalStore


def _engine(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    monkeypatch.setattr(engine, "_proper_name_ok", lambda *_args, **_kwargs: True)
    calls: list[dict] = []
    orig = CrystalStore.apply_ops

    def spy(self, crystal_id, expected_revision, ops, *, holder, commit=True):
        calls.append({"crystal_id": crystal_id, "ops": list(ops)})
        return orig(self, crystal_id, expected_revision, ops, holder=holder, commit=commit)

    monkeypatch.setattr(CrystalStore, "apply_ops", spy)
    engine._apply_calls = calls
    return engine


def _landed_text(engine, name: str) -> str:
    """Thick chapters plus apply_ops values. Switch-off writes fail closed, so ops carry the landing."""
    row = engine.db().execute(
        "SELECT id, chapters_json FROM crystallizations WHERE name=? AND status='active'",
        (name,),
    ).fetchone()
    blob = (row["chapters_json"] or "") if row else ""
    cid = row["id"] if row else None
    for call in getattr(engine, "_apply_calls", []):
        if cid and call["crystal_id"] == cid:
            for op in call["ops"]:
                blob += "\n" + str(op.get("value") or "")
    return blob


def _landed_quotes(engine, source_id: str) -> list[str]:
    quotes = [
        item["quote"] or ""
        for item in engine.db().execute(
            "SELECT quote FROM statements WHERE source_id=? AND invalid_at IS NULL",
            (source_id,),
        )
    ]
    for call in getattr(engine, "_apply_calls", []):
        for op in call["ops"]:
            quotes.append(str(op.get("value") or ""))
    return quotes


def test_resume_lands_on_its_subject_and_products_keep_their_purpose(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "\n".join(
        [
            "林深",
            "13800000000 林深@example.com",
            "2004-2008 厦门大学 本科",
            "2020/05-2024/11：云岚科技 产品负责人",
            "他做了云岚，用来把文章排成版。",
        ]
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('resume','file','qa','qa',?,'resume','h',?,'萃取中','slow',1,'简历.docx')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='resume'").fetchone()
    engine._apply_items(
        source,
        [{"op": "open", "type": "产品", "name": "云岚", "quote": "他做了云岚，用来把文章排成版。"}],
        {},
        [],
        unit_body=body,
    )
    person = engine.db().execute("SELECT * FROM crystallizations WHERE name='林深' AND status='active'").fetchone()
    assert person is not None and person["type"] == "人物"
    chapters = _landed_text(engine, "林深")
    assert "厦门大学" in chapters
    assert "云岚科技" in chapters
    assert "排成版" in chapters
    assert "13800000000" not in chapters
    product = engine.db().execute("SELECT id FROM crystallizations WHERE name='云岚'").fetchone()
    assert product is not None
    assert "排成版" in _landed_text(engine, "云岚")
    owner = _landed_text(engine, "记忆主人")
    assert "厦门大学" not in owner
    assert "云岚科技" not in owner


def test_an_existing_product_page_receives_the_sentence_that_names_it(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    summary = {field: "尚无来源" for field in engine.PRODUCT_FIELDS}
    summary["名称"] = "云岚"
    chapters = {chapter: "暂无来源" for chapter in engine._chapters_for("产品")}
    engine.db().execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('prod','产品','云岚','active',?,?)",
        (json.dumps(summary, ensure_ascii=False), json.dumps(chapters, ensure_ascii=False)),
    )
    body = "林深\n他做了云岚，用来把文章排成版。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES('known','file','qa','qa',?,'known','h',?,'萃取中','slow',1)""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='known'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    assert "排成版" in _landed_text(engine, "云岚")


def test_a_missing_side_sheet_does_not_drop_the_subject(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "\n".join(
        [
            "林深",
            "2020/05-2024/11：腾讯科技 产品负责人",
        ]
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES('side','file','qa','qa',?,'side','h',?,'萃取中','slow',1)""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='side'").fetchone()
    engine._apply_items(
        source,
        [{"op": "open", "type": "组织", "name": "腾讯", "quote": "腾讯"}],
        {},
        [],
        unit_body=body,
    )
    person = engine.db().execute("SELECT id FROM crystallizations WHERE name='林深'").fetchone()
    assert person is not None
    assert "腾讯科技" in _landed_text(engine, "林深")


def test_a_sheet_that_says_none_still_keeps_landed_career(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "\n".join(["林深", "2020/05-2024/11：云岚科技 产品负责人"])
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES('none-sheet','file','qa','qa',?,'none-sheet','h',?,'萃取中','slow',1)""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='none-sheet'").fetchone()
    sheet = engine._complete_sheet(
        [{"op": "open", "type": "人物", "name": "林深", "quote": "林深"}]
    )
    engine._apply_items(
        source,
        [{"op": "open", "type": "人物", "name": "林深", "quote": "林深"}],
        {"林深": True},
        sheet,
        unit_body=body,
    )
    assert "云岚科技" in _landed_text(engine, "林深")


def test_encyclopedic_lead_lands_when_model_returns_nothing(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    samples = [
        (
            "沈北川（1972年—），云岚地区的实业者，曾创办北川纸业。\n他后来把纸厂迁到云岚东岸。",
            "人物",
            "沈北川",
            "事业经历",
        ),
        (
            "thumb|right|会址\n云岚联合会是一家登记在册的行业协会，负责协调会员的标准。\n秘书处设在云岚东岸。",
            "组织",
            "云岚联合会",
            "发展沿革",
        ),
        (
            "《云岚登记条例》是一部关于登记手续的规范文本，自公布之日起施行。\n登记机关按条例核发凭证。",
            "概念",
            "云岚登记条例",
            "定义",
        ),
        (
            "北川纸笺是一种用来订手写稿的本册，在云岚市出售。\n封面用未漂白的竹纸。",
            "产品",
            "北川纸笺",
            "发展沿革",
        ),
    ]
    for index, (body, ctype, name, chapter) in enumerate(samples):
        sid = f"lead{index}"
        now = engine.utcnow()
        engine.db().execute(
            """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
               VALUES(?,'file','qa','qa',?,?,?,?,'萃取中','slow',1,?)""",
            (sid, now, sid, f"h{index}", body, f"{sid}.txt"),
        )
        engine.db().commit()
        source = engine.db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        engine._apply_items(source, [], {}, [], unit_body=body)
        row = engine.db().execute(
            "SELECT type FROM crystallizations WHERE name=? AND status='active'",
            (name,),
        ).fetchone()
        assert row is not None and row["type"] == ctype
        landed = _landed_text(engine, name)
        assert name in landed or body[:8] in landed
        quoted = _landed_quotes(engine, sid)
        assert any(name in text for text in quoted)


def test_a_conflicting_sheet_does_not_drop_landed_subject(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚纸业协会是一家登记在册的行业协会，负责协调会员的标准。\n秘书处设在云岚东岸。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('conflict','file','qa','qa',?,'conflict','h',?,'萃取中','slow',1,'协会.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='conflict'").fetchone()
    sheet = [
        {
            "crystal": "云岚纸业协会",
            "type": "概念",
            "entries": [
                {"kind": "field", "key": "名称", "verdict": "无", "value": "", "quote": ""},
                {"kind": "chapter", "key": "发展沿革", "verdict": "无", "value": "", "quote": ""},
            ],
        }
    ]
    engine._apply_items(
        source,
        [{"op": "open", "type": "概念", "name": "云岚纸业协会", "quote": "云岚纸业协会"}],
        {},
        sheet,
        unit_body=body,
    )
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚纸业协会' AND status='active'"
    ).fetchone()
    assert row is not None
    landed = _landed_text(engine, "云岚纸业协会")
    assert "云岚纸业协会" in landed or "行业协会" in landed


def test_a_bare_thumb_line_is_not_the_subject(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "thumb\n北川纸社是一家登记在册的行业协会，负责协调会员的标准。\n会址在云岚东岸。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('crumb','file','qa','qa',?,'crumb','h',?,'萃取中','slow',1,'会址.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='crumb'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='北川纸社' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "组织"
    quotes = engine.db().execute(
        "SELECT quote FROM statements WHERE source_id='crumb' AND invalid_at IS NULL"
    ).fetchall()
    assert any("北川纸社" in (item["quote"] or "") for item in quotes)
    assert all((item["quote"] or "").strip() != "thumb" for item in quotes)


def test_landed_lead_survives_when_the_current_unit_is_only_a_crumb(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    full = "thumb\n云岚纸笺是一种用来订手写稿的本册，在云岚市出售。\n封面用未漂白的竹纸。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('unit','file','qa','qa',?,'unit','h',?,'萃取中','slow',1,'纸笺.txt')""",
        (now, full),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='unit'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body="thumb")
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚纸笺' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "产品"
    quotes = engine.db().execute(
        "SELECT quote FROM statements WHERE source_id='unit' AND invalid_at IS NULL"
    ).fetchall()
    assert any("云岚纸笺" in (item["quote"] or "") for item in quotes)


def test_bare_statute_title_without_bookquotes_still_opens(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚共和国档案保管法是一部用来规定手写稿归档年限的地方规定。归档满十年的本册另行存放。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('law','file','qa','qa',?,'law','h',?,'萃取中','slow',1,'保管法.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='law'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚共和国档案保管法' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "概念"
    quotes = engine.db().execute(
        "SELECT quote FROM statements WHERE source_id='law' AND invalid_at IS NULL"
    ).fetchall()
    assert any("云岚共和国档案保管法" in (item["quote"] or "") for item in quotes)


def test_short_name_empty_paren_and_career_title_is_a_person(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚（），云岚市企业家，专做手写稿作坊。他在城南开了一间纸笺铺。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('bio','file','qa','qa',?,'bio','h',?,'萃取中','slow',1,'作坊主.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='bio'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "人物"
    quotes = engine.db().execute(
        "SELECT quote FROM statements WHERE source_id='bio' AND invalid_at IS NULL"
    ).fetchall()
    assert any("云岚" in (item["quote"] or "") for item in quotes)


def test_landed_open_cites_the_lead_sentence_not_just_the_name(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚纸社是一家登记在册的行业协会，在云岚市办公。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('leadq','file','qa','qa',?,'leadq','h',?,'萃取中','slow',1,'纸社.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='leadq'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    quotes = _landed_quotes(engine, "leadq")
    assert any("是一家登记在册的行业协会" in text for text in quotes)
    assert engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚纸社' AND status='active'"
    ).fetchone()["type"] == "组织"


def test_narrative_topic_lead_without_a_copula_still_opens(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚旗於历史上的记载、不同的地方，均有不同的意义。城门上仍挂着这面旗。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('topic','file','qa','qa',?,'topic','h',?,'萃取中','slow',1,'旗.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='topic'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚旗' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "概念"
    quotes = _landed_quotes(engine, "topic")
    assert any("云岚旗於历史上的记载" in text for text in quotes)


def test_folded_spelling_still_writes_the_opening_sentence(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    summary = {field: "尚无来源" for field in engine.PERSON_FIELDS}
    summary["姓名"] = "云岚纸"
    chapters = {chapter: "暂无来源" for chapter in engine._chapters_for("人物")}
    engine.db().execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('fold','人物','云岚纸','active',?,?)",
        (json.dumps(summary, ensure_ascii=False), json.dumps(chapters, ensure_ascii=False)),
    )
    body = "云岚紙（），云岚市手写稿匠，专做竹纸本册。后来云岚纸把作坊迁到东岸。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('foldsrc','file','qa','qa',?,'foldsrc','h',?,'萃取中','slow',1,'纸匠.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='foldsrc'").fetchone()
    engine._apply_items(
        source,
        [{"op": "open", "type": "人物", "name": "云岚纸", "quote": "后来云岚纸把作坊迁到东岸。"}],
        {},
        [],
        unit_body=body,
    )
    names = {
        row["name"]
        for row in engine.db().execute(
            "SELECT name FROM crystallizations WHERE type='人物' AND status='active' AND id!='owner'"
        )
    }
    assert names == {"云岚纸"}
    quotes = _landed_quotes(engine, "foldsrc")
    assert any("云岚紙（），云岚市手写稿匠" in text for text in quotes)


def test_trailing_quoted_title_still_lands_on_the_subject(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚纸（），云岚市手写稿匠，专做竹纸本册。\n*“云岚年度匠人”，主办方：城南纸会"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('titleq','file','qa','qa',?,'titleq','h',?,'萃取中','slow',1,'称号.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='titleq'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    quotes = _landed_quotes(engine, "titleq")
    assert any("云岚年度匠人" in text for text in quotes)
    assert engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚纸' AND status='active'"
    ).fetchone()["type"] == "人物"


def test_missing_sheet_keys_are_filled_as_none(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚纸会是一家登记在城南的行业协会，负责协调会员的纸样。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('gate-miss','file','qa','qa',?,'gate-miss','h',?,'萃取中','slow',1,'纸会.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='gate-miss'").fetchone()
    items = [
        {"op": "open", "type": "组织", "name": "云岚纸会", "quote": "云岚纸会", "landed": True},
        {
            "op": "chapter",
            "chapter": "发展沿革",
            "value": body,
            "quote": body,
            "crystal": "云岚纸会",
            "type": "组织",
            "landed": True,
        },
    ]
    sheet = [
        {
            "crystal": "云岚纸会",
            "type": "组织",
            "entries": [
                {"kind": "field", "key": "名称", "verdict": "写入", "value": "云岚纸会", "quote": "云岚纸会"},
            ],
        }
    ]
    engine._require_publish_sheet(source, items, {}, sheet, body, unit_body=body)


def test_missing_product_sheet_keys_are_filled_as_none(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚笺筒是一种用来收纳手写稿的竹纸筒，在城南作坊出售。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('gate-prod','file','qa','qa',?,'gate-prod','h',?,'萃取中','slow',1,'笺筒.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='gate-prod'").fetchone()
    items = [
        {"op": "open", "type": "产品", "name": "云岚笺筒", "quote": "云岚笺筒", "landed": True},
        {
            "op": "chapter",
            "chapter": "发展沿革",
            "value": body,
            "quote": body,
            "crystal": "云岚笺筒",
            "type": "产品",
            "landed": True,
        },
    ]
    sheet = [
        {
            "crystal": "云岚笺筒",
            "type": "产品",
            "entries": [
                {"kind": "field", "key": "名称", "verdict": "写入", "value": "云岚笺筒", "quote": "云岚笺筒"},
            ],
        }
    ]
    engine._require_publish_sheet(source, items, {}, sheet, body, unit_body=body)


def test_incomplete_sheet_still_keeps_the_landed_lead(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚纸会是一家登记在城南的行业协会，负责协调会员的纸样。\n秘书处设在云岚东岸。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('missheet','file','qa','qa',?,'missheet','h',?,'萃取中','slow',1,'纸会.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='missheet'").fetchone()
    sheet = [
        {
            "crystal": "云岚纸会",
            "type": "组织",
            "entries": [
                {
                    "kind": "field",
                    "key": "名称",
                    "verdict": "写入",
                    "value": "云岚纸会",
                    "quote": "云岚纸会是一家登记在城南的行业协会，负责协调会员的纸样。",
                }
            ],
        }
    ]
    engine._apply_items(source, [], {}, sheet, unit_body=body)
    quotes = _landed_quotes(engine, "missheet")
    assert any("云岚纸会是一家登记在城南的行业协会" in text for text in quotes)


def test_incomplete_sheet_still_keeps_a_product_lead(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚笺筒是一种用来收纳手写稿的竹纸筒，在城南作坊出售。\n筒身不施釉。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('missprod','file','qa','qa',?,'missprod','h',?,'萃取中','slow',1,'笺筒.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='missprod'").fetchone()
    sheet = [
        {
            "crystal": "云岚笺筒",
            "type": "产品",
            "entries": [
                {"kind": "field", "key": "名称", "verdict": "写入", "value": "云岚笺筒", "quote": "云岚笺筒"},
            ],
        }
    ]
    engine._apply_items(source, [], {}, sheet, unit_body=body)
    quotes = _landed_quotes(engine, "missprod")
    assert any("云岚笺筒是一种用来收纳手写稿的竹纸筒" in text for text in quotes)


def test_empty_write_quote_still_keeps_the_landed_name(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚砚匣是一种用来收纳朱砂的竹匣，在城南书肆出售。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('emptyq','file','qa','qa',?,'emptyq','h',?,'萃取中','slow',1,'砚匣.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='emptyq'").fetchone()
    items = [
        {"op": "open", "type": "产品", "name": "云岚砚匣", "quote": "云岚砚匣", "landed": True},
        {
            "op": "chapter",
            "chapter": "发展沿革",
            "value": body,
            "quote": body,
            "crystal": "云岚砚匣",
            "type": "产品",
            "landed": True,
        },
    ]
    sheet = [
        {
            "crystal": "云岚砚匣",
            "type": "产品",
            "entries": [
                {"kind": "field", "key": "名称", "verdict": "写入", "value": "云岚砚匣", "quote": ""},
            ],
        }
    ]
    engine._require_publish_sheet(source, items, {}, sheet, body, unit_body=body)


def test_empty_write_quote_still_keeps_an_org_lead(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "云岚砚社是一家登记在城南的书会，专管会员的砚台形制。"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('emptyorg','file','qa','qa',?,'emptyorg','h',?,'萃取中','slow',1,'砚社.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='emptyorg'").fetchone()
    sheet = [
        {
            "crystal": "云岚砚社",
            "type": "组织",
            "entries": [
                {"kind": "field", "key": "名称", "verdict": "写入", "value": "云岚砚社", "quote": ""},
            ],
        }
    ]
    engine._apply_items(source, [], {}, sheet, unit_body=body)
    quotes = _landed_quotes(engine, "emptyorg")
    assert any("云岚砚社是一家登记在城南的书会" in text for text in quotes)


def test_first_encyclopedic_lead_after_opening_chrome_still_lands(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "\n".join(
        [
            "图注|云岚册坊坊徽",
            "云岚册坊是一种用来收纳手写稿的竹纸册，在城南作坊出售。",
            "*“云岚册坊年刊”，主办方：城南纸会",
        ]
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('laterlead','file','qa','qa',?,'laterlead','h',?,'萃取中','slow',1,'册坊.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='laterlead'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚册坊' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "产品"
    quotes = _landed_quotes(engine, "laterlead")
    assert any("云岚册坊是一种用来收纳手写稿的竹纸册" in text for text in quotes)
    assert any("云岚册坊年刊" in text for text in quotes)
    assert all("图注|" not in (item or "") for item in quotes)


def test_first_encyclopedic_lead_after_opening_chrome_still_lands_an_org(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "\n".join(
        [
            "参见：城南纸会旧章",
            "| founded = 1990",
            "云岚帛社是一家登记在城南的书会，专管会员的帛书形制。",
        ]
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('laterorg','file','qa','qa',?,'laterorg','h',?,'萃取中','slow',1,'帛社.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='laterorg'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚帛社' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "组织"
    quotes = _landed_quotes(engine, "laterorg")
    assert any("云岚帛社是一家登记在城南的书会" in text for text in quotes)
    assert all("参见：" not in (item or "") for item in quotes)
    assert all(not (item or "").startswith("|") for item in quotes)


def test_later_section_after_heading_is_not_the_opening_subject(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "\n".join(
        [
            "图注|云岚邻坊坊徽",
            "== 相关条目 ==",
            "云岚邻坊是一种用来装订竹纸的夹板，在城南出售。",
        ]
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('headcut','file','qa','qa',?,'headcut','h',?,'萃取中','slow',1,'邻坊.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='headcut'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT id FROM crystallizations WHERE name='云岚邻坊' AND status='active'"
    ).fetchone()
    assert row is None


def test_alias_line_still_lands_on_the_recognized_subject(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "\n".join(
        [
            "云岚册铺是一种用来收纳手写稿的竹纸铺，在城南作坊出售。",
            "云岚册铺亦称云岚铺号，专做未漂白的封面。",
            "邻坊亦称西铺，与本铺无关。",
        ]
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('aliasprod','file','qa','qa',?,'aliasprod','h',?,'萃取中','slow',1,'册铺.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='aliasprod'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚册铺' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "产品"
    quotes = _landed_quotes(engine, "aliasprod")
    assert any("云岚册铺亦称云岚铺号" in text for text in quotes)
    assert all("邻坊亦称西铺" not in text for text in quotes)


def test_alias_line_still_lands_on_a_recognized_org(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "\n".join(
        [
            "云岚帛馆是一家登记在城南的书会，专管会员的帛书形制。",
            "云岚帛馆又名云岚馆社，馆址在东岸。",
        ]
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
           VALUES('aliasorg','file','qa','qa',?,'aliasorg','h',?,'萃取中','slow',1,'帛馆.txt')""",
        (now, body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='aliasorg'").fetchone()
    engine._apply_items(source, [], {}, [], unit_body=body)
    row = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚帛馆' AND status='active'"
    ).fetchone()
    assert row is not None and row["type"] == "组织"
    quotes = _landed_quotes(engine, "aliasorg")
    assert any("云岚帛馆又名云岚馆社" in text for text in quotes)
