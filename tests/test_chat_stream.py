"""How one chat call is carried: streamed, so the read timeout is an idle check.

A buffered reply keeps the socket quiet for the whole generation, and the timeout then
has to be a budget for that generation. These cases pin the streamed shape, the token
ledger that rides with it, and the whole-body fallback for a response object that has
no ``read1``.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request

import pytest

from rogmem import engine


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine as mod

    mod.connect(tmp_path, tmp_path / "scan")
    mod.set_embed_mode("off")
    return mod


class _Stream:
    """A response that hands over whatever has arrived, then answers b""."""

    status = 200

    def __init__(self, pieces: list[bytes]):
        self.pieces = list(pieces)

    def read1(self, n: int = -1) -> bytes:
        if not self.pieces:
            return b""
        piece = self.pieces[0]
        if n is not None and n >= 0 and len(piece) > n:
            self.pieces[0] = piece[n:]
            return piece[:n]
        return self.pieces.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Whole:
    """A response object that can only be read whole: no read1."""

    status = 200

    def __init__(self, raw: bytes):
        self.raw = raw
        self.reads = 0

    def read(self, _n: int = -1) -> bytes:
        self.reads += 1
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _sse(content: str = "甲在南港。", finish: str = "stop", usage: dict | None = None) -> list[bytes]:
    events = [
        {"choices": [{"delta": {"content": content}}]},
        {"choices": [{"delta": {}, "finish_reason": finish}]},
    ]
    if usage is not None:
        events.append({"choices": [], "usage": usage})
    body = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
    body += "data: [DONE]\n\n"
    # Delivered in three pieces, so the reader really has to loop.
    mid = len(body) // 2
    return [body[:mid].encode(), body[mid:].encode()]


def test_a_streamed_reply_is_asked_for_and_assembled(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["payload"] = json.loads(req.data.decode())
        seen["timeout"] = timeout
        return _Stream(_sse())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    answer, reason = engine_mod._complete_chat(
        "https://gate.test/v1", "sk", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert answer == "甲在南港。"
    assert reason == "stop"
    assert seen["payload"]["stream"] is True
    assert seen["payload"]["stream_options"] == {"include_usage": True}
    assert seen["timeout"] and seen["timeout"] > 0


def test_the_token_ledger_survives_a_streamed_reply(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    usage = {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_a, **_k: _Stream(_sse(usage=usage))
    )
    engine_mod._complete_chat(
        "https://gate.test/v1", "sk", {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        role="distill",
    )
    con = sqlite3.connect(tmp_path / "cost.sqlite")
    try:
        row = con.execute(
            "SELECT input_tokens, output_tokens FROM model_calls ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    assert row == (11, 4)


def test_a_reply_that_stopped_for_room_keeps_its_reason(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_a, **_k: _Stream(_sse(content="写到一半", finish="length"))
    )
    answer, reason = engine_mod._complete_chat(
        "https://gate.test/v1", "sk", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert answer == "写到一半"
    assert reason == "length"


def test_a_response_without_read1_is_read_whole_once(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    body = json.dumps(
        {"choices": [{"message": {"content": "整份读完"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        ensure_ascii=False,
    ).encode()
    resp = _Whole(body)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: resp)
    answer, reason = engine_mod._complete_chat(
        "https://gate.test/v1", "sk", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert answer == "整份读完"
    assert reason == "stop"
    assert resp.reads == 1


def test_the_reader_stops_at_eof_and_at_the_cap():
    pieces = [b"aaaa", b"bbbb", b"cc"]
    resp = _Stream(pieces)
    assert engine._read_completion(resp) == "aaaabbbbcc"

    resp2 = _Stream([b"x" * 10, b"y" * 10, b"z" * 10])
    assert engine._read_completion(resp2, limit=15) == "x" * 10 + "y" * 5


def test_a_body_that_is_a_quota_window_still_classifies(tmp_path, monkeypatch):
    """A 200 that carries a MiniMax fault is read the same way whether or not it streamed."""
    engine_mod = _open(tmp_path, monkeypatch)

    def fake_urlopen(*_a, **_k):
        return _Whole(b'{"choices":[],"base_resp":{"status_code":2056,"status_msg":"usage limit"}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(engine_mod.ProviderRefused) as caught:
        engine_mod._complete_chat(
            "https://gate.test/v1", "sk", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        )
    assert caught.value.reason == "quota_window"
