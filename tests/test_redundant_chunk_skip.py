"""一份材料一次萃取。小块只留给检索。

模型看到的是整份正文。空稿结束这一条，不把小块标成已引用。
写出了中稿但合并失败时，这一条仍然失败。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.nomid_extract_fail_gate


def _engine(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _queue_file(engine, source_id: str, body: str, chunks: list[tuple[int, str]]) -> None:
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready,filename)
           VALUES(?, 'file', 'qa', '极空间', ?, ?, ?, ?, 'fast', 1, '测试材料.md')""",
        (source_id, engine.utcnow(), source_id + "-h", body, engine.ST_VEC_READY),
    )
    for seq, text in chunks:
        con.execute(
            "INSERT INTO chunks(source_id,seq,loc,text,role) VALUES(?,?,?,?, 'child')",
            (source_id, seq, str(seq), text),
        )
    con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'fast', 'queued')", (source_id,))
    con.commit()


BODY = "云岚甲在北窗实验室任职，后来转到南港书院。韩跑跑在腾讯负责本地化团队，后来业务移交给兄弟团队。"
CHUNKS = [
    (0, "云岚甲在北窗实验室任职，后来转到南港书院。"),
    (1, "后来业务移交给兄弟团队。"),
    (2, "韩跑跑在腾讯负责本地化团队，后来业务移交给兄弟团队。"),
]
HITS = [{"id": "c-yunlan", "name": "云岚甲", "type": "人物", "aliases": ["测试员甲"]}]


def test_a_file_is_distilled_once_from_the_whole_body(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {"library_candidates": HITS})
    seen: list[str] = []
    monkeypatch.setattr(engine, "_llm_draft", lambda text, *_a, **_k: seen.append(text) or {"crystals": []})
    _queue_file(engine, "src-whole", BODY, CHUNKS)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-whole'").fetchone()
    qid = engine.db().execute("SELECT id FROM queue WHERE source_id='src-whole'").fetchone()["id"]

    with pytest.raises(RuntimeError):
        engine._distill(source, qid)
    assert len(seen) == 1
    assert BODY in seen[0]
    cited = list(engine.db().execute("SELECT cited FROM chunks WHERE source_id='src-whole'"))
    assert all(row["cited"] == 0 for row in cited)


def test_a_fact_from_the_whole_file_lands_once(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {"library_candidates": HITS})
    calls = {"n": 0}

    def spy_draft(_text, *_a, **_k):
        calls["n"] += 1
        return {
            "crystals": [
                {
                    "name": "韩跑跑",
                    "type": "人物",
                    "facts": [{"claim": "在腾讯负责本地化团队", "evidence": "韩跑跑在腾讯负责本地化团队，后来业务移交给兄弟团队。"}],
                }
            ]
        }

    monkeypatch.setattr(engine, "_llm_draft", spy_draft)
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: {"identity": "new", "ops": [], "conflicts": []})
    _queue_file(engine, "src-land", BODY, CHUNKS)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-land'").fetchone()
    qid = engine.db().execute("SELECT id FROM queue WHERE source_id='src-land'").fetchone()["id"]

    assert engine._distill(source, qid) is True
    assert calls["n"] == 1
    rows = list(
        engine.db().execute(
            "SELECT crystal_id FROM statements WHERE source_id='src-land' AND invalid_at IS NULL"
        )
    )
    assert rows
    assert {row["crystal_id"] for row in rows} == {"owner"}


def test_a_mid_whose_merge_failed_still_stops(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {"library_candidates": HITS})
    monkeypatch.setattr(
        engine,
        "_llm_draft",
        lambda *_a, **_k: {
            "crystals": [
                {
                    "name": "云岚甲",
                    "type": "人物",
                    "facts": [{"claim": "在北窗实验室任职", "evidence": "云岚甲在北窗实验室任职，后来转到南港书院。"}],
                }
            ]
        },
    )
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: None)
    _queue_file(engine, "src-fail", BODY, CHUNKS)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-fail'").fetchone()
    qid = engine.db().execute("SELECT id FROM queue WHERE source_id='src-fail'").fetchone()["id"]

    with pytest.raises(RuntimeError):
        engine._distill(source, qid)
