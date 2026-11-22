"""萃取的成本与质量旋钮：一处登记，管线设置里改。

每个旋钮有一个设置键、所属的组、默认值和允许的范围。代码里的同名常量只是默认值，
运行时一律按这里读：设置里没写、写坏了或超出范围，都回到默认值，不报错。
开关写成 "1"/"0"。百分比写成整数 0～100。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Knob:
    key: str
    group: str
    label: str
    unit: str
    default: int
    low: int = 0
    high: int = 10**9
    switch: bool = False
    note: str = ""


GROUPS = (
    ("triage", "进门分诊", "调模型之前先由程序判断：重复材料、例行任务没有新内容、只拿到宿主报错的单元，直接收尾，不花钱。"),
    ("sketch", "速写", "速写跟着正文走，不是每次并页都重写。正文只小改时，一段时间内沿用原来的速写。"),
    ("merge", "并页", "长页只交回改动的小节；输出额度按要写回的字数给，不按模型上限给。"),
    ("batch", "重页攒批整理", "记忆主人页和很长的页，每次交流只把草稿存起来，攒够了一次整理完。"),
    ("horizon", "出处与中长期", "正文是摘要，程序给新增或改动的段落挂上原文编号；修订记录类小节不进正文。"),
)

KNOBS = (
    Knob("distill_triage", "triage", "进门分诊", "开关", 1, 0, 1, switch=True),
    Knob("triage_template_min_chars", "triage", "例行任务的最短提问", "字，提问至少这么长才当作固定模板", 200, 20, 100_000),
    Knob("triage_routine_min_earlier", "triage", "例行任务认定次数", "次，同一模板至少出现过这么多次", 3, 1, 1000),
    Knob("triage_routine_window_hours", "triage", "例行任务萃取间隔", "小时，这段时间内萃取过一次就不再萃取", 24, 1, 24 * 90),
    Knob("triage_routine_similarity", "triage", "例行回复相似度", "%，回复和上次萃取的相似到这个程度才跳过", 60, 1, 100),
    Knob("sketch_debounce", "sketch", "速写防抖", "开关", 1, 0, 1, switch=True),
    Knob("sketch_rewrite_below_similarity", "sketch", "正文相似度门槛", "%，正文和上次写速写时相似度低于它就重写", 90, 1, 100),
    Knob("sketch_stale_hours", "sketch", "小改最长沿用", "小时，正文有小改时最多沿用旧速写这么久", 24, 1, 24 * 30),
    Knob("merge_partial_from_chars", "merge", "分节并页起点", "字，页面超过它就只交回改动的小节", 6000, 500, 1_000_000),
    Knob("merge_budget_base_tokens", "merge", "并页额度底座", "token，留给思考的固定额度", 32768, 1024, 262144),
    Knob("merge_budget_tokens_per_char", "merge", "并页每字额度", "token/字，按要写回的字数再加", 2, 1, 16),
    Knob("page_batch", "batch", "攒批整理", "开关", 1, 0, 1, switch=True),
    Knob("page_batch_from_chars", "batch", "攒批起点", "字，超过它的页（以及记忆主人页）改为攒批", 30000, 1000, 10_000_000),
    Knob("page_batch_min_drafts", "batch", "攒够几份就整理", "份", 8, 1, 200),
    Knob("page_batch_max_age_hours", "batch", "最久等多久就整理", "小时，最早一份草稿等到这么久就整理", 24, 1, 24 * 30),
    Knob("page_batch_max_drafts", "batch", "一次最多整理", "份", 24, 1, 500),
    Knob("page_batch_max_draft_chars", "batch", "一次最多草稿字数", "字", 60000, 1000, 2_000_000),
    Knob("page_batch_material_chars", "batch", "每份附带原文", "字，每份草稿附带的原材料截到这么长", 4000, 0, 200_000),
    Knob("page_batch_retry_hours", "batch", "整理失败后重试间隔", "小时", 6, 1, 24 * 7),
    Knob("page_batch_max_tries", "batch", "整理最多重试", "次", 5, 1, 50),
    Knob("provenance_marks", "horizon", "程序挂原文编号", "开关", 1, 0, 1, switch=True),
    Knob("provenance_mark_cap", "horizon", "每段最多编号", "个，一段末尾最多留几个原文编号", 3, 1, 10),
    Knob("provenance_paragraph_sources", "horizon", "每段最多归到几轮对话", "个", 2, 1, 10),
    Knob("horizon_gate", "horizon", "去掉修订记录类小节", "开关", 1, 0, 1, switch=True),
)

BY_KEY = {knob.key: knob for knob in KNOBS}
DEFAULTS = {knob.key: knob.default for knob in KNOBS}


def parse(knob: Knob, raw) -> int | None:
    """The stored value for one field, or None when it is not a valid value."""
    if knob.switch:
        if raw is True or str(raw).strip().lower() in {"1", "true", "on", "yes"}:
            return 1
        if raw is False or str(raw).strip().lower() in {"0", "false", "off", "no", ""}:
            return 0
        return None
    text = str(raw if raw is not None else "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    if not knob.low <= value <= knob.high:
        return None
    return value


def value(get_setting, key: str) -> int:
    """The live value: the setting when it is valid, else the default."""
    knob = BY_KEY[key]
    try:
        raw = get_setting(key)
    except Exception:
        return knob.default
    if raw is None or str(raw).strip() == "":
        return knob.default
    parsed = parse(knob, raw)
    return knob.default if parsed is None else parsed


def view(get_setting) -> list[dict]:
    """Every group with its knobs and current values, in display order."""
    groups = []
    for gid, title, note in GROUPS:
        items = [
            {
                "key": knob.key,
                "label": knob.label,
                "unit": knob.unit,
                "switch": knob.switch,
                "value": value(get_setting, knob.key),
                "default": knob.default,
                "low": knob.low,
                "high": knob.high,
            }
            for knob in KNOBS
            if knob.group == gid
        ]
        groups.append({"id": gid, "title": title, "note": note, "items": items})
    return groups


def checked(values: dict) -> tuple[dict[str, str], str]:
    """Validate a save. Missing or blank number fields are left as they are."""
    out: dict[str, str] = {}
    for knob in KNOBS:
        if knob.key not in values:
            continue
        raw = values.get(knob.key)
        if not knob.switch and str(raw if raw is not None else "").strip() == "":
            continue
        parsed = parse(knob, raw)
        if parsed is None:
            return {}, f"「{knob.label}」要是 {knob.low} 到 {knob.high} 的整数"
        out[knob.key] = str(parsed)
    return out, ""
