"""Worker prompts guide the model. The program keeps the form."""

from __future__ import annotations

import json
import threading

from rogmem.prompts import INTEGRATE_PAGE_SYSTEM, distill_scope, worker_prompts

# 维护者指定的让位句，一个字都不能改。
OWNER_SKETCH_YIELD = (
    "开场还会有别的途径装入最近的原话，近况以那些原话为准；本文（结晶速写）的字只留给原话覆盖不到、省掉就会让下一手做错的事，原话覆盖得住的近况、以及不改变下一手默认做法的经过，把字让出去。"
)
_OWNER_YIELD_GONE = (
    "篇幅不够时按这个次序留下",
    "然后是此刻的状态，写成现在这样，写到下一手能接着做",
    "不要把早先的并列进来把眼下冲淡",
    "经过能用一句话带过就带过；还是放不下，就整段省掉。",
    "正在做的事和已经说定的下一步，写成眼下这一件。",
    "已经否决的做法也留下，免得下一手又拿出来。",
)


def test_model_page_names_the_workers():
    from rogmem.ui import pipeline_page

    html = pipeline_page(
        [],
        {},
        {"source": "none"},
        {"scan_interval": "60", "probe_interval": "300", "reconcile_hours": "3", "sweep_at": "03:00"},
    )
    assert "草稿" in html
    assert "身份合并" in html and "向量嵌入" in html
    assert "结晶润色" not in html
    assert "词条整合" in html
    assert "结晶行文" not in html
    assert ">萃取<" not in html and "当前萃取" not in html


def test_the_pipeline_page_carries_the_gate_knobs():
    """闸门的每个数都要能在页面上改；表单必须把它们一起提交，否则保存等于没改。"""
    from rogmem.ui import pipeline_page

    schedule = {
        "scan_interval": "60",
        "probe_interval": "60",
        "reconcile_hours": "3",
        "sweep_at": "03:00",
        "gate_quota_steps": "300,900,1800,3600,7200,18000",
        "gate_throttle_seconds": "60",
        "gate_probation_units": "2",
        "gate_probation_seconds": "300",
        "distill_concurrency": "3",
    }
    html = pipeline_page([], {}, {"source": "none"}, schedule)
    for label, name, value in (
        ("供应商探活", "probe_interval", "60"),
        ("额度阶梯", "gate_quota_steps", "300,900,1800,3600,7200,18000"),
        ("限流等待", "gate_throttle_seconds", "60"),
        ("探活后限流条数", "gate_probation_units", "2"),
        ("探活后限流时长", "gate_probation_seconds", "300"),
        ("萃取并发", "distill_concurrency", "3"),
    ):
        assert label in html
        assert f'name="{name}" value="{value}"' in html
        assert f"{name}:this.{name}.value" in html


