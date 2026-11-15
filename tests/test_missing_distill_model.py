"""A missing distill model pauses before Match=0 land. It is not a land refusal."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from rogmem import engine


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _clear_models() -> None:
    engine._store_providers([])
    for key in ("model_distill", "model_merge", "model_conflict"):
        engine.set_setting(key, "")
    engine.db().commit()


def _queue_file(sid: str, text: str) -> None:
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready,filename)
           VALUES(?, 'file', 'qa', 'qa', ?, ?, ?, ?, 'batch', 1, ?)""",
        (sid, engine.utcnow(), sid + "-h", text, engine.ST_VEC_READY, sid + ".md"),
    )
    con.execute(
        "INSERT INTO chunks(source_id,seq,loc,text,role) VALUES(?,1,'1',?,'child')",
        (sid, text),
    )
    con.execute(
        "INSERT INTO queue(source_id,lane,state,cursor) VALUES(?, 'batch', 'queued', 0)",
        (sid,),
    )
    con.commit()


def test_missing_distill_model_pauses_before_land(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    _clear_models()
    text = "北窗试制只留下一句工序说明，没有点名任何人。"
    _queue_file("src-no-model", text)
    before = {
        row["id"]
        for row in engine_mod.db().execute("SELECT id FROM crystallizations").fetchall()
    }
    assert engine_mod.pump_one() == "src-no-model"
    queued = engine_mod.db().execute(
        "SELECT state, attempts, last_error, next_at FROM queue WHERE source_id='src-no-model'"
    ).fetchone()
    assert queued["last_error"] == "没有配置蒸馏模型"
    assert "integrate/land" not in queued["last_error"]
    assert queued["attempts"] == 0
    assert queued["state"] == "queued"
    assert queued["next_at"].startswith("9999-")
    source = engine_mod.db().execute(
        "SELECT status, last_error FROM sources WHERE id='src-no-model'"
    ).fetchone()
    assert source["status"] == engine_mod.ST_DISTILL_FAIL
    assert source["last_error"] == "没有配置蒸馏模型"
    assert engine_mod.lease_next() is None
    after = {
        row["id"]
        for row in engine_mod.db().execute("SELECT id FROM crystallizations").fetchall()
    }
    assert after == before


def test_assigning_a_distill_model_releases_the_pause(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    _clear_models()
    _queue_file("src-release", "另一句北窗工序，仍然不点名。")
    assert engine_mod.pump_one() == "src-release"
    engine_mod._store_providers(
        [
            {
                "id": "prov1",
                "name": "stub",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "test-key",
                "models": ["stub-model"],
            }
        ]
    )
    engine_mod.db().commit()
    assigned = engine_mod.assign_models({"model_distill": "prov1|stub-model"})
    assert assigned["ok"] is True
    queued = engine_mod.db().execute(
        "SELECT state, attempts, last_error, next_at FROM queue WHERE source_id='src-release'"
    ).fetchone()
    assert queued["state"] == "queued"
    assert queued["attempts"] == 0
    assert queued["last_error"] is None
    assert queued["next_at"] is None
    source = engine_mod.db().execute(
        "SELECT status, last_error FROM sources WHERE id='src-release'"
    ).fetchone()
    assert source["status"] == engine_mod.ST_VEC_READY
    assert source["last_error"] is None
    alert = engine_mod.db().execute(
        "SELECT open FROM alerts WHERE message='没有配置蒸馏模型'"
    ).fetchone()
    assert alert is None or alert["open"] == 0
    wiped = engine_mod.assign_models({})
    assert wiped["ok"] is False
    assert "空分配" in wiped["error"]
    assert engine_mod.get_setting("model_distill") == "prov1|stub-model"


def test_fresh_install_prefills_minimax_china_without_a_key(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(engine_mod, "probe_chat", lambda *_args: calls.append(1) or {"health": "down", "health_detail": "不该探测"})
    engine_mod.connect(tmp_path, tmp_path / "scan")
    providers = {item["id"]: item for item in engine_mod._providers()}
    assert set(providers) == {"minimax-cn", "local-embed", "siliconflow"}
    minimax = providers["minimax-cn"]
    assert minimax["base_url"] == "https://api.minimax.cn/v1"
    assert minimax["models"] == ["MiniMax-M3.1-Flash"]
    assert minimax["model_kinds"]["MiniMax-M3.1-Flash"] == "chat"
    assert minimax["api_key"] == ""
    local = providers["local-embed"]
    assert "BAAI/bge-small-zh-v1.5" in local["models"]
    assert local["model_kinds"]["BAAI/bge-small-zh-v1.5"] == "embed"
    token = "minimax-cn|MiniMax-M3.1-Flash"
    for key in ("model_distill", "model_merge"):
        assert engine_mod.get_setting(key) == token
    assert not engine_mod.get_setting("model_prose")
    assert not engine_mod.get_setting("model_conflict")
    assert engine_mod.get_setting("model_embed") == "siliconflow|BAAI/bge-m3"
    assert engine_mod._embed_is_remote() is False
    silicon = providers["siliconflow"]
    assert silicon["base_url"] == "https://api.siliconflow.cn/v1"
    assert silicon["models"] == list(engine_mod.SILICONFLOW_EMBEDS)
    assert set(silicon["model_kinds"]) == set(engine_mod.SILICONFLOW_EMBEDS)
    assert set(silicon["model_kinds"].values()) == {"embed"}
    assert silicon["api_key"] == ""
    assert calls == []
    engine_mod.connect(tmp_path, tmp_path / "scan")
    again = {item["id"]: item for item in engine_mod._providers()}
    assert set(again) == {"minimax-cn", "local-embed", "siliconflow"}
    assert again["siliconflow"]["models"] == list(engine_mod.SILICONFLOW_EMBEDS)


def test_siliconflow_keeps_a_saved_key_when_more_embed_models_arrive(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    items = engine_mod._providers()
    for item in items:
        if item["id"] != "siliconflow":
            continue
        item["models"] = ["Qwen/Qwen3-Embedding-0.6B"]
        item["model_kinds"] = {"Qwen/Qwen3-Embedding-0.6B": "embed"}
        item["api_key"] = "stored-key"
    engine_mod._store_providers(items)
    engine_mod._ensure_model_catalog()
    silicon = next(item for item in engine_mod._providers() if item["id"] == "siliconflow")
    assert silicon["api_key"] == "stored-key"
    assert silicon["models"][:1] == ["Qwen/Qwen3-Embedding-0.6B"]
    assert silicon["models"][1:] == [
        "BAAI/bge-m3",
        "BAAI/bge-large-zh-v1.5",
        "BAAI/bge-large-en-v1.5",
        "Pro/BAAI/bge-m3",
    ]
    assert set(silicon["model_kinds"].values()) == {"embed"}


def test_the_first_embed_key_uses_bge_m3_and_drops_the_other_vectors(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    _queue_file("src-vec", "中英混在同一段里的材料。")
    engine_mod.db().execute("UPDATE chunks SET vec_json=? WHERE source_id='src-vec'", ("[0.2,0.4]",))
    engine_mod.db().commit()
    assert engine_mod._embed_is_remote() is False
    monkeypatch.setattr(engine_mod, "probe_embed", lambda *_args: {"health": "ok", "health_detail": "正常"})
    saved = engine_mod.set_provider_key("siliconflow", "sk-embed")
    assert saved["ok"] is True
    assert engine_mod.get_setting("model_embed") == "siliconflow|BAAI/bge-m3"
    assert engine_mod._embed_is_remote() is True
    stored = engine_mod.db().execute("SELECT vec_json FROM chunks WHERE source_id='src-vec'").fetchone()
    assert stored["vec_json"] is None
    again = engine_mod.set_provider_key("siliconflow", "sk-embed-2")
    assert again["ok"] is True
    assert engine_mod.get_setting("model_embed") == "siliconflow|BAAI/bge-m3"


def _store_siliconflow_key(engine_mod, key: str = "stored-key") -> None:
    items = engine_mod._providers()
    for item in items:
        if item["id"] == "siliconflow":
            item["api_key"] = key
    engine_mod._store_providers(items)


def test_a_keyed_remote_embedder_does_not_load_local_weights(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    _queue_file("src-keep", "已经有向量的一句。")
    engine_mod.db().execute("UPDATE chunks SET vec_json=? WHERE source_id='src-keep'", ("[0.5]",))
    engine_mod.db().commit()
    _store_siliconflow_key(engine_mod)
    assert engine_mod._embed_is_remote() is True

    def refuse(*_args, **_kwargs):
        raise AssertionError("本机权重不该装")

    import fastembed

    monkeypatch.setattr(fastembed, "TextEmbedding", refuse)
    engine_mod.set_embed_mode("fastembed")
    assert engine_mod._MODEL is None
    assert engine_mod._EMBED == "fastembed"
    assert engine_mod.os.environ["OMP_NUM_THREADS"] == "1"
    kept = engine_mod.db().execute("SELECT vec_json FROM chunks WHERE source_id='src-keep'").fetchone()
    assert kept["vec_json"] == "[0.5]"


def test_a_rejected_embedding_list_is_split(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    _store_siliconflow_key(engine_mod)
    calls: list[int] = []

    class _Resp:
        def __init__(self, count: int):
            self._count = count

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            data = [{"index": index, "embedding": [float(index), 0.2]} for index in range(self._count)]
            return json.dumps({"data": data}).encode()

    def fake_open(req, timeout=60):
        del timeout
        body = json.loads(req.data.decode())
        texts = body["input"]
        count = len(texts) if isinstance(texts, list) else 1
        calls.append(count)
        if count > 2:
            raise urllib.error.HTTPError(req.full_url, 400, "too many", hdrs=None, fp=None)
        return _Resp(count)

    monkeypatch.setattr(engine_mod.urllib.request, "urlopen", fake_open)
    vectors = engine_mod._remote_embed(["甲", "乙", "丙", "丁", "戊"])
    assert [item is not None for item in vectors] == [True, True, True, True, True]
    assert calls[0] == 5
    assert 2 in calls


def test_empty_key_pauses_without_calling_the_network(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(
        engine_mod,
        "_complete_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("empty key must not call the network")),
    )
    _queue_file("src-no-key", "北窗另一句工序，仍然不点名。")
    assert engine_mod.pump_one() == "src-no-key"
    queued = engine_mod.db().execute(
        "SELECT last_error, attempts FROM queue WHERE source_id='src-no-key'"
    ).fetchone()
    assert queued["last_error"] == "供应商密钥还没填"
    assert queued["attempts"] == 0
    assert engine_mod.lease_next() is None


def test_saving_the_token_releases_the_key_pause(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    _queue_file("src-token", "填上密钥之后才该继续的一句。")
    assert engine_mod.pump_one() == "src-token"
    monkeypatch.setattr(engine_mod, "probe_chat", lambda *_args: {"health": "ok", "health_detail": "正常"})
    saved = engine_mod.set_provider_key("minimax-cn", "sk-later")
    assert saved["ok"] is True
    raw = engine_mod.db().execute("SELECT v FROM settings WHERE k='providers'").fetchone()["v"]
    assert "sk-later" not in raw
    queued = engine_mod.db().execute(
        "SELECT state, last_error, next_at, attempts FROM queue WHERE source_id='src-token'"
    ).fetchone()
    assert queued["state"] == "queued"
    assert queued["last_error"] is None
    assert queued["next_at"] is None
    assert queued["attempts"] == 0


def test_existing_provider_is_left_alone(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    engine_mod._store_providers(
        [
            {
                "id": "other",
                "name": "别的",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "kept",
                "models": ["other-model"],
            }
        ]
    )
    engine_mod.set_setting("model_distill", "")
    engine_mod.db().commit()
    engine_mod.connect(tmp_path, tmp_path / "scan")
    providers = {item["id"]: item for item in engine_mod._providers()}
    assert providers["other"]["api_key"] == "kept"
    assert providers["other"]["models"] == ["other-model"]
    assert providers["other"]["model_kinds"]["other-model"] == "chat"
    assert "local-embed" in providers
    assert "siliconflow" in providers
    assert engine_mod.get_setting("model_distill") == ""
