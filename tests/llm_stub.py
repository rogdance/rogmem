"""Local OpenAI-compatible stub. It only proposes; rogmem decides what is published."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAST_SOURCES: list[str] = []


def _attach_sheet(items: list) -> list:
    from rogmem.engine import _canonical_field, checklist

    pages: dict[str, dict] = {}

    def page(name: str, ctype: str, owner: bool) -> dict:
        slot = pages.setdefault(name, {"type": ctype, "owner": owner, "writes": {}})
        slot["type"] = ctype
        slot["owner"] = owner
        return slot

    def verdict(item: dict) -> str:
        value = str(item.get("value") or "")
        trust = item.get("trust") or "written"
        if trust != "written" or any(word in value for word in ("待核实", "可能", "好像")):
            return "待核实"
        return "写入"

    for item in items:
        op = item.get("op")
        if op == "open":
            ctype = item.get("type") or ""
            slot = page(item.get("name") or "", ctype, False)
            title = "姓名" if ctype == "人物" else "名称"
            slot["writes"][("field", title)] = {"verdict": "写入", "value": item.get("name") or "", "quote": item.get("quote") or ""}
            if item.get("repo"):
                slot["writes"][("field", "仓库地址")] = {"verdict": "写入", "value": item.get("repo") or "", "quote": item.get("quote") or ""}
        elif op == "field":
            crystal = item.get("crystal") or "记忆主人"
            owner = crystal == "记忆主人"
            slot = page(crystal, "人物" if owner else "组织", owner)
            key = _canonical_field(item.get("field") or "", owner, slot["type"])
            slot["writes"][("field", key)] = {"verdict": verdict(item), "value": item.get("value") or "", "quote": item.get("quote") or ""}
        elif op == "chapter":
            crystal = item.get("crystal") or "记忆主人"
            owner = crystal == "记忆主人"
            ctype = item.get("type") or ("人物" if owner else "组织")
            slot = page(crystal, ctype, owner)
            slot["writes"][("chapter", item.get("chapter") or "")] = {
                "verdict": verdict(item),
                "value": item.get("value") or "",
                "quote": item.get("quote") or "",
            }
    sheets = []
    for name, slot in pages.items():
        if not name or not slot["type"]:
            continue
        entries = []
        for kind, key in checklist(slot["type"], owner=slot["owner"]):
            hit = slot["writes"].get((kind, key))
            if hit:
                entries.append({"kind": kind, "key": key, **hit})
            else:
                entries.append({"kind": kind, "key": key, "verdict": "无", "value": "", "quote": ""})
        sheets.append({"crystal": name, "type": slot["type"], "entries": entries})
    return sheets


def propose(source: str) -> dict:
    if "交卷缺项" in source:
        return {
            "items": [
                {
                    "op": "field",
                    "field": "真名",
                    "value": "不该发布",
                    "quote": "交卷缺项",
                    "crystal": "记忆主人",
                    "trust": "written",
                }
            ],
            "sheet": [],
            "critic": {},
        }
    items = []
    critic = {}

    def field(name, value, quote, trust="written"):
        items.append({"op": "field", "field": name, "value": value, "quote": quote, "crystal": "记忆主人", "trust": trust})

    def chapter(name, value, quote, crystal="记忆主人", ctype=""):
        item = {"op": "chapter", "chapter": name, "value": value, "quote": quote, "crystal": crystal, "trust": "written"}
        if ctype:
            item["type"] = ctype
        items.append(item)

    def open_page(ctype, name):
        items.append({"op": "open", "type": ctype, "name": name, "quote": name})
        critic[name] = name not in {"石头", "木头", "自行车", "深圳"}

    ev_source = source[7:] if source.startswith("SOURCE\n") else source
    from rogmem.publish_gate import CAREER_CHAPTER, EDU_CHAPTER, career_lines, education_lines, evidence_chapters

    ev = evidence_chapters(ev_source, "人物", True)
    if CAREER_CHAPTER in ev:
        lines = career_lines(ev_source)
        if lines:
            block = "\n".join(lines)
            chapter("事业经历", block, block)
    if EDU_CHAPTER in ev:
        lines = education_lines(ev_source)
        if lines:
            block = "\n".join(lines)
            chapter("教育背景", block, block)
    if "出生时间是1980-01-01" in source:
        field("出生时间", "1980-01-01", "出生时间是1980-01-01")
    if "出生时间是1981-02-02" in source:
        field("出生时间", "1981-02-02", "出生时间是1981-02-02")
    if "出生时间是1992-03-03" in source:
        field("出生时间", "1992-03-03", "出生时间是1992-03-03")
    if "出生时间是1977-07-07" in source:
        field("出生时间", "1977-07-07", "出生时间是1977-07-07")
    if "工号是19900101" in source:
        field("现职", "工程师", "工号是19900101")
    if "劳动合同法第十二条试用期不得超过两个月" in source:
        chapter("事业经历", "试用期有上限", "劳动合同法第十二条试用期不得超过两个月")
    if "未被引用的原句甲乙丙丁" in source:
        chapter("个人生活", "已经记下覆盖", "已经覆盖的原句在这里")
    if "丙后来去了南京" in source:
        chapter("个人生活", "甲在杭州读书。乙在上海工作。\n丙后来去了南京。", "丙后来去了南京")
        chapter("个人生活", "甲在杭州读书。丙后来去了南京。乙在上海工作。", "丙后来去了南京")
    if "员工手册规定加班应事先申请" in source:
        chapter("个人生活", "加班要先打招呼", "员工手册规定加班应事先申请")
        chapter("代表作品", "员工手册规定加班应事先申请", "员工手册规定加班应事先申请")
    if "依据第十三条双方应当书面通知" in source:
        chapter("事业经历", "依据第十三条双方应当书面通知", "依据第十三条双方应当书面通知")
    if "git status --short" in source:
        chapter("个人生活", "跑一下状态", "git status --short")
        chapter("健康档案", "git status --short", "git status --short")
    if "PORT=18080" in source:
        chapter("代表作品", "端口已经设好", "PORT=18080")
        chapter("参与项目", "PORT=18080", "PORT=18080")
    if "现职：工程师" in source:
        chapter("健康档案", "职务是工程师", "现职：工程师")
        chapter("教育背景", "现职：工程师", "现职：工程师")
    if "同事是周生生" in source:
        chapter("社交关系", "同事是周生生", "同事是周生生")
    if "路过周生生" in source:
        chapter("事业经历", "路过周生生", "路过周生生")
    if "项目关联周生生" in source:
        chapter("关联", "关联周生生", "项目关联周生生", "rogmem", "工程")
    if "性别是男" in source:
        field("性别", "男", "性别是男")
    if "裁决无效性别是女" in source:
        field("性别", "女", "裁决无效性别是女")
    if "再裁决成更新性别是女" in source:
        field("性别", "女", "再裁决成更新性别是女")
    if "我叫韩跑跑" in source:
        field("真名", "韩跑跑", "我叫韩跑跑")
    if "真名是韩跑跑" in source:
        field("真名", "韩跑跑", "真名是韩跑跑")
    if "真名是李四" in source:
        field("真名", "李四", "真名是李四")
    if "所属组织是腾讯" in source:
        field("所属组织", "腾讯", "所属组织是腾讯")
    if "我在腾讯任工程师" in source:
        field("所属组织及职务", "腾讯，工程师", "我在腾讯任工程师")
    if "我做决定先看数据，再听反对意见" in source:
        field("决策风格", "先看数据，再听反对意见", "我做决定先看数据，再听反对意见")
    elif "我做决定先看数据" in source:
        field("决策风格", "先看数据", "我做决定先看数据")
    if "我后来喜欢写长文" in source:
        field("沟通风格", "写长文", "我后来喜欢写长文")
    if "我喜欢先看结论" in source:
        field("沟通风格", "先看结论", "我喜欢先看结论")
    if "你喜欢简短" in source:
        field("沟通风格", "简短", "你喜欢简短")
    if "出生日期是1990-01-01" in source:
        field("出生日期", "1990-01-01", "出生日期是1990-01-01")
    if "我可能住在上海" in source:
        field("所在地", "可能在上海", "我可能住在上海")
    if "人在深圳" in source:
        field("所在地", "深圳", "人在深圳")
        open_page("组织", "深圳")
    for name, ctype in (("周生生", "组织"), ("捷安特", "组织"), ("腾讯", "组织"), ("和平精英", "产品"), ("石头", "产品"), ("木头", "产品"), ("自行车", "产品")):
        if name in source:
            open_page(ctype, name)
    if "面试评估框架" in source:
        open_page("概念", "面试评估框架")
    if "面试题设计模板" in source:
        open_page("工程", "面试题设计模板")
    if "github.com/rogdance/rogmem" in source:
        items.append(
            {
                "op": "open",
                "type": "工程",
                "name": "rogmem",
                "quote": "https://github.com/rogdance/rogmem",
                "repo": "https://github.com/rogdance/rogmem",
            }
        )
        critic["rogmem"] = True
    if "北窗记" in source:
        open_page("组织", "北窗记")
    if "青石记" in source:
        open_page("组织", "青石记")
    if "sk-live-" in source:
        field("当前关注", "sk-live-abc123", source.strip())
    return {"items": items, "sheet": _attach_sheet(items), "critic": critic}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode()
        payload = json.loads(raw)
        source = payload["messages"][-1]["content"]
        LAST_SOURCES.append(source)
        if "只回答是或不是" in source:
            name = ""
            ctype = ""
            for line in source.splitlines():
                if line.startswith("名称："):
                    name = line.split("：", 1)[1].strip()
                if line.startswith("类型："):
                    ctype = line.split("：", 1)[1].strip()
            blocked_product = ctype == "产品" and any(word in name for word in ("模板", "框架", "方法论", "量表", "题库"))
            answer = "否" if blocked_product else "是" if name in {"周生生", "捷安特", "腾讯", "和平精英", "青石记", "北窗记", "rogmem", "面试评估框架"} else "否"
            out = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "是不是同一实体" in source:
            answer = "否" if "不是同一人" in source else "是"
            out = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "哪句算当前" in source:
            quote_a = ""
            quote_b = ""
            for line in source.splitlines():
                if line.startswith("甲原文："):
                    quote_a = line.split("：", 1)[1]
                if line.startswith("乙原文："):
                    quote_b = line.split("：", 1)[1]
            if "裁决无效" in source:
                verdict = "猜测"
            elif "再裁决成更新" in source:
                verdict = "更新"
            elif "再裁决成重复" in source:
                verdict = "重复"
            else:
                verdict = "无法说定"
            content = json.dumps({"verdict": verdict, "quote_a": quote_a, "quote_b": quote_b}, ensure_ascii=False)
            out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "触发鉴权失败" in source:
            body = b'{"error":"unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        content = json.dumps(propose(source), ensure_ascii=False)
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, fmt, *args):
        return


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server