def test_worker_prompts_do_not_dump_a_form():
    prompts = worker_prompts(sketch_limit=3500)
    blob = "\n".join(prompts.values())
    assert "的清单：" not in blob
    assert "石头、木头、自行车：否" not in blob
    assert "周生生、捷安特、腾讯：是" not in blob
    assert "模板、框架、方法论" not in blob
    file_prompt = prompts["distill-file"]
    assert "不是在填一张空表" in file_prompt
    assert "自己起一节" in file_prompt
    assert "文件名也是材料" in file_prompt
    assert "[[篇名]]" in file_prompt
    assert "教育背景" not in file_prompt
    assert "仓库在哪" not in file_prompt
    for name in ("人物", "组织", "产品", "概念"):
        sketch = prompts[f"sketch-{name}"]
        assert "不按固定栏目" in sketch
        assert "教育背景" not in sketch
        assert "仓库" not in sketch
    owner_sketch = prompts["sketch-主人"]
    project_sketch = prompts["sketch-工程"]
    assert OWNER_SKETCH_YIELD in owner_sketch
    assert owner_sketch.count(OWNER_SKETCH_YIELD) == 1
    assert "正在做的事" not in owner_sketch
    assert "不要为了凑齐而编" in owner_sketch
    assert "沟通偏好" in owner_sketch
    assert "决策偏好" in owner_sketch
    assert "禁忌" in owner_sketch
    assert "和智能体的约定" in owner_sketch
    assert "不能改的约定" in project_sketch
    assert "不要把整份沿革" in project_sketch
    assert "仓库" not in prompts["sketch-人物"]
    assert "什么情况不要用" in prompts["sketch-概念"]
    assert "必须" not in owner_sketch
    assert "必须" not in project_sketch
    assert prompts["distill-file"] == prompts["distill-user"] == prompts["distill-agent"]
    assert "说话人是不确定" in file_prompt
    assert "不是记忆主人在说话" in file_prompt
    assert "说话人是记忆主人" in file_prompt
    assert "就是记忆主人" in file_prompt
    assert "智能体不是某个人" in file_prompt
    assert "已有工程结晶" in file_prompt
    assert "已有记忆主人结晶" in file_prompt
    assert "篇名尽量用中文" in file_prompt
    assert "别名：" in file_prompt
    assert "曾用名" in file_prompt
    assert "曾经用过的产品名" in file_prompt
    assert "一段或几段" in prompts["sketch-人物"]
    assert "3500字以内" in prompts["sketch-人物"]
    assert "3500字以内" in prompts["sketch-工程"]
    assert "一段或几段" in prompts["sketch-组织"]
    assert "不按固定栏目" in prompts["sketch-产品"]
    assert "无法说定" in file_prompt
    assert "当前态只跟着事件时间走" in file_prompt
    assert "提交时间和文件最后写入时间都不能代替事件时间" in file_prompt
    assert "跟着这段任期离开当前" in file_prompt
    assert "禁忌" in file_prompt
    assert "什么情况不要用" in file_prompt
    assert "跟着这段任期离开当前" in INTEGRATE_PAGE_SYSTEM
    assert "不要把会改变下一手做法的事实收成空话" in INTEGRATE_PAGE_SYSTEM
    base = (
        "写作标准只约束这次新写进页面的句子；已有正文里没有被 gist 或 basis 点名的字保持原句和原次序，"
        "点到名的角标或结晶链接可以离开这句，没点名的仍留在原句上，新事实另起一段插到它该在的地方；"
        "对不上原句的引用不另挂到页尾，裁决成更新的才把当前态换成更晚的说法，被换下的旧说法不再写进这一页。"
    )
    assert INTEGRATE_PAGE_SYSTEM.count(base) == 1
    assert "通读全页" not in INTEGRATE_PAGE_SYSTEM
    assert "没有带来新事实或新结论的重复收掉" not in INTEGRATE_PAGE_SYSTEM
    assert INTEGRATE_PAGE_SYSTEM.count("写出来就要能当一篇专业的词条读。") == 1
    assert "gist 不超过八十字，让以后翻到这条修订的人先知道这次认成了谁、裁决了什么、为什么变成现在这样。" in INTEGRATE_PAGE_SYSTEM
    assert "篇名改不改，只写在 name 里" in INTEGRATE_PAGE_SYSTEM
    assert "只有当你完全确信" in INTEGRATE_PAGE_SYSTEM
    assert "同一主体现在的名字只有按上面的篇名规则完全确信时才改" in INTEGRATE_PAGE_SYSTEM
    assert "没有改名就沿用给出的篇名" in INTEGRATE_PAGE_SYSTEM
    assert "改了篇名时，要点出旧名和新名" in INTEGRATE_PAGE_SYSTEM
    assert "没点到名的，仍留在修订后的这一页上" not in INTEGRATE_PAGE_SYSTEM
    assert "结晶之间的 [[篇名]] 仍指原来那一处" not in INTEGRATE_PAGE_SYSTEM
    assert "程序已经认定是这一页" not in INTEGRATE_PAGE_SYSTEM
    assert "先认出是不是同一个主体并去重" in INTEGRATE_PAGE_SYSTEM
    assert "这不是默认要整合" in INTEGRATE_PAGE_SYSTEM
    assert "程序会把新写成的那一页另存成一篇" in INTEGRATE_PAGE_SYSTEM
    assert "same 填「否」" in INTEGRATE_PAGE_SYSTEM
    assert "在已有这一页上修订，不要另起一篇" in INTEGRATE_PAGE_SYSTEM
    assert "已有正文里的事实、结论和引用都必须完整保留" not in INTEGRATE_PAGE_SYSTEM
    assert "下面裁决环节点名要改的才改" not in INTEGRATE_PAGE_SYSTEM
    assert "只有你已经认定是同一个主体" in INTEGRATE_PAGE_SYSTEM
    assert "绝对不能改，也绝对不能丢" not in INTEGRATE_PAGE_SYSTEM
    assert "只改裁决要求改的地方" not in INTEGRATE_PAGE_SYSTEM
    from rogmem.prompts import CRYSTAL_WRITE, DISTILL_SYSTEM, KEEP_RELATIONS

    assert "自己起一节" in CRYSTAL_WRITE
    assert "通读全页" not in CRYSTAL_WRITE
    assert KEEP_RELATIONS not in INTEGRATE_PAGE_SYSTEM
    assert "在这一页上改，不要再开一篇工程。" in DISTILL_SYSTEM
    assert "写记忆主人时在这一页上改，不要再开一篇。" in DISTILL_SYSTEM
    assert "不是脱离它新写的一篇" in INTEGRATE_PAGE_SYSTEM
    for kind in ("人物：现实中的一个人", "组织：可以独立指称的集体", "产品：已经有名字、被人拿来用的成果", "工程：有自己的 git 仓库地址", "概念：换一份材料仍然成立"):
        assert kind in INTEGRATE_PAGE_SYSTEM
    for field in ("事件时间：这句话被说出来或写下来的时间", "提交时间：这条材料进入记忆库的时间", "最晚事件时间：", "文件最后写入时间：", "未记下："):
        assert field in INTEGRATE_PAGE_SYSTEM
    assert "你的使命是基于获得的信息" in INTEGRATE_PAGE_SYSTEM
    assert "它的发展沿革" in INTEGRATE_PAGE_SYSTEM
    assert "读者是以后来协作的智能体" in INTEGRATE_PAGE_SYSTEM
    for duty in ("兴趣爱好", "决策风格", "沟通偏好", "禁忌", "和智能体的约定", "当前关注", "什么情况不要用", "仓库地址，以及材料里点名的分支", "[^1]: 实", "别名：甲、乙"):
        assert duty in INTEGRATE_PAGE_SYSTEM
    from rogmem.prompts import merge_user

    shown = merge_user("旧正文", "新正文", "原文", page_name="记忆主人", page_type="人物")
    assert "篇名：记忆主人" in shown
    assert "类型：人物" in shown
    assert "删除了什么" not in INTEGRATE_PAGE_SYSTEM
    assert "不要收进正在做和已经说定" in owner_sketch
    assert "留给以后合并" not in file_prompt
    assert "来源链接" in file_prompt
    assert "主人称呼是用户记下的原文，不是这一页名字的上限" in file_prompt
    assert "材料开头若有「主人称呼」" in INTEGRATE_PAGE_SYSTEM
    assert "[^1]: 实" in file_prompt and "[^2]: 推" in file_prompt
    assert "单独去找" in prompts["proper-name"]
    assert "另一种语言" in prompts["merge-entity"]
    assert "腾讯" not in prompts["merge-entity"]
    assert set(prompts) == {
        "distill-user",
        "distill-file",
        "distill-agent",
        "sketch-人物",
        "sketch-主人",
        "sketch-工程",
        "sketch-组织",
        "sketch-产品",
        "sketch-概念",
        "merge-entity",
        "proper-name",
    }


