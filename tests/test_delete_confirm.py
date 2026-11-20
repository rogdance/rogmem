"""Destructive admin controls ask before they post. Cancel never sends the request."""

from rogmem.ui import _revision_item, hosts_page, models_page


def test_provider_delete_uses_the_confirm_dialog():
    html = models_page(
        [{"id": "p1", "name": "MiniMax", "base_url": "https://example.test", "models": ["m"], "key_hint": "已填写"}],
        {},
        {"source": "none"},
    )
    assert 'id="confirm-delete"' in html
    assert "确认删除" in html
    assert "取消" in html
    assert "data-delete=" in html
    assert "确定删除供应商「MiniMax」" in html
    assert "保存密钥" in html
    assert "/admin/api/settings/models/key" in html
    assert 'onclick=\'post("/admin/api/settings/models/delete"' not in html
    assert "授权管理" not in html
    assert "/admin/settings/agents" not in html
    assert 'yes.textContent = action.confirm || "确认删除"' in html
    assert 'document.getElementById("confirm-delete-no").onclick = () => { modal.hidden = true; }' in html


def test_stop_and_rollback_ask_before_they_post():
    hosts = hosts_page(
        [],
        {
            "rows": [
                {
                    "id": "agent-1",
                    "device_id": "dev-a",
                    "device_base": "南窗",
                    "local": False,
                    "agent_name": "南窗 · Cursor",
                    "program": "Cursor",
                    "status": "启用",
                    "connected_at": "",
                    "last_used": "",
                    "session_count": 0,
                    "call_count": 0,
                },
                {
                    "id": "agent-2",
                    "device_id": "dev-b",
                    "device_base": "北窗",
                    "local": False,
                    "agent_name": "北窗 · Hermes",
                    "program": "Hermes",
                    "status": "停用",
                    "connected_at": "",
                    "last_used": "",
                    "session_count": 0,
                    "call_count": 0,
                },
            ],
            "facets": {"devices": [], "agents": []},
            "viewer_known": False,
        },
        {},
    )
    assert "确定停止「南窗 · Cursor」的授权" in hosts
    assert "确认停止授权" in hosts
    assert "/admin/api/agents/agent-1/disable" in hosts
    assert 'onclick=\'post("/admin/api/agents/agent-1/disable"' not in hosts
    assert 'onclick=\'post("/admin/api/agents/agent-2/enable")\'' in hosts

    item = _revision_item(
        "page-1",
        {
            "id": 7,
            "change": "删去了北城。",
            "created_at": "2026-11-08T01:02:03+00:00",
            "actor": "萃取",
            "scope": "正文",
        },
        name="云岚甲",
    )
    assert "撤销修订" in item
    assert "确定撤销「云岚甲」里「删去了北城。」之后的修订" in item
    assert "确认撤销" in item
    assert "/admin/api/crystals/page-1/rollback" in item
    assert "&quot;revision_id&quot;: 7" in item
    assert "onclick='post(" not in item

