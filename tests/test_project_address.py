"""A settled project keeps one page. A later mention does not open another or replace the address."""

from rogmem.crystal_store import CrystalStore, ThinCrystal
from rogmem.hermes_memory import repository_slug
from rogmem.ui import _markdown_editor


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _ship(engine):
    CrystalStore(engine.db()).put(
        ThinCrystal(
            id="dock",
            type="工程",
            name="青渚船坞",
            status="active",
            revision=0,
            body_md="青渚船坞还停在北栈。\nhttps://github.com/example/north-dock\n",
            keys={"仓库地址": "https://github.com/example/north-dock"},
        ),
        commit=True,
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-dock','user','2026-11-09T03:00:00+00:00','2026-11-09T04:00:00+00:00','utterance','h',
                  '船坞又被提到了一次。','蒸馏中','p1',1)"""
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id='src-dock'").fetchone()


def _address(engine) -> str:
    crystal = CrystalStore(engine.db()).get("dock")
    return str((crystal.keys or {}).get("仓库地址") or "")


def test_a_release_page_and_a_fence_are_the_same_repository():
    assert repository_slug("`https://github.com/example/north-dock.git`") == "github.com/example/north-dock"
    assert repository_slug("https://github.com/example/north-dock/releases/tag/v9") == "github.com/example/north-dock"
    assert repository_slug("https://127.0.0.1/healthz") == ""
    assert repository_slug("https://platform.example.cn/console/home") == ""


def test_a_repeat_mention_does_not_open_a_page_or_rewrite_the_address(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    source = _ship(engine)
    body = (
        "探活 https://127.0.0.1/healthz 不是仓库。\n"
        "仓库仍是 `https://github.com/example/north-dock.git`。\n"
        "船坞又被提到了一次。"
    )
    engine._land_markdown_pages(
        source,
        [{"name": "青渚船坞", "type": "工程", "body": body}],
        start="落地",
    )
    rows = engine.db().execute(
        "SELECT id FROM crystallizations WHERE type='工程' AND status='active'"
    ).fetchall()
    assert [row["id"] for row in rows] == ["dock"]
    assert _address(engine) == "https://github.com/example/north-dock"


def test_a_new_repository_stays_beside_the_settled_address(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    source = _ship(engine)
    body = "内部仓库改到 https://github.com/example/north-dock-dev 。船坞还是这一座。"
    engine._land_markdown_pages(
        source,
        [{"name": "青渚船坞", "type": "工程", "body": body}],
        start="落地",
    )
    assert _address(engine) == "https://github.com/example/north-dock"
    assert engine._project_crystal("https://github.com/example/north-dock-dev.git")["id"] == "dock"
    assert engine._project_crystal("`https://github.com/example/north-dock`")["id"] == "dock"
    rows = engine.db().execute(
        "SELECT id FROM crystallizations WHERE type='工程' AND status='active'"
    ).fetchall()
    assert [row["id"] for row in rows] == ["dock"]


def test_a_person_can_replace_the_repository_address(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _ship(engine)
    result = engine.save_crystal_markdown(
        "dock",
        "青渚船坞还停在北栈。\nhttps://github.com/example/north-dock\n",
        address="https://github.com/example/north-dock-dev.git",
    )
    assert result["ok"] is True
    assert _address(engine) == "https://github.com/example/north-dock-dev"
    assert engine._project_crystal("https://github.com/example/north-dock")["id"] == "dock"
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-dock'").fetchone()
    engine._land_markdown_pages(
        source,
        [{"name": "青渚船坞", "type": "工程", "body": "又提到旧地址 https://github.com/example/north-dock.git 。"}],
        start="落地",
    )
    assert _address(engine) == "https://github.com/example/north-dock-dev"
    refused = engine.save_crystal_markdown(
        "dock",
        "青渚船坞还停在北栈。\nhttps://github.com/example/north-dock\n",
        address="",
    )
    assert refused["ok"] is False
    assert _address(engine) == "https://github.com/example/north-dock-dev"


def test_the_editor_offers_the_repository_address_on_an_engineering_page():
    page = _markdown_editor(
        {
            "id": "dock",
            "type": "工程",
            "summary": {"仓库地址": "https://github.com/example/north-dock"},
            "sketch_md": "",
            "body_md": "青渚船坞还停在北栈。",
        },
        "/back",
    )
    assert "data-address" in page
    assert "https://github.com/example/north-dock" in page
    assert "留空不会删掉" in page
    other = _markdown_editor(
        {"id": "note", "type": "概念", "summary": {}, "sketch_md": "", "body_md": "一条概念。"},
        "/back",
    )
    assert "<input data-address" not in other
