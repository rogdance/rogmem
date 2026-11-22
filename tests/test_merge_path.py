"""The merge path: section-level by default, a budget sized to the reply, no splice, and
heavy pages merged in batches from a backlog. Synthetic sentences only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "docs" / "bench" / "wiki-distill" / "corpus.json"

SENTENCES = (
    "守灯人把蓝色信标调到北偏东。",
    "雾钟每隔七分钟敲一次。",
    "补给船改在单数日靠岸。",
    "守灯人开始在塔下种薄荷。",
    "信标换成了琥珀色。",
    "塔顶的风向标锈住了。",
    "守灯人记下第九本潮汐簿。",
    "塔里的旧油灯收进了库房。",
)


def _overlap(text: str, sample: str, size: int = 8) -> str:
    for index in range(0, len(sample) - size + 1):
        window = sample[index : index + size]
        if window in text:
            return window
    return ""


def test_synthetic_sentences_do_not_repeat_the_corpus():
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    blob = "\n".join((item.get("title") or "") + "\n" + (item.get("plain_text") or "") for item in raw["items"])
    for sample in SENTENCES:
        assert _overlap(blob, sample) == ""


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_ask_sketch", lambda *_a, **_k: "守灯人的速写。")
    return engine


def _filler(label: str, size: int) -> str:
    """A long synthetic section: one sentence repeated with a counter, so paragraphs differ."""
    paras = []
    index = 0
    while sum(len(p) + 2 for p in paras) < size:
        index += 1
        paras.append(f"{label}第{index}条：雾钟每隔七分钟敲一次。")
    return "\n\n".join(paras)


def _long_page(size: int) -> str:
    return (
        "## 塔务\n\n守灯人把蓝色信标调到北偏东。\n\n"
        "## 补给\n\n补给船改在单数日靠岸。\n\n"
        "## 簿册\n\n" + _filler("簿册", size)
    )


def _page(engine, cid: str, name: str, body: str) -> None:
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES(?,?,?,'active','{}','{}',?)""",
        (cid, "人物", name, body),
    )
    engine.db().commit()


def _owner_body(engine, body: str) -> None:
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id='owner'", (body,))
    engine.db().commit()


def _body(engine, cid: str) -> str:
    return engine.db().execute("SELECT body_md FROM crystallizations WHERE id=?", (cid,)).fetchone()["body_md"] or ""


def _land(engine, tmp_path, monkeypatch, name: str, draft: str, filename: str) -> str:
    def produce(_text, _kind="", **_kwargs):
        return {"payload_kind": "markdown", "pages": [{"name": name, "type": "人物", "body": draft}]}

    monkeypatch.setattr(engine, "_produce_distill_payload", produce)
    path = tmp_path / "scan" / filename
    path.parent.mkdir(exist_ok=True)
    path.write_text("材料：" + draft, encoding="utf-8")
    opened = engine.ingest_file(path)
    assert engine.pump_one() == opened["id"]
    return opened["id"]


class _Chat:
    def __init__(self, reply: str = "", reason: str = "stop"):
        self.reply = reply
        self.reason = reason
        self.calls: list[dict] = []

    def __call__(self, _base, _key, payload, **kwargs):
        if kwargs.get("role") != "page":
            return "", "stop"
        self.calls.append(payload)
        return self.reply, self.reason


def _merge_reply(page: str) -> str:
    return json.dumps({"same": "是", "page": page, "conflicts": [], "gist": "信标换色。"}, ensure_ascii=False)


