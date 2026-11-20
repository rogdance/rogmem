"""列表画好之后，新入库的会话/文件由一次轻探测发现，并静默刷新列表。

这一支只做「有没有新内容」的判断：探测按列表同样的筛选读最新一条的标记，
页面拿自己画时的标记去比；不同就整页刷新——不弹提示、不要求点击。
"""

from urllib.parse import parse_qs

from rogmem.ui import sources_page


def _row(source_id: str, at: str) -> dict:
    return {
        "id": source_id,
        "kind": "user",
        "preview": "一句话",
        "device_name": "",
        "agent_name": "",
        "status": "已萃取",
        "pipeline": "",
        "ingested_at": at,
        "project_ref": "",
        "note": "",
    }


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _insert(engine, source_id: str, kind: str, at: str, body: str) -> None:
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?,?,?,'h',?,'已萃取','fast',1)""",
        (source_id, kind, at, body),
    )


def test_probe_reads_the_same_rows_the_list_does(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    for index in range(3):
        _insert(engine, f"s{index}", "user", f"2026-01-0{index + 1}T00:00:00+00:00", f"句子{index}")
    _insert(engine, "f0", "file", "2026-01-09T00:00:00+00:00", "一份文件")
    engine.db().commit()

    assert engine.source_latest_probe("session") == {
        "marker": "2026-01-03T00:00:00+00:00|s2",
        "total": 3,
    }
    assert engine.source_latest_probe("file")["marker"].endswith("|f0")
    # 筛选一落空，标记也空：探测不该替列表把看不见的行报出来。
    assert engine.source_latest_probe("session", kind="summary") == {"marker": "", "total": 0}
    assert engine.source_latest_probe("session", query="句子1")["marker"].endswith("|s1")


def test_the_page_carries_a_baseline_the_probe_can_move(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _insert(engine, "s2", "user", "2026-01-03T00:00:00+00:00", "句子")
    engine.db().commit()
    listed = engine.list_sources("session")

    html = sources_page(listed["rows"], "session", {}, {"statuses": []}, listed)
    held = html.split("id='sources-fresh'", 1)[1]
    assert "data-marker='2026-01-03T00:00:00+00:00|s2'" in held
    assert "data-endpoint='/admin/api/sources/latest'" in held
    assert "data-total='1'" in held
    assert parse_qs(held.split("data-query='", 1)[1].split("'", 1)[0])["tab"] == ["session"]


def test_no_flag_off_the_first_page_or_under_a_status_filter():
    row = _row("s0", "2026-01-01T00:00:00+00:00")
    second = {"total": 25, "page": 2, "page_size": 20}
    assert "id='sources-fresh'" not in sources_page([row], "session", {}, {"statuses": []}, second)
    status = sources_page([row], "session", {"status": "已萃取"}, {"statuses": []}, None)
    assert "id='sources-fresh'" not in status


def test_ties_keep_the_list_and_the_probe_on_the_same_top(tmp_path, monkeypatch):
    """同刻入库时，基线（列表首行）和探测必须指同一行，否则列表刚画好就误刷。"""
    engine = _open(tmp_path, monkeypatch)
    for index in range(4):
        _insert(engine, f"t{index}", "user", "2026-01-01T00:00:00+00:00", f"同刻{index}")
    engine.db().commit()

    top = engine.list_sources("session")["rows"][0]
    assert engine.source_latest_probe("session")["marker"] == f"{top['ingested_at']}|{top['id']}"


def test_a_new_row_of_the_same_instant_still_raises_the_count(tmp_path, monkeypatch):
    """头没动、条数涨了，同样是「有新内容」——页面靠净增数把它刷出来。"""
    engine = _open(tmp_path, monkeypatch)
    _insert(engine, "tz", "user", "2026-01-01T00:00:00+00:00", "先到")
    engine.db().commit()
    before = engine.source_latest_probe("session")

    _insert(engine, "ta", "user", "2026-01-01T00:00:00+00:00", "后到")
    engine.db().commit()
    after = engine.source_latest_probe("session")

    assert after["marker"] == before["marker"]
    assert after["total"] == before["total"] + 1


def test_the_latest_route_answers_with_a_marker(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    from rogmem import engine
    from starlette.testclient import TestClient
    from rogmem.app import mcp

    _insert(engine, "s0", "user", "2026-01-01T00:00:00+00:00", "句子")
    engine.db().commit()
    client = TestClient(mcp.streamable_http_app())

    response = client.get("/admin/api/sources/latest?tab=session")
    assert response.status_code == 200
    assert response.json() == {"marker": "2026-01-01T00:00:00+00:00|s0", "total": 1}


def test_new_rows_reload_quietly_without_a_tip():
    """探测载体常隐；发现新入库时脚本直接刷新，不亮提示、不要求点击。"""
    row = _row("s0", "2026-01-01T00:00:00+00:00")
    listed = {"total": 1, "page": 1, "page_size": 20}
    html = sources_page([row], "session", {}, {"statuses": []}, listed)
    assert "id='sources-fresh'" in html
    assert "class='fresh' hidden" in html
    assert ".fresh[hidden] { display: none; }" in html
    assert "fresh-btn" not in html
    assert "点击加载" not in html
    assert "会在上面亮一下" not in html
    assert "location.reload()" in html
