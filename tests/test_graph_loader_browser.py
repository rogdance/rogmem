"""图谱取数在浏览器里的行为：慢重试、半路 502、留存先画、就地重试。

跑的是外壳那一层 + 一个假的画图函数，不依赖 three.js / cosmos 的静态资源。
两种渲染器共用这一层，所以这里守的就是两边共同的行为。
"""

from __future__ import annotations

import json
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from rogmem.ui import _GRAPH_SHELL_JS  # noqa: E402

ORIGIN = "http://rogmem.test"

# 取数预算与退避压到几百毫秒，让超时那一条也能在测试里跑完。
_HARNESS = (
    """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>harness</title></head><body>
<div id="graph" aria-busy="true"><div class="graph-loader" role="status" aria-live="polite"><p>正在铺开星图</p></div></div>
<script>window.__instance = String(Math.random()); window.__drawn = [];</script>
<script>"""
    + _GRAPH_SHELL_JS
    + """</script>
<script>
ROGMEM_GRAPH_FETCH_MS = 400;
ROGMEM_GRAPH_BACKOFF_MS = 30;
window.__drawn = [];
function fakeDraw(nodes, edges) {
  if (window.__drawAlwaysThrows) throw new Error('no webgl');
  window.__drawn.push([nodes.length, edges.length]);
  var el = document.getElementById('graph');
  el.rogmemReady = true;
  el.textContent = '';
  rogmemClearBusy(el);  // 真渲染器在画出第一帧时也会做这一步
}
rogmemLoadGraph(document.getElementById('graph'), fakeDraw);
</script>
</body></html>"""
)

PAYLOAD = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"src": "a", "dst": "b"}]}

LOGIN_PAGE = "<!doctype html><html lang=\"zh-CN\"><body><h1>登录</h1></body></html>"


class Api:
    """按调用次数决定这一次回什么；最后一个答复会被后面的调用一直沿用。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def next_reply(self):
        self.calls += 1
        if not self.replies:
            return "hang"
        return self.replies[min(self.calls, len(self.replies)) - 1]


def _open(page, api: Api):
    def handler(route, request):
        url = request.url
        if url.endswith("/admin/api/graph"):
            reply = api.next_reply()
            if reply == "hang":
                time.sleep(0.9)  # 客户端自己会在这之前放弃
                route.fulfill(status=200, content_type="application/json", body=json.dumps(PAYLOAD))
                return
            if reply == "502":
                route.fulfill(status=502, content_type="text/plain; charset=utf-8", body="没有找到这份中枢")
                return
            if reply == "401":
                route.fulfill(status=401, content_type="application/json", body=json.dumps({"error": "未登录"}))
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(PAYLOAD))
            return
        if url.endswith("/admin/login"):
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=LOGIN_PAGE)
            return
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=_HARNESS)

    page.route("**/*", handler)
    page.goto(ORIGIN + "/admin/graph", wait_until="load")


def _state(page):
    return page.evaluate(
        """() => { const el = document.getElementById('graph');
        return { drawn: window.__drawn, instance: window.__instance,
          text: el ? el.textContent.trim() : '', aria: el ? el.getAttribute('aria-busy') : null,
          hasButton: !!(el && el.querySelector('button')) }; }"""
    )


def test_a_plain_text_502_is_not_read_as_json_and_is_retried(browser):
    """通道够不到中枢时回的是纯文本 502。它既不是 JSON，也不该是终局。"""
    page = browser.new_page()
    api = Api(["502", "ok"])
    _open(page, api)
    page.wait_for_timeout(1500)
    state = _state(page)
    assert api.calls >= 2, "第一次 502 之后必须自己再取一次"
    assert state["drawn"] == [[2, 1]], f"第二次取到的数据要画出来，实际 {state['drawn']}"
    assert state["aria"] == "false"
    page.close()


def test_a_hanging_answer_times_out_and_is_retried(browser):
    page = browser.new_page()
    api = Api(["hang", "ok"])
    _open(page, api)
    page.wait_for_timeout(2000)
    state = _state(page)
    assert api.calls >= 2, "超时之后必须自己再取一次"
    assert state["drawn"] == [[2, 1]], f"实际 {state['drawn']}"
    page.close()


def test_all_tries_failing_shows_the_reason_and_retries_in_place(browser):
    """三次都没回上：给出原因，重试就地再取一次，而不是整页刷新。"""
    page = browser.new_page()
    api = Api(["502", "502", "502", "ok"])
    _open(page, api)
    page.wait_for_timeout(2500)
    state = _state(page)
    assert "图谱没有载入" in state["text"], state["text"]
    assert "502" in state["text"], "错误卡片要说清是中枢这一跳的问题"
    assert state["hasButton"]
    first_instance = state["instance"]
    assert state["drawn"] == []
    page.click("#graph button")
    page.wait_for_timeout(1200)
    state = _state(page)
    assert state["instance"] == first_instance, "重试不得整页刷新"
    assert state["drawn"] == [[2, 1]], f"就地重取之后要画出来，实际 {state['drawn']}"
    page.close()


def test_re_entering_the_page_draws_the_stored_graph_when_the_hub_is_gone(browser):
    """用户报的那一条：换页重进时中枢不回话，也要画得出上一次的星图。"""
    page = browser.new_page()
    _open(page, Api(["ok"]))
    page.wait_for_timeout(800)
    assert _state(page)["drawn"] == [[2, 1]]

    reentry = Api(["502", "502", "502"])
    page.unroute("**/*")
    _open(page, reentry)
    state = _state(page)
    assert state["drawn"] == [[2, 1]], f"重进要先画留存的上一份，实际 {state['drawn']}"
    assert state["text"] == "" or "图谱没有载入" not in state["text"], f"已画出就不该报错：{state['text']}"
    page.close()


def test_a_broken_draw_is_reported_as_a_render_failure(browser):
    """取数成功、画不出来，不能说成「图谱没有载入」。"""
    page = browser.new_page()
    page.add_init_script("window.__drawAlwaysThrows = true;")
    _open(page, Api(["ok"]))
    page.wait_for_timeout(800)
    state = _state(page)
    assert "图谱画不出来" in state["text"], state["text"]
    assert "图谱没有载入" not in state["text"]
    page.close()


def test_expired_session_leaves_the_graph_for_the_login_page(browser):
    page = browser.new_page()
    _open(page, Api(["401"]))
    page.wait_for_timeout(800)
    assert page.url.endswith("/admin/login")
    page.close()
