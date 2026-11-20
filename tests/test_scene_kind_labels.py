"""情景会话类别展示名。只锁 UI 文案，不改 sources.kind 底层值。"""

from rogmem.ui import KIND, _HOST_TIPS


def test_session_kind_display_labels():
    assert KIND == {
        "user": "输入",
        "agent": "输出",
        "file": "文件",
        "summary": "宿主记忆",
    }


def test_host_tips_use_session_kind_labels():
    assert "输入" in _HOST_TIPS["累计录入会话数"]
    assert "输出" in _HOST_TIPS["累计录入会话数"]
    assert "宿主记忆" in _HOST_TIPS["累计录入会话数"]
    assert "主人摘要" not in _HOST_TIPS["累计录入会话数"]
    assert "用户输入" not in _HOST_TIPS["待导入会话数"]
    assert "输入 Agent" not in _HOST_TIPS["待导入会话数"]
    assert "输入" in _HOST_TIPS["待导入会话数"]
    assert "输入 Agent" not in _HOST_TIPS["累计录入会话数"]
