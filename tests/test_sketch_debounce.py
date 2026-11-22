"""A sketch follows the page when the page has moved, not on every merge."""

from datetime import datetime, timedelta, timezone

from rogmem import sketch_basis


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


LONG = "".join(f"第{i}段：青石镇的水车每年清明检修一次，负责人换过三任。\n" for i in range(120))


def test_similarity_tracks_how_much_of_a_body_survives():
    base = sketch_basis.signature(LONG)
    assert sketch_basis.similarity(base, sketch_basis.signature(LONG)) == 1.0
    small = LONG + "补记：水车的木轴去年换成了铁轴。\n"
    assert sketch_basis.similarity(base, sketch_basis.signature(small)) >= sketch_basis.REWRITE_BELOW_SIMILARITY
    other = "".join(f"第{i}条：山北的茶园改种了白茶，采摘期提前到谷雨前。\n" for i in range(120))
    assert sketch_basis.similarity(base, sketch_basis.signature(other)) < 0.3


def test_needs_rewrite_reasons():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    kept = sketch_basis.basis(LONG, now - timedelta(hours=2))
    assert sketch_basis.needs_rewrite(LONG, kept, has_sketch=False, now=now)[0] is True
    assert sketch_basis.needs_rewrite(LONG, None, has_sketch=True, now=now)[0] is True
    assert sketch_basis.needs_rewrite(LONG, kept, has_sketch=True, now=now) == (False, "正文没有变")
    small = LONG + "补记：水车的木轴去年换成了铁轴。\n"
    assert sketch_basis.needs_rewrite(small, kept, has_sketch=True, now=now)[0] is False
    later = now + timedelta(hours=23)
    assert sketch_basis.needs_rewrite(small, kept, has_sketch=True, now=later)[0] is True
    rewritten = LONG[: len(LONG) // 2] + "".join(f"新{i}：镇上开了第二座磨坊。\n" for i in range(80))
    assert sketch_basis.needs_rewrite(rewritten, kept, has_sketch=True, now=now)[0] is True


def _seed_page(engine, body):
    from rogmem.crystal_store import CrystalStore, hydrate

    engine._insert_crystal_page(engine.db(), "pg-1", "概念", "青石水车", holder="test")
    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='pg-1'").fetchone()
    crystal = hydrate(row)
    crystal.body_md = body
    CrystalStore(engine.db()).put(crystal, holder="test")
    engine.db().commit()


def _set_body(engine, body):
    from rogmem.crystal_store import CrystalStore, hydrate

    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='pg-1'").fetchone()
    crystal = hydrate(row)
    crystal.body_md = body
    CrystalStore(engine.db()).put(crystal, holder="test")
    engine.db().commit()


def test_write_sketch_skips_the_model_when_the_page_barely_moved(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    calls = []

    def fake_sketch(body, ctype, *, owner):
        calls.append(len(body))
        return "青石水车是镇上的一座水车，每年清明检修，负责人换过三任。" * 2

    monkeypatch.setattr(engine, "_ask_sketch", fake_sketch)
    _seed_page(engine, LONG)
    engine._write_sketch("pg-1")
    assert len(calls) == 1
    engine._write_sketch("pg-1")
    assert len(calls) == 1, "unchanged body must not buy a new sketch"
    _set_body(engine, LONG + "补记：水车的木轴去年换成了铁轴。\n")
    engine._write_sketch("pg-1")
    assert len(calls) == 1, "a small change within a day keeps the sketch"
    _set_body(engine, "".join(f"新{i}：镇上开了第二座磨坊，水车只留作展示。\n" for i in range(150)))
    engine._write_sketch("pg-1")
    assert len(calls) == 2, "a page that moved gets a new sketch"
    kinds = [row[0] for row in engine.db().execute("SELECT kind FROM crystal_events WHERE crystal_id='pg-1'")]
    assert kinds.count("sketch_basis") == 2
    assert kinds.count("sketch_skip") == 2


def test_debounce_can_be_switched_off(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        engine, "_ask_sketch", lambda body, ctype, *, owner: calls.append(1) or "青石水车每年清明检修，负责人换过三任。" * 3
    )
    _seed_page(engine, LONG)
    engine.set_setting("sketch_debounce", "0")
    engine._write_sketch("pg-1")
    engine._write_sketch("pg-1")
    assert len(calls) == 2
