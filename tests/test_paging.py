"""Memory lists stay small: sources turn pages, the crystal nav loads as you scroll."""

from html import unescape
from urllib.parse import parse_qs, urlparse

from rogmem.ui import crystal_page, sources_page


def _foot_queries(html: str) -> list[dict[str, list[str]]]:
    foot = html.split("class='list-foot'", 1)[1].split("</form></div>", 1)[0]
    found = []
    for piece in foot.split("href='")[1:]:
        href = unescape(piece.split("'", 1)[0])
        found.append(parse_qs(urlparse(href).query))
    return found


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_sources_turn_fifty_at_a_time(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    for index in range(55):
        engine.db().execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
               VALUES(?,?,?,'h',?,'已蒸馏','fast',1)""",
            (f"s{index}", "user", f"2026-01-01T00:{index:02d}:00+00:00", f"句子{index}"),
        )
    engine.db().commit()
    first = engine.list_sources("session", page=1)
    second = engine.list_sources("session", page=2)
    assert first["total"] == 55 and first["pages"] == 2 and len(first["rows"]) == 50
    assert len(second["rows"]) == 5
    assert first["rows"][0]["id"] != second["rows"][0]["id"]
    html = sources_page(first["rows"], "session", {"q": "句子"}, {"statuses": []}, first)
    assert "下一页" in html and "上一页" in html
    assert any(item.get("q") == ["句子"] and item.get("page") == ["2"] for item in _foot_queries(html))
    assert "class='link'" in html and ">重试</button>" in html
    assert "quiet" not in html.split("操作", 1)[1].split("</table>", 1)[0]
    foot = html.split("class='list-foot'", 1)[1].split("</form></div>", 1)[0]
    assert foot.index("共 55 条") < foot.index("<form class='pager'")
    assert "aria-label='页码'" in foot and "value='1'" in foot
    assert "<button type='button' class='quiet' data-step='-1' disabled>上一页</button>" in foot
    file_row = (
        "f0",
        "file",
        "2026-01-02T00:00:00+00:00",
        "一份文件",
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?,?,?,'h',?,'已蒸馏','fast',1)""",
        file_row,
    )
    engine.db().commit()
    files = engine.list_sources("file", page=1)
    file_html = sources_page(files["rows"], "file", {}, {"statuses": []}, files)
    file_foot = file_html.split("class='list-foot'", 1)[1].split("</form></div>", 1)[0]
    assert "共 1 条" in file_foot and "aria-label='页码'" in file_foot
    assert "<button type='button' class='quiet' data-step='1' disabled>下一页</button>" in file_foot


def test_a_page_link_is_the_same_query_as_the_form():
    """页数由总条数和每页条数推出。链接里的查询必须能原样回到筛选，不能在 & 处断开。"""

    html = sources_page(
        [],
        "session",
        {"q": "a&b=c"},
        {},
        {"total": 55, "page": 1, "pages": 9, "page_size": 50},
    )
    foot = html.split("class='list-foot'", 1)[1].split("</form></div>", 1)[0]
    assert "data-page-size='50'" in foot
    assert "data-pages>2</span>" in foot
    assert "data-pages>9</span>" not in foot
    turned = next(item for item in _foot_queries(html) if item.get("page") == ["2"])
    assert turned["q"] == ["a&b=c"]
    assert turned["tab"] == ["session"]
    assert "name='q' value='a&amp;b=c'" in foot


def test_crystal_nav_loads_the_next_window_instead_of_paging(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    for index in range(60):
        engine.db().execute(
            "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES(?,?,?,'active','{}','{}')",
            (f"c{index:02d}", "人物", f"词条{index:02d}"),
        )
    engine.db().commit()
    window = engine.crystal_nav_slice("", "active", 0)
    assert window["total"] == 61 and len(window["items"]) == 50 and window["next"] == 50
    rest = engine.crystal_nav_slice("", "active", 50)
    assert len(rest["items"]) == 11 and rest["next"] == 61
    detail = {
        "id": "c59",
        "type": "人物",
        "name": "词条59",
        "status": "active",
        "status_label": "活跃",
        "summary": {},
        "chapters": {},
        "field_marks": {},
        "chapter_marks": {},
        "lede": [],
        "chains": {},
    }
    html = crystal_page(detail, [], window["items"], "", "active", "body", {"total": window["total"], "next": window["next"]})
    assert "id='crystal-nav'" in html
    assert "data-offset='50'" in html
    assert "/admin/api/crystals/nav" in html
    assert "上一页" not in html
    assert "下一页" not in html
    assert "this.form.requestSubmit()" in html


def test_type_filter_opens_a_matching_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES(?,?,?,'active','{}','{}')",
        ("org1", "组织", "南港书院"),
    )
    engine.db().commit()
    from rogmem.app import crystal_nav_decision

    owner = engine.crystal_detail("owner")
    stayed = crystal_nav_decision(owner, "人物", "active")
    assert stayed["redirect"] == ""
    assert any(item["id"] == "owner" for item in stayed["nav"])
    jumped = crystal_nav_decision(owner, "组织", "active")
    assert jumped["redirect"] == "org1"
    assert all(item["type"] == "组织" for item in jumped["nav"])
    assert all(item["id"] != "owner" for item in jumped["nav"])
    empty = crystal_nav_decision(owner, "工程", "active")
    assert empty["redirect"] == ""
    assert empty["nav"] == []
    from starlette.testclient import TestClient
    from rogmem.app import build_app

    response = TestClient(build_app()).get("/admin/crystals/owner", params={"type": "组织"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/admin/crystals/org1?")
    assert "type=" in response.headers["location"]
    landed = TestClient(build_app()).get("/admin/crystals/owner", params={"type": "组织"})
    assert landed.status_code == 200
    assert "南港书院" in landed.text
    assert ">记忆主人</a>" not in landed.text
