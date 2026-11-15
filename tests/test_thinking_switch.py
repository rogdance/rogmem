"""Each worker has a reasoning level. The top level leaves the request unchanged."""

from rogmem.engine import normalize_reasoning_level, thinking_request
from rogmem.ui import pipeline_page


def test_the_top_level_adds_nothing():
    assert thinking_request("MiniMax-M3.1-Flash", "max") == {}
    assert thinking_request("MiniMax-M3", "max") == {}
    assert normalize_reasoning_level("") == "max"
    assert normalize_reasoning_level("1") == "max"


def test_flash_off_falls_to_its_lowest_effort():
    fields = thinking_request("MiniMax-M3.1-Flash", "off")
    assert fields == {"thinking": {"type": "adaptive"}, "reasoning_effort": "low"}
    assert thinking_request("MiniMax-M3.1-Flash", "0")["reasoning_effort"] == "low"


def test_flash_middle_is_medium():
    assert thinking_request("MiniMax-M3.1-Flash", "medium") == {
        "thinking": {"type": "adaptive"},
        "reasoning_effort": "medium",
    }
    assert thinking_request("MiniMax-M3.1-Flash", "xhigh")["reasoning_effort"] == "xhigh"


def test_m3_off_skips_thinking_and_higher_levels_only_turn_it_on():
    assert thinking_request("MiniMax-M3", "off") == {"thinking": {"type": "disabled"}}
    assert thinking_request("MiniMax-M3", "medium") == {"thinking": {"type": "adaptive"}}


def test_an_unknown_model_is_not_sent_a_field_it_may_reject():
    assert thinking_request("other-model", "low") == {}


def test_embed_worker_lists_only_embedding_models():
    providers = [
        {
            "id": "chatty",
            "name": "对话供应商",
            "models": ["m3"],
            "model_kinds": {"m3": "chat"},
        },
        {
            "id": "local-embed",
            "name": "本地",
            "models": ["BAAI/bge-small-zh-v1.5"],
            "model_kinds": {"BAAI/bge-small-zh-v1.5": "embed"},
        },
    ]
    page = pipeline_page(providers, {"model_embed": "local-embed|BAAI/bge-small-zh-v1.5"}, {"source": "none", "model": ""}, {})
    assert "向量嵌入" in page
    assert "name='model_embed'" in page
    embed = page.split("name='model_embed'", 1)[1].split("</select>", 1)[0]
    assert "bge-small-zh" in embed
    assert "对话供应商" not in embed
    distill = page.split("name='model_distill'", 1)[1].split("</select>", 1)[0]
    assert "对话供应商" in distill
    assert "bge-small-zh" not in distill
    models = __import__("rogmem.ui", fromlist=["models_page"]).models_page(providers, {}, {"source": "none"})
    assert "模型类型" in models
    assert "文本对话" in models
    assert "向量嵌入" in models
    assert "向量模型" not in models
    assert "/admin/api/settings/embed" not in models


def test_pipeline_page_offers_a_level_for_each_worker():
    page = pipeline_page([], {}, {"source": "none", "model": ""}, {})
    for name in ("thinking_distill", "thinking_merge"):
        assert f"name='{name}'" in page
    assert "thinking_prose" not in page
    assert "推理强度" in page
    assert ".worker-field.think select { width: auto; }" in page
    distill = page.split("初始蒸馏", 1)[1].split("身份合并", 1)[0]
    assert "class='worker-controls'" in distill
    assert "name='model_distill'" in distill and "name='thinking_distill'" in distill
    assert "<option value='medium'>中</option>" in page
    assert "<option value='max' selected>最高</option>" in page


def test_embed_worker_accepts_only_an_embedding_model(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    refused = engine.assign_models({"model_embed": "minimax-cn|MiniMax-M3.1-Flash"})
    assert refused["ok"] is False
    saved = engine.assign_models({"model_embed": "local-embed|BAAI/bge-small-zh-v1.5"})
    assert saved["ok"] is True
    assert engine.get_setting("model_embed") == "local-embed|BAAI/bge-small-zh-v1.5"


def test_a_worker_level_can_be_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    saved = engine.assign_models({**engine.assignments(), "thinking_distill": "medium"})
    assert saved["ok"] is True
    assert engine.reasoning_level("distill") == "medium"
    assert engine.reasoning_level("merge") == "max"
    page = pipeline_page([], {}, {"source": "none", "model": ""}, engine.pipeline_config())
    assert "name='thinking_distill'" in page
    assert "<option value='medium' selected>中</option>" in page