def test_a_long_page_is_merged_by_section_from_the_first_call(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = _long_page(engine.MERGE_PARTIAL_FROM_CHARS + 500)
    assert engine.MERGE_PARTIAL_FROM_CHARS < len(old) < engine.PAGE_BATCH_FROM_CHARS
    _page(engine, "lighthouse", "守灯人", old)
    chat = _Chat(_merge_reply("## 塔务\n\n信标换成了琥珀色。"))
    monkeypatch.setattr(engine, "_complete_chat", chat)
    draft = "## 塔务\n\n信标换成了琥珀色。"
    _land(engine, tmp_path, monkeypatch, "守灯人", draft, "塔务.txt")
    assert len(chat.calls) == 1
    user = chat.calls[0]["messages"][1]["content"]
    assert engine.MERGE_PARTIAL_NOTE in user
    body = _body(engine, "lighthouse")
    assert "信标换成了琥珀色。" in body
    assert "守灯人把蓝色信标调到北偏东。" not in body
    # 没交回的小节一字不差
    untouched = old[old.index("## 补给") :]
    assert untouched in body
    assert body.index("## 塔务") < body.index("## 补给") < body.index("## 簿册")


def test_a_short_page_still_gets_a_whole_page_merge(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _page(engine, "lighthouse", "守灯人", "守灯人把蓝色信标调到北偏东。")
    chat = _Chat(_merge_reply("信标换成了琥珀色。"))
    monkeypatch.setattr(engine, "_complete_chat", chat)
    _land(engine, tmp_path, monkeypatch, "守灯人", "信标换成了琥珀色。", "短页.txt")
    assert len(chat.calls) == 1
    assert engine.MERGE_PARTIAL_NOTE not in chat.calls[0]["messages"][1]["content"]


def test_the_merge_budget_is_sized_to_the_reply():
    from rogmem.engine import (
        MERGE_BUDGET_BASE_TOKENS,
        MERGE_BUDGET_TOKENS_PER_CHAR,
        _chat_max_tokens,
        _merge_budget,
    )

    base, model = "https://api.minimax.chat/v1", "MiniMax-M3.1-Flash"
    ceiling = _chat_max_tokens(base, model)
    old = "## 塔务\n\n" + "甲" * 3000 + "\n\n## 簿册\n\n" + "乙" * 9000
    new = "## 塔务\n\n信标换成了琥珀色。"
    full = _merge_budget(base, model, old, new, partial=False)
    part = _merge_budget(base, model, old, new, partial=True)
    assert full == MERGE_BUDGET_BASE_TOKENS + MERGE_BUDGET_TOKENS_PER_CHAR * (len(old) + len(new))
    # 分节：草稿加最长的一节（改动的小节要整节交回），与整页长短无关
    assert part == MERGE_BUDGET_BASE_TOKENS + MERGE_BUDGET_TOKENS_PER_CHAR * (len(new) + 9000)
    assert part < full < ceiling
    huge = "丙" * 400_000
    assert _merge_budget(base, model, huge, new, partial=False) == ceiling
    assert _merge_budget("https://api.other.example/v1", "small-model", old, new, partial=True) == 4096


def test_a_section_merge_that_still_runs_out_parks_the_draft_and_leaves_the_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = _long_page(engine.MERGE_PARTIAL_FROM_CHARS + 500)
    _page(engine, "lighthouse", "守灯人", old)
    chat = _Chat("## 塔务\n\n信标换成了", "length")
    monkeypatch.setattr(engine, "_complete_chat", chat)
    draft = "## 塔务\n\n信标换成了琥珀色。"
    source_id = _land(engine, tmp_path, monkeypatch, "守灯人", draft, "超额.txt")
    assert len(chat.calls) == 1  # 已经是分节模式，不再降级、不再拼接
    assert _body(engine, "lighthouse") == old  # 页一字不动，没有拼进草稿
    parked = engine.db().execute("SELECT * FROM page_backlog WHERE source_id=?", (source_id,)).fetchall()
    assert len(parked) == 1
    assert parked[0]["crystal_id"] == "lighthouse"
    assert parked[0]["reason"] == "over_cap"
    assert "信标换成了琥珀色。" in parked[0]["draft_md"]
    assert parked[0]["consumed_at"] is None
    events = engine.db().execute(
        "SELECT 1 FROM crystal_events WHERE crystal_id='lighthouse' AND kind='page_merge_over_cap'"
    ).fetchall()
    assert events
    queue = engine.db().execute("SELECT state FROM queue WHERE source_id=?", (source_id,)).fetchone()
    assert queue["state"] == "done"  # 单元收尾，不回队列重试
    status = engine.db().execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()["status"]
    assert status == engine.ST_CRYSTAL
    logged = engine.db().execute("SELECT outcome FROM run_log WHERE ref_id='lighthouse'").fetchall()
    assert any(row["outcome"] == "待整理" for row in logged)


def test_owner_drafts_are_parked_without_any_model_call(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "## 塔务\n\n守灯人把蓝色信标调到北偏东。\n\n## 补给\n\n补给船改在单数日靠岸。"
    _owner_body(engine, old)
    chat = _Chat(_merge_reply("## 塔务\n\n不该被调用。"))
    monkeypatch.setattr(engine, "_complete_chat", chat)
    first = _land(engine, tmp_path, monkeypatch, "记忆主人", "## 爱好\n\n守灯人开始在塔下种薄荷。", "薄荷.txt")
    second = _land(engine, tmp_path, monkeypatch, "记忆主人", "## 塔务\n\n信标换成了琥珀色。", "琥珀.txt")
    assert chat.calls == []
    assert _body(engine, "owner") == old
    rows = engine.db().execute(
        "SELECT source_id, reason FROM page_backlog WHERE crystal_id='owner' AND consumed_at IS NULL ORDER BY id"
    ).fetchall()
    assert [row["source_id"] for row in rows] == [first, second]
    assert {row["reason"] for row in rows} == {"batch"}
    for source_id in (first, second):
        status = engine.db().execute("SELECT status, stage_json FROM sources WHERE id=?", (source_id,)).fetchone()
        assert status["status"] == engine.ST_CRYSTAL
        assert not status["stage_json"]  # 单元走完了，没有留下待写的速写


def test_consolidation_merges_every_parked_draft_in_one_call(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "## 塔务\n\n守灯人把蓝色信标调到北偏东。\n\n## 补给\n\n补给船改在单数日靠岸。"
    _owner_body(engine, old)
    idle = _Chat()
    monkeypatch.setattr(engine, "_complete_chat", idle)
    _land(engine, tmp_path, monkeypatch, "记忆主人", "## 爱好\n\n守灯人开始在塔下种薄荷。", "薄荷.txt")
    _land(engine, tmp_path, monkeypatch, "记忆主人", "## 塔务\n\n信标换成了琥珀色。", "琥珀.txt")
    assert idle.calls == []

    # 两份、刚停进去：不到时候
    monkeypatch.setattr(engine, "_LAST_BACKLOG_CHECK", 0.0)
    assert engine.consolidate_backlog() == {}

    chat = _Chat(_merge_reply("## 塔务\n\n信标换成了琥珀色。\n\n## 爱好\n\n守灯人开始在塔下种薄荷。"))
    monkeypatch.setattr(engine, "_complete_chat", chat)
    monkeypatch.setattr(engine, "_LAST_BACKLOG_CHECK", 0.0)
    later = datetime.now(timezone.utc) + timedelta(hours=engine.PAGE_BATCH_MAX_AGE_HOURS + 1)
    done = engine.consolidate_backlog(now=later)
    assert done == {"owner": "merged"}
    assert len(chat.calls) == 1
    user = chat.calls[0]["messages"][1]["content"]
    assert engine.MERGE_PARTIAL_NOTE in user
    assert engine.PAGE_BATCH_NOTE in user
    assert user.index("守灯人开始在塔下种薄荷。") < user.index("信标换成了琥珀色。")  # 从早到晚
    assert later.isoformat() in user  # 今天是这份材料的时钟
    body = _body(engine, "owner")
    assert "信标换成了琥珀色。" in body
    assert "守灯人开始在塔下种薄荷。" in body
    assert "补给船改在单数日靠岸。" in body  # 没交回的小节留着
    assert "守灯人把蓝色信标调到北偏东。" not in body
    left = engine.db().execute("SELECT COUNT(*) FROM page_backlog WHERE consumed_at IS NULL").fetchone()[0]
    assert left == 0
    revision = engine.db().execute(
        "SELECT note, snapshot, actor FROM revisions WHERE crystal_id='owner' AND note='正文' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert revision["snapshot"] == body and revision["actor"] == "萃取"
    sketch = engine.db().execute("SELECT sketch_md FROM crystallizations WHERE id='owner'").fetchone()["sketch_md"]
    assert sketch == "守灯人的速写。"

    # 没有新草稿，再跑一轮不花调用
    monkeypatch.setattr(engine, "_LAST_BACKLOG_CHECK", 0.0)
    assert engine.consolidate_backlog(now=later) == {}
    assert len(chat.calls) == 1


def test_enough_parked_drafts_are_due_without_waiting_a_day(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    for index in range(engine.PAGE_BATCH_MIN_DRAFTS):
        engine.db().execute(
            "INSERT INTO page_backlog(crystal_id,source_id,draft_md,at) VALUES('owner',?,?,?)",
            (f"s{index}", f"第{index}份。", now.isoformat()),
        )
    engine.db().commit()
    assert engine._backlog_due_pages(engine.db(), now) == ["owner"]


def test_a_failed_consolidation_keeps_the_drafts_and_waits(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "## 塔务\n\n守灯人把蓝色信标调到北偏东。"
    _owner_body(engine, old)
    monkeypatch.setattr(engine, "_complete_chat", _Chat())
    _land(engine, tmp_path, monkeypatch, "记忆主人", "## 塔务\n\n信标换成了琥珀色。", "琥珀.txt")
    chat = _Chat("## 塔务\n\n信标换", "length")
    monkeypatch.setattr(engine, "_complete_chat", chat)
    assert engine.consolidate_backlog(force=True) == {"owner": "over_cap"}
    assert _body(engine, "owner") == old
    row = engine.db().execute("SELECT consumed_at, tries FROM page_backlog").fetchone()
    assert row["consumed_at"] is None and row["tries"] == 1
    # 刚失败过：下一轮不急着再试
    monkeypatch.setattr(engine, "_LAST_BACKLOG_CHECK", 0.0)
    later = datetime.now(timezone.utc) + timedelta(hours=engine.PAGE_BATCH_MAX_AGE_HOURS + 1)
    assert engine.consolidate_backlog(now=datetime.now(timezone.utc)) == {}
    assert len(chat.calls) == 1
    # 隔够了再试，草稿一直在
    monkeypatch.setattr(engine, "_LAST_BACKLOG_CHECK", 0.0)
    assert engine.consolidate_backlog(now=later) == {"owner": "over_cap"}
    assert len(chat.calls) == 2
    assert _body(engine, "owner") == old


def test_page_batch_off_merges_the_owner_page_on_each_exchange(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.set_setting("page_batch", "0")
    engine.db().commit()
    old = "## 塔务\n\n守灯人把蓝色信标调到北偏东。\n\n## 补给\n\n补给船改在单数日靠岸。"
    _owner_body(engine, old)
    chat = _Chat(_merge_reply("## 塔务\n\n信标换成了琥珀色。"))
    monkeypatch.setattr(engine, "_complete_chat", chat)
    _land(engine, tmp_path, monkeypatch, "记忆主人", "## 塔务\n\n信标换成了琥珀色。", "琥珀.txt")
    assert len(chat.calls) == 1
    assert engine.MERGE_PARTIAL_NOTE in chat.calls[0]["messages"][1]["content"]  # 记忆主人页仍按分节
    body = _body(engine, "owner")
    assert "信标换成了琥珀色。" in body and "补给船改在单数日靠岸。" in body
    assert engine.db().execute("SELECT COUNT(*) FROM page_backlog").fetchone()[0] == 0


def test_a_section_level_reply_keeps_untouched_sections_byte_for_byte():
    from rogmem.engine import _shape_merged_page

    old = (
        "守灯人住在北岬。\n\n"
        "## 塔务\n\n守灯人把蓝色信标调到北偏东。\n\n"
        "## 补给\n\n补给船改在单数日靠岸。\n\n雾钟每隔七分钟敲一次。\n\n"
        "## 簿册\n\n守灯人记下第九本潮汐簿。"
    )
    reply = "## 塔务\n\n信标换成了琥珀色。\n\n## 风标\n\n塔顶的风向标锈住了。"
    page = _shape_merged_page(old, reply, keep_missing=True)
    assert page.startswith("守灯人住在北岬。")
    assert "## 补给\n\n补给船改在单数日靠岸。\n\n雾钟每隔七分钟敲一次。" in page
    assert "## 簿册\n\n守灯人记下第九本潮汐簿。" in page
    assert "守灯人把蓝色信标调到北偏东。" not in page
    # 新小节接在交回它之前的那一节后面
    assert page.index("## 塔务") < page.index("## 风标") < page.index("## 补给")


def test_a_renamed_section_does_not_stay_twice():
    from rogmem.engine import _shape_merged_page

    old = "## 补给\n\n补给船改在单数日靠岸。\n\n雾钟每隔七分钟敲一次。\n\n## 簿册\n\n守灯人记下第九本潮汐簿。"
    reply = "## 补给与雾钟\n\n补给船改在单数日靠岸。\n\n雾钟每隔七分钟敲一次。\n\n塔里的旧油灯收进了库房。"
    page = _shape_merged_page(old, reply, keep_missing=True)
    assert "## 补给\n" not in page
    assert page.count("补给船改在单数日靠岸。") == 1
    assert "塔里的旧油灯收进了库房。" in page
    assert "守灯人记下第九本潮汐簿。" in page


def test_an_emptied_section_leaves_the_page():
    from rogmem.engine import _shape_merged_page

    old = "## 塔务\n\n守灯人把蓝色信标调到北偏东。\n\n## 簿册\n\n守灯人记下第九本潮汐簿。"
    page = _shape_merged_page(old, "## 塔务\n\n", keep_missing=True)
    assert "## 塔务" not in page
    assert "守灯人记下第九本潮汐簿。" in page