def test_owner_sketch_replaces_the_yield_lines_and_other_sketches_keep_theirs():
    """主人路径用指定的一句换掉旧的让位说明。其他类型仍用原来的次序。"""
    prompts = worker_prompts(sketch_limit=3500)
    owner = prompts["sketch-主人"]
    assert owner.count(OWNER_SKETCH_YIELD) == 1
    for gone in _OWNER_YIELD_GONE:
        assert gone not in owner
    assert "会改变每一轮协作的，写具体" in owner
    assert "沟通偏好" in owner and "决策偏好" in owner and "禁忌" in owner
    assert "和智能体的约定" in owner
    assert "兴趣爱好" not in owner
    assert "只留还在生效的后一种" in owner
    assert "不要收进正在做和已经说定" in owner
    assert "正文要另开才读得到" in owner
    assert "不按固定栏目" in owner
    assert "必须" not in owner
    for name in ("人物", "工程", "组织", "产品", "概念"):
        sketch = prompts[f"sketch-{name}"]
        assert OWNER_SKETCH_YIELD not in sketch
        assert "篇幅不够时按这个次序留下" in sketch
        assert "经过能用一句话带过就带过；还是放不下，就整段省掉。" in sketch
        assert "不要把早先的并列进来把眼下冲淡" in sketch
    assert "做到哪、下一步、卡在哪，写成眼下能接手的状态。" in prompts["sketch-工程"]


