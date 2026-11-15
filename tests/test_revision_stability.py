"""A revision keeps unnamed paragraphs verbatim. Synthetic sentences only."""

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


def test_an_unnamed_paragraph_stays_in_front_of_a_named_change():
    from rogmem.engine import _restore_unnamed_blocks

    old = "航线改到雾港外侧。\n\n夜航员把铜铃留在第三格。"
    new = "青苔记下了第四次潮位。\n\n铜铃改挂到第五格。"
    gist = "夜航员把铜铃留在第三格。这一句改了。"
    page = _restore_unnamed_blocks(old, new, gist, [])
    assert page.index("航线改到雾港外侧。") < page.index("铜铃改挂到第五格。")
    assert "青苔记下了第四次潮位。" not in page
    assert "夜航员把铜铃留在第三格。" not in page


def test_an_extra_paragraph_stays_when_the_only_paragraph_is_rewritten():
    from rogmem.engine import _restore_unnamed_blocks

    old = "夜航员把铜铃留在第三格。"
    new = "铜铃改挂到第五格。\n\n青苔记下了第四次潮位。"
    page = _restore_unnamed_blocks(old, new, "", [])
    assert "夜航员把铜铃留在第三格。" in page
    assert "青苔记下了第四次潮位。" in page
    assert "铜铃改挂到第五格。" in page


def test_an_equal_rewrite_does_not_stack_another_copy():
    from rogmem.engine import _restore_unnamed_blocks

    old = "夜航员把铜铃留在第三格。\n\n航线改到雾港外侧。"
    new = "铜铃改挂到第五格。\n\n青苔记下了第四次潮位。"
    page = _restore_unnamed_blocks(old, new, "", [])
    assert "夜航员把铜铃留在第三格。" in page
    assert "航线改到雾港外侧。" in page
    assert "铜铃改挂到第五格。" not in page
    assert "青苔记下了第四次潮位。" not in page


def test_words_kept_inside_the_paragraph_keep_the_added_fact():
    from rogmem.engine import _restore_unnamed_blocks

    old = "夜航员把铜铃留在第三格。"
    new = "夜航员把铜铃留在第三格。青苔记下了第四次潮位。"
    page = _restore_unnamed_blocks(old, new, "", [])
    assert page.count("夜航员把铜铃留在第三格。") == 1
    assert "青苔记下了第四次潮位。" in page


def test_naming_only_a_link_lets_that_link_leave_and_keeps_the_other_words():
    from rogmem.engine import _restore_unnamed_blocks

    old = "夜航员把铜铃留在[[潮信]]第三格。"
    kept = _restore_unnamed_blocks(old, "夜航员把铜铃留在第三格。", "[[潮信]]", [])
    assert "夜航员把铜铃留在第三格。" in kept
    assert "[[潮信]]" not in kept
    locked = _restore_unnamed_blocks(old, "铜铃改挂到第五格。", "[[潮信]]", [])
    assert "夜航员把铜铃留在第三格。" in locked
    assert "[[潮信]]" not in locked
    assert "铜铃改挂到第五格。" not in locked


def test_a_punctuation_footer_is_not_pasted_back_as_a_paragraph():
    from rogmem.engine import _restore_unnamed_blocks

    old = "夜航员把铜铃留在第三格。\n\n[^1] [[潮信]]。\n\n[^2]： 页尾记号"
    new = "夜航员把铜铃留在第三格。\n\n青苔记下了第四次潮位。\n\n[^1] [[潮信]]。"
    page = _restore_unnamed_blocks(old, new, "", [])
    assert "青苔记下了第四次潮位。" in page
    assert "[[潮信]]" not in page
    assert "页尾记号" not in page
    assert "[^2]" not in page


def test_a_full_rewrite_is_not_logged_as_one_pile_of_deletions():
    from rogmem.engine import _describe_text_change

    before = "夜航员把铜铃留在第三格。\n\n航线改到雾港外侧。\n\n铜铃仍挂在第三格。"
    after = "铜铃改挂到第五格。\n\n青苔记下了第四次潮位。\n\n外海多了一处浮标。"
    change = _describe_text_change(before, after)
    assert change.index("铜铃改挂到第五格。") < change.index("航线改到雾港外侧。")
    assert change.index("青苔记下了第四次潮位。") < change.index("铜铃仍挂在第三格。")


def test_an_unnamed_rewrite_is_pasted_back_and_an_insertion_stays():
    from rogmem.engine import _restore_unnamed_blocks

    old = "夜航员把铜铃留在第三格。\n\n航线改到雾港外侧。"
    new = "铜铃改挂到第五格。\n\n航线改到雾港外侧。\n\n青苔记下了第四次潮位。"
    page = _restore_unnamed_blocks(old, new, "", [])
    assert "夜航员把铜铃留在第三格。" in page
    assert "铜铃改挂到第五格。" not in page
    assert "青苔记下了第四次潮位。" in page
    assert page.index("夜航员把铜铃留在第三格。") < page.index("航线改到雾港外侧。")
    assert page.index("航线改到雾港外侧。") < page.index("青苔记下了第四次潮位。")


