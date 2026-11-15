"""Provider probes: unreachable is red, a answered error is yellow, and the log hears it once."""

import io
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

from rogmem import engine
from rogmem.ui import models_page


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


class _Body:
    def __init__(self, status: int, raw: bytes):
        self.status = status
        self._raw = raw

    def read(self, _n=-1):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _http_error(url: str, status: int, raw: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "error", Message(), io.BytesIO(raw))


def test_unreachable_is_red(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    result = engine.probe_chat("http://vendor.test/v1", "sk", "m")
    assert result["health"] == "down"
    assert result["remark"].startswith("连不上")
    assert "timed out" in result["remark"]


def test_rate_limit_is_yellow_and_keeps_the_vendor_code(monkeypatch):
    def limited(req, timeout=8):
        del timeout
        raise _http_error(req.full_url, 429, b'{"error":{"code":"rate_limit_exceeded","message":"slow down"}}')

    monkeypatch.setattr(urllib.request, "urlopen", limited)
    result = engine.probe_chat("http://vendor.test/v1", "sk", "m")
    assert result["health"] == "warn"
    assert result["remark"] == "HTTP 429 · rate_limit_exceeded"


def test_auth_and_gateway_failures_are_red(monkeypatch):
    def denied(req, timeout=8):
        del timeout
        raise _http_error(req.full_url, 401, b'{"error":{"code":"invalid_api_key"}}')

    monkeypatch.setattr(urllib.request, "urlopen", denied)
    denied_result = engine.probe_chat("http://vendor.test/v1", "sk", "m")
    assert denied_result["health"] == "down"
    assert denied_result["remark"] == "HTTP 401 · invalid_api_key"

    def broke(req, timeout=8):
        del timeout
        raise _http_error(req.full_url, 503, b"")

    monkeypatch.setattr(urllib.request, "urlopen", broke)
    down = engine.probe_chat("http://vendor.test/v1", "sk", "m")
    assert down["health"] == "down"
    assert down["remark"] == "HTTP 503"


def test_an_error_code_inside_a_success_body_is_yellow(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Body(200, b'{"base_resp":{"status_code":1004,"status_msg":"login fail"}}'),
    )
    result = engine.probe_embed("http://vendor.test/v1", "sk", "m")
    assert result["health"] == "warn"
    assert result["remark"] == "HTTP 200 · 1004"


def test_a_normal_success_and_a_zero_status_stay_green(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Body(200, b'{"choices":[{"message":{"content":"hi"}}],"base_resp":{"status_code":0}}'),
    )
    assert engine.probe_chat("http://vendor.test/v1", "sk", "m") == {
        "health": "ok",
        "health_detail": "正常",
        "remark": "",
    }


def test_the_hub_row_leads_the_list_and_remarks_follow_the_lamp(tmp_path, monkeypatch):
    eng = _open(tmp_path, monkeypatch)
    items = eng._providers()
    local = next(item for item in items if item["id"] == eng.LOCAL_EMBED_ID)
    local["name"] = "本地"
    local["health_detail"] = "本机"
    rest = [item for item in items if item["id"] != eng.LOCAL_EMBED_ID]
    eng._store_providers(rest + [local])
    eng._ensure_model_catalog()
    stored = eng._providers()
    assert stored[0]["id"] == eng.LOCAL_EMBED_ID
    assert stored[0]["name"] == "中枢服务器本地"
    assert stored[0]["health_detail"] == "中枢服务器本机"

    shown = eng.public_providers()
    assert shown[0]["name"] == "中枢服务器本地"
    assert shown[0]["id"] == eng.LOCAL_EMBED_ID


def test_an_error_code_opens_one_alert_until_it_changes_or_recovers(tmp_path, monkeypatch):
    eng = _open(tmp_path, monkeypatch)
    notes = []
    monkeypatch.setattr(eng, "notify_alert", notes.append)
    monkeypatch.setattr(
        eng,
        "probe_chat",
        lambda *_args: {
            "health": "warn",
            "health_detail": "HTTP 429 · rate_limit_exceeded",
            "remark": "HTTP 429 · rate_limit_exceeded",
        },
    )
    saved = eng.save_provider("100%_额度", "http://vendor.test/v1", "sk-test", "m")
    rows = eng.db().execute("SELECT message, open FROM alerts ORDER BY id").fetchall()
    assert len(rows) == 1
    assert rows[0]["open"] == 1
    assert "100%_额度" in rows[0]["message"]
    assert "HTTP 429 · rate_limit_exceeded" in rows[0]["message"]
    assert notes == [rows[0]["message"]]

    eng._stamp_health(saved["id"], {"health": "warn", "health_detail": "HTTP 429 · rate_limit_exceeded", "remark": "HTTP 429 · rate_limit_exceeded"})
    assert eng.db().execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"] == 1
    assert notes == [rows[0]["message"]]
    log = eng.operation_log("告警")
    assert any(item["outcome"] == "未结案" and "HTTP 429" in item["summary"] for item in log["entries"])

    eng._stamp_health(saved["id"], {"health": "down", "health_detail": "HTTP 503", "remark": "HTTP 503"})
    open_rows = eng.db().execute("SELECT message FROM alerts WHERE open=1").fetchall()
    assert len(open_rows) == 1
    assert "HTTP 503" in open_rows[0]["message"]
    assert eng.db().execute("SELECT open FROM alerts WHERE message LIKE '%429%'").fetchone()["open"] == 0

    eng._stamp_health(saved["id"], {"health": "ok", "health_detail": "正常", "remark": ""})
    assert eng.db().execute("SELECT COUNT(*) AS n FROM alerts WHERE open=1").fetchone()["n"] == 0
    assert notes[-1].startswith("恢复：")
    assert "HTTP 503" in notes[-1]
    settled = eng.operation_log("告警")
    assert any(item["outcome"] == "已结案" and "HTTP 503" in item["summary"] for item in settled["entries"])

    removed = eng.delete_provider(saved["id"])
    assert removed["ok"] is True


def test_deleting_a_provider_closes_its_probe_alert(tmp_path, monkeypatch):
    eng = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(eng, "notify_alert", lambda *_args: None)
    monkeypatch.setattr(
        eng,
        "probe_chat",
        lambda *_args: {"health": "down", "health_detail": "连不上：timed out", "remark": "连不上：timed out"},
    )
    saved = eng.save_provider("坏供应商", "http://127.0.0.1:9/v1", "sk-test", "none")
    assert eng.db().execute("SELECT open FROM alerts").fetchone()["open"] == 1
    assert eng.delete_provider(saved["id"])["ok"] is True
    assert eng.db().execute("SELECT open FROM alerts").fetchone()["open"] == 0


def test_models_page_leads_with_the_hub_and_paints_yellow_and_red():
    html = models_page(
        [
            {
                "id": "mini",
                "name": "MiniMax",
                "base_url": "https://api.example/v1",
                "models": ["m"],
                "health": "warn",
                "health_detail": "HTTP 429 · rate_limit_exceeded",
                "remark": "HTTP 429 · rate_limit_exceeded",
            },
            {
                "id": "local-embed",
                "name": "本地",
                "base_url": "",
                "builtin": True,
                "models": [
                    "BAAI/bge-small-zh-v1.5",
                    "BAAI/bge-base-zh-v1.5",
                    "BAAI/bge-small-en-v1.5",
                ],
                "model_kinds": {
                    "BAAI/bge-small-zh-v1.5": "embed",
                    "BAAI/bge-base-zh-v1.5": "embed",
                    "BAAI/bge-small-en-v1.5": "embed",
                },
                "health": "ok",
                "health_detail": "本机",
            },
            {
                "id": "down",
                "name": "坏供应商",
                "base_url": "http://127.0.0.1:1/v1",
                "models": ["x"],
                "health": "down",
                "health_detail": "连不上：timed out",
                "remark": "连不上：timed out",
            },
        ],
        {},
        {"source": "none"},
    )
    assert html.find("中枢服务器本地") < html.find("MiniMax")
    assert html.find("中枢服务器本地") < html.find("坏供应商")
    assert "中枢服务器本机" in html
    assert "<th>备注</th>" in html
    assert "lamp warn" in html and "降级" in html
    assert "lamp down" in html and "异常" in html
    assert "HTTP 429 · rate_limit_exceeded" in html
    assert "连不上：timed out" in html
    assert "BAAI/bge-small-zh-v1.5" in html
    assert "运行日志留下一条未结案告警" in html


def test_resident_providers_keep_a_save_and_drop_delete():
    html = models_page(
        [
            {"id": "local-embed", "name": "本地", "builtin": True, "models": ["bge"], "key_hint": ""},
            {"id": "minimax-cn", "name": "MiniMax", "base_url": "https://api.minimax.cn/v1", "models": ["M"], "key_hint": "sk****en"},
            {"id": "siliconflow", "name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1", "models": ["bge"], "key_hint": ""},
            {"id": "extra", "name": "别的", "base_url": "https://example.test/v1", "models": ["x"], "key_hint": ""},
        ],
        {},
        {"source": "none"},
    )
    assert "模型管理" in html
    assert "未填写" not in html
    assert "value='sk****en'" in html
    assert "placeholder='填写密钥'" in html
    for ident in ("minimax-cn", "siliconflow", "local-embed"):
        assert f'"id": "{ident}"' not in html
    assert "form='key-minimax-cn' data-key-save disabled>保存</button>" in html
    assert "form='key-siliconflow' data-key-save disabled>保存</button>" in html
    assert "button[data-key-save]" in html
    assert "<button type='submit'>保存密钥</button>" in html
    assert "确定删除供应商「别的」" in html
    assert "确定删除供应商「MiniMax」" not in html
    assert "确定删除供应商「硅基流动」" not in html


def test_saving_the_shown_mask_leaves_the_real_key(tmp_path, monkeypatch):
    eng = _open(tmp_path, monkeypatch)
    eng._store_providers(
        [
            {
                "id": "minimax-cn",
                "name": "MiniMax",
                "base_url": "https://api.minimax.cn/v1",
                "api_key": "sk-real-secret-key",
                "models": ["MiniMax-M3.1-Flash"],
                "model_kinds": {"MiniMax-M3.1-Flash": "chat"},
            }
        ]
    )
    assert engine.mask_api_key("sk-real-secret-key") == "sk****ey"
    shown = engine.public_providers()[0]["key_hint"]
    assert shown == "sk****ey"
    assert "sk-real-secret-key" not in shown
    calls = []
    monkeypatch.setattr(eng, "_probe_provider", lambda *_args: calls.append(1) or {"health": "ok"})
    kept = eng.set_provider_key("minimax-cn", shown)
    assert kept["ok"] is True
    assert calls == []
    assert eng._providers()[0]["api_key"] == "sk-real-secret-key"
    raw = eng.db().execute("SELECT v FROM settings WHERE k='providers'").fetchone()["v"]
    assert '"key_mask": "sk****ey"' in raw
    assert "sk-real-secret-key" not in raw
    refused = eng.delete_provider("minimax-cn")
    assert refused["ok"] is False
    assert eng._providers()[0]["id"] == "minimax-cn"