def test_owner_sketch_call_keeps_a_synthetic_body_and_only_checks_length(tmp_path, monkeypatch):
    """程序不改材料、不按栏目筛。虚构句与萃取语料无连续八字重叠。"""
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    owner_body = "雾港守夜人把粗盐收进铁盒，回信只留一行。眼下他在等一盏灯自己亮。"
    project_body = "北岸纸店把未干的蓝墨留在柜台上，下一步是等它自己干透。"
    calls: list[dict] = []

    def fake_complete(_base, _key, payload, **_kwargs):
        calls.append(
            {
                "system": payload["messages"][0]["content"],
                "user": payload["messages"][1]["content"],
            }
        )
        if len(calls) == 1:
            return "眼下他在等一盏灯自己亮。", "stop"
        return "北岸纸店等墨干透。", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role in {"distill", "page", "sketch", "proper"} else None)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    owner_sketch = engine._ask_sketch(owner_body, "人物", owner=True)
    project_sketch = engine._ask_sketch(project_body, "工程", owner=False)
    assert owner_sketch == "眼下他在等一盏灯自己亮。"
    assert calls[0]["user"] == owner_body
    assert calls[0]["system"].count(OWNER_SKETCH_YIELD) == 1
    for gone in _OWNER_YIELD_GONE:
        assert gone not in calls[0]["system"]
    assert project_sketch == "北岸纸店等墨干透。"
    assert calls[1]["user"] == project_body
    assert OWNER_SKETCH_YIELD not in calls[1]["system"]
    assert "篇幅不够时按这个次序留下" in calls[1]["system"]


def test_header_names_the_owner_and_the_source_even_when_the_model_cannot_know(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    seen: dict[str, str] = {}

    def fake_complete(_base, _key, payload, **_kwargs):
        speaker = payload["messages"][1]["content"].split("说话人：", 1)[-1].split("\n", 1)[0]
        seen[speaker] = payload["messages"][1]["content"]
        return "# 云岚甲\n类型：人物\n\n他在。\n", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role in {"distill", "page", "sketch", "proper"} else None)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._llm_draft("我喜欢爵士。", "file", filename="档案.md", source_id="src-file")
    engine._llm_draft("我喜欢爵士。", "user", owner_real_name="云岚甲", owner_aliases="甲", source_id="src-user")
    engine._llm_draft("可以这么做。", "agent", owner_real_name="云岚甲", source_id="src-agent")
    file_user = seen["不确定"]
    assert "材料类型：文件" in file_user
    assert "说话人：不确定" in file_user
    assert "记忆主人：尚未认定" in file_user
    assert "来源链接：/admin/sources/src-file" in file_user
    assert "文件名：档案.md" in file_user
    assert "材料类型：会话" in seen["记忆主人"]
    assert "说话人：记忆主人" in seen["记忆主人"]
    assert "记忆主人：云岚甲" in seen["记忆主人"]
    assert "来源链接：/admin/sources/src-user" in seen["记忆主人"]
    assert "说话人：智能体" in seen["智能体"]
    assert seen["智能体"].split("材料类型：", 1)[-1].startswith("会话")
    assert engine._distill_system("file") == engine._distill_system("agent")
    assert "主人称呼" not in file_user


