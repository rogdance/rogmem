"""运行日志是一张表，最新的在最上面，类型可以筛。"""

import json

from rogmem.ui import logs_page


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_operation_log_is_one_list_newest_first(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    con.execute(
        "INSERT INTO alerts(created_at,message,open) VALUES(?,?,1)",
        ("2026-01-01T00:00:00+00:00", "外部模型鉴权失败"),
    )
    con.execute(
        "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
        (
            "2026-06-01T00:00:00+00:00",
            "无法说定",
            json.dumps({"field": "性别", "a": "男", "b": "女"}, ensure_ascii=False),
        ),
    )
    con.execute(
        "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
        ("2026-06-02T00:00:00+00:00", "词条整合", "收起同名 1 篇"),
    )
    con.execute(
        "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
        ("2026-06-03T00:00:00+00:00", "清扫", "重开失败项 0 条"),
    )
    con.execute(
        "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
        (
            "2026-06-04T00:00:00+00:00",
            "可能同一实体",
            json.dumps({"a": "aa", "b": "bb", "name_a": "云岚甲", "name_b": "云岚乙"}, ensure_ascii=False),
        ),
    )
    con.execute(
        """INSERT INTO revisions(crystal_id,created_at,note,actor,change,basis,scope)
           VALUES(?,?,?,?,?,?,?)""",
        ("owner", "2026-03-01T00:00:00+00:00", "速写", "系统", "新写了结晶速写：短", "按这一页当时的正文写成速写。", "速写"),
    )
    con.execute(
        """INSERT INTO revisions(crystal_id,created_at,note,actor,change,scope)
           VALUES(?,?,?,?,?,?)""",
        ("owner", "2026-04-01T00:00:00+00:00", "人工修订正文", "人工", "改了标题", "标题"),
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline,filename)
           VALUES('s','file','2026-01-01T00:00:00+00:00','h','正文','萃取失败','fast','润色:fail','甲.md')"""
    )
    engine.record_run(
        con,
        "萃取",
        "失败",
        "甲.md",
        "模型没有返回",
        step="润色",
        ref_kind="source",
        ref_id="s",
    )
    con.commit()

    log = engine.operation_log()
    assert log["selected"] == "全部"
    assert log["kinds"] == ["告警", "萃取", "修订", "裁决", "词条整合", "清扫"]
    assert log["entries"][0]["kind"] == "萃取"
    assert log["entries"][0]["step"] == "润色"
    assert log["entries"][0]["outcome"] == "失败"
    assert log["entries"][0]["href"] == "/admin/sources/s"
    order = [item["kind"] + ":" + (item.get("step") or "") + ":" + item["outcome"] for item in log["entries"]]
    assert order.index("萃取:润色:失败") < order.index("裁决::可能同一实体")
    assert order.index("裁决::可能同一实体") < order.index("清扫::完成")
    assert order.index("清扫::完成") < order.index("词条整合::完成")
    assert order.index("词条整合::完成") < order.index("裁决::无法说定")
    assert order.index("裁决::无法说定") < order.index("修订::人工")
    sketch_at = next(i for i, item in enumerate(log["entries"]) if "结晶速写" in item["summary"])
    alert_at = next(i for i, item in enumerate(log["entries"]) if item["kind"] == "告警")
    manual_at = order.index("修订::人工")
    assert manual_at < sketch_at < alert_at
    assert log["entries"][sketch_at]["kind"] == "修订"
    assert log["entries"][sketch_at]["step"] == ""

    verdict = next(item for item in log["entries"] if item["outcome"] == "无法说定")
    assert verdict["subject"] == "性别"
    assert "男" in verdict["summary"] and "女" in verdict["summary"]
    assert "{" not in verdict["summary"]
    same = next(item for item in log["entries"] if item["outcome"] == "可能同一实体")
    assert same["subject"] == "云岚甲、云岚乙"
    assert "aa" not in same["subject"] and "bb" not in same["summary"]

    distill = engine.operation_log("萃取")
    assert distill["selected"] == "萃取"
    assert {item["step"] for item in distill["entries"]} == {"润色"}
    assert {item["kind"] for item in distill["entries"]} == {"萃取"}
    assert engine.operation_log("润色")["selected"] == "全部"
    assert engine.operation_log("不存在")["selected"] == "全部"
    con.execute(
        """INSERT INTO run_log(created_at,kind,step,outcome,subject,summary)
           VALUES('2026-02-01T00:00:00+00:00','润色','','失败','旧.md','旧的一步')"""
    )
    con.commit()
    engine.migrate_source_statuses(con)
    con.commit()
    settled = next(item for item in engine.operation_log("萃取")["entries"] if item["summary"] == "旧的一步")
    assert settled["kind"] == "萃取" and settled["step"] == "润色"
    assert engine._failed_stage_name(engine.db(), "s") == "速写"

    page = logs_page(log["entries"], log["kinds"], log["selected"], "")
    assert page.count("<table") == 1
    side = page.split("<nav class='side'>", 1)[1].split("</nav>", 1)[0]
    assert "class='on' href='/admin/settings'>运行日志" in side
    assert "成本明细" in side and "队列状态" in side
    assert "基础设置" not in side
    assert "结晶修订" not in page
    assert "<h2>告警</h2>" not in page
    assert ">全部<" in page and "class='on'" in page
    assert "href='/admin/settings?kind=萃取'" in page
    assert "href='/admin/settings?kind=词条整合'" in page
    assert "href='/admin/settings?kind=并页'" not in page
    assert "href='/admin/settings?kind=润色'" not in page
    assert ">步骤<" in page and ">润色<" in page
    assert page.find("模型没有返回") < page.find("外部模型鉴权失败")
    assert "身份合并" not in page
    filtered = logs_page(distill["entries"], distill["kinds"], distill["selected"], "")
    assert "class='on' href='/admin/settings?kind=萃取'" in filtered
    assert "模型没有返回" in filtered
    assert "外部模型鉴权失败" not in filtered


def test_run_log_pages_fifty_with_the_shared_pager(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    for index in range(55):
        con.execute(
            """INSERT INTO run_log(created_at,kind,step,outcome,subject,summary)
               VALUES(?,?,?,?,?,?)""",
            (
                f"2026-08-01T00:{index:02d}:00+00:00",
                "萃取",
                "润色",
                "失败",
                f"主体{index:02d}",
                "停在这一步",
            ),
        )
    con.commit()
    engine._refresh_operation_log_snap()
    first = engine.operation_log_view(page=1)
    second = engine.operation_log_view(page=2)
    assert first["total"] == 55 and first["pages"] == 3 and first["page_size"] == 20
    assert len(first["entries"]) == 20 and len(second["entries"]) == 20
    assert first["entries"][0]["subject"] != second["entries"][0]["subject"]
    html = logs_page(
        first["entries"],
        first["kinds"],
        first["selected"],
        "",
        page=first["page"],
        total=first["total"],
        page_size=first["page_size"],
    )
    foot = html.split("class='list-foot'", 1)[1].split("</form></div>", 1)[0]
    assert foot.index("共 55 条") < foot.index("<form class='pager'")
    assert "aria-label='页码'" in foot and "page=2" in foot
    assert html.count("<tr>") == 21
    narrowed = engine.operation_log_view("萃取", page=2)
    kept = logs_page(
        narrowed["entries"],
        narrowed["kinds"],
        narrowed["selected"],
        "",
        page=narrowed["page"],
        total=narrowed["total"],
        page_size=narrowed["page_size"],
    )
    from html import unescape
    from urllib.parse import parse_qs, urlparse

    kept_foot = kept.split("class='list-foot'", 1)[1].split("</form></div>", 1)[0]
    kept_query = parse_qs(urlparse(unescape(kept_foot.split("href='", 1)[1].split("'", 1)[0])).query)
    assert kept_query["kind"] == ["萃取"]
    assert "page" not in kept_query
    assert "page=4" not in kept_foot
