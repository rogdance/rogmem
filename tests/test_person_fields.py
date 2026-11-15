"""人物核心摘要不设现职，字段按约定顺序展示。"""

from rogmem import engine
from rogmem.ui import crystal_page

OWNER_ORDER = [
    "真名",
    "别名",
    "性别",
    "出生时间",
    "语言",
    "与记忆主人的关系",
    "所属组织及职务",
    "所在地",
    "概述",
    "兴趣爱好",
    "沟通风格",
    "决策风格",
    "当前关注",
    "约定待办",
]


def test_owner_field_order_has_no_current_post():
    assert "现职" not in engine.PERSON_FIELDS
    assert "现职" not in engine.SINGLE
    assert engine.summary_fields("人物", owner=True) == OWNER_ORDER
    person = engine.summary_fields("人物", owner=False)
    assert person[0] == "姓名"
    assert "当前关注" not in person
    assert "约定待办" not in person
    assert person.index("沟通风格") < person.index("决策风格")


def test_owner_page_lays_out_fields_without_current_post(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    engine.connect(tmp_path, tmp_path / "scan")
    detail = engine.crystal_detail("owner")
    assert list(detail["summary"]) == OWNER_ORDER
    html = crystal_page(detail, [], [{"id": "owner", "name": "记忆主人"}], "", "active")
    assert "id='s-现职'" not in html
    assert "id='s-性别'" not in html
    shown = [key for key in ("真名", "别名") if f"id='s-{key}'" in html]
    positions = [html.find(f"id='s-{key}'") for key in shown]
    assert positions == sorted(pos for pos in positions if pos >= 0)
    assert "id='s-真名'" in html


def test_current_post_write_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    engine.connect(tmp_path, tmp_path / "scan")
    body = "现职是工程师"
    source = engine.db().execute("SELECT * FROM sources WHERE id='none'").fetchone()
    if source is None:
        engine.db().execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,event_at)
               VALUES('job','user','2026-10-27T00:00:00+00:00','job',?,'已蒸馏','fast','2026-10-27T00:00:00+00:00')""",
            (body,),
        )
        engine.db().commit()
        source = engine.db().execute("SELECT * FROM sources WHERE id='job'").fetchone()
    engine._write_field(
        source,
        {"field": "现职", "value": "工程师", "quote": body, "crystal": "记忆主人", "trust": "written"},
        "user",
        body,
    )
    detail = engine.crystal_detail("owner")
    assert "现职" not in detail["summary"]
    loaded = engine.core_memory(budget=8000)["loaded"]
    assert all(item["name"] != "现职" for item in loaded)