def test_a_named_paragraph_can_change_without_taking_its_neighbor():
    from rogmem.engine import _restore_unnamed_blocks

    old = "夜航员把铜铃留在第三格。\n\n航线改到雾港外侧。"
    new = "铜铃改挂到第五格。\n\n外海多了一处浮标。"
    gist = "夜航员把铜铃留在第三格。这一句改了。"
    page = _restore_unnamed_blocks(old, new, gist, [])
    assert "铜铃改挂到第五格。" in page
    assert "航线改到雾港外侧。" in page
    assert "外海多了一处浮标。" not in page
    assert "夜航员把铜铃留在第三格。" not in page


def test_old_headings_keep_their_order_and_a_new_heading_stays():
    from rogmem.engine import _restore_unnamed_blocks

    old = "## 夜航\n\n夜航员把铜铃留在第三格。\n\n## 外海\n\n航线改到雾港外侧。"
    new = "## 外海\n\n航线改到雾港外侧。\n\n## 浮标\n\n外海多了一处浮标。\n\n## 夜航\n\n铜铃改挂到第五格。"
    page = _restore_unnamed_blocks(old, new, "", [])
    assert page.index("## 夜航") < page.index("## 外海") < page.index("## 浮标")
    assert "夜航员把铜铃留在第三格。" in page
    assert "铜铃改挂到第五格。" not in page
    assert "外海多了一处浮标。" in page


def test_an_update_pastes_the_old_wording_back_beside_the_new_sentence():
    from rogmem.engine import _restore_unnamed_blocks

    old = "## 夜航\n\n夜航员把铜铃留在第三格。"
    new = "## 夜航\n\n铜铃改挂到第五格。"
    explanation = "夜航员把铜铃留在第三格。"
    conflicts = [{"matter": "铜铃", "verdict": "更新", "basis": explanation}]
    page = _restore_unnamed_blocks(old, new, explanation, conflicts)
    assert "夜航员把铜铃留在第三格。" in page
    assert "铜铃改挂到第五格。" in page
    assert page.index("夜航员把铜铃留在第三格。") < page.index("铜铃改挂到第五格。")


def test_an_update_does_not_paste_wording_that_already_sits_on_the_page():
    from rogmem.engine import _restore_unnamed_blocks

    old = "## 夜航\n\n夜航员把铜铃留在第三格。\n\n## 外海\n\n航线改到雾港外侧。"
    new = "## 夜航\n\n铜铃改挂到第五格。\n\n## 外海\n\n航线改到雾港外侧。\n\n夜航员把铜铃留在第三格。"
    explanation = "夜航员把铜铃留在第三格。"
    conflicts = [{"matter": "铜铃", "verdict": "更新", "basis": explanation}]
    page = _restore_unnamed_blocks(old, new, explanation, conflicts)
    assert page.count("夜航员把铜铃留在第三格。") == 1
    assert "铜铃改挂到第五格。" in page
    assert page.index("## 外海") < page.index("夜航员把铜铃留在第三格。")


def test_a_trailing_mark_line_is_not_a_paragraph_and_is_not_hung_again():
    from rogmem.engine import _keep_unnamed_relations, _restore_unnamed_blocks

    old = "夜航员把铜铃留在第三格。[^1]\n\n[^1]: 实 「夜航员把铜铃留在第三格。」\n\n[^2] [[潮信]]"
    new = "夜航员把铜铃留在第三格。[^1]\n\n青苔记下了第四次潮位。\n\n[^2] [[潮信]]"
    restored = _restore_unnamed_blocks(old, new, "", [])
    page = _keep_unnamed_relations(restored, "", old, new, hang_unmatched=False)
    assert "青苔记下了第四次潮位。" in page
    assert "夜航员把铜铃留在第三格。[^1]" in page
    assert '[^1]: 实 「夜航员把铜铃留在第三格。」' in page
    assert "[[潮信]]" not in page
    assert not any(line.strip() == "[^2]" or line.strip().startswith("[^2]") for line in page.splitlines())


def test_a_mark_line_with_no_sentence_does_not_come_back_with_the_new_sentence():
    from rogmem.engine import _restore_unnamed_blocks

    old = "[^2] [[潮信]]"
    new = "青苔记下了第四次潮位。\n\n[^2] [[潮信]]"
    page = _restore_unnamed_blocks(old, new, "", [])
    assert page.strip() == "青苔记下了第四次潮位。"
    assert "[[潮信]]" not in page


def test_a_different_subject_is_not_pasted_back_onto_the_old_page():
    from rogmem.engine import _keep_unnamed_relations

    new = "青苔记下了第四次潮位。[^1]\n\n[^1]: 实 「青苔记下了第四次潮位。」"
    page = _keep_unnamed_relations(new, "这是另一处。", new)
    assert page.strip() == new.strip()


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


def test_merge_of_the_same_page_uses_the_restored_body(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    old = "夜航员把铜铃留在第三格。[^1]\n\n[^1]: 实 「夜航员把铜铃留在第三格。」\n\n[^2] [[潮信]]"
    rewritten = "铜铃改挂到第五格。\n\n[^2] [[潮信]]"

    def fake(_base, _key, payload, **_kwargs):
        return (
            json.dumps(
                {"same": "是", "page": rewritten, "conflicts": [], "gist": "铜铃换了格子。"},
                ensure_ascii=False,
            ),
            "stop",
        )

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake)
    page, _conflicts, _note, _gist, separate, _name, _ctype = engine._merge_existing_page(old, rewritten, "合成材料。")
    assert separate is False
    assert "夜航员把铜铃留在第三格。" in page
    assert "铜铃改挂到第五格。" not in page
    assert "[[潮信]]" not in page
    assert "[^2]" not in page
