"""Name matching reads the body once, not once per label.

``_label_in_text`` walks every window of a body. Both sides of the old question went
through pypinyin, so one real 270-character body cost 5 seconds and the whole process is
behind one GIL. These tests pin the two things that fix it: the answer is unchanged, and
the transliteration no longer scales with body length times labels.
"""

from __future__ import annotations

import pypinyin

from rogmem import distill_match, names


def _old_label_in_text(label: str, text: str) -> bool:
    """The walk as it was: every window, each one asked through names_correspond."""
    raw = (label or "").strip()
    if len(raw) < 2 or not text:
        return False
    if raw in text:
        return True
    key = names.name_key(raw)
    if len(key) >= 2 and key in names.name_key(text):
        return True
    width = len(raw)
    for index in range(max(0, len(text) - width + 1)):
        if names.names_correspond(raw, text[index : index + width]):
            return True
    return False


CASES = [
    ("云岚甲", "云岚甲今天在北窗实验室值班。"),
    ("云岚甲", "云-岚甲今天在北窗实验室值班。"),
    ("云岚甲", "值班的是云岚乇，不是别人。"),
    ("甲乙", "名单上写的是乙甲。"),
    ("云岚纸", "这一页讲的是云岚紙的做法。"),
    ("珍珠", "盒子里是珍蛛，不是别的。" ),
    ("Alice", "aliceworks 提交了这份记录。"),
    ("周生生", "周生生 的门店在楼下。"),
    ("云岚甲", "今天只谈天气，不提任何人名。"),
    ("云岚甲", "很长的一段话" * 30 + "末尾才提到云岚甲一次。"),
    ("记忆主人", "这一段什么名字都没有。"),
    ("甲乙丙", "甲乙"),
]


def test_the_walk_answers_exactly_what_it_used_to():
    for label, text in CASES:
        assert distill_match._label_in_text(label, text) == _old_label_in_text(label, text), (label, text)


def test_the_answer_does_not_depend_on_a_prepared_body():
    for label, text in CASES:
        scan = distill_match._BodyScan(text)
        assert distill_match._label_in_text(label, text, scan) == _old_label_in_text(label, text)


def test_one_body_is_transliterated_per_character_not_per_window(monkeypatch):
    names.syllable.cache_clear()
    names._cached_pinyin.cache_clear()
    calls: list[str] = []
    real = pypinyin.lazy_pinyin

    def counted(*args, **kwargs):
        calls.append(args[0] if args else "")
        return real(*args, **kwargs)

    monkeypatch.setattr(pypinyin, "lazy_pinyin", counted)
    body = "银河一百在丙区三号库，库里放着一枚旧罗盘。" * 20
    labels = [f"节点{index}" for index in range(40)]
    for label in labels:
        distill_match._label_in_text(label, body)
    windows = len(body) * len(labels)
    assert windows > 10000
    # One scan per call plus one transliteration of each label. Not one per window.
    assert len(calls) <= len(labels) * (len(set(body)) + 2)


def test_the_library_shares_one_scan_of_the_body(monkeypatch):
    names.syllable.cache_clear()
    names._cached_pinyin.cache_clear()
    calls: list[str] = []
    real = pypinyin.lazy_pinyin

    def counted(*args, **kwargs):
        calls.append(args[0] if args else "")
        return real(*args, **kwargs)

    monkeypatch.setattr(pypinyin, "lazy_pinyin", counted)
    body = "银河一百在丙区三号库，库里放着一枚旧罗盘。" * 20
    library = [{"id": f"c{index}", "name": f"节点{index}"} for index in range(40)]
    distill_match._name_alias_hits(body, library)
    assert len(calls) <= len(set(body)) + len(library) + 2


def test_a_short_key_is_cached_and_a_long_one_is_not_held():
    assert names.name_key("云岚甲") == "云岚甲"
    assert names.name_key("云 岚-甲") == "云岚甲"
    long_text = "云岚甲" * 40
    assert names.name_key(long_text) == "云岚甲" * 40
    assert names.pinyin_key("云岚甲") == names._cached_pinyin("云岚甲")


def test_the_library_answers_what_one_label_at_a_time_would():
    """The batched one-typo walk must not change one answer.

    A typo in the body, a transposition, and a plain miss all go through the batched
    path in ``_name_alias_hits``; the same labels asked one at a time go through
    ``_label_in_text``. The two must agree.
    """
    body = "值班的是云岚乇，名单上写的是乙甲，周生生 的门店在楼下，Alice 也来过。"
    library = [
        {"id": "c0", "name": "云岚甲"},
        {"id": "c1", "name": "甲乙"},
        {"id": "c2", "name": "周生生"},
        {"id": "c3", "name": "Alice", "aliases": ["艾丽丝"]},
        {"id": "c4", "name": "珍珠"},
        {"id": "c5", "name": "云岚纸"},
    ]
    one_at_a_time = [
        item["id"] for item in library if distill_match._label_in_text(item["name"], body)
    ]
    batched = [item["id"] for item in distill_match._name_alias_hits(body, library)]
    assert batched == one_at_a_time
    assert {"c0", "c1", "c2", "c3"} <= set(batched)
    assert "c4" not in batched


def test_the_typo_walk_costs_one_body_per_width_not_one_body_per_label(monkeypatch):
    """The body is walked once per width in play, so the typo rules are asked far less.

    Before the batch, every label compared its key against every window: the two rules
    were called ``labels × windows`` times. Counting the calls pins that the batch is
    not quietly doing the same work.
    """
    calls = {"n": 0}
    real_sub = distill_match._one_substitution
    real_trans = distill_match._one_transposition

    def counted_sub(*args, **kwargs):
        calls["n"] += 1
        return real_sub(*args, **kwargs)

    def counted_trans(*args, **kwargs):
        calls["n"] += 1
        return real_trans(*args, **kwargs)

    monkeypatch.setattr(distill_match, "_one_substitution", counted_sub)
    monkeypatch.setattr(distill_match, "_one_transposition", counted_trans)
    body = "银河一百在丙区三号库，库里放着一枚旧罗盘。" * 80
    library = [{"id": f"c{index}", "name": f"节点{index}"} for index in range(40)]
    distill_match._name_alias_hits(body, library)
    walk_the_old_way = len(body) * len(library)
    assert walk_the_old_way > 60000
    assert calls["n"] < walk_the_old_way // 20


def test_a_syllable_is_the_spoken_form_of_one_character():
    assert names.syllable("云") == "yun"
    assert names.syllable("a") == ""
    assert names.syllable("") == ""
