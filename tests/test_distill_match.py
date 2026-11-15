"""Program Match: name/alias only. ≥1 hits → all candidates; 0 → {}."""

from __future__ import annotations

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED
from rogmem.distill_match import match_integrate_parts

LIBRARY = [
    {"id": "c-yunlan", "name": "云岚甲", "type": "人物", "aliases": ["测试员甲"]},
    {"id": "owner", "name": "记忆主人", "type": "人物", "aliases": []},
]

TEXT_HIT = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
TEXT_MISS = "今天只谈天气，不提任何人名。"

DRAFT_WITH_FACTS = {
    "name": "云岚甲",
    "type": "人物",
    "aliases": ["测试员甲"],
    "facts": [
        {
            "claim": "云岚甲于 2019 年在北窗实验室任职。",
            "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
        }
    ],
}


def test_match_unique_alias_returns_candidates_without_empty_facts_stub():
    assert DISTILLER_STORE_WRITE_ENABLED is True
    parts = match_integrate_parts(TEXT_HIT, LIBRARY)
    assert parts["library_candidates"][0]["id"] == "c-yunlan"
    assert parts["library_candidates"][0]["name"] == "云岚甲"
    assert "draft_crystal" not in parts
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_match_zero_hits_returns_empty():
    assert match_integrate_parts(TEXT_MISS, LIBRARY) == {}
    assert match_integrate_parts(TEXT_HIT, []) == {}
    assert match_integrate_parts("", LIBRARY) == {}
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_match_two_library_hits_returns_all_candidates():
    """≥2 name hits stay on Integrate: return every hit, never {}."""
    library = [
        {"id": "c-lab", "name": "北窗实验室", "type": "组织", "aliases": []},
        {"id": "c-school", "name": "南港书院", "type": "组织", "aliases": []},
    ]
    parts = match_integrate_parts(TEXT_HIT, library)
    ids = [item["id"] for item in parts["library_candidates"]]
    assert ids == ["c-lab", "c-school"]
    assert "draft_crystal" not in parts
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_match_reuses_names_correspond_for_spelling():
    library = [{"id": "c-zhang", "name": "张三", "type": "人物", "aliases": []}]
    parts = match_integrate_parts("章三来了", library)
    assert parts["library_candidates"][0]["id"] == "c-zhang"
    assert parts["library_candidates"][0]["name"] == "张三"
    assert "draft_crystal" not in parts
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_match_skips_candidate_without_id():
    library = [{"name": "云岚甲", "type": "人物", "aliases": ["测试员甲"]}]
    assert match_integrate_parts(TEXT_HIT, library) == {}
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_pick_matched_draft_crystal_requires_nonempty_facts():
    from rogmem.distill_match import pick_matched_draft_crystal

    hit = LIBRARY[0]
    assert pick_matched_draft_crystal({"crystals": [DRAFT_WITH_FACTS]}, hit) == DRAFT_WITH_FACTS
    stub = {"name": "云岚甲", "type": "人物", "aliases": ["测试员甲"], "facts": []}
    assert pick_matched_draft_crystal({"crystals": [stub]}, hit) is None
    assert pick_matched_draft_crystal(DRAFT_WITH_FACTS, hit) == DRAFT_WITH_FACTS
    other = {
        "name": "北窗实验室",
        "type": "组织",
        "aliases": [],
        "facts": [{"claim": "x", "evidence": "y"}],
    }
    assert pick_matched_draft_crystal({"crystals": [other, DRAFT_WITH_FACTS]}, hit) == DRAFT_WITH_FACTS
    assert pick_matched_draft_crystal({"crystals": [other]}, hit) is None
    lab = {"id": "c-lab", "name": "北窗实验室", "type": "组织", "aliases": []}
    assert pick_matched_draft_crystal({"crystals": [other]}, [hit, lab]) == other
    assert pick_matched_draft_crystal({"crystals": [DRAFT_WITH_FACTS]}, [lab, hit]) == DRAFT_WITH_FACTS
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_list_draft_crystals_keeps_all_nonempty_facts():
    """N-loop parse: every non-empty-facts mid-稿, empty stubs dropped, order kept."""
    from rogmem.distill_match import list_draft_crystals

    stub = {"name": "云岚甲", "type": "人物", "aliases": ["测试员甲"], "facts": []}
    other = {
        "name": "北窗实验室",
        "type": "组织",
        "aliases": [],
        "facts": [{"claim": "x", "evidence": "y"}],
    }
    assert list_draft_crystals({"crystals": [stub, other, DRAFT_WITH_FACTS]}) == [other, DRAFT_WITH_FACTS]
    assert list_draft_crystals({"crystals": [stub]}) == []
    assert list_draft_crystals(DRAFT_WITH_FACTS) == [DRAFT_WITH_FACTS]
    assert list_draft_crystals(None) == []
    assert DISTILLER_STORE_WRITE_ENABLED is True