def test_owner_appellation_is_handed_to_the_model_as_written(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    raw = "云岚甲、甲哥   lin"
    assert engine.save_basic({"sketch_char_limit": "3500", "owner_appellation": raw})["ok"] is True
    seen: dict[str, str] = {}

    def fake_complete(_base, _key, payload, **_kwargs):
        seen["user"] = payload["messages"][1]["content"]
        system = payload["messages"][0]["content"]
        if "并页写作者" in system:
            return '{"same":"无法说定","page":"旧正文","conflicts":[],"gist":"拿不准"}', "stop"
        if "整合" in system:
            return "{}", "stop"
        return "# 云岚甲\n类型：人物\n\n他在。\n", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._llm_draft("我喜欢爵士。", "file", filename="档案.md", source_id="src-file")
    draft = seen["user"]
    assert f"记忆主人：{raw}" in draft
    assert "记忆主人：尚未认定" not in draft
    assert "主人称呼：" not in draft
    assert "主人别名：无" in draft
    engine._llm_draft(
        "我喜欢爵士。",
        "file",
        filename="档案.md",
        source_id="src-file",
        owner_real_name="页上的真名",
        owner_aliases="页上的别名",
    )
    named = seen["user"]
    assert "记忆主人：页上的真名" in named
    assert "主人别名：页上的别名" in named
    assert f"主人称呼：{raw}" in named
    assert "尚未认定" not in named
    engine._merge_existing_page("旧正文", "新正文", "原文", page_name="记忆主人", page_type="人物")
    assert seen["user"].startswith(f"主人称呼：{raw}\n")
    engine._llm_integrate(
        "材料",
        "user",
        draft_crystal={"name": "甲", "type": "人物"},
        library_candidates=[{"id": "c1", "type": "人物", "name": "甲"}],
        owner_real_name="页上的真名",
        owner_aliases="页上的别名",
    )
    assert seen["user"].startswith(f"主人称呼：{raw}\n")
    assert "真名：页上的真名" in seen["user"]
    assert "别名：页上的别名" in seen["user"]
    engine._llm_integrate(
        "材料",
        "user",
        draft_crystal={"name": "甲", "type": "人物"},
        library_candidates=[{"id": "c1", "type": "人物", "name": "甲"}],
        owner_real_name="",
        owner_aliases="",
    )
    assert f"真名：{raw}" in seen["user"]
    assert not seen["user"].startswith("主人称呼：")
    assert engine.crystal_detail("owner")["summary"]["真名"] == "尚无来源"


def test_owner_address_is_one_fact():
    from rogmem.prompts import owner_address

    assert owner_address("", "", "") == ("", "", "")
    assert owner_address("", "", "  云岚甲、甲哥   lin\n") == ("云岚甲、甲哥   lin", "", "")
    assert owner_address("页上的真名", "页上的别名", "云岚甲、甲哥   lin") == (
        "页上的真名",
        "页上的别名",
        "云岚甲、甲哥   lin",
    )
    assert owner_address("云岚甲、甲哥   lin", "别名", "云岚甲、甲哥   lin") == (
        "云岚甲、甲哥   lin",
        "别名",
        "",
    )


def test_partial_sheet_is_completed_from_what_the_model_wrote(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    body = "我叫韩跑跑"
    data = engine._normalize_extract(
        {
            "items": [
                {"op": "field", "field": "真名", "value": "韩跑跑", "quote": body, "crystal": "记忆主人"},
            ],
            "sheet": [
                {
                    "crystal": "记忆主人",
                    "type": "人物",
                    "entries": [
                        {"kind": "field", "key": "真名", "verdict": "写入", "value": "韩跑跑", "quote": body},
                    ],
                }
            ],
        },
        body,
    )
    keys = {entry["key"] for block in data["sheet"] for entry in block["entries"]}
    assert "真名" in keys
    assert "事业经历" in keys
    assert "现职" not in keys
    assert "沟通风格" in keys and "决策风格" in keys


def test_filename_is_shown_and_its_facts_can_land(tmp_path, monkeypatch):
    from tests.llm_stub import LAST_SOURCES, serve

    server = serve(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
        monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
        monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
        monkeypatch.setenv("ROGMEM_EMBED", "off")
        monkeypatch.setenv("ROGMEM_LLM_BASE", f"http://127.0.0.1:{port}/v1")
        monkeypatch.setenv("ROGMEM_LLM_KEY", "stub")
        monkeypatch.setenv("ROGMEM_LLM_MODEL", "stub")
        from rogmem import engine

        engine.connect(tmp_path, tmp_path / "scan")
        monkeypatch.setattr(engine, "_proper_name_ok", lambda *_args, **_kwargs: True)
        filename = "青石金行_2016年成立_位于深圳_主营黄金零售.txt"
        body = "陈雪任总经理。注册资本五千万元，法定代表人陈雪。2019年取得ISO9001认证。"
        scope = distill_scope(filename, body)
        assert scope.startswith("文件名：" + filename)
        items = [
            {"op": "open", "type": "组织", "name": "青石金行", "quote": "青石金行"},
            {"op": "open", "type": "人物", "name": "陈雪", "quote": "陈雪"},
            {
                "op": "field",
                "field": "所在地",
                "value": "深圳",
                "quote": "位于深圳",
                "crystal": "青石金行",
            },
            {
                "op": "chapter",
                "chapter": "发展沿革",
                "value": "2016年成立",
                "quote": "2016年成立",
                "crystal": "青石金行",
            },
            {
                "op": "chapter",
                "chapter": "业务与产品",
                "value": "主营黄金零售",
                "quote": "主营黄金零售",
                "crystal": "青石金行",
            },
            {
                "op": "chapter",
                "chapter": "登记与资质",
                "value": "注册资本五千万元，法定代表人陈雪。2019年取得ISO9001认证。",
                "quote": "注册资本五千万元，法定代表人陈雪。2019年取得ISO9001认证。",
                "crystal": "青石金行",
            },
            {
                "op": "field",
                "field": "所属组织及职务",
                "value": "青石金行，总经理",
                "quote": "陈雪任总经理",
                "crystal": "陈雪",
            },
        ]
        data = engine._normalize_extract({"items": items, "critic": {"青石金行": True, "陈雪": True}}, scope)
        con = engine.db()
        con.execute(
            """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready,filename)
               VALUES('dossier','file','qa','qa',?,'dossier-h',?,'队列中','slow',1,?)""",
            (engine.utcnow(), body, filename),
        )
        con.commit()
        source = con.execute("SELECT * FROM sources WHERE id='dossier'").fetchone()
        engine._apply_items(source, data["items"], data["critic"], data["sheet"], unit_body=scope)
        org_row = con.execute(
            "SELECT id, body_md, envelope_json FROM crystallizations WHERE name='青石金行'"
        ).fetchone()
        assert org_row is not None
        org = engine.crystal_detail(org_row["id"])
        assert org is not None
        assert "深圳" in (org["summary"].get("所在地") or "")
        landed = "\n".join(
            [
                json.dumps(org["chapters"], ensure_ascii=False),
                org_row["body_md"] or "",
                org_row["envelope_json"] or "",
                "\n".join(
                    row["text"]
                    for row in con.execute(
                        "SELECT text FROM statements WHERE crystal_id=? AND invalid_at IS NULL",
                        (org_row["id"],),
                    )
                ),
            ]
        )
        assert "2016年成立" in landed
        assert "黄金零售" in landed
        assert "ISO9001" in landed
        assert "陈雪" in landed
        person_row = con.execute("SELECT id FROM crystallizations WHERE name='陈雪'").fetchone()
        assert person_row is not None
        person = engine.crystal_detail(person_row["id"])
        assert person is not None
        assert "总经理" in json.dumps(person["summary"], ensure_ascii=False)

        scan = tmp_path / "scan"
        scan.mkdir(exist_ok=True)
        path = scan / filename
        path.write_text("这份档案记录青石金行的登记事项，正文写到这里就够了。", encoding="utf-8")
        LAST_SOURCES.clear()
        opened = engine.ingest_file(path)
        assert engine.pump_one() == opened["id"]
        # Match≥1（库内已有青石金行）+ stub 无 mid → unwritten，不再掉 extract。
        # 文件名仍须出现在 Draft 用户消息；禁 SOURCE\n extract 假消化。
        assert any(filename in item for item in LAST_SOURCES), LAST_SOURCES
        assert not any(item.startswith("SOURCE\n") for item in LAST_SOURCES)
    finally:
        server.shutdown()


def test_an_over_length_sketch_is_never_cut(tmp_path, monkeypatch):
    """速写不剪尾：改写压不下去时保留完整原文，绝不落半截。"""
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: ("", "stop"))

    text = "甲" * 3600 + "。"
    out = engine._fit_or_compress(text, "http://stub/v1", "k", "m", limit=3500)
    assert out == text
    assert "已截断" not in out


def test_an_over_length_sketch_is_rewritten_not_sliced(tmp_path, monkeypatch):
    """超上限先让模型压回去，只交回完整正文；压不动才保留原草稿——两条路都不剪尾。"""
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    seen: list[str] = []

    def fake_complete(_base, _key, payload, **_kwargs):
        seen.append(payload["messages"][0]["content"])
        if len(seen) == 1:
            return "甲" * 3600 + "。", "stop"
        return "压回上限的正文。", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    out = engine._ask_sketch("正文", "人物", owner=True)
    assert out == "压回上限的正文。"
    assert len(seen) == 2
    assert "超出" in seen[1]


def test_a_compression_that_does_not_shrink_keeps_the_whole_sketch(tmp_path, monkeypatch):
    """压缩没变短（模型跑偏）时不能顶替原草稿，也不剪尾——保留完整的原草稿。"""
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    seen: list[str] = []

    def fake_complete(_base, _key, payload, **_kwargs):
        seen.append(payload["messages"][0]["content"])
        if len(seen) == 1:
            return "甲" * 3600 + "。", "stop"
        return "乙" * 3600 + "。", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    out = engine._ask_sketch("正文", "人物", owner=True)
    assert out == "甲" * 3600 + "。"
    assert "已截断" not in out


def test_a_sketch_reply_that_stopped_for_room_is_not_a_sketch(tmp_path, monkeypatch):
    """没写完的回复不是速写：再要的仍没写完就返回空，交上层留待办——不落半截。"""
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: ("写了半句就停住", "length"))
    assert engine._ask_sketch("正文" * 60, "人物", owner=False) == ""


def test_bodies_and_sketches_keep_what_still_holds_months_later():
    """一条时间尺度：三个月后还用得上、还成立的才进正文；这一轮的过程留在原文和近期原话里。"""
    from rogmem.prompts import CRYSTAL_WRITE, DISTILL_SYSTEM

    prompts = worker_prompts(sketch_limit=3500)
    assert "三个月后" in CRYSTAL_WRITE and "三个月后" in DISTILL_SYSTEM
    assert "正文只是摘要" in CRYSTAL_WRITE
    assert "顺着页上的来源回原文" in CRYSTAL_WRITE
    assert "新会话开场另有最近的原话" in CRYSTAL_WRITE
    assert CRYSTAL_WRITE.strip() in INTEGRATE_PAGE_SYSTEM
    for gone in (
        "这次的症状：报错原文、失败的命令、错误输出",
        "验证到哪一步：测过并且过了",
        "他此刻在做、很关注的工作",
        "然后是此刻的状态，写成现在这样，写到下一手能接着做",
        "修订记录里留着原文",
    ):
        assert gone not in "\n".join(prompts.values()) + INTEGRATE_PAGE_SYSTEM, gone
    assert "只在过了这一轮仍然成立、还会改变下一手做法时才写" in CRYSTAL_WRITE
    assert "不用写进页面" in INTEGRATE_PAGE_SYSTEM
    for name in ("人物", "工程", "组织", "产品", "概念"):
        assert "这一轮的进度和数字留给开场的近期原话" in prompts[f"sketch-{name}"]
    for name in ("人物", "主人", "工程", "组织", "产品", "概念"):
        sketch = prompts[f"sketch-{name}"]
        assert sketch.count("〔源 ") == 1
        assert "每一条最多跟一个" in sketch


def _prefix_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    seen: list[str] = []

    def fake_complete(_base, _key, payload, **_kwargs):
        seen.append(payload["messages"][1]["content"])
        if "并页写作者" in payload["messages"][0]["content"]:
            return '{"same":"无法说定","page":"旧正文","conflicts":[],"gist":"拿不准"}', "stop"
        return "# 雾港守灯人\n类型：人物\n\n他在。\n", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    return engine, seen


def test_draft_puts_the_stable_sketches_before_the_header_and_the_material(tmp_path, monkeypatch):
    """供应商按前缀缓存：两份不同材料的草稿，速写那一段一字不差地排在最前。"""
    engine, seen = _prefix_engine(tmp_path, monkeypatch)
    engine.db().execute("UPDATE crystallizations SET body_md=?, sketch_md=? WHERE id='owner'", ("守灯人偏好短句。", "守灯人偏好短句。"))
    engine.db().commit()
    engine._llm_draft("第一份合成材料：铜铃挂在东窗。", "user", source_id="src-a", session_id="s-1", ingested_at="2026-11-01T01:00:00+00:00")
    engine._llm_draft("第二份合成材料：灯油换成松脂。", "user", source_id="src-b", session_id="s-2", ingested_at="2026-11-02T01:00:00+00:00")
    first, second = seen
    assert first.startswith("记忆主人速写：")
    assert first.index("守灯人偏好短句。") < first.index("材料类型：") < first.index("第一份合成材料")
    shared = first.split("材料类型：", 1)[0]
    assert second.startswith(shared)
    assert "来源链接：/admin/sources/src-a" not in shared and "会话：s-1" not in shared


def test_merge_puts_the_stored_page_before_the_per_material_lines(tmp_path, monkeypatch):
    engine, seen = _prefix_engine(tmp_path, monkeypatch)
    old = "守灯人把铜铃挂在东窗。"
    from rogmem.prompts import merge_user

    a = merge_user(old, "新稿甲", "材料甲", event_at="2026-11-01T01:00:00+00:00", session_id="s-1", agent_name="宿主甲", page_name="雾港守灯人", page_type="人物")
    b = merge_user(old, "新稿乙", "材料乙", event_at="2026-11-03T01:00:00+00:00", session_id="s-2", agent_name="宿主乙", page_name="雾港守灯人", page_type="人物")
    shared = a.split("新写成的一页：", 1)[0]
    assert old in shared and b.startswith(shared)
    for variable in ("会话：", "来自哪个智能体：", "事件时间：2026", "提交时间："):
        assert variable not in shared
    engine._merge_existing_page(old, "新稿甲", "材料甲", page_name="雾港守灯人", page_type="人物")
    assert seen[-1].index(old) < seen[-1].index("这份材料：")
