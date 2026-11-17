"""The written page is the page. Synthetic sentences only.

A merge used to paste back every paragraph the model left out, so a page that had once
accumulated wordings kept all of them: the live owner page reached 860,572 characters, one
merge then sent all of it back to the model, and the provider refused the call. What a
merge replaced now leaves the page and stays readable in `revisions`, which stores the
whole page before and after each change.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "docs" / "bench" / "wiki-distill" / "corpus.json"

SENTENCES = (
    "夜航员把铜铃留在第三格。",
    "航线改到雾港外侧。",
    "青苔记下了第四次潮位。",
    "别名：雾港铃、潮信",
    "铜铃改挂到第五格。",
    "外海多了一处浮标。",
    "铜铃仍挂在第三格。",
    "潮信只在页尾那一行。",
    "铜铃换了格子。",
    "这一句改了。",
    "夜航员把铜铃留在[[潮信]]第三格。",
)


def _overlap(text: str, sample: str, size: int = 8) -> str:
    for index in range(0, len(sample) - size + 1):
        window = sample[index : index + size]
        if window in text:
            return window
    return ""


def test_synthetic_sentences_do_not_repeat_the_corpus():
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    blob = "\n".join(
        (item.get("title") or "") + "\n" + (item.get("plain_text") or "") for item in raw["items"]
    )
    for sample in SENTENCES:
        hit = _overlap(blob, sample)
        assert hit == "", hit


def test_the_written_page_is_the_page():
    from rogmem.engine import _shape_merged_page

    old = "航线改到雾港外侧。\n\n夜航员把铜铃留在第三格。"
    new = "青苔记下了第四次潮位。\n\n铜铃改挂到第五格。"
    page = _shape_merged_page(old, new)
    assert "青苔记下了第四次潮位。" in page
    assert "铜铃改挂到第五格。" in page
    assert "航线改到雾港外侧。" not in page
    assert "夜航员把铜铃留在第三格。" not in page


def test_a_paragraph_the_writer_left_out_leaves_the_page():
    from rogmem.engine import _shape_merged_page

    old = "夜航员把铜铃留在第三格。\n\n航线改到雾港外侧。"
    new = "铜铃改挂到第五格。"
    page = _shape_merged_page(old, new)
    assert page.strip() == "铜铃改挂到第五格。"


def test_old_headings_keep_their_order_and_a_new_heading_stays():
    from rogmem.engine import _shape_merged_page

    old = "## 夜航\n\n夜航员把铜铃留在第三格。\n\n## 外海\n\n航线改到雾港外侧。"
    new = "## 外海\n\n航线改到雾港外侧。\n\n## 浮标\n\n外海多了一处浮标。\n\n## 夜航\n\n铜铃改挂到第五格。"
    page = _shape_merged_page(old, new)
    assert page.index("## 夜航") < page.index("## 外海") < page.index("## 浮标")
    assert "铜铃改挂到第五格。" in page
    assert "外海多了一处浮标。" in page
    assert "夜航员把铜铃留在第三格。" not in page


def test_a_note_definition_is_not_carried_as_a_paragraph():
    from rogmem.engine import _shape_merged_page

    old = "夜航员把铜铃留在第三格。[^1]\n\n[^1]: 实 「夜航员把铜铃留在第三格。」"
    new = "铜铃改挂到第五格。\n\n[^2] [[潮信]]"
    page = _shape_merged_page(old, new)
    assert page.strip() == "铜铃改挂到第五格。"
    assert "[^1]" not in page
    assert "[^2]" not in page
    assert "[[潮信]]" not in page


def test_a_mark_line_with_no_sentence_is_not_a_paragraph():
    from rogmem.engine import _shape_merged_page

    page = _shape_merged_page("[^2] [[潮信]]", "青苔记下了第四次潮位。\n\n[^2] [[潮信]]")
    assert page.strip() == "青苔记下了第四次潮位。"
    assert "[[潮信]]" not in page


def test_a_footnote_keeps_its_note_when_its_sentence_stays():
    from rogmem.engine import _shape_merged_page

    old = "夜航员把铜铃留在第三格。[^1]\n\n[^1]: 实 「夜航员把铜铃留在第三格。」"
    new = "夜航员把铜铃留在第三格。[^1]\n\n青苔记下了第四次潮位。\n\n[^1]: 实 「夜航员把铜铃留在第三格。」"
    page = _shape_merged_page(old, new)
    assert "夜航员把铜铃留在第三格。[^1]" in page
    assert '[^1]: 实 「夜航员把铜铃留在第三格。」' in page
    assert "青苔记下了第四次潮位。" in page


def test_a_different_subject_is_not_pasted_back_onto_the_old_page():
    from rogmem.engine import _keep_unnamed_relations

    new = "青苔记下了第四次潮位。[^1]\n\n[^1]: 实 「青苔记下了第四次潮位。」"
    page = _keep_unnamed_relations(new, "这是另一处。", new)
    assert page.strip() == new.strip()


def test_a_full_rewrite_is_not_logged_as_one_pile_of_deletions():
    from rogmem.engine import _describe_text_change

    before = "夜航员把铜铃留在第三格。\n\n航线改到雾港外侧。\n\n铜铃仍挂在第三格。"
    after = "铜铃改挂到第五格。\n\n青苔记下了第四次潮位。\n\n外海多了一处浮标。"
    change = _describe_text_change(before, after)
    assert change.index("铜铃改挂到第五格。") < change.index("航线改到雾港外侧。")
    assert change.index("青苔记下了第四次潮位。") < change.index("铜铃仍挂在第三格。")


def test_a_diff_is_blocked_in_reading_order_and_the_card_shows_the_first():
    from rogmem.engine import _body_touch_scope, _describe_text_change, _scope_between
    from rogmem.ui import _revision_item, revision_detail

    before = "## 夜航\n\n夜航员把铜铃留在第三格。\n\n## 外海\n\n航线改到雾港外侧。"
    after = "## 夜航\n\n铜铃改挂到第五格。\n\n## 外海\n\n青苔记下了第四次潮位。"
    change = _describe_text_change(before, after)
    assert change.index("夜航员把铜铃留在第三格。") < change.index("铜铃改挂到第五格。")
    assert change.index("铜铃改挂到第五格。") < change.index("航线改到雾港外侧。")
    assert change.index("航线改到雾港外侧。") < change.index("青苔记下了第四次潮位。")
    assert _body_touch_scope(before, after) == "夜航、外海"
    assert _scope_between({"name": "雾港铃", "body": before, "sketch": ""}, {"name": "雾港铃", "body": after, "sketch": ""}) == "夜航、外海"
    assert _scope_between({"name": "", "body": "", "sketch": ""}, {"name": "", "body": "青苔记下了第四次潮位。", "sketch": ""}) == "正文"
    item = {
        "id": 3,
        "change": change,
        "basis": "潮信只在页尾那一行。",
        "gist": "铜铃改挂到第五格。",
        "created_at": "2026-11-13T00:00:00+00:00",
        "actor": "蒸馏",
        "scope": "夜航、外海",
    }
    html = _revision_item("page-1", item, name="雾港铃")
    assert "铜铃改挂到第五格。" in html
    assert "摘要" in html
    assert "航线改到雾港外侧。" not in html
    detail = revision_detail(item)
    assert "航线改到雾港外侧。" in detail
    assert "青苔记下了第四次潮位。" in detail


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_an_updated_fact_is_not_stacked_on_the_page_it_replaced(tmp_path, monkeypatch):
    """A 更新 used to leave the replaced wording beside the new one, so a page grew with
    every merge until a single merge sent the whole page to the model."""
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    page = "夜航员把铜铃留在第三格。"
    for turn in range(6):
        revised = f"夜航员把铜铃挂到了第{turn}格。"
        canned = json.dumps(
            {
                "same": "是",
                "page": revised,
                "conflicts": [{"matter": "铜铃", "verdict": "更新", "basis": page}],
                "gist": "",
            },
            ensure_ascii=False,
        )
        monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: (canned, "stop"))
        page, *_ = engine._merge_existing_page(page, revised, "合成材料。")
    assert page.count("夜航员把铜铃") == 1
    assert page.count("第5格") == 1
    assert "第三格" not in page


def test_a_reply_with_no_usable_paragraph_leaves_the_page_alone():
    from rogmem.engine import _shape_merged_page

    old = "夜航员把铜铃留在第三格。"
    page = _shape_merged_page(old, "## 夜航\n\n[^9]: 实 「没有正文，只有注。」")
    assert page.strip() == old


def test_a_reply_that_stopped_for_room_is_not_written_as_the_page(tmp_path, monkeypatch):
    """A reply cut off for room is not a page. Writing it would drop whatever it never
    reached, and the guard no longer pastes the missing paragraphs back."""
    import pytest

    from rogmem.engine import ModelCallFailed

    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: ("铜铃改挂到第五格。", "length"))
    with pytest.raises(ModelCallFailed):
        engine._merge_existing_page("夜航员把铜铃留在第三格。", "铜铃改挂到第五格。", "合成材料。")


def test_merge_of_the_same_page_writes_the_written_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "夜航员把铜铃留在第三格。[^1]\n\n[^1]: 实 「夜航员把铜铃留在第三格。」\n\n[^2] [[潮信]]"
    written = "铜铃改挂到第五格。\n\n[^2] [[潮信]]"

    def fake(_base, _key, payload, **_kwargs):
        return (
            json.dumps(
                {"same": "是", "page": written, "conflicts": [], "gist": "铜铃换了格子。"},
                ensure_ascii=False,
            ),
            "stop",
        )

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake)
    page, _conflicts, _note, _gist, separate, _name, _ctype = engine._merge_existing_page(old, written, "合成材料。")
    assert separate is False
    assert "铜铃改挂到第五格。" in page
    assert "夜航员把铜铃留在第三格。" not in page
    assert "[[潮信]]" not in page
    assert "[^2]" not in page


def test_landing_the_same_repository_page_twice_does_not_grow_it():
    from rogmem.engine import _keep_both_pages

    page = "## 仓库\n\n仓库地址为 `https://example.invalid/x`。"
    assert _keep_both_pages(page, page) == page
    longer = _keep_both_pages(page, page + "\n\n新的坑：提交前先跑测试。")
    assert longer.count("https://example.invalid/x") == 1
    assert "新的坑：提交前先跑测试。" in longer
    other = _keep_both_pages(page, "## 别的\n\n另一段完全不同的正文。")
    assert other.count("另一段完全不同的正文。") == 1
    assert other.startswith(page)
