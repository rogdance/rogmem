"""Admin pages. Labels follow the product spec."""

from __future__ import annotations

import contextvars
import email.utils
import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from rogmem import document_view, file_preview
from rogmem.device_identity import display_device_name
from rogmem.progress import LAMP_WORD as _LAMP_WORD
from rogmem.progress import flow_states, processing_filters, stage_note as _stage_note, vector_state
from rogmem.version import product_version

CHANNEL = contextvars.ContextVar("rogmem_channel", default=False)

# 一项导航是一页。带孩子的项是一栏：顶栏亮哪一项、侧栏列哪些页，都由当前页的键推出来。
CONSOLE = (
    ("/admin/sources?tab=session", "原始会话", "session", ()),
    ("/admin/sources?tab=file", "文件资料", "file", ()),
    ("/admin/crystals", "结晶记忆", "crystals", ()),
    ("/admin/graph", "记忆星图", "graph", ()),
    ("/admin/costs", "监控中心", "monitor", (
        ("/admin/costs", "成本明细", "costs"),
        ("/admin/settings/queue", "队列状态", "queue"),
        ("/admin/settings", "运行日志", "logs"),
    )),
    ("/admin/settings/basic", "系统设置", "settings", (
        ("/admin/settings/basic", "基础设置", "basic"),
        ("/admin/hosts", "宿主管理", "hosts"),
        ("/admin/settings/models", "模型管理", "models"),
        ("/admin/settings/pipeline", "管线设置", "pipeline"),
        ("/admin/settings/about", "关于我们", "about"),
    )),
)
NAV = tuple(item[:3] for item in CONSOLE)


def _top_links(top: str, current: str) -> str:
    """有下级的项带上同一套浮层；没有下级的仍是原来的一级链接。"""
    parts = []
    for href, label, key, children in CONSOLE:
        klass = "on" if key == top else ""
        if not children:
            parts.append(f"<a class='{klass}' href='{href}'>{label}</a>")
            continue
        sub_id = f"nav-sub-{key}"
        rows = "".join(
            f"<a tabindex='-1' role='menuitem' class='{'on' if child_key == current else ''}' href='{child_href}'>{child_label}</a>"
            for child_href, child_label, child_key in children
        )
        parts.append(
            "<span class='nav-item'>"
            f"<a aria-haspopup='menu' aria-expanded='false' aria-controls='{sub_id}' class='{klass}' href='{href}'>{label}</a>"
            f"<span class='nav-sub' id='{sub_id}'><span class='nav-sub-panel' role='menu' aria-label='{label}'>{rows}</span></span>"
            "</span>"
        )
    return "".join(parts)


def _locate(key: str):
    """一页的键落到它自己，或落到它所属的那一栏。栏名不是另一套名单。"""
    if not key:
        return None, None
    for item in CONSOLE:
        if item[2] == key:
            return item, None
        for child in item[3]:
            if child[2] == key:
                return item, child
    return None, None


KIND = {"user": "输入", "agent": "输出", "file": "文件", "summary": "宿主记忆"}
FILE_TYPES = ("txt", "md", "markdown", "pdf", "docx", "pptx", "xlsx", "xls", "csv")


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


BRAND_LOCKUP_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-5.120 -5.120 482.638 74.240"'
    ' role="img" aria-label="ROGMEM" fill="none">'
    '<defs><linearGradient id="rogmem-grad" x1="16%" y1="6%" x2="88%" y2="94%"'
    ' gradientUnits="objectBoundingBox">'
    '<stop offset="0%" stop-color="#FF3763"/>'
    '<stop offset="100%" stop-color="#FF7038"/>'
    '</linearGradient></defs>'
    '<path fill="url(#rogmem-grad)" fill-rule="evenodd"'
    ' d="M 46.365 63.950 C 44.557 63.733 42.534 63.141 41.086 62.407 C 40.363 62.041 39.324 61.166 38.936 60.598'
    ' C 38.514 59.980 38.163 58.999 38.168 58.450 C 38.183 56.642 39.063 55.126 41.637 52.477'
    ' C 43.686 50.369 44.818 48.875 45.741 47.064 C 46.785 45.011 47.265 43.050 47.340 40.525'
    ' C 47.403 38.421 47.180 36.727 46.629 35.111 C 46.320 34.204 46.204 34.090 46.204 34.695'
    ' C 46.204 36.050 45.590 38.343 44.888 39.613 C 44.161 40.929 42.710 42.642 41.709 43.367'
    ' C 41.507 43.513 41.117 43.809 40.843 44.023 C 40.340 44.417 38.884 45.337 37.896 45.885'
    ' C 33.855 48.129 31.311 49.198 27.605 50.209 C 23.011 51.463 18.353 51.623 14.366 50.664'
    ' C 10.828 49.814 7.341 47.793 4.452 44.920 C 1.864 42.345 0.391 39.750 0.000 37.077'
    ' C -0.252 35.353 0.287 33.201 1.452 31.283 C 3.779 27.449 7.488 24.859 9.935 25.357'
    ' C 11.087 25.592 12.027 26.177 12.742 27.105 C 13.768 28.436 14.240 29.448 15.496 33.007'
    ' C 16.501 35.858 17.674 37.783 19.560 39.679 C 22.086 42.220 25.162 43.785 28.168 44.060'
    ' C 28.731 44.111 28.682 44.057 27.444 43.259 C 23.436 40.673 21.758 35.990 21.765 27.408'
    ' C 21.769 21.982 22.265 17.973 23.330 14.752 C 24.770 10.399 26.962 7.163 30.267 4.515'
    ' C 32.809 2.478 35.920 0.955 39.178 0.152 C 40.478 -0.168 43.156 -0.249 44.188 -0.000'
    ' C 46.835 0.639 48.701 2.324 50.278 5.499 C 50.832 6.613 51.237 7.689 51.558 8.901'
    ' C 52.269 11.581 52.086 13.608 51.012 14.970 C 50.611 15.478 50.053 16.022 49.927 16.026'
    ' C 49.881 16.028 49.664 16.142 49.444 16.279 C 48.473 16.884 46.429 17.163 44.160 17.000'
    ' C 42.017 16.847 39.580 16.863 38.432 17.039 C 33.667 17.768 30.202 19.863 27.819 23.454'
    ' C 27.476 23.971 27.222 24.419 27.253 24.450 C 27.284 24.481 27.545 24.326 27.832 24.105'
    ' C 28.531 23.569 30.411 22.620 31.291 22.359 C 35.400 21.140 39.288 21.984 45.025 25.338'
    ' C 49.601 28.014 54.303 32.229 56.676 35.781 C 58.634 38.713 59.726 41.494 60.368 45.188'
    ' C 60.554 46.257 60.555 50.320 60.370 51.459 C 60.045 53.450 59.360 55.740 58.638 57.248'
    ' C 56.468 61.776 53.346 63.858 48.512 64.000 C 47.744 64.023 46.777 64.000 46.365 63.950 Z'
    ' M 36.133 39.075 C 37.842 38.500 39.192 37.100 39.721 35.357 C 39.901 34.765 39.927 33.461 39.775 32.659'
    ' C 39.547 31.460 38.549 29.998 37.477 29.293 C 36.981 28.967 36.134 28.587 35.512 28.412'
    ' C 35.174 28.316 33.808 28.314 33.233 28.408 C 32.002 28.609 30.342 29.752 29.605 30.906'
    ' C 28.969 31.902 28.784 32.576 28.788 33.880 C 28.793 35.138 28.973 35.807 29.562 36.756'
    ' C 30.275 37.904 31.863 39.011 33.182 39.280 C 33.955 39.438 35.342 39.342 36.133 39.075 Z"/>'
    '<path fill="currentColor"'
    ' d="M79.59656686410541 59.519999999999996V4.479999999999997H104.16131379668724Q116.23491587776246 4.479999999999997 122.7177163113198 9.928429048341641Q129.20051674487715 15.376858096683286 129.20051674487715 24.823144331237806Q129.20051674487715 31.07005905050943 126.21940215567273 35.58443078257099Q123.2382875664683 40.09880251463255 117.75054423241237 42.49968835898547Q112.26280089835643 44.900574203338394 104.70360037808763 44.900574203338394H88.23771561762807L95.17612030004732 38.34719895946239V59.519999999999996ZM113.61553447737226 59.519999999999996 99.94502071192679 39.494114199002816H116.50337458142603L130.3067455764401 59.519999999999996ZM95.17612030004732 40.049943030565785 88.23771561762807 32.8810814654238H103.79257085283292Q108.67016721095128 32.8810814654238 111.07913664515895 30.736454368090175Q113.48810607936663 28.591827270756554 113.48810607936663 24.823144331237806Q113.48810607936663 21.030061478430518 111.07913664515895 18.897634337741163Q108.67016721095128 16.765207197051808 103.79257085283292 16.765207197051808H88.23771561762807L95.17612030004732 9.571945718621294Z"/>'
    '<path fill="currentColor"'
    ' d="M168.69356466178624 60.60725774983741Q162.08592946575368 60.60725774983741 156.4776496390245 58.484286321265984Q150.8693698122953 56.36131489269456 146.76305530980235 52.4936005202688Q142.6567408073094 48.62588614784305 140.37379782009936 43.40190149319818Q138.09085483288928 38.17791683855332 138.09085483288928 31.974435061162552Q138.09085483288928 25.770953283771775 140.37397738519252 20.584153994708657Q142.65709993749576 15.39735470564554 146.76633837263836 11.51449911361842Q150.875576807781 7.6316435215913 156.4715343649165 5.512192885876942Q162.067491922052 3.392742250162584 168.66114228140435 3.392742250162584Q175.30119985781883 3.392742250162584 180.866944182211 5.507853303983346Q186.43268850660314 7.622964357804108 190.53673660931378 11.497959575080557Q194.6407847120244 15.372954792357007 196.92390726432762 20.56817444365303Q199.20702981663086 25.763394094949057 199.20702981663086 31.976875052491405Q199.20702981663086 38.19035601003375 196.9240868294208 43.42032103558267Q194.64114384221074 48.65028606113158 190.53691617440694 52.51366085166374Q186.43268850660314 56.37703564219589 180.866944182211 58.49214669601665Q175.30119985781883 60.60725774983741 168.69356466178624 60.60725774983741ZM168.6587141325324 47.760792889659655Q171.81708314418344 47.760792889659655 174.5112032267199 46.64281660985835Q177.20532330925639 45.524840330057046 179.22342902338153 43.454332586446256Q181.24153473750664 41.383824842835466 182.3680769443135 38.47943147380864Q183.49461915112036 35.57503810478181 183.49461915112036 31.992633988441195Q183.49461915112036 28.410229872100583 182.37081850760435 25.515297927033288Q181.24701786408835 22.620365981965996 179.22774444708006 20.548505879997283Q177.2084710300718 18.47664577802857 174.51520523599956 17.357926444184454Q171.82193944192733 16.239207110340338 168.66357043027628 16.239207110340338Q165.50520141862523 16.239207110340338 162.8038415782133 17.358717815041878Q160.10248173780138 18.478228519743418 158.0790045904138 20.551528916672634Q156.05552744302622 22.62482931360185 154.92939647071302 25.521676376144097Q153.80326549839978 28.418523438686343 153.80326549839978 31.991946826648608Q153.80326549839978 35.56537021461088 154.92827204632835 38.46976358363771Q156.05327859425688 41.374156952664535 158.07472736824468 43.45313267099951Q160.09617614223245 45.53210838933448 162.7982606315569 46.64645063949707Q165.50034512088135 47.760792889659655 168.6587141325324 47.760792889659655Z"/>'
    '<path fill="currentColor"'
    ' d="M238.10914193455847 60.60725774983741Q231.52331301628402 60.60725774983741 225.97456989900678 58.52360060698027Q220.4258267817295 56.439943464123125 216.34662660830656 52.5993434207674Q212.26742643488362 48.75874337741166 210.0115977767436 43.52722896163017Q207.75576911860355 38.29571454584868 207.75576911860355 31.999999999999996Q207.75576911860355 25.70428545415131 210.0115977767436 20.47277103836982Q212.26742643488362 15.241256622588331 216.38594089402085 11.4006565792326Q220.50445535315808 7.560056535876868 226.10742712857535 5.476399393019726Q231.71039890399265 3.392742250162584 238.42908505183573 3.392742250162584Q246.16725804121356 3.392742250162584 252.294887609824 5.9739428571428554Q258.4225171784345 8.555143464123127 262.4922318109946 13.422001474094948L252.5469106750744 22.391206763494473Q249.73256859833475 19.30572139605463 246.44916950880514 17.772464253197484Q243.16577041927556 16.239207110340338 239.20182851595885 16.239207110340338Q235.67171587776244 16.239207110340338 232.76791739521315 17.353549360502925Q229.86411891266383 18.46789161066551 227.7926915551958 20.556947756340776Q225.72126419772778 22.64600390201604 224.5947219909209 25.52811688705831Q223.46817978411406 28.410229872100583 223.46817978411406 31.999999999999996Q223.46817978411406 35.4569128983308 224.5947219909209 38.36614021244309Q225.72126419772778 41.275367526555385 227.7926915551958 43.37662362887492Q229.86411891266383 45.47787973119445 232.70148878042883 46.61933631042705Q235.53885864819384 47.760792889659655 239.0147426282502 47.760792889659655Q242.5014842714325 47.760792889659655 245.78216894518062 46.61390747886408Q249.06285361892873 45.4670220680685 252.46828210364583 42.70147981790592L261.21245934188556 53.609314892694556Q256.44862981663084 57.01474337741166 250.26948600566186 58.81100056362453Q244.09034219469288 60.60725774983741 238.10914193455847 60.60725774983741ZM261.21245934188556 53.609314892694556 247.411593961439 51.6164564491654V30.82325601560806H261.21245934188556Z"/>'
    '<path fill="currentColor"'
    ' d="M274.28090972124824 59.519999999999996V4.479999999999997H287.09486124953787L309.84285864819384 41.926051766746156H303.09153985348325L325.30810833386477 4.479999999999997H338.1220598621544L338.254917091723 59.519999999999996H323.911765129876L323.77890790030744 26.295670149577283H326.23268010928206L309.6557727604852 54.10280164751788H302.74719682291743L285.6632604691347 26.295670149577283H288.6240616830952V59.519999999999996Z"/>'
    '<path fill="currentColor"'
    ' d="M352.4377097212482 59.519999999999996V4.479999999999997H395.5425726737737V16.548292564491653H367.80034852462995V47.45170743550834H396.5891440156337V59.519999999999996ZM366.71309077479253 37.38748892261002V25.801825449815738H392.2700014186252V37.38748892261002Z"/>'
    '<path fill="currentColor"'
    ' d="M408.4212525783911 59.519999999999996V4.479999999999997H421.2352041066807L443.9832015053367 41.926051766746156H437.2318827106261L459.4484511910076 4.479999999999997H472.26240271929726L472.3952599488658 59.519999999999996H458.05210798701887L457.9192507574503 26.295670149577283H460.3730229664249L443.79611561762806 54.10280164751788H436.8875396800603L419.80360332627754 26.295670149577283H422.76440454023805V59.519999999999996Z"/>'
    '</svg>'
)


def login_page() -> str:
    """登录页。

    3D 神经网络视效铺满屏幕，登录窗落在垂直中线与下方黄金分割线的交汇点。
    口令交服务端校验：`POST /admin/login` 通过后才写入会话 cookie 并放行管理台。
    配色与圆角沿用管理台的设计 token 和品牌色。
    """
    from rogmem.engine import INITIAL_ADMIN_PASSWORD

    page = """<!doctype html>
<html lang="zh-CN" data-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>登录 · rogmem</title>
<style>
:root {
  color-scheme: dark;
  --surface-page: #171717;
  --surface-base: #212121;
  --surface-subtle: #282828;
  --surface-popover: #262626;
  --text-primary: #f2f2f2;
  --text-secondary: #d6d6d6;
  --text-muted: #adadad;
  --border: #3d3d3d;
  --border-strong: #5e5e5e;
  --input: #4d4d4d;
  --primary: #f2f2f2;
  --primary-foreground: #212121;
  --primary-hover: #dedede;
  --success-text: #97dbb2;
  --danger-text: #ffadb9;
  --brand: #ef6576;
  --focus-color: #f095a8;
  --shadow-dialog: 0 20px 64px rgba(0, 0, 0, .4);
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; width: 100%; }
body {
  overflow: hidden;
  font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.5;
  color: var(--text-primary);
  background: var(--surface-page);
}
canvas#neural-network-canvas {
  display: block;
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  z-index: 1;
}
/* 左上角品牌 logo，与 /admin/crystals 的 header .brand 一致 */
.top {
  position: fixed;
  inset: 0 0 auto 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 60px;
  padding: 0 16px;
  pointer-events: none;
}
.brand { pointer-events: auto; color: var(--text-primary); text-decoration: none; }
.brand svg { display: block; height: 26px; width: auto; }
:focus-visible { outline: 2px solid var(--focus-color); outline-offset: 2px; }
/* 登录窗：页面正中，垂直中线 x=50% 与水平中线 y=50% 的交汇点 */
.login-panel {
  position: fixed;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  z-index: 30;
  width: min(380px, calc(100vw - 40px));
  padding: 28px 26px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: linear-gradient(145deg, rgba(40, 40, 40, .4) 0%, rgba(23, 23, 23, .32) 100%);
  -webkit-backdrop-filter: blur(14px) saturate(130%);
  backdrop-filter: blur(14px) saturate(130%);
  box-shadow: var(--shadow-dialog);
  transition: background .4s cubic-bezier(.25, .8, .25, 1), border-color .4s;
}
.login-panel:hover {
  background: linear-gradient(145deg, rgba(48, 48, 48, .46) 0%, rgba(28, 28, 28, .38) 100%);
  border-color: var(--border-strong);
}
.login-title {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: .02em;
  line-height: 1.4;
  text-align: center;
}
.password-input {
  width: 100%;
  min-height: 40px;
  padding: 10px 12px;
  font: inherit;
  letter-spacing: .3px;
  color: var(--text-primary);
  background: var(--surface-subtle);
  border: 1px solid var(--input);
  border-radius: 8px;
  outline: none;
  user-select: text;
  -webkit-user-select: text;
  transition: border-color .2s ease, box-shadow .2s ease;
}
.password-input::placeholder { color: var(--text-muted); }
.password-input:focus {
  border-color: var(--focus-color);
  box-shadow: 0 0 0 3px rgba(240, 149, 168, .22);
}
.login-button {
  width: 100%;
  min-height: 40px;
  padding: 10px 14px;
  border: 0;
  border-radius: 8px;
  font: inherit;
  font-weight: 600;
  letter-spacing: .06em;
  color: var(--primary-foreground);
  background: var(--primary);
  cursor: pointer;
  transition: background .2s ease;
}
.login-button:hover { background: var(--primary-hover); }
.login-button:disabled { opacity: .5; cursor: not-allowed; }
.login-message {
  min-height: 18px;
  margin-top: -8px;
  font-size: 13px;
  letter-spacing: .3px;
  text-align: center;
  opacity: 0;
  transition: opacity .3s ease;
}
.login-message.show { opacity: 1; }
.login-message.error { color: var(--danger-text); }
.login-message.success { color: var(--success-text); }
.login-hint {
  margin-top: -12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  font-size: 13px;
  color: var(--text-muted);
  letter-spacing: .3px;
  text-align: center;
}
.login-legal {
  position: fixed;
  z-index: 30;
  left: 0;
  right: 0;
  bottom: 16px;
  margin: 0;
  text-align: center;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-muted);
}
.login-legal a { color: inherit; }
@media (max-width: 480px) {
  .login-panel { padding: 24px 20px 18px; }
  .login-title { font-size: 18px; }
}
</style></head><body>
<canvas id="neural-network-canvas"></canvas>
<header class="top"><a class="brand" href="/admin/login" aria-label="ROGMEM">__BRAND__</a></header>
<main class="login-panel" aria-label="登录">
<div class="login-title">ROGMEM 私人记忆中枢</div>
<input id="password-input" class="password-input" name="password" type="password" placeholder="输入访问密码" autocomplete="current-password" spellcheck="false" aria-label="登录密码">
<button id="login-button" class="login-button" type="button">立即登录</button>
<p id="login-message" class="login-message" role="status" aria-live="polite"></p>
<p class="login-hint">初始密码：__INITIAL__</p>
</main>
<p class="login-legal">Copyright (c) 2026 by Rogdance (<a href="https://github.com/rogdance/rogmem">https://github.com/rogdance/rogmem</a>)</p>
<script type="importmap">
{
  "imports": {
    "three": "/admin/static/three/build/three.module.js",
    "three/addons/": "/admin/static/three/examples/jsm/"
  }
}
</script>
<script type="module">
import * as THREE from 'three';
// Techartist, 3D Quantum Neural Network, MIT. https://codepen.io/VoXelo/pen/dPMeGze
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

const config = {
  paused: false,
  activePaletteIndex: 0,
  currentFormation: 0,
  densityFactor: 1
};

const colorPalettes = [
  [
    // 取自 /admin/crystals 顶部提示条背景色 #2c2024 的玫瑰色系（暗→亮）
    new THREE.Color(0x2c2024),
    new THREE.Color(0x53343e),
    new THREE.Color(0x8f5a67),
    new THREE.Color(0xef6576),
    new THREE.Color(0xedb4c2)
  ],
  [
    new THREE.Color(0xf857a6),
    new THREE.Color(0xff5858),
    new THREE.Color(0xfeca57),
    new THREE.Color(0xff6348),
    new THREE.Color(0xff9068)
  ],
  [
    new THREE.Color(0x4facfe),
    new THREE.Color(0x00f2fe),
    new THREE.Color(0x43e97b),
    new THREE.Color(0x38f9d7),
    new THREE.Color(0x4484ce)
  ]
];

// ---------- 场景基础 ----------
const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x000000, 0.002);

const camera = new THREE.PerspectiveCamera(65, window.innerWidth / window.innerHeight, 0.1, 1000);
// 默认镜头拉近至原距离的 1/1.5，使核心网络在默认视角下放大约 1.5 倍并铺满屏幕
camera.position.set(0, 5.3, 18.7);

const canvasElement = document.getElementById('neural-network-canvas');
const renderer = new THREE.WebGLRenderer({
  canvas: canvasElement,
  antialias: true,
  powerPreference: "high-performance"
});
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0x000000);
renderer.outputColorSpace = THREE.SRGBColorSpace;

const clock = new THREE.Clock();

// ---------- 星空背景 ----------
function createStarfield() {
  const count = 8000;
  const positions = [];
  const colors = [];
  const sizes = [];
  for (let i = 0; i < count; i++) {
    const r = THREE.MathUtils.randFloat(50, 150);
    const phi = Math.acos(THREE.MathUtils.randFloatSpread(2));
    const theta = THREE.MathUtils.randFloat(0, Math.PI * 2);
    positions.push(
      r * Math.sin(phi) * Math.cos(theta),
      r * Math.sin(phi) * Math.sin(theta),
      r * Math.cos(phi)
    );
    const colorChoice = Math.random();
    if (colorChoice < 0.7) {
      colors.push(1, 1, 1);
    } else if (colorChoice < 0.85) {
      colors.push(0.7, 0.8, 1);
    } else {
      colors.push(1, 0.9, 0.8);
    }
    sizes.push(THREE.MathUtils.randFloat(0.1, 0.3));
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geo.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
  geo.setAttribute('size', new THREE.Float32BufferAttribute(sizes, 1));
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 }
    },
    vertexShader: `
      attribute float size;
      attribute vec3 color;
      varying vec3 vColor;
      uniform float uTime;
      void main() {
          vColor = color;
          vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
          float twinkle = sin(uTime * 2.0 + position.x * 100.0) * 0.3 + 0.7;
          gl_PointSize = size * twinkle * (300.0 / -mvPosition.z);
          gl_Position = projectionMatrix * mvPosition;
      }
    `,
    fragmentShader: `
      varying vec3 vColor;
      void main() {
          vec2 center = gl_PointCoord - 0.5;
          float dist = length(center);
          if (dist > 0.5) discard;
          float alpha = 1.0 - smoothstep(0.0, 0.5, dist);
          gl_FragColor = vec4(vColor, alpha * 0.8);
      }
    `,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  });
  return new THREE.Points(geo, mat);
}

const starField = createStarfield();
scene.add(starField);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.05;
controls.rotateSpeed = 0.6;
controls.minDistance = 8;
controls.maxDistance = 80;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.2;
controls.enablePan = false;

// ---------- 后期泛光 ----------
const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloomPass = new UnrealBloomPass(
  new THREE.Vector2(window.innerWidth, window.innerHeight),
  1.8,
  0.6,
  0.7
);
composer.addPass(bloomPass);
composer.addPass(new OutputPass());

// ---------- 着色器 ----------
const pulseUniforms = {
  uTime: { value: 0.0 },
  uPulsePositions: {
    value: [
      new THREE.Vector3(1e3, 1e3, 1e3),
      new THREE.Vector3(1e3, 1e3, 1e3),
      new THREE.Vector3(1e3, 1e3, 1e3)
    ]
  },
  uPulseTimes: { value: [-1e3, -1e3, -1e3] },
  uPulseColors: {
    value: [
      new THREE.Color(1, 1, 1),
      new THREE.Color(1, 1, 1),
      new THREE.Color(1, 1, 1)
    ]
  },
  uPulseSpeed: { value: 18.0 },
  uBaseNodeSize: { value: 0.6 }
};

const noiseFunctions = `
  vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
  vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
  vec4 permute(vec4 x) { return mod289(((x * 34.0) + 1.0) * x); }
  vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }

  float snoise(vec3 v) {
      const vec2 C = vec2(1.0/6.0, 1.0/3.0);
      const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);

      vec3 i = floor(v + dot(v, C.yyy));
      vec3 x0 = v - i + dot(i, C.xxx);

      vec3 g = step(x0.yzx, x0.xyz);
      vec3 l = 1.0 - g;
      vec3 i1 = min(g.xyz, l.zxy);
      vec3 i2 = max(g.xyz, l.zxy);

      vec3 x1 = x0 - i1 + C.xxx;
      vec3 x2 = x0 - i2 + C.yyy;
      vec3 x3 = x0 - D.yyy;

      i = mod289(i);
      vec4 p = permute(permute(permute(
          i.z + vec4(0.0, i1.z, i2.z, 1.0))
          + i.y + vec4(0.0, i1.y, i2.y, 1.0))
          + i.x + vec4(0.0, i1.x, i2.x, 1.0));

      float n_ = 0.142857142857;
      vec3 ns = n_ * D.wyz - D.xzx;

      vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
      vec4 x_ = floor(j * ns.z);
      vec4 y_ = floor(j - 7.0 * x_);

      vec4 x = x_ * ns.x + ns.yyyy;
      vec4 y = y_ * ns.x + ns.yyyy;
      vec4 h = 1.0 - abs(x) - abs(y);

      vec4 b0 = vec4(x.xy, y.xy);
      vec4 b1 = vec4(x.zw, y.zw);

      vec4 s0 = floor(b0) * 2.0 + 1.0;
      vec4 s1 = floor(b1) * 2.0 + 1.0;
      vec4 sh = -step(h, vec4(0.0));

      vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
      vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;

      vec3 p0 = vec3(a0.xy, h.x);
      vec3 p1 = vec3(a0.zw, h.y);
      vec3 p2 = vec3(a1.xy, h.z);
      vec3 p3 = vec3(a1.zw, h.w);

      vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2,p2), dot(p3,p3)));
      p0 *= norm.x;
      p1 *= norm.y;
      p2 *= norm.z;
      p3 *= norm.w;

      vec4 m = max(0.6 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0);
      m = m * m;
      return 42.0 * dot(m * m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
  }`;

const nodeShader = {
  vertexShader: `${noiseFunctions}
    attribute float nodeSize;
    attribute float nodeType;
    attribute vec3 nodeColor;
    attribute float distanceFromRoot;

    uniform float uTime;
    uniform vec3 uPulsePositions[3];
    uniform float uPulseTimes[3];
    uniform float uPulseSpeed;
    uniform float uBaseNodeSize;

    varying vec3 vColor;
    varying float vNodeType;
    varying vec3 vPosition;
    varying float vPulseIntensity;
    varying float vDistanceFromRoot;
    varying float vGlow;

    float getPulseIntensity(vec3 worldPos, vec3 pulsePos, float pulseTime) {
        if (pulseTime < 0.0) return 0.0;
        float timeSinceClick = uTime - pulseTime;
        if (timeSinceClick < 0.0 || timeSinceClick > 4.0) return 0.0;
        float pulseRadius = timeSinceClick * uPulseSpeed;
        float distToClick = distance(worldPos, pulsePos);
        float pulseThickness = 3.0;
        float waveProximity = abs(distToClick - pulseRadius);
        return smoothstep(pulseThickness, 0.0, waveProximity) * smoothstep(4.0, 0.0, timeSinceClick);
    }

    void main() {
        vNodeType = nodeType;
        vColor = nodeColor;
        vDistanceFromRoot = distanceFromRoot;
        vec3 worldPos = (modelMatrix * vec4(position, 1.0)).xyz;
        vPosition = worldPos;
        float totalPulseIntensity = 0.0;
        for (int i = 0; i < 3; i++) {
            totalPulseIntensity += getPulseIntensity(worldPos, uPulsePositions[i], uPulseTimes[i]);
        }
        vPulseIntensity = min(totalPulseIntensity, 1.0);
        float breathe = sin(uTime * 0.7 + distanceFromRoot * 0.15) * 0.15 + 0.85;
        float baseSize = nodeSize * breathe;
        float pulseSize = baseSize * (1.0 + vPulseIntensity * 2.5);
        vGlow = 0.5 + 0.5 * sin(uTime * 0.5 + distanceFromRoot * 0.2);
        vec3 modifiedPosition = position;
        if (nodeType > 0.5) {
            float noise = snoise(position * 0.08 + uTime * 0.08);
            modifiedPosition += normal * noise * 0.15;
        }
        vec4 mvPosition = modelViewMatrix * vec4(modifiedPosition, 1.0);
        gl_PointSize = pulseSize * uBaseNodeSize * (1000.0 / -mvPosition.z);
        gl_Position = projectionMatrix * mvPosition;
    }`,
  fragmentShader: `
    uniform float uTime;
    uniform vec3 uPulseColors[3];

    varying vec3 vColor;
    varying float vNodeType;
    varying vec3 vPosition;
    varying float vPulseIntensity;
    varying float vDistanceFromRoot;
    varying float vGlow;

    void main() {
        vec2 center = 2.0 * gl_PointCoord - 1.0;
        float dist = length(center);
        if (dist > 1.0) discard;
        float glow1 = 1.0 - smoothstep(0.0, 0.5, dist);
        float glow2 = 1.0 - smoothstep(0.0, 1.0, dist);
        float glowStrength = pow(glow1, 1.2) + glow2 * 0.3;
        float breatheColor = 0.9 + 0.1 * sin(uTime * 0.6 + vDistanceFromRoot * 0.25);
        vec3 baseColor = vColor * breatheColor;
        vec3 finalColor = baseColor;
        if (vPulseIntensity > 0.0) {
            vec3 pulseColor = mix(vec3(1.0), uPulseColors[0], 0.4);
            finalColor = mix(baseColor, pulseColor, vPulseIntensity * 0.8);
            finalColor *= (1.0 + vPulseIntensity * 1.2);
            glowStrength *= (1.0 + vPulseIntensity);
        }
        float coreBrightness = smoothstep(0.4, 0.0, dist);
        finalColor += vec3(1.0) * coreBrightness * 0.3;
        float alpha = glowStrength * (0.95 - 0.3 * dist);
        float camDistance = length(vPosition - cameraPosition);
        float distanceFade = smoothstep(100.0, 15.0, camDistance);
        if (vNodeType > 0.5) {
            finalColor *= 1.1;
            alpha *= 0.9;
        }
        finalColor *= (1.0 + vGlow * 0.1);
        gl_FragColor = vec4(finalColor, alpha * distanceFade);
    }`
};

const connectionShader = {
  vertexShader: `${noiseFunctions}
    attribute vec3 startPoint;
    attribute vec3 endPoint;
    attribute float connectionStrength;
    attribute float pathIndex;
    attribute vec3 connectionColor;

    uniform float uTime;
    uniform vec3 uPulsePositions[3];
    uniform float uPulseTimes[3];
    uniform float uPulseSpeed;

    varying vec3 vColor;
    varying float vConnectionStrength;
    varying float vPulseIntensity;
    varying float vPathPosition;
    varying float vDistanceFromCamera;

    float getPulseIntensity(vec3 worldPos, vec3 pulsePos, float pulseTime) {
        if (pulseTime < 0.0) return 0.0;
        float timeSinceClick = uTime - pulseTime;
        if (timeSinceClick < 0.0 || timeSinceClick > 4.0) return 0.0;

        float pulseRadius = timeSinceClick * uPulseSpeed;
        float distToClick = distance(worldPos, pulsePos);
        float pulseThickness = 3.0;
        float waveProximity = abs(distToClick - pulseRadius);

        return smoothstep(pulseThickness, 0.0, waveProximity) * smoothstep(4.0, 0.0, timeSinceClick);
    }

    void main() {
        float t = position.x;
        vPathPosition = t;
        vec3 midPoint = mix(startPoint, endPoint, 0.5);
        float pathOffset = sin(t * 3.14159) * 0.15;
        vec3 perpendicular = normalize(cross(normalize(endPoint - startPoint), vec3(0.0, 1.0, 0.0)));
        if (length(perpendicular) < 0.1) perpendicular = vec3(1.0, 0.0, 0.0);
        midPoint += perpendicular * pathOffset;
        vec3 p0 = mix(startPoint, midPoint, t);
        vec3 p1 = mix(midPoint, endPoint, t);
        vec3 finalPos = mix(p0, p1, t);
        float noiseTime = uTime * 0.15;
        float noise = snoise(vec3(pathIndex * 0.08, t * 0.6, noiseTime));
        finalPos += perpendicular * noise * 0.12;
        vec3 worldPos = (modelMatrix * vec4(finalPos, 1.0)).xyz;
        float totalPulseIntensity = 0.0;
        for (int i = 0; i < 3; i++) {
            totalPulseIntensity += getPulseIntensity(worldPos, uPulsePositions[i], uPulseTimes[i]);
        }
        vPulseIntensity = min(totalPulseIntensity, 1.0);
        vColor = connectionColor;
        vConnectionStrength = connectionStrength;

        vDistanceFromCamera = length(worldPos - cameraPosition);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(finalPos, 1.0);
    }`,
  fragmentShader: `
    uniform float uTime;
    uniform vec3 uPulseColors[3];

    varying vec3 vColor;
    varying float vConnectionStrength;
    varying float vPulseIntensity;
    varying float vPathPosition;
    varying float vDistanceFromCamera;

    void main() {
        float flowPattern1 = sin(vPathPosition * 25.0 - uTime * 4.0) * 0.5 + 0.5;
        float flowPattern2 = sin(vPathPosition * 15.0 - uTime * 2.5 + 1.57) * 0.5 + 0.5;
        float combinedFlow = (flowPattern1 + flowPattern2 * 0.5) / 1.5;

        vec3 baseColor = vColor * (0.8 + 0.2 * sin(uTime * 0.6 + vPathPosition * 12.0));
        float flowIntensity = 0.4 * combinedFlow * vConnectionStrength;
        vec3 finalColor = baseColor;
        if (vPulseIntensity > 0.0) {
            vec3 pulseColor = mix(vec3(1.0), uPulseColors[0], 0.3);
            finalColor = mix(baseColor, pulseColor * 1.2, vPulseIntensity * 0.7);
            flowIntensity += vPulseIntensity * 0.8;
        }
        finalColor *= (0.7 + flowIntensity + vConnectionStrength * 0.5);
        float baseAlpha = 0.7 * vConnectionStrength;
        float flowAlpha = combinedFlow * 0.3;
        float alpha = baseAlpha + flowAlpha;
        alpha = mix(alpha, min(1.0, alpha * 2.5), vPulseIntensity);
        float distanceFade = smoothstep(100.0, 15.0, vDistanceFromCamera);
        gl_FragColor = vec4(finalColor, alpha * distanceFade);
    }`
};

// ---------- 网络结构生成 ----------
class Node {
  constructor(position, level = 0, type = 0) {
    this.position = position;
    this.connections = [];
    this.level = level;
    this.type = type;
    this.size = type === 0 ? THREE.MathUtils.randFloat(0.8, 1.4) : THREE.MathUtils.randFloat(0.5, 1.0);
    this.distanceFromRoot = 0;
  }

  addConnection(node, strength = 1.0) {
    if (!this.isConnectedTo(node)) {
      this.connections.push({ node, strength });
      node.connections.push({ node: this, strength });
    }
  }

  isConnectedTo(node) {
    return this.connections.some(conn => conn.node === node);
  }
}

function generateNeuralNetwork(formationIndex, densityFactor = 1.0) {
  let nodes = [];
  let rootNode;

  function generateCrystallineSphere() {
    rootNode = new Node(new THREE.Vector3(0, 0, 0), 0, 0);
    rootNode.size = 2.0;
    nodes.push(rootNode);
    const layers = 5;
    const goldenRatio = (1 + Math.sqrt(5)) / 2;
    for (let layer = 1; layer <= layers; layer++) {
      const radius = layer * 4;
      const numPoints = Math.floor(layer * 12 * densityFactor);
      for (let i = 0; i < numPoints; i++) {
        const phi = Math.acos(1 - 2 * (i + 0.5) / numPoints);
        const theta = 2 * Math.PI * i / goldenRatio;
        const pos = new THREE.Vector3(
          radius * Math.sin(phi) * Math.cos(theta),
          radius * Math.sin(phi) * Math.sin(theta),
          radius * Math.cos(phi)
        );
        const isLeaf = layer === layers || Math.random() < 0.3;
        const node = new Node(pos, layer, isLeaf ? 1 : 0);
        node.distanceFromRoot = radius;
        nodes.push(node);
        if (layer > 1) {
          const prevLayerNodes = nodes.filter(n => n.level === layer - 1 && n !== rootNode);
          prevLayerNodes.sort((a, b) =>
            pos.distanceTo(a.position) - pos.distanceTo(b.position)
          );
          for (let j = 0; j < Math.min(3, prevLayerNodes.length); j++) {
            const dist = pos.distanceTo(prevLayerNodes[j].position);
            const strength = 1.0 - (dist / (radius * 2));
            node.addConnection(prevLayerNodes[j], Math.max(0.3, strength));
          }
        } else {
          rootNode.addConnection(node, 0.9);
        }
      }
      const layerNodes = nodes.filter(n => n.level === layer && n !== rootNode);
      for (let i = 0; i < layerNodes.length; i++) {
        const node = layerNodes[i];
        const nearby = layerNodes.filter(n => n !== node)
          .sort((a, b) =>
            node.position.distanceTo(a.position) - node.position.distanceTo(b.position)
          ).slice(0, 5);
        for (const nearNode of nearby) {
          const dist = node.position.distanceTo(nearNode.position);
          if (dist < radius * 0.8 && !node.isConnectedTo(nearNode)) {
            node.addConnection(nearNode, 0.6);
          }
        }
      }
    }
    const outerNodes = nodes.filter(n => n.level >= 3);
    for (let i = 0; i < Math.min(20, outerNodes.length); i++) {
      const n1 = outerNodes[Math.floor(Math.random() * outerNodes.length)];
      const n2 = outerNodes[Math.floor(Math.random() * outerNodes.length)];
      if (n1 !== n2 && !n1.isConnectedTo(n2) &&
        Math.abs(n1.level - n2.level) > 1) {
        n1.addConnection(n2, 0.4);
      }
    }
  }

  function generateHelixLattice() {
    rootNode = new Node(new THREE.Vector3(0, 0, 0), 0, 0);
    rootNode.size = 1.8;
    nodes.push(rootNode);
    const numHelices = 4;
    const height = 30;
    const maxRadius = 12;
    const nodesPerHelix = Math.floor(50 * densityFactor);
    const helixArrays = [];
    for (let h = 0; h < numHelices; h++) {
      const helixPhase = (h / numHelices) * Math.PI * 2;
      const helixNodes = [];
      for (let i = 0; i < nodesPerHelix; i++) {
        const t = i / (nodesPerHelix - 1);
        const y = (t - 0.5) * height;
        const radiusScale = Math.sin(t * Math.PI) * 0.7 + 0.3;
        const radius = maxRadius * radiusScale;
        const angle = helixPhase + t * Math.PI * 6;
        const pos = new THREE.Vector3(
          radius * Math.cos(angle),
          y,
          radius * Math.sin(angle)
        );
        const level = Math.ceil(t * 5);
        const isLeaf = i > nodesPerHelix - 5 || Math.random() < 0.25;
        const node = new Node(pos, level, isLeaf ? 1 : 0);
        node.distanceFromRoot = Math.sqrt(radius * radius + y * y);
        node.helixIndex = h;
        node.helixT = t;
        nodes.push(node);
        helixNodes.push(node);
      }
      helixArrays.push(helixNodes);
      rootNode.addConnection(helixNodes[0], 1.0);
      for (let i = 0; i < helixNodes.length - 1; i++) {
        helixNodes[i].addConnection(helixNodes[i + 1], 0.85);
      }
    }
    for (let h = 0; h < numHelices; h++) {
      const currentHelix = helixArrays[h];
      const nextHelix = helixArrays[(h + 1) % numHelices];
      for (let i = 0; i < currentHelix.length; i += 5) {
        const t = currentHelix[i].helixT;
        const targetIdx = Math.round(t * (nextHelix.length - 1));
        if (targetIdx < nextHelix.length) {
          currentHelix[i].addConnection(nextHelix[targetIdx], 0.7);
        }
      }
    }
    for (const helix of helixArrays) {
      for (let i = 0; i < helix.length; i += 8) {
        const node = helix[i];
        const innerNodes = nodes.filter(n =>
          n !== node &&
          n !== rootNode &&
          n.distanceFromRoot < node.distanceFromRoot * 0.5
        );
        if (innerNodes.length > 0) {
          const nearest = innerNodes.sort((a, b) =>
            node.position.distanceTo(a.position) - node.position.distanceTo(b.position)
          )[0];
          node.addConnection(nearest, 0.5);
        }
      }
    }
    const allHelixNodes = nodes.filter(n => n !== rootNode);
    for (let i = 0; i < Math.floor(30 * densityFactor); i++) {
      const n1 = allHelixNodes[Math.floor(Math.random() * allHelixNodes.length)];
      const nearby = allHelixNodes.filter(n => {
        const dist = n.position.distanceTo(n1.position);
        return n !== n1 && dist < 8 && dist > 3 && !n1.isConnectedTo(n);
      });
      if (nearby.length > 0) {
        const n2 = nearby[Math.floor(Math.random() * nearby.length)];
        n1.addConnection(n2, 0.45);
      }
    }
  }

  function generateFractalWeb() {
    rootNode = new Node(new THREE.Vector3(0, 0, 0), 0, 0);
    rootNode.size = 1.6;
    nodes.push(rootNode);
    const branches = 6;
    const maxDepth = 4;

    function createBranch(startNode, direction, depth, strength, scale) {
      if (depth > maxDepth) return;
      const branchLength = 5 * scale;
      const endPos = new THREE.Vector3()
        .copy(startNode.position)
        .add(direction.clone().multiplyScalar(branchLength));
      const isLeaf = depth === maxDepth || Math.random() < 0.3;
      const newNode = new Node(endPos, depth, isLeaf ? 1 : 0);
      newNode.distanceFromRoot = rootNode.position.distanceTo(endPos);
      nodes.push(newNode);
      startNode.addConnection(newNode, strength);
      if (depth < maxDepth) {
        const subBranches = 3;
        for (let i = 0; i < subBranches; i++) {
          const angle = (i / subBranches) * Math.PI * 2;
          const perpDir1 = new THREE.Vector3(-direction.y, direction.x, 0).normalize();
          const perpDir2 = direction.clone().cross(perpDir1).normalize();
          const newDir = new THREE.Vector3()
            .copy(direction)
            .add(perpDir1.clone().multiplyScalar(Math.cos(angle) * 0.7))
            .add(perpDir2.clone().multiplyScalar(Math.sin(angle) * 0.7))
            .normalize();
          createBranch(newNode, newDir, depth + 1, strength * 0.7, scale * 0.75);
        }
      }
    }

    for (let i = 0; i < branches; i++) {
      const phi = Math.acos(1 - 2 * (i + 0.5) / branches);
      const theta = Math.PI * (1 + Math.sqrt(5)) * i;
      const direction = new THREE.Vector3(
        Math.sin(phi) * Math.cos(theta),
        Math.sin(phi) * Math.sin(theta),
        Math.cos(phi)
      ).normalize();
      createBranch(rootNode, direction, 1, 0.9, 1.0);
    }
    const leafNodes = nodes.filter(n => n.level >= 2);
    for (let i = 0; i < leafNodes.length; i++) {
      const node = leafNodes[i];
      const nearby = leafNodes.filter(n => {
        const dist = n.position.distanceTo(node.position);
        return n !== node && dist < 10 && !node.isConnectedTo(n);
      }).sort((a, b) =>
        node.position.distanceTo(a.position) - node.position.distanceTo(b.position)
      ).slice(0, 3);
      for (const nearNode of nearby) {
        if (Math.random() < 0.5 * densityFactor) {
          node.addConnection(nearNode, 0.5);
        }
      }
    }
    const midLevelNodes = nodes.filter(n => n.level >= 2 && n.level <= 3);
    for (const node of midLevelNodes) {
      if (Math.random() < 0.3) {
        const innerNodes = nodes.filter(n =>
          n !== node &&
          n.distanceFromRoot < node.distanceFromRoot * 0.6
        );
        if (innerNodes.length > 0) {
          const target = innerNodes[Math.floor(Math.random() * innerNodes.length)];
          if (!node.isConnectedTo(target)) {
            node.addConnection(target, 0.4);
          }
        }
      }
    }
  }

  switch (formationIndex % 3) {
    case 0: generateCrystallineSphere(); break;
    case 1: generateHelixLattice(); break;
    case 2: generateFractalWeb(); break;
  }

  if (densityFactor < 1.0) {
    const targetCount = Math.ceil(nodes.length * Math.max(0.3, densityFactor));
    const toKeep = new Set([rootNode]);
    const sortedNodes = nodes.filter(n => n !== rootNode)
      .sort((a, b) => {
        const scoreA = a.connections.length * (1 / (a.distanceFromRoot + 1));
        const scoreB = b.connections.length * (1 / (b.distanceFromRoot + 1));
        return scoreB - scoreA;
      });
    for (let i = 0; i < Math.min(targetCount - 1, sortedNodes.length); i++) {
      toKeep.add(sortedNodes[i]);
    }
    nodes = nodes.filter(n => toKeep.has(n));
    nodes.forEach(node => {
      node.connections = node.connections.filter(conn => toKeep.has(conn.node));
    });
  }

  return { nodes, rootNode };
}

// ---------- 可视化构建 ----------
let neuralNetwork = null;
let nodesMesh = null;
let connectionsMesh = null;

function createNetworkVisualization(formationIndex, densityFactor = 1.0) {
  if (nodesMesh) {
    scene.remove(nodesMesh);
    nodesMesh.geometry.dispose();
    nodesMesh.material.dispose();
  }
  if (connectionsMesh) {
    scene.remove(connectionsMesh);
    connectionsMesh.geometry.dispose();
    connectionsMesh.material.dispose();
  }

  neuralNetwork = generateNeuralNetwork(formationIndex, densityFactor);
  if (!neuralNetwork || neuralNetwork.nodes.length === 0) {
    console.error("Network generation failed");
    return;
  }

  const nodesGeometry = new THREE.BufferGeometry();
  const nodePositions = [];
  const nodeTypes = [];
  const nodeSizes = [];
  const nodeColors = [];
  const distancesFromRoot = [];
  const palette = colorPalettes[config.activePaletteIndex];

  neuralNetwork.nodes.forEach((node) => {
    nodePositions.push(node.position.x, node.position.y, node.position.z);
    nodeTypes.push(node.type);
    nodeSizes.push(node.size);
    distancesFromRoot.push(node.distanceFromRoot);
    const colorIndex = Math.min(node.level, palette.length - 1);
    const baseColor = palette[colorIndex % palette.length].clone();
    baseColor.offsetHSL(
      THREE.MathUtils.randFloatSpread(0.03),
      THREE.MathUtils.randFloatSpread(0.08),
      THREE.MathUtils.randFloatSpread(0.08)
    );
    nodeColors.push(baseColor.r, baseColor.g, baseColor.b);
  });

  nodesGeometry.setAttribute('position', new THREE.Float32BufferAttribute(nodePositions, 3));
  nodesGeometry.setAttribute('nodeType', new THREE.Float32BufferAttribute(nodeTypes, 1));
  nodesGeometry.setAttribute('nodeSize', new THREE.Float32BufferAttribute(nodeSizes, 1));
  nodesGeometry.setAttribute('nodeColor', new THREE.Float32BufferAttribute(nodeColors, 3));
  nodesGeometry.setAttribute('distanceFromRoot', new THREE.Float32BufferAttribute(distancesFromRoot, 1));

  const nodesMaterial = new THREE.ShaderMaterial({
    uniforms: THREE.UniformsUtils.clone(pulseUniforms),
    vertexShader: nodeShader.vertexShader,
    fragmentShader: nodeShader.fragmentShader,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  });

  nodesMesh = new THREE.Points(nodesGeometry, nodesMaterial);
  scene.add(nodesMesh);

  const connectionsGeometry = new THREE.BufferGeometry();
  const connectionColors = [];
  const connectionStrengths = [];
  const connectionPositions = [];
  const startPoints = [];
  const endPoints = [];
  const pathIndices = [];
  const processedConnections = new Set();
  let pathIndex = 0;

  neuralNetwork.nodes.forEach((node, nodeIndex) => {
    node.connections.forEach(connection => {
      const connectedNode = connection.node;
      const connectedIndex = neuralNetwork.nodes.indexOf(connectedNode);
      if (connectedIndex === -1) return;
      const key = [Math.min(nodeIndex, connectedIndex), Math.max(nodeIndex, connectedIndex)].join('-');
      if (!processedConnections.has(key)) {
        processedConnections.add(key);
        const startPoint = node.position;
        const endPoint = connectedNode.position;
        const numSegments = 20;
        for (let i = 0; i < numSegments; i++) {
          const t = i / (numSegments - 1);
          connectionPositions.push(t, 0, 0);
          startPoints.push(startPoint.x, startPoint.y, startPoint.z);
          endPoints.push(endPoint.x, endPoint.y, endPoint.z);
          pathIndices.push(pathIndex);
          connectionStrengths.push(connection.strength);
          const avgLevel = Math.min(Math.floor((node.level + connectedNode.level) / 2), palette.length - 1);
          const baseColor = palette[avgLevel % palette.length].clone();
          baseColor.offsetHSL(
            THREE.MathUtils.randFloatSpread(0.03),
            THREE.MathUtils.randFloatSpread(0.08),
            THREE.MathUtils.randFloatSpread(0.08)
          );
          connectionColors.push(baseColor.r, baseColor.g, baseColor.b);
        }
        pathIndex++;
      }
    });
  });

  connectionsGeometry.setAttribute('position', new THREE.Float32BufferAttribute(connectionPositions, 3));
  connectionsGeometry.setAttribute('startPoint', new THREE.Float32BufferAttribute(startPoints, 3));
  connectionsGeometry.setAttribute('endPoint', new THREE.Float32BufferAttribute(endPoints, 3));
  connectionsGeometry.setAttribute('connectionStrength', new THREE.Float32BufferAttribute(connectionStrengths, 1));
  connectionsGeometry.setAttribute('connectionColor', new THREE.Float32BufferAttribute(connectionColors, 3));
  connectionsGeometry.setAttribute('pathIndex', new THREE.Float32BufferAttribute(pathIndices, 1));

  const connectionsMaterial = new THREE.ShaderMaterial({
    uniforms: THREE.UniformsUtils.clone(pulseUniforms),
    vertexShader: connectionShader.vertexShader,
    fragmentShader: connectionShader.fragmentShader,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  });

  connectionsMesh = new THREE.LineSegments(connectionsGeometry, connectionsMaterial);
  scene.add(connectionsMesh);

  palette.forEach((color, i) => {
    if (i < 3) {
      connectionsMaterial.uniforms.uPulseColors.value[i].copy(color);
      nodesMaterial.uniforms.uPulseColors.value[i].copy(color);
    }
  });
}

// ---------- 点击脉冲 ----------
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const interactionPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
const interactionPoint = new THREE.Vector3();
let lastPulseIndex = 0;

function triggerPulse(clientX, clientY) {
  pointer.x = (clientX / window.innerWidth) * 2 - 1;
  pointer.y = -(clientY / window.innerHeight) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  interactionPlane.normal.copy(camera.position).normalize();
  interactionPlane.constant = -interactionPlane.normal.dot(camera.position) + camera.position.length() * 0.5;
  if (raycaster.ray.intersectPlane(interactionPlane, interactionPoint)) {
    const time = clock.getElapsedTime();
    if (nodesMesh && connectionsMesh) {
      lastPulseIndex = (lastPulseIndex + 1) % 3;
      nodesMesh.material.uniforms.uPulsePositions.value[lastPulseIndex].copy(interactionPoint);
      nodesMesh.material.uniforms.uPulseTimes.value[lastPulseIndex] = time;
      connectionsMesh.material.uniforms.uPulsePositions.value[lastPulseIndex].copy(interactionPoint);
      connectionsMesh.material.uniforms.uPulseTimes.value[lastPulseIndex] = time;
      const palette = colorPalettes[config.activePaletteIndex];
      const randomColor = palette[Math.floor(Math.random() * palette.length)];
      nodesMesh.material.uniforms.uPulseColors.value[lastPulseIndex].copy(randomColor);
      connectionsMesh.material.uniforms.uPulseColors.value[lastPulseIndex].copy(randomColor);
    }
  }
}

renderer.domElement.addEventListener('click', (e) => {
  if (e.target.closest('.login-panel')) return;
  if (!config.paused) triggerPulse(e.clientX, e.clientY);
});

renderer.domElement.addEventListener('touchstart', (e) => {
  if (e.target.closest('.login-panel')) return;
  if (e.touches.length > 0 && !config.paused) {
    triggerPulse(e.touches[0].clientX, e.touches[0].clientY);
  }
}, { passive: true });

// ---------- 渲染循环 ----------
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

function animate() {
  requestAnimationFrame(animate);
  const t = clock.getElapsedTime();
  if (!config.paused && !reduceMotion.matches) {
    if (nodesMesh) {
      nodesMesh.material.uniforms.uTime.value = t;
      nodesMesh.rotation.y = Math.sin(t * 0.04) * 0.05;
    }
    if (connectionsMesh) {
      connectionsMesh.material.uniforms.uTime.value = t;
      connectionsMesh.rotation.y = Math.sin(t * 0.04) * 0.05;
    }
  }
  if (!reduceMotion.matches) {
    starField.rotation.y += 0.0002;
  }
  starField.material.uniforms.uTime.value = t;
  controls.update();
  composer.render();
}

function onWindowResize() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  composer.setSize(window.innerWidth, window.innerHeight);
  bloomPass.resolution.set(window.innerWidth, window.innerHeight);
}

window.addEventListener('resize', onWindowResize);

createNetworkVisualization(config.currentFormation, config.densityFactor);
animate();

// ---------- 登录 ----------
const passwordInput = document.getElementById('password-input');
const loginButton = document.getElementById('login-button');
const loginMessage = document.getElementById('login-message');

function showMessage(text, type) {
  loginMessage.textContent = text;
  loginMessage.className = 'login-message show ' + type;
}

let submitting = false;

async function handleLogin() {
  if (submitting) return;
  const value = passwordInput.value;
  if (!value) {
    showMessage('请输入密码', 'error');
    passwordInput.focus();
    return;
  }
  submitting = true;
  loginButton.disabled = true;
  showMessage('正在校验…', '');
  let response;
  try {
    response = await fetch('/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: value })
    });
  } catch (error) {
    submitting = false;
    loginButton.disabled = false;
    showMessage('连不上中枢，请稍后重试', 'error');
    return;
  }
  if (response.ok) {
    showMessage('登录成功，正在进入…', 'success');
    location = '/';
    return;
  }
  submitting = false;
  loginButton.disabled = false;
  passwordInput.select();
  if (response.status === 429) {
    showMessage('尝试太频繁，请稍后再试', 'error');
    return;
  }
  const payload = await response.json().catch(() => ({}));
  showMessage(payload.error || '登录失败，请重试', 'error');
}

loginButton.addEventListener('click', handleLogin);
passwordInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') handleLogin();
});
passwordInput.addEventListener('input', () => {
  loginMessage.className = 'login-message';
});
</script>
</body></html>
"""
    return (
        page.replace("__BRAND__", BRAND_LOCKUP_SVG)
        .replace("__INITIAL__", esc(INITIAL_ADMIN_PASSWORD))
    )


def confirm_button(
    message: str,
    url: str,
    body: dict | None = None,
    *,
    label: str,
    confirm: str,
    quiet: bool = False,
    title: str = "",
) -> str:
    """Ask in the shared dialog. Cancel hides it and does not post."""
    payload = esc(
        json.dumps(
            {"message": message, "url": url, "body": body or {}, "confirm": confirm},
            ensure_ascii=False,
        )
    )
    klass = "quiet" if quiet else "danger"
    tip = f" title='{esc(title)}'" if title else ""
    return f"<button class='{klass}' type='button' data-delete='{payload}'{tip}>{esc(label)}</button>"


def delete_button(message: str, url: str, body: dict | None = None) -> str:
    return confirm_button(message, url, body, label="删除", confirm="确认删除")


def when(value) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    return f"<time datetime='{esc(text)}'>{esc(text)}</time>"


def channel_bar() -> str:
    if not CHANNEL.get():
        return ""
    return (
        "<div class='channel-bar'>"
        "为保障记忆服务通畅，请务必保持本窗口开启或最小化。关闭本窗口，本机AGENT将无法连接ROGMEM记忆中枢！"
        "</div>"
    )


def _nav_model_health() -> str:
    """顶栏右侧：当前调用目标最近一次探活的炭火色，不是这一小时汇总。"""
    from rogmem.availability import fill, latest_probe

    mark = {
        "tone": "unknown",
        "depth": 0.0,
        "detail": "未探活",
        "at": "",
        "name": "",
        "model": "",
    }
    try:
        from rogmem.engine import get_setting, public_providers

        mark = latest_probe(public_providers(), get_setting("model_distill") or "")
    except Exception:
        pass
    tone = str(mark.get("tone") or "unknown")
    paint = fill(tone, float(mark.get("depth") or 0.0)) if tone in {"ok", "warn", "down"} else ""
    words = {"ok": "可用", "warn": "降级", "down": "不可用"}.get(tone, "未探活")
    who = " · ".join(part for part in (str(mark.get("name") or ""), str(mark.get("model") or "")) if part)
    detail = str(mark.get("detail") or words)
    title = f"{who} · {detail}" if who else detail
    when = str(mark.get("at") or "").strip()
    if when:
        title = f"{title} · {when}"
    style = f' style="background:{paint}"' if paint else ""
    return (
        f'<a href="/admin/costs" id="model-health" class="model-health" data-tone="{esc(tone)}"'
        f'{style} aria-label="模型调用{esc(words)}" title="{esc(title)}"></a>'
    )


def page(title: str, active: str, body: str, sub: str = "", reader: bool = False) -> str:
    section, child = _locate(sub or active)
    if section is None:
        section, child = _locate(active)
    top = section[2] if section else active
    current = child[2] if child else ""
    links = _top_links(top, current)
    drawer_links = "".join(
        (
            f'<a aria-current="page" href="{href}">{label}</a>'
            if key == top
            else f'<a href="{href}">{label}</a>'
        )
        for href, label, key, _children in CONSOLE
    )
    side = ""
    if section and section[3]:
        side = "<nav class='side'>" + "".join(
            f"<a class='{'on' if key == current else ''}' href='{href}'>{label}</a>"
            for href, label, key in section[3]
        ) + "</nav>"
    classes = []
    if top == "file":
        classes.append("file-screen")
    if top == "graph":
        classes.append("graph-screen")
    if reader:
        classes.append("reader-screen")
    body_class = " ".join(classes)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{"ROGMEM · " + esc(title) if str(title or "").strip() else "ROGMEM"}</title>
<script>
try {{
  var savedTheme = localStorage.getItem("rogmem-theme");
  if (savedTheme !== "light") document.documentElement.setAttribute("data-theme", "dark");
}} catch (err) {{
  document.documentElement.setAttribute("data-theme", "dark");
}}
</script>
<style>
:root {{
  color-scheme: light;
  --surface-page: #fff;
  --surface-base: #fff;
  --surface-subtle: #f8f9fa;
  --surface-sidebar: #f8f8f9;
  --surface-hover: #f0f1f2;
  --surface-selected: #e9eaec;
  --surface-popover: #fff;
  --text-primary: #202124;
  --text-secondary: #454950;
  --text-muted: #5c6066;
  --border: #e4e6e8;
  --border-strong: #ced1d5;
  --border-hover: #b9bdc3;
  --input: #dfe1e5;
  --primary: #252528;
  --primary-foreground: #fff;
  --primary-hover: #39393e;
  --destructive: #b43b4d;
  --destructive-hover: #982d3e;
  --brand: #ef6576;
  --rev-tick: #6a7078;
  --link-color: #8b3e50;
  --focus-color: #b64a60;
  --success-bg: #edf5f0;
  --success-text: #27684a;
  --danger-bg: #fff0f1;
  --danger-text: #9c3344;
  --warning-bg: #fff7e8;
  --warning-text: #80551b;
  --status-icon-success: #2f9e63;
  --status-icon-danger: #d93d52;
  --status-icon-warning: #e0a300;
  --notice-bg: #fff3f4;
  --notice-border: #edd6dc;
  --notice-text: #823e50;
  --chart-bar: #ff3763;
  --chart-bar-hover: #e21744;
  --gather-ink: var(--text-muted);
  --gather-glint: #fff;
  --chart-grid: var(--border);
  --chart-axis: var(--text-muted);
  --overlay: rgba(20, 21, 24, .45);
  --shadow-menu: 0 8px 28px rgb(32 33 36 / 8%);
  --shadow-dialog: 0 20px 64px rgba(32, 33, 36, .15);
  --selection-bg: #f2d6de;
  --ink: var(--text-primary);
  --muted: var(--text-muted);
  --line: var(--border);
  --bg: var(--surface-page);
  --card: var(--surface-base);
  --head: var(--surface-subtle);
  --lede: var(--surface-subtle);
  --lede-line: var(--border);
  --mark: var(--selection-bg);
  --accent: var(--link-color);
}}
html[data-theme="dark"] {{
  color-scheme: dark;
  --surface-page: #171717;
  --surface-base: #212121;
  --surface-subtle: #282828;
  --surface-sidebar: #151515;
  --surface-hover: #303030;
  --surface-selected: #383838;
  --surface-popover: #262626;
  --text-primary: #f2f2f2;
  --text-secondary: #d6d6d6;
  --text-muted: #adadad;
  --border: #3d3d3d;
  --border-strong: #5e5e5e;
  --border-hover: #777;
  --input: #4d4d4d;
  --primary: #f2f2f2;
  --primary-foreground: #212121;
  --primary-hover: #dedede;
  --destructive: #b43b4d;
  --destructive-hover: #982d3e;
  --brand: #ef6576;
  --rev-tick: #c8c8c8;
  --link-color: #f2acba;
  --focus-color: #f095a8;
  --success-bg: #173b2c;
  --success-text: #97dbb2;
  --danger-bg: #40252d;
  --danger-text: #ffadb9;
  --warning-bg: #3b3121;
  --warning-text: #f1cc90;
  --status-icon-success: #5cc98c;
  --status-icon-danger: #ff7a8a;
  --status-icon-warning: #f2c14e;
  --notice-bg: #2c2024;
  --notice-border: #53343e;
  --notice-text: #edb4c2;
  --chart-bar: #ff3763;
  --chart-bar-hover: #ff5a7c;
  --gather-ink: #7a7a7a;
  --gather-glint: #fff;
  --chart-grid: var(--border);
  --chart-axis: var(--text-muted);
  --overlay: rgba(0, 0, 0, .65);
  --shadow-menu: 0 8px 28px rgba(0, 0, 0, .28);
  --shadow-dialog: 0 20px 64px rgba(0, 0, 0, .4);
  --selection-bg: #553040;
  --ink: var(--text-primary);
  --muted: var(--text-muted);
  --line: var(--border);
  --bg: var(--surface-page);
  --card: var(--surface-base);
  --head: var(--surface-subtle);
  --lede: var(--surface-subtle);
  --lede-line: var(--border);
  --mark: var(--selection-bg);
  --accent: var(--link-color);
}}
* {{ box-sizing: border-box; }}
::selection {{ background: var(--selection-bg); color: var(--text-primary); }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0;
  font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.5;
  color: var(--text-primary);
  background: var(--surface-page);
}}
a {{ color: var(--link-color); text-decoration: none; text-underline-offset: 4px; }}
a:hover {{ text-decoration: underline; }}
:focus-visible {{ outline: 2px solid var(--focus-color); outline-offset: 2px; }}
header {{
  display: flex;
  align-items: center;
  gap: 8px;
  height: 60px;
  padding: 0 12px;
  background: var(--surface-sidebar);
  border-bottom: 1px solid var(--border);
}}
header .brand {{
  flex: none;
  display: inline-flex;
  align-items: center;
  color: var(--text-primary);
  padding: 0 4px;
  text-decoration: none;
}}
header .brand svg {{ display: block; height: 26px; width: auto; }}
header nav {{ display: flex; gap: 2px; flex-wrap: nowrap; min-width: 0; overflow-x: auto; }}
header nav a {{
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: nowrap;
  text-decoration: none;
  transition: background-color .15s ease, color .15s ease;
}}
header nav a:hover {{ background: var(--surface-hover); color: var(--text-primary); text-decoration: none; }}
header nav a.on {{ background: var(--surface-selected); color: var(--text-primary); font-weight: 500; }}
header nav .nav-item {{ position: relative; display: flex; align-items: center; }}
header nav .nav-sub {{
  display: none;
  position: absolute;
  z-index: 40;
  top: 100%;
  left: 0;
  width: max-content;
  min-width: 100%;
  max-width: calc(100vw - 16px);
  padding-top: 4px;
  background: transparent;
  border: 0;
  box-shadow: none;
}}
header nav .nav-item:hover > .nav-sub,
header nav .nav-item:focus-within > .nav-sub,
header nav .nav-item.open > .nav-sub {{ display: block; }}
header nav .nav-sub-panel {{
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 4px;
  background: var(--surface-popover);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-menu);
}}
header nav .nav-sub a {{
  display: flex;
  width: 100%;
  min-height: 32px;
  margin: 0;
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 400;
  line-height: 1.5;
  color: var(--text-secondary);
}}
header nav .nav-sub a:hover,
header nav .nav-sub a:focus-visible {{ background: var(--surface-hover); color: var(--text-primary); text-decoration: none; }}
header nav .nav-sub a.on {{ background: var(--surface-selected); color: var(--text-primary); font-weight: 400; }}
header .ver {{ margin-left: auto; color: var(--text-muted); font-size: 12px; font-weight: 400; letter-spacing: 0; white-space: nowrap; }}
header .model-health {{
  position: relative; flex: none; box-sizing: border-box;
  width: 10px; height: 10px; min-height: 10px;
  padding: 0; border-radius: 50%; border: 1px solid transparent; text-decoration: none;
  background: var(--surface-subtle); border-color: var(--border-strong);
}}
header .model-health::after {{ content: ""; position: absolute; inset: -8px -4px; border-radius: 50%; }}
header .model-health[data-tone="ok"],
header .model-health[data-tone="warn"],
header .model-health[data-tone="down"] {{ border-color: transparent; }}
header .model-health[data-tone="unknown"] {{ background: var(--surface-subtle); border-color: var(--border-strong); }}
.theme-mode {{ display: inline-flex; gap: 8px; }}
.theme-mode button[aria-pressed="true"] {{ background: var(--surface-selected); color: var(--text-primary); border-color: var(--border-strong); }}
main {{ display: flex; gap: 20px; padding: 24px 24px 48px; align-items: flex-start; }}
body.reader-screen {{ --page-pad: 24px; height: 100vh; overflow: hidden; display: flex; flex-direction: column; }}
body.reader-screen header, body.reader-screen .channel-bar {{ flex: none; }}
body.reader-screen main.reader {{ display: flex; flex-direction: column; flex: 1; height: auto; min-height: 0; align-items: stretch; padding: 0 var(--page-pad) 48px 0; }}
.reader-sheet .wiki {{ align-items: stretch; }}
.reader-sheet .wiki, .reader-sheet .wiki.solo {{ grid-template-columns: calc(252px + var(--page-pad)) minmax(0, 1fr); }}
.reader-sheet .wiki-article, .reader-sheet .wiki-side {{ min-height: 0; height: 100%; max-height: none; position: static; }}
.reader-sheet .wiki-main {{ display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; padding-top: var(--page-pad); padding-left: var(--page-pad); }}
.reader-sheet .wiki-main > .wiki-article {{ flex: 1 1 auto; height: auto; min-height: 0; }}
html:not(.nav-collapsed) .reader-sheet .wiki-side {{ padding-left: calc(12px + var(--page-pad)); }}
html.nav-collapsed .reader-sheet .wiki-main {{ padding-left: var(--page-pad); }}
main.reader .content {{ height: 100%; min-height: 0; display: flex; flex-direction: column; }}
.reader-sheet {{ display: flex; flex-direction: column; flex: 1; min-height: 0; }}
.entry-head {{ flex: none; }}
.entry-head h1 {{ display: flex; align-items: center; gap: 8px; margin: 0; }}
.entry-head > .meta {{ display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 4px 16px; margin: 4px 0 16px; }}
.entry-head > .meta .kind {{ color: var(--text-muted); font-size: 14px; line-height: 1.5; font-weight: 400; }}
.entry-head .freshness {{ font-size: 12px; line-height: 1.4; font-weight: 400; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px 14px; margin-left: auto; color: color-mix(in srgb, var(--text-muted) 70%, transparent); }}
.wiki {{ display: grid; grid-template-columns: 252px minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); flex: 1; min-height: 0; align-items: start; }}
.wiki.solo {{ grid-template-columns: 252px minmax(0, 1fr); }}
.wiki-side {{
  border-right: 1px solid var(--border);
  padding: 16px 12px 28px;
  max-height: calc(100vh - 60px);
  overflow: auto;
  position: sticky;
  top: 0;
  background: var(--surface-sidebar);
}}
.wiki > aside.wiki-side {{ align-self: stretch; min-height: 0; max-height: none; overflow: auto; }}
.toc-dock {{ position: fixed; z-index: 24; top: var(--sheet-top, 76px); right: 0; display: flex; flex-direction: row-reverse; align-items: flex-start; max-height: calc(100vh - var(--sheet-top, 76px) - 16px); }}
.toc-dock button.toc-tab,
html.nav-collapsed button.nav-fold {{
  writing-mode: vertical-rl;
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: .04em;
  margin: 0;
  min-height: 0;
  padding: 14px 7px;
  border: 1px solid var(--border);
  background: var(--surface-base);
  color: var(--text-muted);
  box-shadow: var(--shadow-menu);
}}
.toc-dock button.toc-tab {{
  border-right: 0;
  border-radius: 8px 0 0 8px;
  cursor: pointer;
}}
.toc-dock .toc-panel {{
  display: none;
  width: 208px;
  max-height: calc(100vh - var(--sheet-top, 76px) - 16px);
  overflow: auto;
  padding: 10px 12px 14px;
  background: var(--surface-popover);
  border: 1px solid var(--border);
  border-right: 0;
  border-radius: 12px 0 0 12px;
  box-shadow: var(--shadow-menu);
}}
.toc-dock.open .toc-panel {{ display: block; }}
.toc-group + .toc-group {{ margin-top: 16px; }}
.toc-group .wiki-label {{ margin: 0 0 6px; padding: 0 0 6px; border-bottom: 1px solid var(--border); font-size: 12px; font-weight: 600; line-height: 1.4; color: var(--text-primary); }}
.toc-group .toc a {{ padding: 6px 8px; }}
.toc-group .muted {{ margin: 0; padding: 4px 8px; font-size: 13px; line-height: 1.5; }}
.nav-head {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 0 8px; }}
.nav-head .wiki-label {{ margin: 0; }}
button.nav-fold {{ flex: none; width: 28px; height: 28px; min-height: 28px; padding: 0; color: var(--text-secondary); }}
button.nav-fold svg {{ display: block; width: 16px; height: 16px; }}
button.nav-fold .fold-name {{ display: none; }}
html.nav-collapsed button.nav-fold svg {{ display: none; }}
html.nav-collapsed button.nav-fold .fold-name {{ display: block; }}
html.nav-collapsed .wiki, html.nav-collapsed .wiki.solo {{ grid-template-columns: minmax(0, 1fr); }}
html.nav-collapsed .wiki > aside.wiki-side {{ position: fixed; z-index: 26; top: var(--sheet-top, 76px); left: 0; right: auto; width: auto; height: auto; max-height: none; overflow: visible; padding: 0; border: 0; background: transparent; }}
html.nav-collapsed.nav-dock-right .wiki > aside.wiki-side {{ left: auto; right: 0; }}
html.nav-collapsed.nav-dock-right .toc-dock {{ top: calc(var(--sheet-top, 76px) + 96px); }}
html.nav-collapsed #crystal-nav-body {{ display: none; }}
html.nav-collapsed .nav-head {{ margin: 0; }}
html.nav-collapsed .nav-head .wiki-label {{ display: none; }}
html.nav-collapsed button.nav-fold {{
  width: auto;
  height: auto;
  border-left: 0;
  border-radius: 0 8px 8px 0;
  cursor: grab;
  touch-action: none;
  user-select: none;
}}
html.nav-collapsed.nav-dock-right button.nav-fold {{ border-left: 1px solid var(--border); border-right: 0; border-radius: 8px 0 0 8px; }}
.wiki-filter {{ display: flex; gap: 4px; align-items: center; margin: 0 0 10px; }}
.wiki-filter select {{ flex: 1; min-width: 0; width: auto; margin: 0; padding: 4px 8px; font-size: 12px; min-height: 28px; }}
.wiki-filter button.quiet {{ flex: none; min-height: 28px; padding: 4px 8px; font-size: 12px; }}
.wiki-label {{ margin: 0 0 8px; font-size: 12px; font-weight: 500; line-height: 1.5; letter-spacing: 0; color: var(--text-muted); }}
.wiki-article {{ padding: 0 0 72px; max-height: none; overflow: auto; min-width: 0; font-size: 16px; line-height: 1.65; flex: 1; }}
.wiki-article h1 {{ font-size: 28px; line-height: 1.4; font-weight: 600; letter-spacing: -0.4px; margin: 0 0 8px; display: flex; align-items: center; gap: 8px; }}
.wiki-article h2 {{ font-size: 18px; line-height: 1.5; font-weight: 600; margin: 28px 0 8px; }}
.wiki-article h3 {{ font-size: 16px; line-height: 1.5; font-weight: 600; margin: 22px 0 8px; }}
.wiki-article p {{ margin: 0 0 12px; }}
.wiki-article a {{ color: var(--link-color); }}
.owner-lock {{ display: inline-flex; color: var(--text-muted); cursor: help; flex: none; }}
.owner-lock svg {{ display: block; width: 18px; height: 18px; }}
.entry-head {{ display: block; }}
.lede {{ display: block; width: 100%; margin: 12px 0 2px; padding: 12px 14px 10px; background: var(--surface-subtle); border: 1px solid var(--border); border-radius: 12px; }}
.lede b {{ display: block; font-size: 12px; line-height: 1.5; letter-spacing: 0; color: var(--brand); font-weight: 600; }}
.lede p {{ margin: 2px 0 0; }}
.lede.muted p {{ color: var(--text-muted); }}
sup.cite {{ position: relative; font-size: 12px; line-height: 0; margin-left: 1px; cursor: help; color: var(--link-color); font-weight: 600; }}
sup.cite a {{ color: var(--link-color); text-decoration: none; font-weight: 600; }}
sup.cite .cite-card {{ display: none; position: absolute; left: 0; top: 1.4em; z-index: 30; width: 280px; padding: 8px 10px; line-height: 1.5; font-size: 12px; font-weight: 400; color: var(--text-primary); background: var(--surface-popover); border: 1px solid var(--border); border-radius: 8px; box-shadow: var(--shadow-menu); white-space: normal; }}
sup.cite .cite-card b {{ display: block; font-size: 12px; letter-spacing: 0; color: var(--brand); }}
sup.cite .cite-card q {{ display: block; margin: 4px 0; quotes: none; }}
sup.cite:hover .cite-card, sup.cite:focus .cite-card, sup.cite:focus-within .cite-card {{ display: block; }}
sup.cite.src, sup.cite.src a {{ cursor: pointer; font-weight: 400; color: var(--text-muted); }}
.rev-sheet {{ display: flex; align-items: flex-start; gap: 14px; }}
.rev-rail {{ position: sticky; top: 0; z-index: 2; flex: none; display: flex; flex-direction: column; justify-content: flex-start; gap: 1px; width: 22px; height: calc(100vh - 220px); margin: 0; padding: 2px 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin; }}
.rev-tick, button.rev-tick {{ position: relative; flex: 1 0 5px; display: block; min-height: 5px; max-height: 28px; width: 22px; margin: 0; padding: 0; border: 0; border-radius: 0; background: transparent; color: inherit; box-shadow: none; cursor: pointer; transform: none; }}
button.rev-tick:hover, button.rev-tick:focus-visible {{ background: transparent; color: inherit; }}
.rev-tick i {{ display: block; width: 4px; min-height: 3px; height: calc(100% - 2px); margin: 1px auto 0; border-radius: 999px; background: var(--rev-tick); opacity: .92; transition: width .14s cubic-bezier(.22, .8, .28, 1), background-color .14s ease, opacity .14s ease, box-shadow .14s ease; }}
.rev-tick.on i {{ opacity: 1; background: var(--brand); animation: rev-tick-on .28s ease; }}
.rev-tick.wave-3 i {{ width: 6px; opacity: .8; }}
.rev-tick.wave-2 i {{ width: 8px; opacity: .88; }}
.rev-tick.wave-1 i {{ width: 10px; opacity: .94; }}
.rev-tick.hot i {{ width: 12px; opacity: 1; background: var(--brand); box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand) 28%, transparent); animation: none; }}
.rev-rail:hover .rev-tick.on:not(.hot) i, .rev-rail:focus-within .rev-tick.on:not(.hot) i {{ background: var(--rev-tick); box-shadow: none; animation: none; }}
@keyframes rev-tick-on {{ from {{ transform: scaleY(.6); opacity: .4; }} to {{ transform: scaleY(1); opacity: 1; }} }}
.rev-tip {{ display: none; position: absolute; left: calc(100% + 8px); top: 0; width: max-content; max-width: 360px; padding: 8px 10px; font-size: 12px; line-height: 1.5; font-weight: 400; text-align: left; color: var(--text-primary); background: var(--surface-popover); border: 1px solid var(--border); border-radius: 8px; box-shadow: var(--shadow-menu); z-index: 6; pointer-events: none; white-space: normal; }}
.rev-tick:hover .rev-tip, .rev-tick:focus-visible .rev-tip {{ display: block; }}
.rev-log {{ display: flex; flex-direction: column; gap: 12px; margin: 0; padding: 0; list-style: none; flex: 1; min-width: 0; }}
.rev {{ background: var(--surface-base); border: 1px solid var(--border); border-radius: 12px; padding: 16px 16px 14px; }}
.rev-gist {{ margin: 8px 0 0; font-size: 14px; line-height: 1.5; }}
.rev-head {{ display: flex; align-items: center; gap: 10px 14px; flex-wrap: wrap; font-size: 14px; }}
.rev-head time {{ font-variant-numeric: tabular-nums; color: var(--text-secondary); }}
.rev-actions {{ margin-left: auto; display: inline-flex; align-items: center; gap: 8px; flex: none; }}
.rev-head .rev-actions button {{ margin-left: 0; }}
.rev-k {{ color: var(--text-muted); margin-right: 4px; }}
.rev-meta {{ display: flex; flex-wrap: wrap; gap: 8px 14px; margin: 0 0 12px; font-size: 14px; }}
.rev-detail-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 0 0 16px; }}
.rev-detail-bar h2 {{ margin: 0; }}
#rev-detail-body {{ max-height: min(62vh, 560px); overflow: auto; }}
.rev-change, .rev-basis {{ margin: 10px 0 0; white-space: pre-wrap; }}
.rev-change {{ padding-top: 10px; border-top: 1px solid var(--border); }}
.flow {{ display: inline-flex; align-items: center; gap: 7px; padding: 4px 8px; border-radius: 999px; background: var(--surface-subtle); border: 1px solid var(--border); }}
.flow-progress {{ margin-left: 6px; font-size: 12px; line-height: 1; color: var(--text-secondary); font-variant-numeric: tabular-nums; white-space: nowrap; }}
.flow .branch {{ display: inline-flex; flex-direction: column; align-items: center; gap: 3px; }}
.flow button.lamp {{ width: 18px; height: 18px; min-height: 0; margin: 0; padding: 0; border: 0; border-radius: 50%; background: transparent; color: inherit; box-shadow: none; cursor: pointer; line-height: 0; transform: none; }}
.flow button.lamp i {{ display: block; width: 10px; height: 10px; margin: 4px auto; border-radius: 50%; box-sizing: border-box; border: 1.5px solid var(--text-muted); background: transparent; }}
.flow button.ok i {{ background: var(--status-icon-success); border-color: var(--status-icon-success); }}
.flow button.fail i {{ background: var(--status-icon-danger); border-color: var(--status-icon-danger); }}
.flow button.run i {{ background: var(--text-primary); border-color: var(--text-primary); animation: lamp-breathe 1.6s ease-in-out infinite; }}
.flow button.retry i {{ background: var(--status-icon-warning); border-color: var(--status-icon-warning); animation: lamp-breathe-retry 1.6s ease-in-out infinite; }}
.lamp-pop {{ position: fixed; z-index: 40; left: 0; top: 0; max-width: 240px; padding: 6px 8px; font-size: 12px; line-height: 1.5; font-weight: 400; color: var(--text-primary); background: var(--surface-popover); border: 1px solid var(--border); border-radius: 8px; box-shadow: var(--shadow-menu); pointer-events: none; }}
.lamp-pop[hidden] {{ display: none; }}
@keyframes lamp-breathe {{
  0%, 100% {{ opacity: .45; }}
  50% {{ opacity: 1; }}
}}
@keyframes lamp-breathe-retry {{
  0%, 100% {{ opacity: .45; }}
  50% {{ opacity: 1; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .flow button.run i, .flow button.retry i {{ animation: none; opacity: 1; }}
  .rev-tick i {{ transition: none; }}
  .rev-tick.on i {{ animation: none; }}
  .rev-tick.hot i, .rev-tick.wave-1 i, .rev-tick.wave-2 i, .rev-tick.wave-3 i, .rev-tick:focus-visible i {{ transition: none; }}
  button:active, .btn:active {{ transform: none; }}
}}
.basis {{ margin: 4px 0 10px; white-space: pre-wrap; }}
.change {{ margin: 4px 0; white-space: pre-wrap; font: inherit; }}
ol.chain {{ margin: 6px 0 0; padding-left: 1.2em; }}
.basis q {{ color: var(--text-primary); }}
mark {{ background: var(--selection-bg); color: var(--text-primary); }}
.wiki-tabs {{ display: flex; align-items: center; gap: 0; border-bottom: 1px solid var(--border); margin: 16px 0 28px; }}
.wiki-tabs a {{ padding: 8px 14px 10px; border-bottom: 2px solid transparent; color: var(--text-muted); text-decoration: none; font-size: 14px; font-weight: 500; border-radius: 0; }}
.wiki-tabs a:hover {{ color: var(--text-primary); text-decoration: none; background: var(--surface-hover); }}
.wiki-tabs a.on {{ color: var(--text-primary); border-bottom-color: var(--text-primary); font-weight: 600; background: transparent; }}
.wiki-tabs a.edit-toggle {{ margin-left: auto; margin-bottom: 8px; min-height: 32px; padding: 4px 14px; border: 1px solid var(--border); border-radius: 8px; color: var(--text-primary); background: var(--surface-base); }}
.wiki-tabs a.edit-toggle:hover {{ background: var(--surface-hover); text-decoration: none; }}
.wiki-tabs a.edit-toggle.on {{ color: var(--primary-foreground); background: var(--primary); border-color: var(--primary); }}
.edit-sheet {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }}
.edit-sheet label {{ display: flex; flex-direction: column; gap: 4px; color: var(--text-secondary); font-size: 13px; line-height: 1.5; }}
.edit-sheet .wide {{ grid-column: 1 / -1; }}
.edit-sheet input, .edit-sheet textarea {{ width: 100%; color: var(--text-primary); }}
.edit-sheet textarea {{ min-height: 4.5em; }}
.edit-sheet textarea.chapter {{ min-height: 8em; }}
.edit-actions {{ display: flex; gap: 8px; align-items: center; margin: 8px 0 0; }}
.edit-actions a {{ color: var(--text-primary); background: var(--surface-base); border: 1px solid var(--border); border-radius: 8px; min-height: 36px; padding: 7px 14px; text-decoration: none; font-size: 14px; font-weight: 500; }}
.edit-actions a:hover {{ background: var(--surface-hover); text-decoration: none; }}
@media (max-width: 760px) {{ .edit-sheet {{ grid-template-columns: 1fr; }} }}
table.summary {{ width: 100%; margin: 4px 0 8px; }}
table.summary th, table.summary td {{ padding: 8px 10px; border: 0; border-bottom: 1px solid var(--border); font-weight: 400; font-size: 14px; }}
table.summary th {{ width: 8.5em; color: var(--text-muted); font-size: 13px; font-weight: 500; white-space: nowrap; background: var(--surface-subtle); }}
.side {{ width: 168px; flex: none; background: var(--surface-sidebar); border: 1px solid var(--border); border-radius: 12px; padding: 8px; }}
.side a {{ display: flex; align-items: center; min-height: 36px; padding: 8px 10px; border-radius: 8px; font-size: 14px; font-weight: 500; color: var(--text-secondary); text-decoration: none; transition: background-color .15s ease, color .15s ease; }}
.side a:hover {{ background: var(--surface-hover); color: var(--text-primary); text-decoration: none; }}
.side a.on {{ background: var(--surface-selected); color: var(--text-primary); font-weight: 500; }}
.content {{ flex: 1; min-width: 0; }}
h1 {{ font-size: 28px; line-height: 1.4; font-weight: 600; letter-spacing: -0.4px; margin: 0 0 16px; color: var(--text-primary); }}
h2 {{ font-size: 18px; line-height: 1.5; font-weight: 600; margin: 28px 0 8px; color: var(--text-primary); }}
h3 {{ font-size: 16px; line-height: 1.5; font-weight: 600; margin: 22px 0 8px; }}
.card, .panel {{ background: var(--surface-base); border: 1px solid var(--border); border-radius: 12px; }}
.panel {{ padding: 24px; }}
.settings-stack {{ display: flex; flex-direction: column; gap: 16px; }}
.settings-form {{ padding: 0; overflow: hidden; }}
.setting-row {{ padding: 20px 24px 22px; }}
.setting-row + .setting-row {{ border-top: 1px solid var(--border); }}
.setting-row h2, .setting-block h2 {{ margin: 0 0 6px; font-size: 16px; line-height: 1.5; }}
.setting-line h2 {{ margin: 0; }}
.knob-group {{ margin: 0; padding: 18px 24px 0; font-size: 15px; border-top: 1px solid var(--border); }}
.knob-group + .note {{ padding: 4px 24px 8px; margin: 0; }}
.setting-line .sub {{ margin-left: 8px; opacity: .6; font-size: 12px; }}
.setting-row .note, .setting-block .note {{ margin: 0 0 12px; }}
.setting-line .note {{ margin: 2px 0 0; }}
.setting-name {{ margin: 0 0 8px; font-size: 14px; line-height: 1.5; font-weight: 600; color: var(--text-primary); }}
.setting-line .setting-name {{ margin: 0; }}
.setting-line > div {{ min-width: 0; }}
.plate > .setting-head {{ background: var(--surface-subtle); }}
@media (max-width: 760px) {{
  .plate .setting-line {{ align-items: flex-start; flex-wrap: wrap; row-gap: 8px; }}
}}
.setting-row input:not([type="checkbox"]), .setting-block input {{ display: block; width: min(36em, 100%); }}
.setting-head {{ padding: 16px 24px; border-bottom: 1px solid var(--border); }}
.setting-head h2 {{ margin: 0; font-size: 16px; line-height: 1.5; }}
.setting-line {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
.setting-line .job-field {{ display: flex; align-items: center; gap: 8px; margin: 0; color: var(--text-secondary); font-size: 13px; white-space: nowrap; }}
.setting-line .job-field input {{ display: inline-block; width: 6.5em; }}
.setting-line .job-field.wide input {{ width: 22em; }}
.setting-line .switch {{ margin: 0; max-width: none; }}
.setting-fact {{ margin: 8px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.5; }}
.setting-foot {{
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  padding: 14px 24px;
  border-top: 1px solid var(--border);
  background: var(--surface-subtle);
}}
.setting-foot .note {{ margin: 0; }}
.initial-password {{ margin: 0; color: var(--text-muted); font-size: 12px; line-height: 1.5; }}
.login-password-switch {{ border-top: 1px solid var(--border); }}
.login-password-grid {{
  display: grid;
  grid-template-columns: 7.5em min(24em, 100%);
  column-gap: 16px;
  row-gap: 12px;
  align-items: start;
  justify-content: start;
}}
.login-password-grid > .setting-name {{
  margin: 0;
  min-height: 36px;
  display: flex;
  align-items: center;
}}
.password-stack {{ display: flex; flex-direction: column; gap: 4px; min-width: 0; }}
.password-field {{ position: relative; display: block; width: 100%; min-width: 0; }}
.login-password-grid .password-field input {{
  display: block;
  width: 100%;
  box-sizing: border-box;
  padding-right: 36px;
}}
.password-field .peek {{ position: absolute; top: 50%; right: 4px; transform: translateY(-50%); }}
.login-password-grid #password-gap {{ grid-column: 2; }}
.login-password-save {{ grid-column: 2; display: flex; justify-content: flex-end; }}
@media (max-width: 760px) {{
  .login-password-grid {{ grid-template-columns: minmax(0, 1fr); }}
  .login-password-grid #password-gap,
  .login-password-save {{ grid-column: 1; }}
}}
.peek {{
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}}
.peek svg {{ display: block; }}
.peek .eye-off {{ display: none; }}
.peek[aria-pressed="true"] .eye-on {{ display: none; }}
.peek[aria-pressed="true"] .eye-off {{ display: block; }}
table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: var(--surface-base); border: 1px solid var(--border); border-radius: 12px; }}
.table-scroll {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-base); }}
.table-scroll table {{ border: 0; border-radius: 0; }}
.fresh[hidden] {{ display: none; }}
table.sources {{ width: max-content; min-width: 100%; }}
table.sources th.fit, table.sources td.fit {{ white-space: nowrap; }}
table.sources th.name, table.sources td.name {{ min-width: 10em; max-width: 18em; }}
table.sources th.note, table.sources td.note {{ min-width: 14em; max-width: 28em; }}
table.sources th.num, table.sources td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
table.sources span.clip {{ cursor: help; text-decoration: underline dotted; text-underline-offset: 3px; }}
table.sources a.clip {{ text-decoration: underline dotted; text-underline-offset: 3px; }}
.source-cost {{ display: flex; flex-wrap: wrap; gap: 18px 28px; margin: 0 0 16px; padding: 12px 16px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-base); }}
.source-cost .k {{ display: block; margin: 0 0 2px; font-size: 12px; color: var(--text-secondary); }}
.source-cost strong {{ font-size: 22px; line-height: 1.3; font-weight: 600; letter-spacing: -0.3px; font-variant-numeric: tabular-nums; }}
.clip-pop {{ position: fixed; z-index: 41; left: 0; top: 0; box-sizing: border-box; max-width: min(360px, calc(100vw - 16px)); max-height: min(40vh, 280px); overflow: auto; padding: 8px 10px; font-size: 13px; line-height: 1.5; font-weight: 400; text-align: left; color: var(--text-primary); background: var(--surface-popover); border: 1px solid var(--border); border-radius: 8px; box-shadow: var(--shadow-menu); white-space: pre-wrap; overflow-wrap: anywhere; }}
.clip-pop[hidden] {{ display: none; }}
table.sources td.ops a, table.sources td.ops button.link {{ color: var(--link-color); background: none; border: 0; min-height: 0; padding: 0; font: inherit; font-weight: 500; text-decoration: underline; text-underline-offset: 4px; cursor: pointer; box-shadow: none; }}
table.sources td.ops button.link {{ margin-left: 12px; }}
.upload-meter {{ position: relative; display: inline-flex; align-items: center; justify-content: flex-end; box-sizing: border-box; width: 8.75em; height: 28px; padding: 0 8px; border-radius: 999px; background: var(--surface-subtle); border: 1px solid var(--border); vertical-align: middle; overflow: hidden; }}
.upload-meter .track {{ position: absolute; inset: 0; border-radius: inherit; overflow: hidden; pointer-events: none; }}
.upload-meter .fill {{ display: block; height: 100%; width: 0%; background: color-mix(in srgb, var(--brand) 62%, transparent); transition: width .15s linear; }}
.upload-meter.wait .fill {{ width: 42%; transition: none; animation: upload-slide 1.1s ease-in-out infinite alternate; }}
.upload-meter.hold {{ width: auto; min-width: 8.75em; }}
.upload-meter.hold .fill {{ width: 100%; animation: upload-hold .9s ease-in-out infinite; }}
.upload-meter .pct {{ position: relative; z-index: 1; flex: none; width: 2.6em; font-variant-numeric: tabular-nums; font-size: 12px; line-height: 1; color: var(--text-primary); text-align: right; }}
.upload-meter.hold .pct {{ width: auto; padding-left: 8px; }}
.upload-meter.fail {{ width: auto; max-width: 16em; height: auto; min-height: 28px; padding: 4px 8px; justify-content: flex-start; color: var(--danger-text); background: var(--danger-bg); border-color: transparent; white-space: normal; }}
@keyframes upload-slide {{ 0% {{ transform: translateX(0); }} 100% {{ transform: translateX(140%); }} }}
@keyframes upload-hold {{ 0%, 100% {{ opacity: .55; }} 50% {{ opacity: 1; }} }}
@media (prefers-reduced-motion: reduce) {{
  .upload-meter .fill, .upload-meter.wait .fill, .upload-meter.hold .fill {{ animation: none; transition: none; }}
  .upload-meter.wait .fill {{ width: 42%; transform: none; opacity: 1; }}
}}
main .content.drop-ready {{
  outline: 2px dashed color-mix(in srgb, var(--brand) 72%, transparent);
  outline-offset: -6px;
  background: color-mix(in srgb, var(--brand) 7%, transparent);
}}
.import-menu {{ position: relative; margin-left: auto; flex: none; }}
.import-menu [role="menu"] {{ position: absolute; z-index: 40; right: 0; top: 100%; padding-top: 4px; background: transparent; border: 0; max-width: calc(100vw - 16px); }}
.import-menu [role="menu"][hidden] {{ display: none; }}
.import-menu .import-panel {{ display: flex; flex-direction: row; gap: 4px; padding: 4px; background: var(--surface-popover); border: 1px solid var(--border); border-radius: 8px; box-shadow: var(--shadow-menu); }}
.import-menu [role="menuitem"] {{ background: transparent; color: var(--text-primary); border: 0; box-shadow: none; min-height: 44px; padding: 8px 12px; font-weight: 400; white-space: nowrap; justify-content: center; }}
.import-menu [role="menuitem"]:hover, .import-menu [role="menuitem"]:focus-visible {{ background: var(--surface-hover); color: var(--text-primary); }}
#nas-import {{ border: 1px solid var(--border); border-radius: 12px; padding: 16px; width: min(36rem, calc(100% - 32px)); max-width: calc(100% - 32px); max-height: min(80vh, 640px); background: var(--surface-base); color: var(--text-primary); box-shadow: var(--shadow-dialog); }}
#nas-import[open] {{ display: flex; flex-direction: column; }}
#nas-import::backdrop {{ background: var(--overlay); }}
#nas-import .nas-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
#nas-import .nas-head h2 {{ margin: 0; font-size: 16px; line-height: 1.5; font-weight: 600; }}
#nas-import .nas-crumb {{ display: flex; flex-wrap: wrap; align-items: center; gap: 2px; margin: 8px 0 0; min-width: 0; }}
#nas-import .nas-jump {{ background: transparent; color: var(--text-secondary); border: 0; box-shadow: none; min-height: 32px; padding: 4px 6px; font-weight: 400; }}
#nas-import .nas-jump:hover, #nas-import .nas-jump:focus-visible {{ background: var(--surface-hover); color: var(--text-primary); }}
#nas-import .nas-body {{ min-height: 0; }}
#nas-import.nas-browse {{ height: min(80vh, 640px); overflow: hidden; }}
#nas-import.nas-browse .nas-body {{ flex: 1 1 auto; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }}
#nas-import .nas-list {{ display: flex; flex-direction: column; gap: 2px; margin: 8px 0; overflow: auto; max-height: 24em; }}
#nas-import.nas-browse .nas-list {{ flex: 1 1 auto; min-height: 0; max-height: none; overflow: auto; }}
#nas-import .nas-row {{ display: flex; align-items: center; gap: 8px; width: 100%; min-height: 44px; margin: 0; padding: 8px 10px; background: transparent; color: var(--text-primary); border: 0; border-radius: 8px; box-shadow: none; font-weight: 400; text-align: left; justify-content: flex-start; }}
#nas-import .nas-row:hover, #nas-import .nas-row:focus-visible {{ background: var(--surface-hover); color: var(--text-primary); }}
#nas-import .muted {{ color: var(--text-secondary); }}
#nas-import .nas-fail {{ color: var(--danger-text); }}
#nas-import .nas-foot {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; margin-top: 8px; }}
#nas-import [hidden] {{ display: none !important; }}
#nas-import .nas-scope {{ margin: 0; }}
@media (max-width: 760px) {{
  #nas-import.nas-browse {{
    width: 100%; max-width: 100%; height: 100dvh; max-height: 100dvh; margin: 0;
    border: 0; border-radius: 0; overflow: hidden;
    padding: max(12px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) max(12px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
  }}
}}
table.models {{ width: 100%; }}
table.models td.url {{ word-break: break-all; }}
table.models th.key, table.models td.key {{ width: 9em; max-width: 9em; }}
table.models td.key input {{ width: 100%; min-width: 0; }}
table.models th.health, table.models td.health, table.models th.ops, table.models td.ops {{ white-space: nowrap; }}
th, td {{ text-align: left; padding: 12px; border-bottom: 1px solid var(--border); vertical-align: top; font-size: 14px; line-height: 1.5; transition: background-color .15s ease; }}
tr:last-child td {{ border-bottom: 0; }}
th {{ color: var(--text-muted); font-size: 13px; font-weight: 500; background: var(--surface-subtle); }}
tr:hover td {{ background: var(--surface-hover); }}
#host-fleet td.ops {{ white-space: nowrap; vertical-align: middle; }}
#host-fleet td.plugin {{ white-space: nowrap; }}
#host-fleet td.plugin code {{ font-size: 12px; opacity: .8; }}
#host-fleet td.plugin .badge {{ display: inline-block; padding: 0 6px; border-radius: 8px; font-size: 12px; }}
#host-fleet td.plugin-current .badge {{ opacity: .7; }}
#host-fleet td.plugin-outdated .badge, #host-fleet td.plugin-unreported .badge {{ background: var(--warning-bg); }}
#host-fleet td.plugin-incompatible .badge {{ background: var(--danger-bg); color: var(--danger-text); }}
#host-fleet .host-actions {{ display: inline-flex; align-items: center; gap: 8px; }}
#host-fleet .job-status {{ display: block; max-width: 16em; margin-top: 2px; font-size: 12px; line-height: 1.45; white-space: normal; }}
#host-fleet .job-status:empty {{ display: none; }}
#host-fleet .job-status.is-fail {{ color: var(--danger-text); }}
.tabs {{ display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }}
.tabs a {{ display: inline-flex; align-items: center; min-height: 32px; padding: 4px 10px; border-radius: 8px; color: var(--text-secondary); font-size: 14px; font-weight: 500; text-decoration: none; transition: background-color .15s ease, color .15s ease; }}
.tabs a:hover {{ background: var(--surface-hover); color: var(--text-primary); text-decoration: none; }}
.tabs a.on {{ background: var(--surface-selected); color: var(--text-primary); font-weight: 500; }}
.tabs.log-kinds {{ flex-wrap: wrap; row-gap: 6px; }}
table.logs th.when, table.logs td.when, table.logs th.kind, table.logs td.kind, table.logs th.step, table.logs td.step, table.logs th.out, table.logs td.out {{ white-space: nowrap; }}
table.logs td.rec {{ white-space: pre-wrap; min-width: 16em; }}
table.logs td.rec .body {{ max-height: 9em; overflow: auto; }}
table.logs .sub {{ display: block; color: var(--text-muted); margin-top: 4px; font-size: 13px; }}
.filters {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }}
.filters label:not(.btn), .worker-field, .edit-sheet label {{ font-size: 13px; line-height: 1.5; color: var(--text-secondary); }}
form.source-filters {{ --filter-box: 8.5em; --filter-search: 12em; }}
form.source-filters label:not(.btn) {{ display: inline-flex; align-items: center; gap: 6px; flex: none; white-space: nowrap; min-width: 0; }}
form.source-filters select {{
  field-sizing: fixed;
  width: var(--filter-box);
  min-width: var(--filter-box);
  max-width: var(--filter-box);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
form.source-filters input[name="q"] {{
  field-sizing: fixed;
  flex: none;
  width: var(--filter-search);
  min-width: var(--filter-search);
  max-width: var(--filter-search);
}}
form.source-filters .pick {{ position: relative; display: block; width: var(--filter-box); flex: none; }}
form.source-filters .pick-face {{
  position: relative;
  display: block;
  width: 100%;
  min-height: 36px;
  padding: 7px 28px 7px 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
  font-weight: 400;
  background: var(--surface-base);
  color: var(--text-primary);
  border: 1px solid var(--input);
  border-radius: 8px;
  box-shadow: none;
}}
form.source-filters .pick-face::after {{
  content: "";
  position: absolute;
  right: 12px;
  top: 50%;
  width: 0;
  height: 0;
  margin-top: -2px;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 5px solid var(--text-muted);
  pointer-events: none;
}}
form.source-filters .pick-face:hover,
form.source-filters .pick-face:focus-visible {{ background: var(--surface-base); border-color: var(--border-hover); color: var(--text-primary); }}
form.source-filters .pick-native {{
  position: absolute;
  width: 1px;
  height: 1px;
  min-width: 0;
  max-width: 1px;
  margin: 0;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}}
form.source-filters .pick-menu {{
  position: absolute;
  z-index: 40;
  top: calc(100% + 4px);
  left: 0;
  width: max-content;
  min-width: 100%;
  max-width: calc(100vw - 16px);
  max-height: 16rem;
  overflow: auto;
  margin: 0;
  padding: 4px;
  background: var(--surface-popover);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-menu);
}}
form.source-filters .pick-menu[hidden] {{ display: none; }}
form.source-filters .pick-item {{
  display: block;
  width: max-content;
  min-width: 100%;
  min-height: 32px;
  margin: 0;
  padding: 4px 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-primary);
  font-weight: 400;
  text-align: left;
  white-space: nowrap;
  box-shadow: none;
  transform: none;
}}
form.source-filters .pick-item:hover,
form.source-filters .pick-item:focus-visible {{ background: var(--surface-hover); color: var(--text-primary); }}
form.source-filters .pick-item[aria-selected="true"] {{ background: var(--surface-selected); }}
.workers {{ display: flex; flex-direction: column; }}
.worker {{ display: grid; grid-template-columns: 7.5em minmax(0, 1fr); align-items: center; gap: 12px 20px; padding: 16px 24px; }}
.worker + .worker {{ border-top: 1px solid var(--border); }}
.worker-name {{ display: flex; flex-direction: column; gap: 2px; font-size: 14px; font-weight: 600; color: var(--text-primary); }}
.worker-env {{ font-size: 12px; font-weight: 400; color: var(--text-muted); }}
.worker-controls {{ display: flex; align-items: center; gap: 16px; min-width: 0; }}
.worker-field {{ display: flex; align-items: center; gap: 8px; min-width: 0; white-space: nowrap; }}
.worker-field.model {{ flex: 1 1 auto; }}
.worker-field select {{ min-width: 0; }}
.worker-field.model select {{ flex: 1 1 auto; width: 1px; min-width: 0; }}
.worker-field.think {{ flex: 0 0 auto; }}
.worker-field.think select {{ width: auto; }}
.setting-row .types {{ margin: 0; }}
.types {{ border: 0; margin: 8px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 8px 14px; }}
.types legend {{ width: 100%; padding: 0; margin: 0 0 6px; font-size: 13px; color: var(--text-secondary); }}
.types label {{ display: flex; flex-direction: row; align-items: center; gap: 6px; font-size: 14px; color: var(--text-primary); }}
.switch {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; max-width: 28em; margin-top: 12px; font-size: 14px; }}
.switch .knob {{ position: relative; width: 36px; height: 20px; flex: none; }}
.switch .knob input {{ position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; padding: 0; border: 0; opacity: 0; cursor: pointer; }}
.switch .knob i {{ position: absolute; inset: 0; border-radius: 99px; background: var(--border-strong); pointer-events: none; transition: background-color .15s ease; }}
.switch .knob i:before {{ content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: left .15s ease; }}
.switch .knob input:checked + i {{ background: var(--primary); }}
.switch .knob input:checked + i:before {{ left: 18px; }}
html[data-theme="dark"] .switch .knob input:checked + i:before {{ background: var(--primary-foreground); }}
input.locked {{ background: var(--surface-subtle); color: var(--text-secondary); }}
@media (max-width: 760px) {{
  .worker {{ grid-template-columns: 1fr; gap: 8px; }}
  .worker-controls {{ flex-wrap: nowrap; }}
}}
.filters .import-file, .filters .row-end {{ margin-left: auto; flex: none; }}
.filters > button, .filters label.btn {{ height: 36px; min-height: 36px; padding: 0 14px; box-sizing: border-box; }}
.import-file input {{ display: none; }}
.modal {{ position: fixed; inset: 0; background: var(--overlay); display: grid; place-items: center; z-index: 80; }}
.modal[hidden] {{ display: none; }}
.modal-card {{ width: min(420px, calc(100% - 32px)); background: var(--surface-popover); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow-dialog); padding: 24px; }}
.modal-card h2, .modal-card > p:first-child {{ margin: 0 0 8px; font-size: 20px; line-height: 1.5; font-weight: 600; color: var(--text-primary); }}
.modal-card .note {{ font-size: 14px; line-height: 1.5; }}
.modal-card a {{ color: var(--link-color); margin-left: 8px; }}
input, select, textarea {{
  font: inherit;
  font-size: 14px;
  line-height: 1.5;
  color: var(--text-primary);
  background: var(--surface-base);
  border: 1px solid var(--input);
  border-radius: 8px;
  min-height: 36px;
  padding: 7px 12px;
  transition: border-color .15s ease, background-color .15s ease;
}}
textarea {{ width: 100%; min-height: 72px; padding: 10px 12px; }}
input:hover, select:hover, textarea:hover {{ border-color: var(--border-hover); }}
input:focus, select:focus, textarea:focus {{ border-color: var(--focus-color); outline: 2px solid var(--focus-color); outline-offset: 1px; }}
::placeholder {{ color: var(--text-muted); }}
button, .btn {{
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  line-height: 1.5;
  background: var(--primary);
  color: var(--primary-foreground);
  border: 1px solid transparent;
  border-radius: 8px;
  min-height: 36px;
  padding: 7px 14px;
  cursor: pointer;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  transition: background-color .15s ease, border-color .15s ease, color .15s ease, transform .15s ease;
}}
button:hover, .btn:hover {{ background: var(--primary-hover); text-decoration: none; }}
button:active:not(:disabled):not(.lamp):not(.nav-fold):not(.toc-tab):not(.rev-tick):not(.pick-face):not(.pick-item):not(.nas-row):not(.nas-jump):not([role="menuitem"]),
.btn:active {{ transform: scale(0.97); }}
button:disabled, .btn:disabled {{ opacity: .45; cursor: not-allowed; }}
button.quiet, .btn.quiet {{ background: var(--surface-base); color: var(--text-primary); border: 1px solid var(--border); }}
button.quiet:hover, .btn.quiet:hover {{ background: var(--surface-hover); border-color: var(--border-hover); }}
button.danger {{ background: var(--destructive); color: #fff; border-color: transparent; }}
button.danger:hover {{ background: var(--destructive-hover); }}
button.link {{ background: none; border: 0; min-height: 0; padding: 0; color: var(--link-color); box-shadow: none; text-decoration: underline; text-underline-offset: 4px; }}
button.link:hover {{ background: none; color: var(--link-color); }}
button.link:active {{ transform: none; }}
button.peek {{
  flex: none;
  width: 28px;
  height: 28px;
  min-height: 28px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
}}
button.peek:hover {{ background: var(--surface-hover); color: var(--text-primary); }}
button.peek:active {{ transform: none; }}
.cols {{ display: grid; grid-template-columns: 200px minmax(0, 1fr) 180px; gap: 16px; }}
.muted {{ color: var(--text-muted); }}
.empty {{ padding: 40px 24px; color: var(--status-ink, var(--text-muted)); text-align: center; font-size: 14px; }}
.note {{ color: var(--status-ink, var(--text-muted)); margin: 8px 0 0; font-size: 13px; line-height: 1.5; }}
.pager {{ display: flex; gap: 14px; align-items: center; margin: 14px 0 0; color: var(--text-muted); font-size: 13px; line-height: 1.5; }}
.pager a {{ color: var(--link-color); text-decoration: none; }}
.pager a:hover {{ text-decoration: underline; }}
.pager .off {{ color: var(--text-muted); }}
.tip {{ cursor: help; border-bottom: 1px dotted var(--text-muted); }}
.page-head {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 0 0 16px; }}
.page-head h1 {{ margin: 0; }}
.graph-count {{ margin: 0; font-size: 12px; line-height: 1.5; color: var(--text-muted); font-weight: 400; font-variant-numeric: tabular-nums; white-space: nowrap; }}
#graph > .graph-count {{ position: absolute; top: 14px; right: 16px; z-index: 20; color: #c8c8c8; text-shadow: 0 0 4px #111, 0 1px 2px #111; pointer-events: none; }}
#graph > canvas.graph-3d {{ display: block; position: relative; z-index: 1; }}
.local-mark {{ color: var(--brand); font-weight: 600; }}
.modal-card.wide {{ width: min(720px, calc(100% - 32px)); }}
.lamp {{ display: inline-flex; align-items: center; gap: 6px; }}
.lamp i {{ width: 10px; height: 10px; border-radius: 50%; background: var(--text-muted); display: inline-block; }}
.lamp.ok i {{ background: var(--status-icon-success); }}
.lamp.warn i {{ background: var(--status-icon-warning); }}
.lamp.down i {{ background: var(--status-icon-danger); }}
.names a, .toc a {{ display: block; padding: 8px 10px; border-radius: 8px; color: var(--text-secondary); text-decoration: none; font-size: 14px; font-weight: 500; line-height: 1.5; transition: background-color .15s ease, color .15s ease; }}
.names a:hover, .toc a:hover {{ background: var(--surface-hover); color: var(--text-primary); text-decoration: none; }}
.names a.on {{ background: var(--surface-selected); color: var(--text-primary); font-weight: 500; }}
.crystal-nav select {{ display: block; width: 100%; margin: 0 0 8px; }}
#graph {{ height: 640px; background: #111111; overflow: hidden; position: relative; }}
#graph .note {{ color: #c8c8c8; padding: 12px 14px; }}
#graph-tip {{ position: fixed; z-index: 30; max-width: 280px; padding: 0 0 0 2px; font-size: 13px; line-height: 1.35; color: #f4f4f4; background: transparent; border: 0; pointer-events: none; text-shadow: 0 0 4px #111, 0 1px 2px #111; }}
#graph-tip[hidden] {{ display: none; }}
.graph-loader {{ position: absolute; inset: 0; z-index: 10; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px; background: #111111; color: #c8c8c8; font-size: 13px; line-height: 1.5; transition: opacity 240ms ease; }}
.graph-loader.is-gone {{ opacity: 0; pointer-events: none; }}
.graph-loader-dots {{ display: inline-flex; gap: 6px; }}
.graph-loader-dots i {{ width: 7px; height: 7px; border-radius: 50%; background: #c8c8c8; opacity: .35; transform: scale(.6); animation: graph-loader-pulse 1400ms ease-in-out infinite; }}
.graph-loader-dots i:nth-child(1) {{ animation-delay: 0ms; }}
.graph-loader-dots i:nth-child(2) {{ animation-delay: -175ms; }}
.graph-loader-dots i:nth-child(3) {{ animation-delay: -350ms; }}
.graph-loader-dots i:nth-child(4) {{ animation-delay: -525ms; }}
.graph-loader-dots i:nth-child(5) {{ animation-delay: -700ms; }}
.graph-loader-dots i:nth-child(6) {{ animation-delay: -875ms; }}
.graph-loader-dots i:nth-child(7) {{ animation-delay: -1050ms; }}
.graph-loader p {{ margin: 0; color: #c8c8c8; }}
.graph-error {{ padding: 16px 20px; text-align: center; color: #c8c8c8; }}
.graph-error .quiet {{ margin-top: 10px; }}
@keyframes graph-loader-pulse {{
  0%, 100% {{ transform: scale(.6); opacity: .35; }}
  50% {{ transform: scale(1); opacity: 1; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .graph-loader-dots i {{ animation: none; opacity: .7; transform: scale(.8); }}
  .graph-loader {{ transition: none; }}
}}
body.graph-screen {{ height: 100vh; overflow: hidden; display: flex; flex-direction: column; }}
body.graph-screen header, body.graph-screen .channel-bar {{ flex: none; }}
body.graph-screen main {{ flex: 1; min-height: 0; padding: 0; overflow: hidden; align-items: stretch; gap: 0; }}
body.graph-screen .content {{ display: flex; flex-direction: column; height: 100%; min-height: 0; }}
body.graph-screen #graph {{ flex: 1; width: 100%; height: auto; min-height: 0; }}
.channel-bar {{ padding: 8px 16px; background: var(--notice-bg); color: var(--notice-text); border-bottom: 1px solid var(--notice-border); font-size: 13px; line-height: 1.5; }}
form.inline {{ display: inline; }}
tr.draft input {{ width: 100%; }}
.about {{ padding: 0; border-top: 3px solid var(--brand); overflow: hidden; }}
.about-hero {{ padding: 28px 28px 22px; }}
.about-kicker {{ margin: 0 0 10px; font-size: 12px; line-height: 1.5; letter-spacing: 0; color: var(--brand); font-weight: 600; }}
.about h2 {{ margin: 0 0 14px; font-size: 28px; letter-spacing: -0.4px; font-weight: 600; line-height: 1.4; }}
.about-lead {{ margin: 0; font-size: 16px; line-height: 1.65; color: var(--text-secondary); }}
.about-versions {{ display: grid; grid-template-columns: 1fr 1fr; border-top: 1px solid var(--border); }}
.about-versions > div {{ padding: 18px 28px 16px; }}
.about-versions > div:nth-child(even) {{ border-left: 1px solid var(--border); }}
.about-versions > div:nth-child(n+3) {{ border-top: 1px solid var(--border); }}
.about-versions .about-hint {{ margin-top: 8px; }}
.about-versions .about-hint code {{ font-size: 12px; }}
.about-note {{ line-height: 1.7; }}
.about-label {{ margin: 0; font-size: 12px; letter-spacing: 0; color: var(--text-muted); font-weight: 500; }}
.about-value {{ margin: 8px 0 0; font-size: 18px; line-height: 1.5; font-weight: 600; font-variant-numeric: tabular-nums; }}
.about-value code, .about-line code {{ font-size: 18px; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; background: none; padding: 0; }}
.about-line {{ margin: 8px 0 0; font-size: 18px; line-height: 1.5; font-variant-numeric: tabular-nums; display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }}
.about-line-row {{ display: flex; align-items: center; flex-wrap: wrap; gap: 8px; max-width: 100%; }}
.about-primary code {{ white-space: nowrap; }}
.about-primary.about-stale {{ color: var(--text-secondary); font-weight: 500; }}
.about-line button {{ min-height: 28px; padding: 2px 10px; font-size: 12px; line-height: 1.5; }}
.about-line .about-state {{ margin: 0; }}
.about-hint {{ display: block; margin: 0; color: var(--text-muted); font-size: 13px; line-height: 1.5; font-weight: 500; }}
.about-hint time {{ font-variant-numeric: tabular-nums; }}
.about-hint.about-fail {{ color: var(--warning-text); font-weight: 600; }}
.about-hint.about-muted {{ color: var(--text-muted); font-weight: 500; }}
.about-state {{ margin: 6px 0 0; color: var(--text-muted); font-weight: 600; font-size: 13px; }}
.about-state.due {{ color: var(--brand); }}
.about-fail {{ color: var(--warning-text); font-weight: 600; }}
.about-hint {{ margin: 4px 0 0; color: var(--text-muted); font-size: 13px; }}
.about-links {{ list-style: none; margin: 0; padding: 0 28px; border-top: 1px solid var(--border); }}
.about-links li {{ display: grid; grid-template-columns: 6em minmax(0, 1fr); gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border); align-items: baseline; }}
.about-links li:last-child {{ border-bottom: 0; }}
.about-links span {{ color: var(--text-muted); font-size: 12px; letter-spacing: 0; }}
.about-links a {{ color: var(--link-color); word-break: break-all; }}
.about-credits {{ margin: 22px 28px 26px; padding: 18px 20px 8px; background: var(--surface-subtle); border: 1px solid var(--border); border-radius: 12px; }}
.about-credits .about-label {{ margin: 0 0 8px; }}
.about-credits ul {{ list-style: none; margin: 0; padding: 0; }}
.about-credits li {{ display: grid; grid-template-columns: 12em minmax(0, 1fr); gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border); align-items: baseline; }}
.about-credits li:last-child {{ border-bottom: 0; }}
.about-credits li > span:first-child {{ color: var(--text-muted); font-size: 12px; }}
.about-credits .about-credit-meta {{ min-width: 0; color: var(--text-secondary); overflow-wrap: anywhere; }}
.about-credits a {{ color: var(--link-color); }}
code, kbd, pre, samp {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
.src-body {{ max-width: 100%; min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; }}
.file-preview {{ margin: 0 0 24px; }}
.file-preview-bar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-bottom: 12px; font-size: 13px; color: var(--text-muted); }}
.file-preview-name {{ color: var(--text-primary); font-weight: 600; overflow-wrap: anywhere; }}
.file-preview-kind {{ padding: 1px 8px; border: 1px solid var(--border); border-radius: 10px; font-size: 12px; }}
/* 预览的是文件原件：渲染器一律按浅色纸张写死字色，舞台也固定白底深字，
   不随管理台深浅主题翻转，否则深色主题下 Word / txt / markdown 会变成深底深字。 */
.file-preview-stage {{ box-sizing: border-box; width: 100%; min-height: 320px; max-height: 72vh; overflow: auto; padding: 16px; border: 1px solid var(--border); border-radius: 8px; background: #fff; color: #202124; }}
.file-preview-stage table {{ border-collapse: collapse; background: #fff; }}
.file-preview-stage .odv-docx-content-wrapper {{ background: #fff; }}
.file-preview-stage .odv-docx-content {{ background: #fff; color: #000; }}
.file-preview-stage img {{ max-width: 100%; }}
.file-preview-error {{ color: var(--danger-text); background: var(--danger-bg); padding: 10px 12px; border-radius: 8px; }}
.wiki-article code {{ font-size: .92em; background: var(--surface-subtle); border-radius: 4px; padding: 1px 5px; }}
.board {{ display: flex; flex-direction: column; gap: 16px; }}
.board .panel {{ padding: 0; overflow: hidden; }}
.board .setting-head {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
.board-tools {{ display: flex; align-items: center; gap: 12px; margin-left: auto; }}
.fx {{ display: inline-flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 13px; white-space: nowrap; }}
.fx input {{ width: 5.2em; min-height: 32px; padding: 4px 8px; }}
.ov-filters {{ display: flex; align-items: center; gap: 12px; padding: 14px 24px; border-bottom: 1px solid var(--border); }}
.ov-left {{ display: flex; align-items: center; gap: 12px; min-width: 0; flex-wrap: wrap; }}
.ov-filters .tabs, .detail .tabs {{ margin: 0; }}
.ov-filters .unit {{ margin-left: auto; flex: none; }}
.ov-filters label, .detail .filters label {{ display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }}
.ov-filters select {{ min-height: 32px; padding: 4px 10px; }}
.ov-figures {{ display: grid; grid-template-columns: 1fr 1fr; }}
.ov-figures.three {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
.ov-figures > div {{ padding: 18px 24px 16px; min-width: 0; }}
.ov-figures > div + div {{ border-left: 1px solid var(--border); }}
.ov-figures .k {{ margin: 0 0 4px; color: var(--text-muted); font-size: 13px; font-weight: 500; }}
.ov-figures .money {{ margin: 0; font-size: 22px; line-height: 1.4; font-weight: 600; letter-spacing: -0.3px; font-variant-numeric: tabular-nums; }}
.ov-figures .is-secondary .money {{ color: var(--status-ink, var(--text-secondary)); font-weight: 500; }}
.ov-split {{ margin: 6px 0 0; color: var(--text-muted); font-size: 13px; font-variant-numeric: tabular-nums; }}
.ov-activity {{ margin: 0; padding: 10px 24px 14px; color: var(--text-muted); font-size: 13px; }}
.ov-chart {{ position: relative; margin: 0; padding: 12px 24px 16px; border-top: 1px solid var(--border); }}
.ov-chart svg {{ display: block; width: 100%; height: 244px; touch-action: manipulation; outline: none; }}
.ov-chart svg:focus-visible {{ box-shadow: 0 0 0 2px var(--focus-color); border-radius: 4px; }}
.ov-chart svg rect.hit {{ fill: transparent; cursor: pointer; }}
.ov-chart svg rect.bar {{ fill: var(--chart-bar); }}
.ov-chart svg rect.bar.on {{
  fill: var(--chart-bar-hover);
  stroke: var(--focus-color);
  stroke-width: 2;
  clip-path: inset(0);
}}
.ov-chart svg rect.avail.unknown {{
  fill: var(--surface-subtle);
  stroke: var(--border-strong);
  stroke-width: 1;
}}
.ov-chart svg rect.avail.on {{ stroke: var(--focus-color); stroke-width: 1.5px; }}
.ov-chart svg line.grid {{ stroke: var(--chart-grid); stroke-dasharray: 2 4; fill: none; }}
.ov-chart svg line.axis {{ stroke: var(--chart-grid); fill: none; }}
.ov-chart svg line.tick {{ stroke: var(--chart-axis); fill: none; }}
.ov-chart svg text.axis {{ fill: var(--chart-axis); font-size: 0.75rem; font-variant-numeric: tabular-nums; }}
.ov-chart .ov-caption {{ margin: 8px 0 0; color: var(--status-ink, var(--chart-axis)); font-size: 12px; }}
.ov-avail-legend {{ margin: 2px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.5; }}
.gathering {{
  --status-ink: transparent;
  background-image: linear-gradient(105deg, var(--gather-ink) 0%, var(--gather-ink) 28%, var(--gather-glint) 50%, var(--gather-ink) 72%, var(--gather-ink) 100%);
  background-size: 220% 100%;
  background-repeat: no-repeat;
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
  animation: gathering-sheen 1.8s linear infinite;
}}
@keyframes gathering-sheen {{
  from {{ background-position: 100% 0; }}
  to {{ background-position: 0% 0; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .gathering {{
    animation: none;
    --status-ink: unset;
    -webkit-text-fill-color: unset;
    color: unset;
    background: none;
  }}
}}
.ov-chart .ov-degrade {{ margin: 8px 0 0; color: var(--text-muted); font-size: 12px; text-align: center; }}
.ov-tip {{
  position: fixed; z-index: 40; pointer-events: none; min-width: 168px; max-width: 280px;
  padding: 8px 10px; background: var(--surface-base); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; font-size: 12px; line-height: 1.5;
}}
.ov-tip[hidden] {{ display: none; }}
.ov-tip b {{ display: block; font-size: 13px; font-weight: 600; }}
.ov-tip .scope {{ display: block; margin-top: 2px; color: var(--text-muted); }}
.ov-tip p {{ margin: 2px 0 0; font-variant-numeric: tabular-nums; }}
.detail {{ padding: 16px 24px 20px; }}
.detail .filters {{ margin-top: 0; }}
table.calls td.clip {{ max-width: 16em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
table.calls td.num, table.calls th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.pager {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 12px; }}
.pager label {{ display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 13px; white-space: nowrap; }}
.pager input {{ width: 3.4em; min-height: 32px; padding: 4px 8px; text-align: center; }}
.list-foot {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 8px; }}
.list-foot > .note {{ margin: 0; }}
.list-foot > .pager {{ margin: 0; flex: none; }}
#rogmem-progress {{
  position: fixed; top: 0; left: 0; right: 0; height: 2px; z-index: 90;
  pointer-events: none; opacity: 0; overflow: hidden; background: transparent;
  transition: opacity .18s ease;
}}
html.rogmem-loading #rogmem-progress {{ opacity: 1; }}
#rogmem-progress .bar {{
  display: block; height: 100%; width: 30%;
  background: var(--brand);
  transform: translateX(-130%);
}}
html.rogmem-loading #rogmem-progress .bar {{
  animation: rogmem-progress-run 1.05s cubic-bezier(.4, 0, .2, 1) infinite;
}}
@keyframes rogmem-progress-run {{
  0% {{ transform: translateX(-130%); }}
  100% {{ transform: translateX(430%); }}
}}
.content.rogmem-enter {{ animation: rogmem-fade-in .2s ease; }}
@keyframes rogmem-fade-in {{
  from {{ opacity: .55; }}
  to {{ opacity: 1; }}
}}
.list-busy {{ position: relative; }}
.list-busy::before {{
  content: "";
  position: absolute; top: 0; left: 0; right: 0; height: 2px; z-index: 3;
  background: var(--brand); transform-origin: left center;
  animation: rogmem-list-pulse 1s ease-in-out infinite; pointer-events: none;
}}
.list-busy::after {{
  content: "";
  position: absolute; inset: 0; z-index: 2; pointer-events: none;
  background: color-mix(in srgb, var(--surface-page) 52%, transparent);
}}
@keyframes rogmem-list-pulse {{
  0%, 100% {{ opacity: .35; transform: scaleX(.32); }}
  50% {{ opacity: 1; transform: scaleX(1); }}
}}
@media (prefers-reduced-motion: reduce) {{
  html.rogmem-loading #rogmem-progress .bar {{
    animation: none; width: 100%; transform: none; opacity: .85;
  }}
  .list-busy::before {{ animation: none; opacity: .7; transform: none; }}
  .content.rogmem-enter {{ animation: none; }}
}}
.price-dialog {{
  width: min(880px, calc(100% - 48px)); max-height: min(760px, calc(100vh - 48px));
  display: flex; flex-direction: column; background: var(--surface-base);
  border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
}}
.price-dialog .setting-head, .price-dialog .setting-foot {{ flex: none; }}
.price-dialog .setting-head {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
.price-head {{ display: flex; align-items: baseline; gap: 10px; }}
.price-head h2 {{ margin: 0; }}
.price-unit {{ color: var(--text-muted); font-size: 13px; font-weight: 400; }}
.price-body {{
  flex: 1; min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 12px;
  padding: 16px 20px 20px; background: var(--surface-page);
}}
.price-group {{ background: var(--surface-base); border: 1px solid var(--border); border-radius: 10px; }}
.price-group h3 {{
  margin: 0; padding: 12px 16px; font-size: 14px; font-weight: 600; line-height: 1.5;
  border-bottom: 1px solid var(--border);
}}
.rate-sheet {{ padding: 0 16px 8px; }}
.rate-labels, .rate-row {{
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) 6.6em minmax(76px, 1fr) minmax(76px, 1fr) minmax(76px, 1fr);
  column-gap: 12px; align-items: center;
}}
.rate-labels {{ padding: 10px 0 6px; color: var(--text-muted); font-size: 13px; font-weight: 500; }}
.ov-filters .tabs button {{
  font: inherit; font-size: 14px; font-weight: 500; line-height: 1.5;
  background: none; color: var(--text-secondary); border: 0; box-shadow: none;
  min-height: 32px; padding: 4px 10px; border-radius: 8px;
}}
.ov-filters .tabs button:hover {{ background: var(--surface-hover); color: var(--text-primary); }}
.ov-filters .tabs button.on {{ background: var(--surface-selected); color: var(--text-primary); }}
.ov-filters .tabs button:active {{ transform: none; }}
.price-dialog .setting-head button[aria-label="关闭"] {{ width: 28px; min-height: 28px; padding: 0; }}
.rate-row {{ min-height: 44px; }}
.rate-row .name {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.rate-row input, .rate-row select {{ width: 100%; min-width: 0; }}
@media (max-width: 760px) {{
  header nav {{ overflow-x: auto; }}
  main {{ flex-direction: column; padding: 16px 12px 36px; gap: 12px; }}
  .content {{ width: 100%; max-width: 100%; min-width: 0; }}
  body.file-screen .content {{ align-self: stretch; }}
  .side {{ width: 100%; box-sizing: border-box; }}
  .side a {{ padding: 10px 12px; }}
  .about-versions {{ grid-template-columns: 1fr; }}
  .about-versions > div + div, .about-versions > div:nth-child(even) {{ border-left: 0; border-top: 1px solid var(--border); }}
  .about-hero {{ padding: 20px 16px 16px; }}
  .about-versions > div {{ padding: 14px 16px; }}
  .about-links {{ padding: 0 16px; }}
  .about-links li {{ grid-template-columns: 1fr; gap: 2px; padding: 10px 0; }}
  .about-credits {{ margin: 16px 16px 20px; padding: 14px 16px 6px; }}
  .about-credits li {{ grid-template-columns: 1fr; gap: 4px; }}
  .board .panel {{ min-width: 0; }}
  .ov-filters {{ flex-wrap: wrap; row-gap: 8px; }}
  .ov-filters .tabs {{ min-width: 0; }}
  .ov-figures, .ov-figures.three {{ grid-template-columns: 1fr; }}
  .ov-figures > div + div {{ border-left: 0; border-top: 1px solid var(--border); }}
}}
/* 顶部导航响应式（规格：docs/superpowers/specs/2026-11-12-responsive-top-navigation-design.md）
   渐进增强：hide-desktop-nav / show-hamburger 一律挂在 html.nav-drawer-ready 下，
   JS 初始化失败或宿主不支持 <dialog> 时，退化为原生 <nav> 横排 + ≤760px 横滚现状。 */
@media (max-width: 860px) {{
  header {{ padding: 0 8px; gap: 6px; }}
  header .brand svg {{ height: 22px; }}
  header .ver {{ font-size: 11px; white-space: nowrap; margin-right: 4px; }}
  html.nav-drawer-ready header > nav {{ display: none; }}
  html.nav-drawer-ready header #nav-toggle {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 44px; height: 44px; min-height: 44px; padding: 0;
    color: var(--text-secondary); background: transparent;
    border: 1px solid transparent; border-radius: 8px; cursor: pointer;
  }}
  html.nav-drawer-ready header #nav-toggle:hover {{ background: var(--surface-hover); color: var(--text-primary); }}
  html.nav-drawer-ready header #nav-toggle svg {{ display: block; width: 22px; height: 22px; }}
}}
@media (max-width: 360px) {{
  header .brand svg {{ height: 20px; }}
}}
@media (min-width: 861px) {{
  header > nav {{ overflow: visible; }}
  #nav-toggle, #nav-drawer {{ display: none; }}
}}
html.nav-drawer-open, html.nav-drawer-open body {{ overflow: hidden; }}
#nav-drawer {{
  position: fixed;
  inset: 0 0 0 auto;
  margin: 0;
  width: min(84vw, 320px);
  max-width: 100vw;
  height: 100dvh;
  padding: max(12px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) max(12px, env(safe-area-inset-bottom)) 16px;
  border: 0;
  background: var(--surface-base);
  color: var(--text-primary);
  box-shadow: var(--shadow-dialog);
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  transform: translateX(100%);
  opacity: 0;
  transition: transform .22s ease,
              opacity .22s ease,
              overlay .22s ease allow-discrete,
              display .22s ease allow-discrete;
}}
#nav-drawer[open] {{ transform: translateX(0); opacity: 1; }}
@starting-style {{
  #nav-drawer[open] {{ transform: translateX(100%); opacity: 0; }}
}}
#nav-drawer::backdrop {{
  background: var(--overlay);
  opacity: 0;
  transition: opacity .18s ease,
              overlay .18s ease allow-discrete,
              display .18s ease allow-discrete;
}}
#nav-drawer[open]::backdrop {{ opacity: 1; }}
@starting-style {{
  #nav-drawer[open]::backdrop {{ opacity: 0; }}
}}
.nav-drawer-head {{ display: flex; align-items: center; justify-content: space-between; min-height: 44px; margin: 0 0 8px; color: var(--text-muted); font-size: 14px; }}
.nav-drawer-list {{ display: flex; flex-direction: column; gap: 2px; margin: 0; padding: 0; }}
.nav-drawer-list a {{
  display: flex; align-items: center;
  min-height: 44px; padding: 10px 12px;
  border-radius: 8px;
  font-size: 15px; font-weight: 500;
  color: var(--text-secondary); text-decoration: none;
}}
.nav-drawer-list a:hover {{ background: var(--surface-hover); color: var(--text-primary); text-decoration: none; }}
.nav-drawer-list a[aria-current="page"] {{ background: var(--surface-selected); color: var(--text-primary); font-weight: 500; }}
.nav-close {{
  display: inline-flex; align-items: center; justify-content: center;
  width: 44px; height: 44px; min-height: 44px; padding: 0;
  background: transparent; color: var(--text-secondary);
  border: 1px solid transparent; border-radius: 8px; cursor: pointer;
}}
.nav-close:hover {{ background: var(--surface-hover); color: var(--text-primary); }}
.nav-close svg {{ display: block; width: 18px; height: 18px; }}
@media (prefers-reduced-motion: reduce) {{
  #nav-drawer, #nav-drawer::backdrop {{ transition: none; }}
}}
</style></head><body class="{body_class}">
<div id="rogmem-progress" aria-hidden="true"><span class="bar"></span></div>
{channel_bar()}
<header><a class="brand" href="/admin/crystals" aria-label="ROGMEM">{BRAND_LOCKUP_SVG}</a><nav>{links}</nav><span class="ver">{esc(product_version())}</span>{_nav_model_health()}{_AUDIO_BUTTON}<button type="button" id="nav-toggle" class="nav-toggle" aria-label="打开导航" aria-expanded="false" aria-controls="nav-drawer" hidden><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/></svg></button></header>
<main class="{'reader' if reader else ''}">{side}<div class="content">{body}</div></main>
<dialog id="nav-drawer" class="nav-drawer" aria-label="主导航">
<div class="nav-drawer-head"><span>导航</span><button type="button" class="nav-close" aria-label="关闭导航"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button></div>
<nav class="nav-drawer-list" aria-label="主导航">{drawer_links}</nav>
</dialog>
<div id="confirm-delete" class="modal" hidden>
<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-delete-text">
<p id="confirm-delete-text">确定删除？</p>
<p>
<button type="button" id="confirm-delete-yes" class="danger">确认删除</button>
<button type="button" class="quiet" id="confirm-delete-no">取消</button>
</p>
</div></div>
<script>
(function () {{
  function paintTheme() {{
    var dark = document.documentElement.getAttribute("data-theme") === "dark";
    var mode = dark ? "dark" : "light";
    document.querySelectorAll("[data-theme-pick]").forEach(function (btn) {{
      btn.setAttribute("aria-pressed", btn.getAttribute("data-theme-pick") === mode ? "true" : "false");
    }});
  }}
  function applyTheme(mode) {{
    try {{
      if (mode === "light") {{
        document.documentElement.removeAttribute("data-theme");
        localStorage.setItem("rogmem-theme", "light");
      }} else {{
        document.documentElement.setAttribute("data-theme", "dark");
        localStorage.setItem("rogmem-theme", "dark");
      }}
    }} catch (err) {{}}
    paintTheme();
  }}
  paintTheme();
  document.querySelectorAll("[data-theme-pick]").forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      applyTheme(btn.getAttribute("data-theme-pick") === "light" ? "light" : "dark");
    }});
  }});

  document.querySelectorAll("header > nav .nav-item").forEach(function (item) {{
    var trigger = item.querySelector("a");
    var sub = item.querySelector(".nav-sub");
    var links = Array.prototype.slice.call(item.querySelectorAll(".nav-sub a"));
    if (!trigger || !sub || !links.length) return;
    function place() {{
      sub.style.left = "0px";
      sub.style.right = "auto";
      var rect = sub.getBoundingClientRect();
      if (rect.right > window.innerWidth - 8) {{
        sub.style.left = "auto";
        sub.style.right = "0px";
      }}
    }}
    function setOpen(open) {{
      item.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) place();
    }}
    item.addEventListener("pointerenter", function () {{ setOpen(true); }});
    item.addEventListener("pointerleave", function () {{
      if (!item.contains(document.activeElement)) setOpen(false);
    }});
    item.addEventListener("focusin", function () {{ setOpen(true); }});
    item.addEventListener("focusout", function (event) {{
      if (!item.contains(event.relatedTarget)) setOpen(false);
    }});
    function move(event, next) {{
      if (!next) return;
      event.preventDefault();
      setOpen(true);
      next.focus();
    }}
    trigger.addEventListener("keydown", function (event) {{
      if (event.key === "ArrowDown") move(event, links[0]);
      else if (event.key === "Escape") trigger.blur();
    }});
    links.forEach(function (link, index) {{
      link.addEventListener("keydown", function (event) {{
        if (event.key === "ArrowDown") move(event, links[Math.min(index + 1, links.length - 1)]);
        else if (event.key === "ArrowUp") move(event, index === 0 ? trigger : links[index - 1]);
        else if (event.key === "Home") move(event, links[0]);
        else if (event.key === "End") move(event, links[links.length - 1]);
        else if (event.key === "Escape") move(event, trigger);
      }});
    }});
  }});

  var nav = document.getElementById("nav-drawer");
  var toggle = document.getElementById("nav-toggle");
  if (!nav || !toggle || typeof nav.showModal !== "function") {{
    // 宿主不支持 <dialog>：保持原生 <nav> 横排与 ≤760px 横滚；汉堡保持 hidden。
    return;
  }}
  // 进入增强状态：让 CSS 隐藏桌面 <nav> 并显示汉堡。
  document.documentElement.classList.add("nav-drawer-ready");
  toggle.hidden = false;
  var closeBtn = nav.querySelector(".nav-close");
  function openDrawer() {{
    try {{ nav.showModal(); }} catch (err) {{ return; }}
    document.documentElement.classList.add("nav-drawer-open");
    toggle.setAttribute("aria-expanded", "true");
    toggle.setAttribute("aria-label", "关闭导航");
    var first = nav.querySelector(".nav-drawer-list a");
    if (first) first.focus();
  }}
  function closeDrawer() {{
    if (nav.open) nav.close();
  }}
  toggle.addEventListener("click", function () {{
    if (nav.open) closeDrawer(); else openDrawer();
  }});
  if (closeBtn) closeBtn.addEventListener("click", closeDrawer);
  nav.addEventListener("click", function (event) {{
    // 真正的 ::backdrop 点击：target 是 dialog 自身 且 click 坐标落在 dialog 盒子之外。
    // 仅凭 target===nav 会把抽屉自身 padding/空白区域的点击误判成遮罩。
    if (event.target !== nav) return;
    var r = nav.getBoundingClientRect();
    var inside = event.clientX >= r.left && event.clientX <= r.right
              && event.clientY >= r.top && event.clientY <= r.bottom;
    if (!inside) closeDrawer();
  }});
  nav.querySelectorAll(".nav-drawer-list a").forEach(function (link) {{
    link.addEventListener("click", function () {{ closeDrawer(); }});
  }});
  nav.addEventListener("close", function () {{
    document.documentElement.classList.remove("nav-drawer-open");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "打开导航");
    var togglevis = toggle.offsetParent !== null;
    if (togglevis) {{
      toggle.focus();
    }} else {{
      var active = document.activeElement;
      if (active && active !== document.body && typeof active.blur === "function") active.blur();
    }}
  }});
  if (typeof window.matchMedia === "function") {{
    var mql = window.matchMedia("(min-width: 861px)");
    var onChange = function (event) {{
      if (event.matches && nav.open) closeDrawer();
    }};
    if (typeof mql.addEventListener === "function") mql.addEventListener("change", onChange);
    else if (typeof mql.addListener === "function") mql.addListener(onChange);
  }}
}})();
window.RogmemLoad = (function () {{
  var enterArmed = false;
  function start() {{
    document.documentElement.classList.add("rogmem-loading");
  }}
  function done() {{
    document.documentElement.classList.remove("rogmem-loading");
  }}
  function list(root, on) {{
    if (!root) return;
    root.classList.toggle("list-busy", !!on);
    if (on) root.setAttribute("aria-busy", "true");
    else root.removeAttribute("aria-busy");
  }}
  function sameOriginAdmin(url) {{
    try {{
      var next = new URL(url, location.href);
      if (next.origin !== location.origin) return false;
      return next.pathname.indexOf("/admin") === 0;
    }} catch (err) {{
      return false;
    }}
  }}
  function shouldIgnoreClick(event, link) {{
    if (!link || event.defaultPrevented) return true;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return true;
    if (link.target && link.target !== "_self") return true;
    if (link.hasAttribute("download")) return true;
    if (link.closest(".list-foot[data-partial]")) return true;
    var href = link.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return true;
    return !sameOriginAdmin(link.href);
  }}
  document.addEventListener("click", function (event) {{
    var link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (shouldIgnoreClick(event, link)) return;
    start();
  }}, true);
  document.addEventListener("submit", function (event) {{
    var form = event.target;
    if (!form || event.defaultPrevented) return;
    if (form.target && form.target !== "_self") return;
    if (form.closest && form.closest(".list-foot[data-partial]")) return;
    var action = form.getAttribute("action") || location.pathname;
    if (!sameOriginAdmin(action)) return;
    start();
  }});
  window.addEventListener("pageshow", function () {{ done(); }});
  window.addEventListener("pagehide", function () {{ /* keep bar until next paint */ }});
  function armEnter() {{
    if (enterArmed) return;
    enterArmed = true;
    var content = document.querySelector("main .content");
    if (content) content.classList.add("rogmem-enter");
  }}
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", armEnter);
  else armEnter();
  return {{ start: start, done: done, list: list }};
}})();
async function post(url, body, thenUrl) {{
  if (window.RogmemLoad) RogmemLoad.start();
  const response = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body || {{}})}});
  if (response.status === 401) location = '/admin/login';
  if (!response.ok) {{
    const text = await response.text();
    let message = text;
    try {{
      const data = JSON.parse(text);
      if (data && data.error) message = data.error;
    }} catch (err) {{}}
    if (window.RogmemLoad) RogmemLoad.done();
    alert(message || '没有完成');
    window.__costLeave = false;
    if (typeof window.__costResume === "function") window.__costResume();
    return false;
  }}
  window.__costLeave = true;
  if (typeof thenUrl === 'string' && thenUrl) {{
    location = thenUrl;
  }} else {{
    location.reload();
  }}
  return true;
}}
function confirmDelete(action) {{
  const modal = document.getElementById("confirm-delete");
  document.getElementById("confirm-delete-text").textContent = action.message;
  const yes = document.getElementById("confirm-delete-yes");
  yes.textContent = action.confirm || "确认删除";
  yes.onclick = () => {{
    modal.hidden = true;
    post(action.url, action.body || {{}});
  }};
  document.getElementById("confirm-delete-no").onclick = () => {{ modal.hidden = true; }};
  modal.hidden = false;
}}
var lampPop = document.createElement("div");
lampPop.className = "lamp-pop";
lampPop.hidden = true;
document.body.appendChild(lampPop);
var lampPinned = null;
function lampOf(target) {{
  return target && target.closest ? target.closest(".flow .lamp") : null;
}}
function placeLamp(lamp) {{
  var box = lamp.getBoundingClientRect();
  lampPop.textContent = lamp.getAttribute("aria-label") || "";
  lampPop.hidden = false;
  lampPop.style.visibility = "hidden";
  lampPop.style.left = "0px";
  lampPop.style.top = "0px";
  var width = lampPop.offsetWidth;
  var height = lampPop.offsetHeight;
  lampPop.style.visibility = "visible";
  var margin = 8;
  var left = box.left + box.width / 2 - width / 2;
  if (left + width > window.innerWidth - margin) left = window.innerWidth - margin - width;
  if (left < margin) left = margin;
  var top = box.top - 6 - height;
  if (top < margin) top = box.bottom + 6;
  lampPop.style.left = left + "px";
  lampPop.style.top = top + "px";
}}
function hideLamp() {{
  lampPinned = null;
  lampPop.hidden = true;
}}
document.addEventListener("mouseover", (event) => {{
  var lamp = lampOf(event.target);
  if (!lamp || lampPinned) return;
  placeLamp(lamp);
}});
document.addEventListener("mouseout", (event) => {{
  if (lampPinned) return;
  var lamp = lampOf(event.target);
  if (!lamp || lampOf(event.relatedTarget) === lamp) return;
  lampPop.hidden = true;
}});
var clipPop = document.createElement("div");
clipPop.className = "clip-pop";
clipPop.hidden = true;
document.body.appendChild(clipPop);
var clipOpen = null;
function clipOf(target) {{
  return target && target.closest ? target.closest(".clip") : null;
}}
function placeClip(clip) {{
  var full = clip.querySelector(".clip-full");
  clipPop.textContent = full ? full.textContent : "";
  clipPop.hidden = false;
  clipPop.style.visibility = "hidden";
  clipPop.style.left = "0px";
  clipPop.style.top = "0px";
  var width = clipPop.offsetWidth;
  var height = clipPop.offsetHeight;
  clipPop.style.visibility = "visible";
  var box = clip.getBoundingClientRect();
  var margin = 8;
  var left = box.left;
  if (left + width > window.innerWidth - margin) left = window.innerWidth - margin - width;
  if (left < margin) left = margin;
  var top = box.bottom - 2;
  if (top + height > window.innerHeight - margin) top = box.top - height + 2;
  if (top < margin) top = margin;
  clipPop.style.left = left + "px";
  clipPop.style.top = top + "px";
}}
function hideClip() {{
  clipOpen = null;
  clipPop.hidden = true;
}}
function stillOnClip(target) {{
  if (!clipOpen || !target) return false;
  if (clipPop.contains(target)) return true;
  return clipOf(target) === clipOpen;
}}
document.addEventListener("mouseover", (event) => {{
  var clip = clipOf(event.target);
  if (clip) {{
    clipOpen = clip;
    placeClip(clip);
  }}
}});
document.addEventListener("mouseout", (event) => {{
  if (!clipOpen || stillOnClip(event.relatedTarget)) return;
  var fromClip = clipOf(event.target) === clipOpen;
  var fromPop = clipPop.contains(event.target);
  if (!fromClip && !fromPop) return;
  hideClip();
}});
document.addEventListener("focusin", (event) => {{
  var lamp = lampOf(event.target);
  if (lamp && !lampPinned) placeLamp(lamp);
  var clip = clipOf(event.target);
  if (clip) {{
    clipOpen = clip;
    placeClip(clip);
  }}
}});
document.addEventListener("focusout", (event) => {{
  if (!lampPinned) lampPop.hidden = true;
  if (clipOpen && !stillOnClip(event.relatedTarget)) hideClip();
}});
window.addEventListener("scroll", (event) => {{
  if (event.target !== clipPop) hideClip();
  if (lampPinned) placeLamp(lampPinned);
  else lampPop.hidden = true;
}}, true);
function pinSheetTop() {{
  var sheet = document.querySelector("main");
  if (!sheet) return;
  var top = Math.max(0, Math.round(sheet.getBoundingClientRect().top));
  document.documentElement.style.setProperty("--sheet-top", top + "px");
}}
pinSheetTop();
window.addEventListener("resize", () => {{
  pinSheetTop();
  if (clipOpen) placeClip(clipOpen);
}});
document.querySelectorAll(".toc-dock").forEach((dock) => {{
  var tab = dock.querySelector(".toc-tab");
  if (!tab) return;
  var pinned = false;
  var inside = false;
  function paint() {{
    var open = pinned || inside;
    dock.classList.toggle("open", open);
    tab.setAttribute("aria-expanded", open ? "true" : "false");
  }}
  dock.addEventListener("mouseenter", () => {{ inside = true; paint(); }});
  dock.addEventListener("mouseleave", () => {{ inside = false; if (!pinned) paint(); }});
  tab.addEventListener("click", (event) => {{
    event.preventDefault();
    pinned = !pinned;
    if (!pinned) inside = false;
    paint();
  }});
  dock.addEventListener("focusout", (event) => {{
    if (dock.contains(event.relatedTarget)) return;
    inside = false;
    if (!pinned) paint();
  }});
}});
document.addEventListener("click", (event) => {{
  var lamp = lampOf(event.target);
  if (lamp) {{
    event.preventDefault();
    if (lampPinned === lamp) hideLamp();
    else {{ lampPinned = lamp; placeLamp(lamp); }}
    return;
  }}
  if (lampPinned) hideLamp();
  const button = event.target.closest("[data-delete]");
  if (!button) return;
  confirmDelete(JSON.parse(button.getAttribute("data-delete")));
}});
function localClock(raw) {{
  if (!raw || !/(?:Z|[+-]\\d{{2}}:\\d{{2}})$/.test(raw)) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
    + " " + pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
}}
document.querySelectorAll("time[datetime]").forEach((node) => {{
  const shown = localClock(node.getAttribute("datetime"));
  if (shown) node.textContent = shown;
}});
document.querySelectorAll("[data-utc]").forEach((node) => {{
  const shown = localClock(node.getAttribute("data-utc"));
  if (shown) node.title = (node.title ? node.title + " " : "") + shown;
}});
(function shanghaiTimes() {{
  var fmt = null;
  try {{
    fmt = new Intl.DateTimeFormat("zh-CN", {{
      timeZone: "Asia/Shanghai",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }});
  }} catch (e) {{ return; }}
  // Idempotent: re-runnable by callers that swap DOM subtrees in place
  // (e.g. the refresh-release button replaces .about-line.innerHTML, so
  // the fresh <time data-utc> nodes need to be formatted again). Reading
  // from data-utc every time keeps repeated calls producing the same
  // text; nothing else is mutated.
  window.formatShanghaiTimes = function (root) {{
    var scope = root || document;
    scope.querySelectorAll("time[data-utc]").forEach(function (node) {{
      var raw = node.getAttribute("data-utc");
      if (!raw) return;
      var date = new Date(raw);
      if (isNaN(date.getTime())) return;
      var text = fmt.format(date).replace(/\\//g, "-");
      node.textContent = text + " UTC+8";
      node.setAttribute("title", "原始 UTC：" + raw);
    }});
  }};
  window.formatShanghaiTimes(document);
}})();
</script>
<script>
function formatSourceClocks(root) {{
  (root || document).querySelectorAll("time[datetime]").forEach(function (node) {{
    var raw = node.getAttribute("datetime");
    var date = new Date(raw);
    if (!isNaN(date.getTime())) node.textContent = date.toLocaleString();
  }});
}}
formatSourceClocks(document);
</script>
{audio_layer()}
</body></html>"""


def _options(values: list[str], current: str, labels: dict[str, str] | None = None) -> str:
    html_options = "<option value=''>全部</option>"
    for value in values:
        label = (labels or {}).get(value, value)
        selected = " selected" if value == current else ""
        html_options += f"<option value='{esc(value)}'{selected}>{esc(label)}</option>"
    return html_options


def _page_size(stated) -> int:
    try:
        size = int(stated)
    except (TypeError, ValueError):
        size = 0
    if size > 0:
        return size
    from rogmem.engine import PAGE_SIZE

    return PAGE_SIZE


def _page_count(total: int, page_size: int) -> int:
    if total <= 0:
        return 1
    return (int(total) + page_size - 1) // page_size


def _query_pairs(params: dict, page: int = 1) -> list[tuple[str, str]]:
    pairs = [
        (str(key), str(value))
        for key, value in params.items()
        if key != "page" and value not in ("", None)
    ]
    if page > 1:
        pairs.append(("page", str(page)))
    return pairs


def _href(base: str, params: dict, page: int = 1) -> str:
    pairs = _query_pairs(params, page)
    if not pairs:
        return base
    return base + "?" + urlencode(pairs)


def _hidden_inputs(params: dict) -> str:
    return "".join(
        f"<input type='hidden' name='{esc(key)}' value='{esc(value)}'>"
        for key, value in _query_pairs(params, 1)
    )


def _step_control(label: str, step: int, href: str) -> str:
    if href:
        return f"<a class='btn quiet' data-step='{step}' href='{esc(href)}'>{label}</a>"
    return f"<button type='button' class='quiet' data-step='{step}' disabled>{label}</button>"


def list_pager(
    action: str,
    params: dict,
    *,
    total: int,
    page: int,
    page_size: int | None = None,
    note: str | None = None,
    note_class: str = "",
    partial: str = "",
) -> str:
    """One window: count on the left, page controls on the right.

    Page count, the previous link and the next link all come from this window
    and the same query. Callers do not pass a second page count or a second URL.
    """

    total = int(total or 0)
    size = _page_size(page_size)
    pages = _page_count(total, size)
    current = int(page or 1)
    if current < 1:
        current = 1
    if current > pages:
        current = pages
    shown = esc(note) if note is not None else f"共 {total} 条"
    klass = "note" + (f" {note_class}" if note_class else "")
    prev = _step_control("上一页", -1, _href(action, params, current - 1) if current > 1 else "")
    nxt = _step_control("下一页", 1, _href(action, params, current + 1) if current < pages else "")
    marker = f" data-partial='{esc(partial)}'" if partial else ""
    return (
        f"<div class='list-foot'{marker} data-total='{total}' data-page='{current}' data-page-size='{size}'>"
        f"<p class='{klass}' data-total='{total}'>{shown}</p>"
        f"<form class='pager' method='get' action='{esc(action)}'>"
        f"{_hidden_inputs(params)}"
        f"{prev}"
        f"<label>第 <input name='page' value='{current}' inputmode='numeric' aria-label='页码' onchange='this.form.submit()'> / <span data-pages>{pages}</span> 页</label>"
        f"{nxt}"
        "</form></div>"
    )


_LIST_PARTIAL_SCRIPT = r"""
<script>
(function () {
  function rootOf(foot) {
    var kind = foot.getAttribute("data-partial");
    if (kind === "calls") return document.getElementById("calls");
    if (kind === "queue") return document.getElementById("queue-tasks");
    return null;
  }
  function paint(foot, html) {
    var root = rootOf(foot);
    if (!root) return;
    var doc = new DOMParser().parseFromString(html, "text/html");
    var nextBody = doc.querySelector("tbody");
    var nextFoot = doc.querySelector(".list-foot");
    var body = root.querySelector("tbody");
    var current = root.querySelector(".list-foot");
    if (nextBody && body) body.replaceWith(nextBody);
    if (nextFoot && current) current.replaceWith(nextFoot);
  }
  function setBusy(root, on) {
    if (window.RogmemLoad && typeof RogmemLoad.list === "function") RogmemLoad.list(root, on);
    else if (root) {
      root.classList.toggle("list-busy", !!on);
      if (on) root.setAttribute("aria-busy", "true");
      else root.removeAttribute("aria-busy");
    }
  }
  var listAbort = null;
  function refresh(foot, href) {
    var kind = foot.getAttribute("data-partial");
    if (!kind || !href) return;
    var root = rootOf(foot);
    var url = new URL(href, location.href);
    url.searchParams.set("part", kind);
    if (listAbort) listAbort.abort();
    listAbort = typeof AbortController === "function" ? new AbortController() : null;
    setBusy(root, true);
    fetch(url.pathname + url.search, {
      credentials: "same-origin",
      cache: "no-store",
      headers: {"X-Rogmem-List": "1"},
      signal: listAbort ? listAbort.signal : undefined
    })
      .then(function (response) {
        var type = response.headers.get("content-type") || "";
        if (!response.ok || type.indexOf("html") < 0) throw new Error("list");
        return response.text();
      })
      .then(function (html) {
        paint(foot, html);
        setBusy(root, false);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        setBusy(root, false);
        location.href = href;
      });
  }
  document.addEventListener("click", function (event) {
    var link = event.target && event.target.closest ? event.target.closest(".list-foot[data-partial] a[href]") : null;
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    var foot = link.closest(".list-foot");
    if (!foot) return;
    event.preventDefault();
    refresh(foot, link.getAttribute("href"));
  });
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input || input.name !== "page" || !input.form) return;
    var foot = input.form.closest ? input.form.closest(".list-foot[data-partial]") : null;
    if (!foot) return;
    event.preventDefault();
    var params = new URLSearchParams(new FormData(input.form));
    refresh(foot, input.form.getAttribute("action") + "?" + params.toString());
  });
})();
</script>
"""


_CLIP_LEN = 30


def _flat_head(text: str) -> str:
    """The visible head stays one line. Line breaks inside it become spaces."""
    return text[:_CLIP_LEN].replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")


def clip_html(text: str, link: str = "") -> str:
    """A list cell shows the first 30 characters. The rest stays in the cell for the hover card.

    `link` is already escaped. Shorter text is the cell itself, with nothing extra to open.
    """
    raw = "" if text is None else str(text)
    if len(raw) <= _CLIP_LEN:
        shown = esc(raw)
        return f"<a href='{link}'>{shown}</a>" if link else shown
    inner = (
        f"<span class='clip-shown'>{esc(_flat_head(raw))}…</span>"
        f"<span class='clip-full' hidden>{esc(raw)}</span>"
    )
    if link:
        return f"<a class='clip' href='{link}'>{inner}</a>"
    return f"<span class='clip' tabindex='0'>{inner}</span>"


def _source_cost_cells(row: dict) -> str:
    """Token and money cells. Same naming as the cost overview card."""
    tokens = row.get("cost_tokens")
    if tokens is None and isinstance(row.get("cost"), dict):
        tokens = row["cost"].get("tokens")
    money = row.get("cost_money")
    if money is None and "cost_money" not in row and isinstance(row.get("cost"), dict):
        money = row["cost"].get("money")
    unpriced = int(row.get("cost_unpriced") or 0)
    if money is None and unpriced:
        money_text = "未计入"
    else:
        money_text = _yuan(money or 0)
    return (
        f"<td class='fit num'>{esc(_grouped(tokens or 0))}</td>"
        f"<td class='fit num'>{esc(money_text)}</td>"
    )


def _source_cost_banner(row: dict) -> str:
    """Clear numbers in the detail header: real tokens, money at current prices."""
    tokens = row.get("cost_tokens")
    if tokens is None and isinstance(row.get("cost"), dict):
        tokens = row["cost"].get("tokens")
    money = row.get("cost_money")
    if money is None and "cost_money" not in row and isinstance(row.get("cost"), dict):
        money = row["cost"].get("money")
    unpriced = int(row.get("cost_unpriced") or 0)
    if money is None and unpriced:
        money_text = "未计入"
    else:
        money_text = _yuan(money or 0)
    return (
        "<div class='source-cost' aria-label='萃取成本'>"
        f"<span><span class='k'>Token</span> <strong>{esc(_grouped(tokens or 0))}</strong></span>"
        f"<span><span class='k'>金额</span> <strong>{esc(money_text)}</strong></span>"
        "</div>"
    )


def source_row_html(row: dict, origin: str) -> str:
    """One source in the list. An import receipt returns this same row."""
    file_row = origin == "file"
    href = f"/admin/sources/{esc(row['id'])}?from={esc('file' if file_row else 'session')}"
    if file_row:
        label = row.get("filename") or row.get("preview") or ""
        # 点文件名直接看原件；「查看」仍进详情页的文本解析。
        name = f"<a href='{href}&part=preview'>{esc(label)}</a>"
    else:
        name = clip_html(row.get("preview") or "", link=href)
    view = f"<a href='{href}'>查看</a>"
    retry = (
        f"<button class='link' type='button' onclick='post(\"/admin/api/sources/{esc(row['id'])}/retry\")'>重试</button>"
    )
    device_cell = (
        f"<td class='fit'>{esc(display_device_name(row.get('device_name')))}</td>" if file_row else ""
    )
    return (
        f"<tr data-source-id='{esc(str(row['id']))}'>"
        f"<td class='fit'>{esc(KIND.get(row['kind'], row['kind']))}</td>"
        f"<td class='name'>{name}</td>"
        f"{device_cell}<td class='fit'>{esc(row.get('agent_name'))}</td>"
        f"<td class='fit'>{esc(row.get('project_ref'))}</td><td class='fit'>{esc(row['id'])}</td>"
        f"<td class='fit'>{when(row.get('ingested_at'))}</td>"
        f"<td class='fit'>{flow_html(row.get('pipeline') or '', row.get('status') or '', row.get('note') or row.get('last_error') or '')}"
        f"{_progress_span(row)}</td>"
        f"{_source_cost_cells(row)}"
        f"<td class='fit ops'>{view}{retry}</td><td class='note'>{clip_html(row.get('note') or '')}</td>"
        "</tr>"
    )


def _progress_span(row: dict) -> str:
    """The stage a row is on, beside its lamps. Empty on a row that is not moving.

    A caller that already counted a stage's units passes ``progress``. Everyone else
    gets the same words from the two stored marks, so one renderer serves both.
    """
    label = row.get("progress")
    if label is None:
        label = _stage_note(row.get("pipeline") or "", row.get("status") or "")
    label = (label or "").strip()
    if not label:
        return ""
    return f"<span class='flow-progress'>{esc(label)}</span>"


def _lamp_button(name: str, state: str, error: str = "") -> str:
    title = f"{name}：{_LAMP_WORD.get(state, '未到')}"
    if state == "fail" and (error or "").strip():
        title = f"{title}。{error.strip()}"
    return f"<button type='button' class='lamp {state}' aria-label='{esc(title)}'><i></i></button>"


def flow_html(pipeline: str, status: str, error: str = "") -> str:
    lamps = []
    states = flow_states(pipeline, status)
    vector = vector_state(pipeline, status)
    for index, (name, state) in enumerate(states):
        button = _lamp_button(name, state, error)
        if index == 1:
            button = f"<span class='branch'>{button}{_lamp_button('向量化', vector, error)}</span>"
        lamps.append(button)
    return f"<span class='flow'>{''.join(lamps)}</span>"


_SOURCE_PICK = """<script>
(function () {
  var form = document.querySelector("form.source-filters");
  if (!form) return;
  function closeAll(except) {
    form.querySelectorAll(".pick.open").forEach(function (pick) {
      if (pick === except) return;
      pick.classList.remove("open");
      var menu = pick.querySelector(".pick-menu");
      var face = pick.querySelector(".pick-face");
      if (menu) menu.hidden = true;
      if (face) face.setAttribute("aria-expanded", "false");
    });
  }
  form.querySelectorAll("select").forEach(function (select) {
    var pick = document.createElement("span");
    pick.className = "pick";
    var face = document.createElement("button");
    face.type = "button";
    face.className = "pick-face";
    face.setAttribute("aria-haspopup", "listbox");
    face.setAttribute("aria-expanded", "false");
    var menu = document.createElement("span");
    menu.className = "pick-menu";
    menu.hidden = true;
    menu.setAttribute("role", "listbox");
    select.classList.add("pick-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.parentNode.insertBefore(pick, select);
    pick.append(face, menu, select);
    function paint() {
      var option = select.options[select.selectedIndex];
      var text = option ? option.textContent : "";
      face.textContent = text;
      face.title = text;
    }
    function fill() {
      menu.replaceChildren();
      Array.prototype.forEach.call(select.options, function (option) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "pick-item";
        item.setAttribute("role", "option");
        item.textContent = option.textContent;
        item.title = option.textContent;
        if (option.selected) item.setAttribute("aria-selected", "true");
        item.addEventListener("click", function () {
          select.value = option.value;
          paint();
          shut();
          face.focus();
        });
        menu.appendChild(item);
      });
    }
    function place() {
      var faceWidth = pick.getBoundingClientRect().width;
      var cap = Math.max(faceWidth, window.innerWidth - 16);
      menu.style.left = "0px";
      menu.style.maxWidth = cap + "px";
      menu.style.width = "max-content";
      var need = Math.ceil(menu.getBoundingClientRect().width);
      menu.style.width = Math.max(faceWidth, Math.min(need, cap)) + "px";
      var overflow = menu.getBoundingClientRect().right - (window.innerWidth - 8);
      if (overflow > 0) menu.style.left = (-overflow) + "px";
    }
    function open() {
      closeAll(pick);
      fill();
      menu.hidden = false;
      pick.classList.add("open");
      face.setAttribute("aria-expanded", "true");
      place();
      var current = menu.querySelector('[aria-selected="true"]') || menu.querySelector(".pick-item");
      if (current) current.focus();
    }
    function shut() {
      menu.hidden = true;
      pick.classList.remove("open");
      face.setAttribute("aria-expanded", "false");
    }
    face.addEventListener("click", function () {
      if (menu.hidden) open();
      else shut();
    });
    var label = select.closest("label");
    if (label) {
      label.addEventListener("click", function (event) {
        if (event.target !== label) return;
        event.preventDefault();
        event.stopPropagation();
        if (menu.hidden) open();
        else shut();
      });
    }
    face.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    menu.addEventListener("keydown", function (event) {
      var items = Array.prototype.slice.call(menu.querySelectorAll(".pick-item"));
      var index = items.indexOf(document.activeElement);
      if (event.key === "Escape") {
        event.preventDefault();
        shut();
        face.focus();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        (items[Math.min(items.length - 1, index + 1)] || items[0]).focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        (items[Math.max(0, index - 1)] || items[0]).focus();
      }
    });
    paint();
  });
  document.addEventListener("click", function (event) {
    form.querySelectorAll(".pick.open").forEach(function (pick) {
      if (!pick.contains(event.target)) closeAll(null);
    });
  });
})();
</script>"""


_FRESH_SCRIPT = """<script>
(function () {
  var box = document.getElementById('sources-fresh');
  if (!box || !box.dataset.endpoint) return;
  var url = box.dataset.endpoint + '?' + (box.dataset.query || '');
  var beat = 20000;
  var timer = 0;
  var loading = false;
  function rest() { if (timer) { clearInterval(timer); timer = 0; } }
  function refresh() {
    if (loading) return;
    loading = true;
    rest();
    location.reload();
  }
  function look() {
    if (loading) return;
    fetch(url, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data || typeof data.marker !== 'string') return;
        // 读当下 dataset：本页导入会经 rebaselineFresh 改基线，闭包里的旧值不能再当真。
        var liveMarker = box.dataset.marker || '';
        var liveTotal = parseInt(box.dataset.total || '0', 10) || 0;
        var extra = (parseInt(data.total, 10) || 0) - liveTotal;
        if (data.marker === liveMarker && extra <= 0) return;
        refresh();
      })
      .catch(function () {});
  }
  function watch() { if (!timer) { timer = setInterval(look, beat); } }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) rest();
    else { look(); watch(); }
  });
  watch();
})();
</script>"""


def _fresh_hint(tab: str, params: dict, rows: list[dict], total: int, page_no: int, status: str) -> str:
    """Hidden probe above the table. Newer rows reload the list; no tip, no click.

    Page one only, and only with no status filter: a status filter narrows in Python,
    and the probe reads columns, so it would refresh for rows the list itself would hide.
    """
    if page_no != 1 or status:
        return ""
    top = rows[0] if rows else {}
    marker = f"{top.get('ingested_at') or ''}|{top.get('id') or ''}"
    return (
        "<div id='sources-fresh' class='fresh' hidden aria-hidden='true'"
        " data-endpoint='/admin/api/sources/latest'"
        f" data-query='{esc(urlencode(params))}'"
        f" data-marker='{esc(marker)}'"
        f" data-total='{int(total)}'></div>"
        f"{_FRESH_SCRIPT}"
    )


def sources_page(rows: list[dict], tab: str, filters: dict, facets: dict, pager: dict | None = None) -> str:
    tab = "file" if tab == "file" else "session"
    title = "文件资料" if tab == "file" else "原始会话"
    kind_values = ["user", "agent", "summary"] if tab != "file" else []
    status_pairs = list(processing_filters())
    chosen_status = filters.get("status", "")
    if chosen_status and chosen_status not in {value for value, _label in status_pairs}:
        status_pairs.insert(0, (chosen_status, chosen_status))
    status_labels = {value: label for value, label in status_pairs}
    status_values = [value for value, _label in status_pairs]
    kind_filter = ""
    project_filter = ""
    if tab != "file":
        kind_filter = f"<label>类别<select name='kind'>{_options(kind_values, filters.get('kind', ''), KIND)}</select></label>"
        project_filter = f"<label>关联工程<select name='project'>{_options(facets.get('projects') or [], filters.get('project', ''))}</select></label>"
    device_label = "来源设备" if tab == "file" else "设备"
    form = (
        f"<form class='filters source-filters' method='get' action='/admin/sources'>"
        f"<input type='hidden' name='tab' value='{esc(tab)}'>"
        f"{kind_filter}"
        f"<label>{device_label}<select name='device'>{_options(facets.get('devices') or [], filters.get('device', ''), {value: display_device_name(value) for value in (facets.get('devices') or [])})}</select></label>"
        f"<label>智能体<select name='agent'>{_options(facets.get('agents') or [], filters.get('agent', ''))}</select></label>"
        f"{project_filter}"
        f"<label>处理状态<select name='status'>{_options(status_values, filters.get('status', ''), status_labels)}</select></label>"
        f"<input name='q' value='{esc(filters.get('q', ''))}' placeholder='搜索原文、文件名或来源编号'>"
        f"<button class='quiet' type='submit'>筛选</button>"
    )
    session = (filters.get("session") or "").strip()
    turn = (filters.get("turn") or "").strip()
    if tab != "file" and session:
        turn_hidden = (
            f'<input type="hidden" name="turn" value="{esc(turn)}">' if turn else ""
        )
        form += (
            f"<input type='hidden' name='session' value='{esc(session)}'>"
            f"{turn_hidden}"
            f"<p class='muted'>会话 {esc(session)}"
            f"{(' · 回合 ' + esc(turn)) if turn else ''} "
            f"<a href='/admin/sources?tab=session'>清除会话</a></p>"
        )
    if tab == "file":
        form += """<span class="import-menu"><button type="button" class="btn" id="import-open" aria-haspopup="menu" aria-expanded="false" aria-controls="import-menu">导入</button><span id="import-menu" role="menu" hidden><span class="import-panel"><button type="button" role="menuitem" id="import-local">从本机导入</button><button type="button" role="menuitem" id="import-nas">从极空间导入</button></span></span></span><input id="import-files" type="file" multiple hidden onchange="sendFiles(this)">
<dialog id="nas-import" aria-label="从极空间导入" aria-labelledby="nas-title">
<div class="nas-head"><h2 id="nas-title">从极空间导入</h2><button type="button" id="nas-close" class="quiet">关闭</button></div>
<p id="nas-crumb" class="nas-crumb" hidden></p>
<div class="nas-body">
<p id="nas-status" class="empty" role="status" hidden></p>
<div id="nas-list" class="nas-list" hidden></div>
<div id="nas-result"></div>
</div>
<p id="nas-scope" class="note nas-scope" hidden></p>
<div class="nas-foot">
<button type="button" id="nas-up" class="quiet" hidden>返回上一层</button>
<button type="button" id="nas-page-prev" class="quiet" hidden>上一页</button>
<button type="button" id="nas-page-next" class="quiet" hidden>下一页</button>
<button type="button" id="nas-retry" class="quiet" hidden>再试一次</button>
<button type="button" id="nas-here" class="quiet" hidden>导入这一层</button>
<button type="button" id="nas-selected" hidden>导入所选</button>
</div>
</dialog>
<div id="dup" class="modal" hidden>
<div class="modal-card">
<p id="dup-text">这个文件已经在系统中。</p>
<div id="dup-list"></div>
<p><button type="button" id="dup-close">知道了</button></p>
</div></div>
<script>
function showDuplicates(items, alsoImported) {
  const box = document.getElementById('dup');
  document.getElementById('dup-text').textContent = alsoImported ? '有的文件已经在系统中，没有再次导入。' : '这个文件已经在系统中。';
  const list = document.getElementById('dup-list');
  list.replaceChildren();
  for (const item of items) {
    const row = document.createElement('p');
    row.textContent = item.filename || '已有文件';
    const link = document.createElement('a');
    link.href = '/admin/sources/' + item.id + '?from=file';
    link.textContent = '详情';
    row.append(link);
    list.append(row);
  }
  box.hidden = false;
  document.getElementById('dup-close').onclick = () => { location = '/admin/sources?tab=file'; };
}
function sourceTable() {
  return document.querySelector('table.sources');
}
function blankCell(className) {
  const td = document.createElement('td');
  td.className = className;
  return td;
}
function pendingRow(file) {
  const row = document.createElement('tr');
  row.className = 'uploading';
  row.setAttribute('aria-busy', 'true');
  const kind = blankCell('fit');
  kind.textContent = '文件';
  const name = blankCell('name');
  name.textContent = file.name || '';
  const status = blankCell('fit status');
  const meter = document.createElement('span');
  meter.className = 'upload-meter wait';
  meter.setAttribute('role', 'progressbar');
  meter.setAttribute('aria-valuemin', '0');
  meter.setAttribute('aria-valuemax', '100');
  meter.setAttribute('aria-valuenow', '0');
  meter.setAttribute('aria-label', '上传进度');
  const track = document.createElement('span');
  track.className = 'track';
  const fill = document.createElement('span');
  fill.className = 'fill';
  track.appendChild(fill);
  const pct = document.createElement('span');
  pct.className = 'pct';
  pct.textContent = '0%';
  meter.append(track, pct);
  status.appendChild(meter);
  row.append(
    kind,
    name,
    blankCell('fit device'),
    blankCell('fit agent'),
    blankCell('fit project'),
    blankCell('fit source-id'),
    blankCell('fit when'),
    status,
    blankCell('fit num'),
    blankCell('fit num'),
    blankCell('fit ops'),
    blankCell('note')
  );
  return row;
}
function revealFiles(files) {
  const table = sourceTable();
  if (!table) return [];
  const head = table.querySelector('tr');
  const empty = table.querySelector('td.empty');
  if (empty && empty.parentElement) empty.parentElement.remove();
  let cursor = head;
  const rows = [];
  for (const file of files) {
    const row = pendingRow(file);
    cursor.insertAdjacentElement('afterend', row);
    cursor = row;
    rows.push(row);
  }
  return rows;
}
function paintTransfer(row, ratio) {
  if (!row.isConnected) return;
  const meter = row.querySelector('.upload-meter');
  if (!meter || meter.classList.contains('fail')) return;
  const fill = meter.querySelector('.fill');
  const pct = meter.querySelector('.pct');
  if (!(ratio >= 0) || ratio >= 1) {
    meter.classList.remove('wait');
    meter.classList.add('hold');
    fill.style.width = '100%';
    pct.textContent = '导入中';
    meter.setAttribute('aria-valuetext', '导入中');
    meter.removeAttribute('aria-valuenow');
    return;
  }
  const shown = Math.max(0, Math.min(99, Math.round(ratio * 100)));
  meter.classList.remove('wait', 'hold');
  meter.setAttribute('aria-valuenow', String(shown));
  meter.removeAttribute('aria-valuetext');
  fill.style.width = shown + '%';
  pct.textContent = shown + '%';
}
function failRow(row, message) {
  row.classList.remove('uploading');
  row.removeAttribute('aria-busy');
  const status = row.querySelector('td.status');
  const meter = document.createElement('span');
  meter.className = 'upload-meter fail';
  meter.textContent = message || '没有导入';
  status.replaceChildren(meter);
}
function reconcileList() {
  const table = sourceTable();
  if (!table) return;
  const body = Array.prototype.filter.call(table.querySelectorAll('tr'), function (_row, index) { return index > 0; });
  if (body.length) return;
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.colSpan = 10;
  td.className = 'empty';
  td.textContent = '这一栏还没有文件。';
  tr.appendChild(td);
  table.appendChild(tr);
}
function pageCount(total, size) {
  var count = Number(total) || 0;
  var step = Number(size) || 0;
  if (step < 1) step = 50;
  if (count < 1) return 1;
  return Math.ceil(count / step);
}
function footHref(form, page) {
  var params = new URLSearchParams();
  Array.prototype.forEach.call(form.querySelectorAll("input[type=hidden]"), function (input) {
    if (input.name) params.append(input.name, input.value);
  });
  if (page > 1) params.set("page", String(page));
  var query = params.toString();
  return form.getAttribute("action") + (query ? "?" + query : "");
}
function paintStep(form, step, page, pages) {
  var node = form.querySelector("[data-step='" + step + "']");
  if (!node) return;
  var target = page + Number(step);
  var live = target >= 1 && target <= pages;
  var label = Number(step) < 0 ? "上一页" : "下一页";
  if (!live) {
    if (node.tagName === "BUTTON" && node.disabled) return;
    var dead = document.createElement("button");
    dead.type = "button";
    dead.className = "quiet";
    dead.disabled = true;
    dead.setAttribute("data-step", String(step));
    dead.textContent = label;
    node.replaceWith(dead);
    return;
  }
  var href = footHref(form, target);
  if (node.tagName === "A" && node.getAttribute("href") === href) return;
  var link = document.createElement("a");
  link.className = "btn quiet";
  link.href = href;
  link.setAttribute("data-step", String(step));
  link.textContent = label;
  node.replaceWith(link);
}
function paintFoot(total) {
  var foot = document.querySelector(".list-foot");
  if (!foot) return;
  var form = foot.querySelector("form.pager");
  if (!form) return;
  var page = Number(foot.dataset.page || "1");
  var pages = pageCount(total, foot.dataset.pageSize);
  foot.dataset.total = String(total);
  var note = foot.querySelector(".note");
  if (note) {
    note.dataset.total = String(total);
    note.textContent = "共 " + total + " 条";
  }
  var mark = form.querySelector("[data-pages]");
  if (mark) mark.textContent = String(pages);
  paintStep(form, -1, page, pages);
  paintStep(form, 1, page, pages);
}
function noteCommit() {
  var foot = document.querySelector(".list-foot");
  if (!foot) return;
  paintFoot(Number(foot.dataset.total || "0") + 1);
}
function settleRow(row, item, outcome) {
  const host = document.createElement('table');
  host.innerHTML = item.row_html || '';
  const next = host.querySelector('tr');
  if (!next) {
    failRow(row, '没有导入');
    return;
  }
  row.replaceWith(next);
  if (typeof formatSourceClocks === 'function') formatSourceClocks(next);
  if (outcome) outcome.imported = true;
  noteCommit();
}
function uploadFile(file, row, dupes, done, outcome) {
  const data = new FormData();
  data.append('files', file, file.name);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/admin/api/sources/import');
  xhr.upload.addEventListener('progress', function (event) {
    if (!event.lengthComputable || !event.total) return;
    const ratio = event.loaded / event.total;
    paintTransfer(row, ratio);
  });
  xhr.addEventListener('load', function () {
    let payload = null;
    try { payload = JSON.parse(xhr.responseText || ''); } catch (err) { payload = null; }
    const item = payload && payload.imported && payload.imported[0];
    if (!item) failRow(row, '没有导入');
    else if (item.duplicate && item.id) {
      row.remove();
      dupes.push(item);
      reconcileList();
    }
    else if (item.row_html) settleRow(row, item, outcome);
    else failRow(row, (item && item.error) || '没有导入');
    if (item && item.id && item.row_html) { watchSource(item.id); rebaselineFresh(); }
    done();
  });
  xhr.addEventListener('error', function () {
    failRow(row, '没有传上去');
    done();
  });
  xhr.send(data);
}
function queueFiles(files) {
  files = Array.prototype.slice.call(files || []);
  if (!files.length) return;
  const rows = revealFiles(files);
  const dupes = [];
  const outcome = { imported: false };
  let index = 0;
  function next() {
    while (index < files.length) {
      const file = files[index];
      const row = rows[index];
      index += 1;
      if (file.size > __UPLOAD_MAX_BYTES__) {
        failRow(row, '文件太大');
        continue;
      }
      uploadFile(file, row, dupes, next, outcome);
      return;
    }
    if (!dupes.length) return;
    showDuplicates(dupes, outcome.imported);
  }
  next();
}
function sendFiles(input) {
  const files = Array.prototype.slice.call(input.files || []);
  input.value = '';
  queueFiles(files);
}
function knowledgeDropHasFiles(event) {
  const transfer = event.dataTransfer;
  if (!transfer || !transfer.types) return false;
  return Array.prototype.indexOf.call(transfer.types, 'Files') >= 0;
}
function bindKnowledgeDrop() {
  if (!document.getElementById('import-files')) return;
  const surface = document.querySelector('main .content');
  if (!surface) return;
  let depth = 0;
  function paint(on) {
    surface.classList.toggle('drop-ready', !!on);
  }
  surface.addEventListener('dragenter', function (event) {
    if (!knowledgeDropHasFiles(event)) return;
    event.preventDefault();
    depth += 1;
    paint(true);
  });
  surface.addEventListener('dragover', function (event) {
    if (!knowledgeDropHasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  });
  surface.addEventListener('dragleave', function (event) {
    if (!knowledgeDropHasFiles(event)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) paint(false);
  });
  surface.addEventListener('drop', function (event) {
    if (!knowledgeDropHasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    paint(false);
    const dialog = document.getElementById('nas-import');
    if (dialog && dialog.open) return;
    queueFiles(event.dataTransfer && event.dataTransfer.files);
  });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bindKnowledgeDrop);
} else {
  bindKnowledgeDrop();
}
</script>
<script>
var sourceWatch = new Set();
var sourcePoll = 0;
function sourceWatchRoot() {
  return document.querySelector('.table-scroll[data-active]');
}
function sourceRowById(id) {
  return document.querySelector("tr[data-source-id='" + id + "']");
}
function kickSourcePoll() {
  if (sourcePoll || !sourceWatch.size) return;
  sourcePoll = setInterval(pollSources, 1500);
  pollSources();
}
function watchSource(id) {
  if (!id) return;
  sourceWatch.add(id);
  kickSourcePoll();
}
function paintSourceRow(item) {
  var row = sourceRowById(item.id);
  if (!row) return;
  var host = document.createElement('table');
  host.innerHTML = item.row_html || '';
  var next = host.querySelector('tr');
  if (!next) return;
  row.replaceWith(next);
  if (typeof formatSourceClocks === 'function') formatSourceClocks(next);
}
function pollSources() {
  if (!sourceWatch.size) { clearInterval(sourcePoll); sourcePoll = 0; return; }
  var ids = Array.from(sourceWatch);
  fetch('/admin/api/sources/progress?ids=' + encodeURIComponent(ids.join(',')))
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (data) {
      if (!data || !data.rows) return;
      data.rows.forEach(function (item) {
        paintSourceRow(item);
        if (item.active) sourceWatch.add(item.id); else sourceWatch.delete(item.id);
      });
      if (!sourceWatch.size) { clearInterval(sourcePoll); sourcePoll = 0; }
    })
    .catch(function () {});
}
function seedSourceWatch() {
  // 订阅只服务文件资料页。原始会话页没有导入入口，也就没有这一行要看的东西。
  if (!document.getElementById('import-files')) return;
  var root = sourceWatchRoot();
  if (!root) return;
  (root.getAttribute('data-active') || '').split(',').forEach(function (id) { if (id) sourceWatch.add(id); });
  kickSourcePoll();
}
function rebaselineFresh() {
  // 探测拿列表画好那一刻的顶部当基线。刚把新行画进列表的是我们自己，
  // 基线要跟着走，否则过 20 秒它会把这条已经看得见的行当成「有新的」整页刷新。
  var box = document.getElementById('sources-fresh');
  if (!box || !box.dataset.endpoint) return;
  fetch(box.dataset.endpoint + '?' + (box.dataset.query || ''), { credentials: 'same-origin', cache: 'no-store' })
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (data) {
      if (!data || typeof data.marker !== 'string') return;
      box.dataset.marker = data.marker;
      box.dataset.total = String(parseInt(data.total, 10) || 0);
    })
    .catch(function () {});
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', seedSourceWatch);
} else {
  seedSourceWatch();
}
</script>
<script>
function nasJoin(parent, name) {
  return parent ? parent + '/' + name : name;
}
function nasOpen() {
  var dialog = document.getElementById('nas-import');
  return !!(dialog && dialog.open);
}
function nasOwns(session, request) {
  return nasOpen() && session === nasSession && request === nasRequest;
}
function nasShow(id, on) {
  var node = document.getElementById(id);
  if (!node) return;
  node.hidden = !on;
  node.disabled = !on || nasImporting;
}
function nasSetStatus(text, failed, quiet) {
  var node = document.getElementById('nas-status');
  node.textContent = text || '';
  node.hidden = !text;
  node.className = (quiet ? 'empty' : 'note') + (failed ? ' nas-fail' : '');
}
function hideNasActions() {
  ['nas-up', 'nas-page-prev', 'nas-page-next', 'nas-retry', 'nas-here', 'nas-selected'].forEach(function (id) {
    nasShow(id, false);
  });
  nasShow('nas-scope', false);
}
function disarmNasSelection() {
  document.querySelectorAll('#nas-list input[type="checkbox"]').forEach(function (box) {
    var row = box.closest('.nas-row');
    if (row) row.remove();
  });
  nasShow('nas-selected', false);
  nasShow('nas-here', false);
  nasShow('nas-scope', false);
}
function showQuiet(text, failed) {
  var dialog = document.getElementById('nas-import');
  dialog.classList.remove('nas-browse');
  var list = document.getElementById('nas-list');
  list.replaceChildren();
  list.hidden = true;
  nasSetStatus(text, failed, true);
}
function beginNasSession() {
  nasSession += 1;
  nasRequest = 0;
  nasImporting = false;
  nasPath = '';
  nasAsk = '';
  nasOffset = 0;
  document.getElementById('nas-result').replaceChildren();
  document.getElementById('nas-crumb').replaceChildren();
  document.getElementById('nas-crumb').hidden = true;
  hideNasActions();
  showQuiet('列出中', false);
}
var nasPath = '';
var nasAsk = '';
var nasOffset = 0;
var nasAskOffset = 0;
var nasMore = false;
var nasPage = 200;
var nasSession = 0;
var nasRequest = 0;
var nasImporting = false;
function paintSelection() {
  if (nasImporting) return;
  var n = document.querySelectorAll('#nas-list input[type="checkbox"]:checked').length;
  nasShow('nas-selected', n > 0);
}
function paintNas(payload, session, request) {
  if (!nasOwns(session, request)) return;
  var dialog = document.getElementById('nas-import');
  var list = document.getElementById('nas-list');
  var crumb = document.getElementById('nas-crumb');
  dialog.removeAttribute('aria-busy');
  list.replaceChildren();
  crumb.replaceChildren();
  hideNasActions();
  document.getElementById('nas-result').replaceChildren();
  if (!payload || payload.ok === false) {
    var message = (payload && payload.error) || '没有列出来';
    var unmounted = message === '极空间目录还没挂进容器';
    crumb.hidden = true;
    showQuiet(unmounted ? '个人空间还没挂上' : message, !unmounted);
    if (unmounted) {
      var status = document.getElementById('nas-status');
      status.append(document.createTextNode('。在'));
      var where = document.createElement('a');
      where.href = '/admin/settings/basic';
      where.textContent = '基础设置';
      status.append(where, document.createTextNode('里填写。'));
    }
    if (!unmounted && nasAsk) nasShow('nas-up', true);
    if (message === '没有列出来') nasShow('nas-retry', true);
    return;
  }
  nasPath = payload.path || '';
  nasAsk = nasPath;
  nasOffset = payload.offset || 0;
  nasMore = !!payload.more;
  nasPage = payload.limit || 200;
  var parts = nasPath ? nasPath.split('/') : [];
  function crumbButton(label, path) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'nas-jump';
    button.textContent = label;
    button.addEventListener('click', function () { loadNas(path, 0); });
    crumb.append(button);
  }
  if (!parts.length) {
    var root = document.createElement('span');
    root.textContent = '个人空间';
    crumb.append(root);
  } else {
    crumbButton('个人空间', '');
    parts.forEach(function (part, index) {
      crumb.append(document.createTextNode(' / '));
      if (index === parts.length - 1) {
        var current = document.createElement('span');
        current.textContent = part;
        crumb.append(current);
      } else {
        crumbButton(part, parts.slice(0, index + 1).join('/'));
      }
    });
  }
  crumb.hidden = false;
  var accepted = 0;
  (payload.entries || []).forEach(function (entry) {
    if (entry.kind === 'dir') {
      var open = document.createElement('button');
      open.type = 'button';
      open.className = 'nas-row';
      open.textContent = entry.name;
      open.addEventListener('click', function () { loadNas(nasJoin(nasPath, entry.name), 0); });
      list.append(open);
    } else if (entry.accepted) {
      accepted += 1;
      var label = document.createElement('label');
      label.className = 'nas-row';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.value = nasJoin(nasPath, entry.name);
      box.addEventListener('change', paintSelection);
      label.append(box, document.createTextNode(entry.name));
      list.append(label);
    } else {
      var plain = document.createElement('p');
      plain.className = 'muted';
      plain.textContent = entry.name + ' 不接受的文件类型';
      list.append(plain);
    }
  });
  var empty = !(payload.entries || []).length;
  if (empty) {
    showQuiet('暂无文件', false);
    crumb.hidden = false;
  } else {
    dialog.classList.add('nas-browse');
    list.hidden = false;
    nasSetStatus('', false, false);
  }
  var unsure = !!payload.more || !!payload.truncated;
  var showHere = !empty && (accepted > 0 || unsure);
  nasShow('nas-here', showHere);
  var scope = document.getElementById('nas-scope');
  if (showHere) {
    scope.hidden = false;
    scope.disabled = false;
    scope.textContent = '不含子目录，一次最多 50 个' + (payload.truncated ? '。只列出前 5000 项' : '');
  }
  nasShow('nas-up', !!nasPath);
  nasShow('nas-page-prev', nasOffset > 0);
  nasShow('nas-page-next', !!nasMore);
  paintSelection();
}
async function loadNas(path, offset) {
  if (!nasOpen() || nasImporting) return;
  var session = nasSession;
  var request = ++nasRequest;
  nasAsk = path || '';
  nasAskOffset = offset || 0;
  disarmNasSelection();
  var dirsLeft = document.querySelectorAll('#nas-list button.nas-row').length;
  if (!dirsLeft) showQuiet('列出中', false);
  else nasSetStatus('列出中', false, false);
  document.getElementById('nas-import').setAttribute('aria-busy', 'true');
  try {
    var response = await fetch('/admin/api/sources/nas?path=' + encodeURIComponent(path || '') + '&offset=' + (offset || 0));
    var payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!nasOwns(session, request)) return;
    paintNas(payload && response.ok ? payload : {ok: false, error: (payload && payload.error) || '没有列出来'}, session, request);
  } catch (err) {
    if (!nasOwns(session, request)) return;
    paintNas({ok: false, error: '没有列出来'}, session, request);
  }
}
function lockNasControls() {
  ['nas-selected', 'nas-here', 'nas-up', 'nas-page-prev', 'nas-page-next', 'nas-retry'].forEach(function (id) {
    var node = document.getElementById(id);
    if (node) node.disabled = true;
  });
  document.querySelectorAll('#nas-list button, #nas-list input, #nas-crumb button').forEach(function (node) {
    node.disabled = true;
  });
}
function unlockNasBrowse() {
  nasImporting = false;
  document.getElementById('nas-import').removeAttribute('aria-busy');
  document.querySelectorAll('#nas-list button, #nas-list input, #nas-crumb button').forEach(function (node) {
    node.disabled = false;
  });
  ['nas-up', 'nas-page-prev', 'nas-page-next', 'nas-retry', 'nas-here'].forEach(function (id) {
    var node = document.getElementById(id);
    if (node && !node.hidden) node.disabled = false;
  });
  paintSelection();
}
function settleNasImport(session, request, apply) {
  if (!nasOwns(session, request)) return;
  apply();
  unlockNasBrowse();
}
async function postNas(body) {
  if (!nasOpen()) return;
  if (nasImporting) return;
  var session = nasSession;
  var request = ++nasRequest;
  nasImporting = true;
  lockNasControls();
  nasSetStatus('导入中', false, false);
  document.getElementById('nas-import').setAttribute('aria-busy', 'true');
  try {
    var response = await fetch('/admin/api/sources/import-nas', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    var payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!payload || !payload.imported) {
      settleNasImport(session, request, function () {
        nasSetStatus((payload && payload.error) || '没有导入', true, false);
      });
      return;
    }
    var rows = payload.imported;
    var pending = rows.some(function (item) { return !item.ok || item.duplicate; });
    if (!pending) {
      if (!nasOwns(session, request)) return;
      location = '/admin/sources?tab=file';
      return;
    }
    settleNasImport(session, request, function () {
      nasSetStatus('', false, false);
      var box = document.getElementById('nas-result');
      box.replaceChildren();
      rows.forEach(function (item) {
        var line = document.createElement('p');
        if (item.duplicate && item.id) {
          line.append(document.createTextNode((item.filename || '已有文件') + ' '));
          var link = document.createElement('a');
          link.href = '/admin/sources/' + item.id + '?from=file';
          link.textContent = '详情';
          line.append(link);
        } else if (!item.ok) {
          line.textContent = (item.filename || '文件') + ' ' + (item.error || '没有导入');
        } else {
          line.textContent = (item.filename || '文件') + ' 已导入';
        }
        box.append(line);
      });
    });
  } catch (err) {
    settleNasImport(session, request, function () {
      nasSetStatus('没有导入', true, false);
    });
  }
}
function setImportPhase(next, focusItem) {
  importClock += 1;
  clearTimeout(importTimer);
  importPhase = next;
  var menu = document.getElementById('import-menu');
  var face = document.getElementById('import-open');
  if (!menu || !face) return;
  var open = next !== 'closed';
  menu.hidden = !open;
  face.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (!open) {
    if (focusItem) face.focus();
    return;
  }
  placeImportMenu();
  if (!focusItem) return;
  var first = menu.querySelector('[role="menuitem"]');
  if (first) first.focus();
}
function placeImportMenu() {
  var menu = document.getElementById('import-menu');
  var panel = menu.querySelector('.import-panel');
  menu.style.right = '0px';
  menu.style.left = 'auto';
  var rect = panel.getBoundingClientRect();
  var overflowLeft = 8 - rect.left;
  if (overflowLeft > 0) menu.style.right = (-overflowLeft) + 'px';
  rect = panel.getBoundingClientRect();
  var overflowRight = rect.right - (window.innerWidth - 8);
  if (overflowRight > 0) menu.style.right = ((parseFloat(menu.style.right) || 0) + overflowRight) + 'px';
}
function schedulePreviewClose() {
  if (importPhase !== 'preview') return;
  var mark = ++importClock;
  clearTimeout(importTimer);
  importTimer = setTimeout(function () {
    if (mark !== importClock || importPhase !== 'preview') return;
    setImportPhase('closed', false);
  }, 200);
}
function importFinePointer() {
  return window.matchMedia('(hover: hover) and (pointer: fine)').matches;
}
var importPhase = 'closed';
var importClock = 0;
var importTimer = 0;
var importRoot = document.getElementById('import-open').parentElement;
document.getElementById('import-open').addEventListener('click', function () {
  if (importPhase === 'closed' || importPhase === 'preview') setImportPhase('pinned', false);
  else setImportPhase('closed', false);
});
document.getElementById('import-local').addEventListener('click', function () {
  setImportPhase('closed', false);
  document.getElementById('import-files').click();
});
document.getElementById('import-nas').addEventListener('click', function () {
  setImportPhase('closed', false);
  document.getElementById('import-open').focus();
  beginNasSession();
  document.getElementById('nas-import').showModal();
  loadNas('', 0);
});
importRoot.addEventListener('pointerenter', function () {
  if (!importFinePointer()) return;
  if (importPhase === 'closed') setImportPhase('preview', false);
  else if (importPhase === 'preview') {
    importClock += 1;
    clearTimeout(importTimer);
  }
});
importRoot.addEventListener('pointerleave', function () {
  if (!importFinePointer()) return;
  if (importPhase === 'preview') schedulePreviewClose();
});
document.getElementById('import-open').addEventListener('keydown', function (event) {
  if (event.key === 'ArrowDown' || event.key === 'ArrowRight' || event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    setImportPhase('pinned', true);
  }
});
document.getElementById('import-menu').addEventListener('keydown', function (event) {
  var items = Array.prototype.slice.call(document.querySelectorAll('#import-menu [role="menuitem"]'));
  var index = items.indexOf(document.activeElement);
  if (event.key === 'Escape') {
    event.preventDefault();
    setImportPhase('closed', true);
  } else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
    event.preventDefault();
    items[(index + 1 + items.length) % items.length].focus();
  } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
    event.preventDefault();
    items[(index - 1 + items.length) % items.length].focus();
  } else if (event.key === 'Tab') {
    setImportPhase('closed', false);
  }
});
document.addEventListener('keydown', function (event) {
  if (event.key !== 'Escape') return;
  var menu = document.getElementById('import-menu');
  var dialog = document.getElementById('nas-import');
  if (!menu || menu.hidden || (dialog && dialog.open)) return;
  event.preventDefault();
  setImportPhase('closed', true);
});
document.getElementById('nas-import').addEventListener('close', function () {
  nasSession += 1;
  nasRequest += 1;
  nasImporting = false;
  setTimeout(function () { document.getElementById('import-open').focus(); }, 0);
});
document.getElementById('nas-close').addEventListener('click', function () {
  document.getElementById('nas-import').close();
});
document.getElementById('nas-up').addEventListener('click', function () {
  if (this.disabled) return;
  var base = nasAsk || nasPath;
  var parts = base.split('/');
  parts.pop();
  loadNas(parts.join('/'), 0);
});
document.getElementById('nas-page-prev').addEventListener('click', function () {
  if (this.disabled) return;
  loadNas(nasPath, Math.max(0, nasOffset - nasPage));
});
document.getElementById('nas-page-next').addEventListener('click', function () {
  if (this.disabled) return;
  loadNas(nasPath, nasOffset + nasPage);
});
document.getElementById('nas-retry').addEventListener('click', function () {
  if (this.disabled) return;
  loadNas(nasAsk, nasAskOffset);
});
document.getElementById('nas-selected').addEventListener('click', function () {
  if (this.disabled || nasImporting) return;
  var paths = [];
  document.querySelectorAll('#nas-list input[type="checkbox"]:checked').forEach(function (box) {
    if (!box.disabled) paths.push(box.value);
  });
  if (!paths.length) {
    nasSetStatus('没有文件', true, false);
    return;
  }
  postNas({paths: paths});
});
document.getElementById('nas-here').addEventListener('click', function () {
  if (this.disabled || nasImporting) return;
  postNas({dir: nasPath});
});
document.addEventListener('click', function (event) {
  var menu = document.getElementById('import-menu');
  if (menu && !menu.hidden && importPhase !== 'closed' && !menu.parentElement.contains(event.target)) {
    setImportPhase('closed', false);
  }
});
</script>
"""
    from rogmem.engine import DOCUMENT_MAX_BYTES

    form = form.replace("__UPLOAD_MAX_BYTES__", str(DOCUMENT_MAX_BYTES))
    form += "</form>" + _SOURCE_PICK
    name_head = "文件名" if tab == "file" else "原文"
    device_head = "<th class='fit'>设备名称</th>" if tab == "file" else ""
    columns = 12 if tab == "file" else 11
    head = (
        "<tr><th class='fit'>类别</th>"
        f"<th class='name'>{name_head}</th>"
        f"{device_head}<th class='fit'>智能体</th><th class='fit'>关联工程</th>"
        "<th class='fit'>来源编号</th><th class='fit'>更新时间</th><th class='fit'>处理状态</th>"
        "<th class='fit num'>Token</th><th class='fit num'>金额</th>"
        "<th class='fit'>操作</th><th class='note'>备注</th></tr>"
    )
    moving = ",".join(str(row.get("id")) for row in rows if row.get("active"))
    if not rows:
        empty = "这一栏还没有文件。" if tab == "file" else "这一栏还没有原始会话。"
        table = f"<div class='table-scroll' data-active='{esc(moving)}'><table class='sources'>{head}<tr><td colspan='{columns}' class='empty'>{empty}</td></tr></table></div>"
    else:
        body = []
        for row in rows:
            body.append(source_row_html(row, tab))
        table = f"<div class='table-scroll' data-active='{esc(moving)}'><table class='sources'>{head}{''.join(body)}</table></div>"
    params = {"tab": tab}
    for key, value in filters.items():
        if value not in ("", None):
            params[key] = value
    if pager:
        total = int(pager.get("total") or 0)
        page_no = int(pager.get("page") or 1)
        size = pager.get("page_size")
    else:
        total = len(rows)
        page_no = 1
        size = None
    pager_html = list_pager(
        "/admin/sources",
        params,
        total=total,
        page=page_no,
        page_size=size,
    )
    fresh = _fresh_hint(tab, params, rows, total, page_no, chosen_status)
    return page(title, tab, f"{form}{fresh}{table}{pager_html}")


def _filled(value) -> str:
    text = "" if value is None else str(value).strip()
    return esc(text) if text else "<span class='muted'>—</span>"


def _meta_table(pairs: list[tuple[str, str]]) -> str:
    wide = {"最后错误", "幂等键", "同回合"}
    rows = []
    index = 0

    def cell(label: str, html: str, span: bool = False) -> str:
        extra = " colspan='3'" if span else ""
        return f"<th>{esc(label)}</th><td{extra}>{html}</td>"

    while index < len(pairs):
        label, html = pairs[index]
        nxt = pairs[index + 1] if index + 1 < len(pairs) else None
        if label in wide or nxt is None or nxt[0] in wide:
            rows.append(f"<tr>{cell(label, html, True)}</tr>")
            index += 1
        else:
            rows.append(f"<tr>{cell(label, html)}{cell(nxt[0], nxt[1])}</tr>")
            index += 2
    return f"<table class='summary'>{''.join(rows)}</table>"


def _cited_from_statements(statements: list[dict]) -> list[dict]:
    cited: list[dict] = []
    index: dict[str, dict] = {}
    for item in statements:
        cid = item.get("crystal_id")
        if not cid:
            continue
        row = index.get(cid)
        if row is None:
            row = {"id": cid, "name": item.get("crystal_name") or cid, "passages": []}
            index[cid] = row
            cited.append(row)
        sentence = str(item.get("text") or "").strip()
        if sentence and sentence not in {"尚无来源", "暂无来源", "已忘掉"} and sentence not in row["passages"]:
            row["passages"].append(sentence)
    return cited


def _cited_html(cited: list[dict]) -> str:
    if not cited:
        return "<p class='muted'>还没有结晶关联这份材料。</p>"
    items = []
    for item in cited:
        passages = "".join(f"<li>{esc(sentence)}</li>" for sentence in item.get("passages") or [])
        quote = f"<ul>{passages}</ul>" if passages else ""
        items.append(
            f"<li><a href='/admin/crystals/{esc(item['id'])}'>{esc(item.get('name') or item['id'])}</a>{quote}</li>"
        )
    return f"<ul class='cited'>{''.join(items)}</ul>"


def source_detail(row: dict, statements: list[dict], mate: dict | None, origin: str = "") -> str:
    kind = row.get("kind") or ""
    if origin not in {"file", "session"}:
        origin = "file" if kind == "file" else "session"
    mate_link = "<span class='muted'>—</span>"
    if mate:
        mate_link = f"<a href='/admin/sources/{esc(mate['id'])}?from={esc(origin)}'>{esc(KIND.get(mate['kind'], mate['kind']))} {esc(mate['id'])}</a>"
    title = "文件资料" if origin == "file" else "原始会话"
    status = row.get("status") or ""
    if origin == "file":
        pairs = [
            ("类别", _filled(KIND.get(kind, kind))),
            ("文件名", _filled(row.get("filename"))),
            ("来源编号", _filled(row.get("id"))),
            ("提交时间", when(row.get("ingested_at")) or "<span class='muted'>—</span>"),
            ("设备名称", _filled(display_device_name(row.get("device_name")))),
            ("智能体", _filled(row.get("agent_name"))),
            ("处理状态", flow_html(row.get("pipeline") or "", status, row.get("last_error") or "")),
            ("尝试次数", _filled(row.get("attempt"))),
            ("最后错误", _filled(row.get("last_error"))),
            ("幂等键", _filled(row.get("idempotency_key"))),
        ]
    else:
        pairs = [
            ("类别", _filled(KIND.get(kind, kind))),
            ("来源编号", _filled(row.get("id"))),
            ("会话编号", _session_cell(row)),
            ("回合编号", _turn_cell(row)),
            ("消息序号", _filled(row.get("msg_seq"))),
            ("提交时间", when(row.get("ingested_at")) or "<span class='muted'>—</span>"),
            ("事件时间", when(row.get("event_at")) or "<span class='muted'>—</span>"),
            ("设备名称", _filled(display_device_name(row.get("device_name")))),
            ("设备标识", _filled(row.get("device_id"))),
            ("智能体", _filled(row.get("agent_name"))),
            ("智能体标识", _filled(row.get("agent_id"))),
            ("关联工程", _filled(row.get("project_ref"))),
            ("处理状态", flow_html(row.get("pipeline") or "", status, row.get("last_error") or "")),
            ("尝试次数", _filled(row.get("attempt"))),
            ("最后错误", _filled(row.get("last_error"))),
            ("幂等键", _filled(row.get("idempotency_key"))),
            ("同回合", mate_link),
        ]
    clock = _clock_note(row) if origin != "file" else ""
    cited = row.get("_cited")
    if cited is None:
        cited = _cited_from_statements(statements)
    has_file = origin == "file"
    part = row.get("_part") or "text"
    allowed = {"text", "preview", "crystals"} if has_file else {"text", "crystals"}
    if part not in allowed:
        part = "text"
    base = f"/admin/sources/{esc(row['id'])}?from={esc(origin)}"

    def piece(name: str, label: str) -> str:
        href = base if name == "text" else base + f"&part={name}"
        return f"<a class='{'on' if part == name else ''}' href='{href}'>{label}</a>"

    if part == "preview":
        switched = file_preview.preview_body(
            row["id"], str(row.get("filename") or ""), int(row.get("_file_size") or 0)
        )
    elif part == "text":
        switched = _marked_body(row, statements)
    else:
        switched = _cited_html(cited)
    # 原始会话没有原文件，不摆「文件预览」这个入口。
    tabs = [piece("text", "文本解析")]
    if has_file:
        tabs.append(piece("preview", "文件预览"))
    tabs.append(piece("crystals", "关联结晶"))
    section_tabs = "".join(tabs)
    body = f"""<div class='page-head'><a href='/admin/sources?tab={esc(origin)}'>返回列表</a><button type='button' onclick='post("/admin/api/sources/{esc(row['id'])}/retry")'>重试</button></div>
{_source_cost_banner(row)}
<div class='panel'>
{_meta_table(pairs)}
{clock}
<nav class='wiki-tabs'>{section_tabs}</nav>
{switched}
</div>"""
    return page(title, origin, body)


def _clock_note(row: dict) -> str:
    """Say what the two clocks are. Speech time is not the moment the file arrived."""
    basis = str(row.get("event_basis") or "")
    if basis == "file":
        return (
            "<p class='note'>事件时间是文件最后写入的时间，记录里没有每一句的发言时间。"
            "它不是提交时间。提交时间只表示这条材料何时进入记忆库。</p>"
        )
    if str(row.get("event_at") or "").strip():
        return (
            "<p class='note'>事件时间是这句话发生的时间。提交时间是它进入记忆库的时间。"
            "判断是不是最近的状态，看事件时间。</p>"
        )
    return "<p class='note'>这条没有记下事件时间。提交时间不能用来代替它。</p>"


def _crystal_query(type_name: str, status: str, view: str = "", page: int = 1) -> str:
    parts = []
    if type_name:
        parts.append("type=" + esc(type_name))
    if status and status != "active":
        parts.append("status=" + esc(status))
    if view:
        parts.append("view=" + esc(view))
    if page > 1:
        parts.append("page=" + esc(str(page)))
    return ("?" + "&".join(parts)) if parts else ""


_NAV_FOLD_BOOT = """<script>
try {
  var savedNav = localStorage.getItem("rogmem-crystal-nav");
  if (savedNav === "left" || savedNav === "right") {
    document.documentElement.classList.add("nav-collapsed");
    if (savedNav === "right") document.documentElement.classList.add("nav-dock-right");
  }
} catch (err) {}
</script>"""



_NAV_FOLD_SCRIPT = """<script>
(function () {
  var key = "rogmem-crystal-nav";
  var sideKey = "rogmem-crystal-nav-side";
  var root = document.documentElement;
  var fold = document.getElementById("crystal-nav-fold");
  var aside = fold && fold.closest(".wiki-side");
  if (!fold || !aside) return;
  var dock = "left";
  try {
    var savedSide = localStorage.getItem(sideKey);
    if (savedSide === "left" || savedSide === "right") dock = savedSide;
    else if (root.classList.contains("nav-dock-right")) dock = "right";
  } catch (err) {}
  function collapsed() { return root.classList.contains("nav-collapsed"); }
  function paint(isCollapsed, nextDock) {
    if (nextDock === "left" || nextDock === "right") dock = nextDock;
    root.classList.toggle("nav-collapsed", isCollapsed);
    root.classList.toggle("nav-dock-right", isCollapsed && dock === "right");
    fold.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
    fold.setAttribute("aria-label", isCollapsed ? "展开导航" : "收起导航");
    fold.title = isCollapsed ? "展开导航，拖动可贴到另一侧" : "收起导航";
    aside.style.left = "";
    aside.style.right = "";
    aside.style.top = "";
    try {
      localStorage.setItem(key, isCollapsed ? dock : "open");
      localStorage.setItem(sideKey, dock);
    } catch (err) {}
  }
  if (collapsed()) paint(true, dock);
  var dragged = false;
  var tracking = false;
  var suppress = false;
  var startX = 0;
  var startY = 0;
  function place(clientX) {
    var width = aside.getBoundingClientRect().width || 32;
    var next = clientX - width / 2;
    var max = Math.max(0, window.innerWidth - width);
    aside.style.left = Math.min(max, Math.max(0, next)) + "px";
    aside.style.right = "auto";
  }
  fold.addEventListener("pointerdown", function (event) {
    suppress = false;
    dragged = false;
    if (!collapsed() || event.button !== 0) return;
    tracking = true;
    startX = event.clientX;
    startY = event.clientY;
    try { fold.setPointerCapture(event.pointerId); } catch (err) {}
  });
  window.addEventListener("pointermove", function (event) {
    if (!tracking || !collapsed()) return;
    if (Math.hypot(event.clientX - startX, event.clientY - startY) < 6) return;
    dragged = true;
    place(event.clientX);
  });
  window.addEventListener("pointerup", function (event) {
    if (!tracking) return;
    tracking = false;
    if (!dragged) return;
    suppress = true;
    paint(true, event.clientX < window.innerWidth / 2 ? "left" : "right");
  });
  window.addEventListener("pointercancel", function () {
    if (!tracking) return;
    tracking = false;
    if (!dragged) return;
    suppress = true;
    paint(true, dock);
  });
  fold.addEventListener("click", function () {
    if (suppress) {
      suppress = false;
      dragged = false;
      return;
    }
    paint(!collapsed(), dock);
  });
})();
</script>"""


def _write_stamp(label: str, value) -> str:
    shown = when(value)
    if not shown:
        shown = "<span>—</span>"
    return f"<span>{label}：{shown}</span>"


# First paint keeps a short card page. The rail still lists every revision.
REVISION_LOG_PAGE = 40


def _entry_head(detail: dict, lock: str) -> str:
    """Title, then the type row. Body and sketch clocks sit on the right of that row."""
    kind = f"{esc(detail['type'])} · {esc(detail.get('status_label') or detail['status'])}"
    freshness = (
        "<span class='freshness'>"
        + _write_stamp("正文更新", detail.get("body_updated_at"))
        + _write_stamp("速写更新", detail.get("sketch_updated_at"))
        + "</span>"
    )
    return (
        f"<div class='entry-head'><h1>{esc(detail['name'])}{lock}</h1>"
        f"<p class='meta'><span class='kind'>{kind}</span>{freshness}</p></div>"
    )


def crystal_page(
    detail: dict,
    revisions: list[dict],
    nav: list[dict],
    type_name: str,
    status: str,
    view: str = "body",
    nav_more: dict | None = None,
    editing: bool = False,
    auto_labels: bool = False,
    label_budget: int = 50,
) -> str:
    types = ["", "人物", "工程", "组织", "产品", "概念"]
    statuses = [("active", "活跃"), ("已合并", "已合并"), ("已撤回", "已撤回"), ("全部", "全部")]
    type_options = "".join(
        f"<option value='{esc(item)}'{' selected' if item == type_name else ''}>{esc(item or '全部类型')}</option>"
        for item in types
    )
    status_options = "".join(
        f"<option value='{esc(value)}'{' selected' if value == status else ''}>{esc(label)}</option>"
        for value, label in statuses
    )
    names = "".join(
        f"<a data-id='{esc(row['id'])}' class='{'on' if row['id'] == detail['id'] else ''}' href='/admin/crystals/{esc(row['id'])}{_crystal_query(type_name, status)}'>{esc(row['name'])}</a>"
        for row in nav
    ) or "<p class='muted'>没有符合筛选的结晶。</p>"
    more = nav_more or {}
    total = int(more.get("total") or len(nav))
    nxt = int(more.get("next") or len(nav))
    nav_attrs = (
        f" id='crystal-nav' data-offset='{nxt}' data-total='{total}' data-type='{esc(type_name)}'"
        f" data-status='{esc(status)}' data-current='{esc(detail['id'])}'"
    )
    nav_script = ""
    if nxt < total:
        nav_script = """
<script>
(function () {
  var nav = document.getElementById("crystal-nav");
  if (!nav) return;
  var side = nav.closest(".wiki-side");
  var sentinel = document.createElement("div");
  nav.after(sentinel);
  var loading = false;
  function query() {
    var params = new URLSearchParams();
    params.set("offset", nav.getAttribute("data-offset") || "0");
    params.set("type", nav.getAttribute("data-type") || "");
    params.set("status", nav.getAttribute("data-status") || "active");
    return params.toString();
  }
  function linkQuery() {
    var type = nav.getAttribute("data-type") || "";
    var status = nav.getAttribute("data-status") || "active";
    var bits = [];
    if (type) bits.push("type=" + encodeURIComponent(type));
    if (status && status !== "active") bits.push("status=" + encodeURIComponent(status));
    return bits.length ? "?" + bits.join("&") : "";
  }
  function pump() {
    var offset = Number(nav.getAttribute("data-offset") || "0");
    var all = Number(nav.getAttribute("data-total") || "0");
    if (loading || offset >= all) {
      observer.disconnect();
      return;
    }
    loading = true;
    fetch("/admin/api/crystals/nav?" + query()).then(function (response) {
      if (!response.ok) throw new Error("nav");
      return response.json();
    }).then(function (data) {
      var current = nav.getAttribute("data-current") || "";
      var suffix = linkQuery();
      (data.items || []).forEach(function (item) {
        if (!item || nav.querySelector("[data-id='" + item.id + "']")) return;
        var link = document.createElement("a");
        link.setAttribute("data-id", item.id);
        link.href = "/admin/crystals/" + encodeURIComponent(item.id) + suffix;
        link.textContent = item.name || "";
        if (item.id === current) link.className = "on";
        nav.appendChild(link);
      });
      nav.setAttribute("data-offset", String(data.next || offset));
      nav.setAttribute("data-total", String(data.total || all));
      loading = false;
      if (Number(nav.getAttribute("data-offset")) >= Number(nav.getAttribute("data-total"))) observer.disconnect();
    }).catch(function () { loading = false; });
  }
  var observer = new IntersectionObserver(function (entries) {
    if (entries[0] && entries[0].isIntersecting) pump();
  }, {root: side, rootMargin: "120px"});
  observer.observe(sentinel);
})();
</script>"""
    nav_html = (
        "<div class='nav-head'>"
        "<p class='wiki-label'>结晶导航</p>"
        "<button type='button' class='quiet nav-fold' id='crystal-nav-fold' aria-expanded='true' aria-controls='crystal-nav-body' aria-label='收起导航' title='收起导航'>"
        "<svg class='fold-close' xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><rect width='18' height='18' x='3' y='3' rx='2'/><path d='M9 3v18'/><path d='m16 15-3-3 3-3'/></svg>"
        "<span class='fold-name'>导航</span>"
        "</button>"
        "</div>"
        "<div id='crystal-nav-body'>"
        f"<form class='wiki-filter' method='get' action='/admin/crystals/{esc(detail['id'])}'>"
        f"<select name='type' onchange='this.form.requestSubmit()'>{type_options}</select>"
        f"<select name='status' onchange='this.form.requestSubmit()'>{status_options}</select>"
        f"<button class='quiet' type='submit'>筛选</button></form>"
        f"<nav class='names'{nav_attrs}>{names}</nav>{nav_script}"
        "</div>"
        + _NAV_FOLD_SCRIPT
    )
    filled_summary = {
        key: value
        for key, value in detail["summary"].items()
        if key != "概述" and not _is_placeholder(value)
    }
    filled_chapters = {key: value for key, value in detail["chapters"].items() if not _is_placeholder(value)}
    summary = _summary_table(filled_summary, detail.get("field_marks") or {})
    chapters = "".join(
        f"<section id='c-{esc(k)}'><h2>{esc(k)}</h2><p>{_chapter_html(v, detail.get('link_targets') or {})}{_cite_marks(detail.get('chapter_marks', {}).get(k) or [])}</p></section>"
        for k, v in filled_chapters.items()
    )
    toc = "".join(f"<a href='#s-{esc(k)}'>{esc(k)}</a>" for k in filled_summary) + "".join(
        f"<a href='#c-{esc(k)}'>{esc(k)}</a>" for k in filled_chapters
    )
    if revisions:
        newest_first = list(reversed(revisions))
        first_page = newest_first[:REVISION_LOG_PAGE]
        more = len(newest_first) > REVISION_LOG_PAGE
        list_attrs = (
            f" class='rev-log' data-crystal='{esc(detail['id'])}' data-name='{esc(detail.get('name') or '')}'"
            f" data-rev-offset='{len(first_page)}' data-rev-total='{len(newest_first)}'"
        )
        cards = "".join(
            _revision_item(detail["id"], item, index, detail.get("name") or "")
            for index, item in enumerate(first_page)
        )
        sentinel = "<div id='rev-more' aria-hidden='true'></div>" if more else ""
        logs = (
            "<div class='rev-sheet'>"
            + _revision_rail(newest_first)
            + f"<ol{list_attrs}>{cards}</ol>"
            + sentinel
            + _revision_detail_modal()
            + _revision_rail_script()
            + (_revision_more_script() if more else "")
            + "</div>"
        )
    else:
        logs = "<p class='muted'>还没有修订。</p>"
    q_body = _crystal_query(type_name, status)
    q_graph = _crystal_query(type_name, status, "graph")
    q_log = _crystal_query(type_name, status, "log")
    edit_href = f"/admin/crystals/{esc(detail['id'])}{q_body}{'&' if q_body else '?'}edit=1"
    show_href = f"/admin/crystals/{esc(detail['id'])}{q_body}"
    toggle = (
        f"<a class='edit-toggle on' href='{show_href}'>取消</a>"
        if editing
        else f"<a class='edit-toggle' href='{edit_href}'>编辑</a>"
    )
    tabs = (
        f"<nav class='wiki-tabs'><a class='{'on' if view == 'body' or editing else ''}' href='{show_href}'>词条详情</a>"
        f"<a class='{'on' if view == 'graph' and not editing else ''}' href='/admin/crystals/{esc(detail['id'])}{q_graph}'>关联图谱</a>"
        f"<a class='{'on' if view == 'log' and not editing else ''}' href='/admin/crystals/{esc(detail['id'])}{q_log}'>修订日志</a>"
        f"{toggle}</nav>"
    )
    overview = _lede_html(detail.get("lede") or [])
    quiet = " muted" if overview == "尚无来源" else ""
    lock = ""
    if detail.get("id") == "owner":
        tip = "本词条是专为记忆主人预制的特殊词条，系统将自动维护，无法删除。"
        lock = (
            f"<span class='owner-lock' title='{esc(tip)}' aria-label='{esc(tip)}'>"
            "<svg viewBox='0 0 24 24' width='18' height='18' aria-hidden='true'>"
            "<path fill='currentColor' d='M17 8h-1V6a4 4 0 0 0-8 0v2H7a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2zm-7-2a2 2 0 1 1 4 0v2h-4V6zm7 14H7V10h10v10zm-5-3a2 2 0 1 0 0-4 2 2 0 0 0 0 4z'/>"
            "</svg></span>"
        )
    has_body = bool((detail.get("body_md") or "").strip())
    head = _entry_head(detail, lock)
    lede_block = "" if has_body else f"<aside class='lede{quiet}'><b>概述</b><p>{overview}</p></aside>"
    if view == "graph":
        middle = lede_block + tabs + shared_graph(
            "var selfId = " + json_id(detail["id"]) + ";"
            "edges = edges.filter(function(e) { return e.src === selfId || e.dst === selfId; });"
            "var keep = Object.create(null); keep[selfId] = true; edges.forEach(function(e) { keep[e.src] = true; keep[e.dst] = true; });"
            "nodes = nodes.filter(function(n) { return keep[n.id]; });",
            auto_labels=auto_labels,
            label_budget=label_budget,
        )
        side = ""
        shell = "wiki solo"
    elif view == "log":
        middle = lede_block + tabs + f"<h2>修订日志</h2>{logs}"
        side = ""
        shell = "wiki solo"
    elif editing:
        middle = lede_block + tabs + _markdown_editor(detail, show_href)
        side = ""
        shell = "wiki solo"
    else:
        body_md = (detail.get("body_md") or "").strip()
        toc = ""
        if has_body:
            from rogmem.pages_md import render_markdown

            sketch = (detail.get("sketch_md") or "").strip()
            sketch_html = (
                f"<aside class='lede' id='sketch'><b>结晶速写</b>{render_markdown(sketch, detail.get('link_targets') or {}, detail.get('source_marks') or {})}</aside>"
                if sketch
                else "<aside class='lede muted' id='sketch'><b>结晶速写</b><p>还没有写。</p></aside>"
            )
            toc = _markdown_toc(body_md)
            middle = (
                lede_block
                + tabs
                + sketch_html
                + f"<article class='md-body'>{render_markdown(body_md, detail.get('link_targets') or {}, detail.get('source_marks') or {})}</article>"
            )
        else:
            parts = []
            if summary:
                parts.append(f"<h2>已写下的要点</h2>{summary}")
            if chapters:
                parts.append(f"<h2>正文</h2>{chapters}")
            if not parts:
                parts.append("<p class='muted'>还没有正文。</p>")
            middle = lede_block + tabs + "".join(parts)
        side = (
            "<aside class='toc-dock'>"
            "<button type='button' class='toc-tab' aria-expanded='false'>目录</button>"
            f"<div class='toc-panel'>{_toc_group('目录', 'toc-label-contents', toc)}"
            f"{_wiki_links(detail)}</div></aside>"
        )
        shell = "wiki solo"
    body = f"""{_NAV_FOLD_BOOT}<div class='reader-sheet'><div class='{shell}'>
<aside class='wiki-side'>{nav_html}</aside>
<div class='wiki-main'>{head}<article class='wiki-article'>{middle}</article></div>
{side}
</div></div>"""
    return page(detail["name"], "crystals", body, reader=True)


def _is_placeholder(value) -> bool:
    return str(value or "").strip() in {"", "尚无来源", "暂无来源"}


def _markdown_toc(body: str) -> str:
    links = ["<a href='#sketch'>结晶速写</a>"]
    for line in (body or "").splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
        elif line.startswith("# ") and not line.startswith("##"):
            title = line[2:].strip()
        else:
            continue
        if title:
            links.append(f"<a href='#{esc(title)}'>{esc(title)}</a>")
    return "".join(links)


def _markdown_editor(detail: dict, cancel_href: str) -> str:
    """Edit the page as the markdown that is stored. Saving does not rebuild a form."""
    cid = esc(detail["id"])
    address_field = ""
    if detail.get("type") == "工程":
        current = str((detail.get("summary") or {}).get("仓库地址") or "")
        if current in {"尚无来源", "暂无来源"}:
            current = ""
        others = str((detail.get("summary") or {}).get("其他仓库") or "")
        if others in {"尚无来源", "暂无来源"}:
            others = ""
        extra = f"<p class='note'>也曾用过：{esc(others)}</p>" if others else ""
        address_field = (
            f"<label class='wide'>仓库地址<input data-address value='{esc(current)}'></label>"
            "<p class='note'>改这一格会换上新的当前地址，原来的地址还留着，用来认出同一工程。留空不会删掉它。</p>"
            + extra
        )
    return f"""<form class='edit-sheet' onsubmit="event.preventDefault(); saveMarkdown(this);">
<label class='wide'>结晶速写<textarea data-sketch rows='6'>{esc(detail.get('sketch_md') or '')}</textarea></label>
<label class='wide'>正文<textarea data-markdown rows='18'>{esc(detail.get('body_md') or '')}</textarea></label>
{address_field}
<div class='edit-actions wide'>
<button type='submit'>保存</button>
<a href='{cancel_href}'>取消</a>
<span class='note'>保存的就是这份 Markdown。</span>
</div>
</form>
<script>
async function saveMarkdown(form) {{
  const payload = {{
    markdown: form.querySelector('[data-markdown]').value,
    sketch: form.querySelector('[data-sketch]').value
  }};
  const address = form.querySelector('[data-address]');
  if (address) payload.address = address.value;
  const response = await fetch('/admin/api/crystals/{cid}/body', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify(payload)
  }});
  if (response.status === 401) location = '/admin/login';
  if (!response.ok) {{
    alert((await response.text()) || '没有完成');
    return;
  }}
  location = '{cancel_href}';
}}
</script>"""


def _one_sentence(text: str) -> str:
    compact = " ".join(str(text or "").split())
    if not compact:
        return "尚无来源"
    if len(compact) <= 180:
        return compact
    count = 0
    for index, char in enumerate(compact):
        if char in "。！？!?":
            count += 1
            if count == 2:
                return compact[: index + 1]
    return compact[:180]


def _toc_group(label: str, label_id: str, inner: str) -> str:
    return (
        f"<section class='toc-group' aria-labelledby='{label_id}'>"
        f"<p class='wiki-label' id='{label_id}'>{label}</p>"
        f"<nav class='toc'>{inner}</nav></section>"
    )


def _wiki_links(detail: dict) -> str:
    def items(rows: list[dict]) -> str:
        if not rows:
            return "<p class='muted'>没有</p>"
        return "".join(
            f"<a href='/admin/crystals/{esc(row['id'])}'>{esc(row['name'])}</a>" for row in rows
        )

    return _toc_group("链接", "toc-label-out", items(detail.get("out_links") or [])) + _toc_group(
        "反向链接", "toc-label-in", items(detail.get("in_links") or [])
    )


def _chapter_html(text: str, targets: dict | None = None) -> str:
    lines = []
    for line in str(text or "").split("\n"):
        if line.startswith("~~") and "~~" in line[2:]:
            inner, _, rest = line[2:].partition("~~")
            lines.append(f"<s>{esc(inner)}</s>{esc(rest)}")
        else:
            lines.append(_wikilink_html(line, targets or {}))
    return "<br>".join(lines)


def _wikilink_html(text: str, targets: dict) -> str:
    parts = []
    cursor = 0
    for match in re.finditer(r"\[\[([^\[\]\n]{1,80})\]\]", text or ""):
        parts.append(esc(text[cursor : match.start()]))
        label = match.group(1).strip()
        dest = targets.get(label)
        if dest:
            parts.append(f"<a href='/admin/crystals/{esc(dest)}'>{esc(label)}</a>")
        else:
            parts.append(esc(match.group(0)))
        cursor = match.end()
    parts.append(esc((text or "")[cursor:]))
    return "".join(parts)


def _cite_marks(items: list[dict]) -> str:
    seen = []
    marks = []
    for item in items or []:
        number = item.get("n")
        if number in seen or not item.get("source_id"):
            continue
        seen.append(number)
        quote = " ".join(str(item.get("quote") or "").split())
        if len(quote) > 80:
            quote = quote[:80] + "…"
        href = item.get("session_href") or item.get("source_href") or ""
        if not href:
            href = f"/admin/sources/{esc(item['source_id'])}?cite={esc(item.get('id') or '')}#cite-{esc(item.get('id') or '')}"
        marks.append(f"<sup class='cite'><a href='{esc(href)}' title='{esc(quote)}'>{int(number)}</a></sup>")
    return "".join(marks)


def _lede_html(parts: list[dict]) -> str:
    if not parts:
        return "尚无来源"
    return "".join(esc(part.get("text") or "") + _cite_marks(part.get("marks") or []) for part in parts)


def _revision_gist(item: dict) -> str:
    from rogmem.engine import revision_gist_for

    return revision_gist_for(item)


def _revision_anchor(item: dict, index: int) -> str:
    raw = item.get("id")
    if raw is None or str(raw).strip() == "":
        return f"rev-n{index}"
    return f"rev-{int(raw)}"


def _revision_rail(items: list[dict]) -> str:
    ticks = []
    for index, item in enumerate(items):
        anchor = _revision_anchor(item, index)
        gist = _revision_gist(item) or "这一笔没有写下摘要。"
        mark = " on" if index == 0 else ""
        ticks.append(
            f"<button type='button' class='rev-tick{mark}' data-target='{esc(anchor)}' aria-label='{esc(gist)}'>"
            f"<i></i><span class='rev-tip'>{esc(gist)}</span></button>"
        )
    return f"<nav class='rev-rail' aria-label='修订定位'>{''.join(ticks)}</nav>"


def revision_cards_html(crystal_id: str, items: list[dict], *, name: str = "", start_index: int = 0) -> str:
    """HTML for one thin page of revision cards. Newest-first order is the caller's job."""
    return "".join(
        _revision_item(crystal_id, item, start_index + index, name)
        for index, item in enumerate(items)
    )


def _revision_more_script() -> str:
    return f"""
<script>
(function () {{
  var list = document.querySelector("ol.rev-log");
  var sentinel = document.getElementById("rev-more");
  if (!list || !sentinel) return;
  var loading = false;
  function pump() {{
    var offset = Number(list.getAttribute("data-rev-offset") || "0");
    var total = Number(list.getAttribute("data-rev-total") || "0");
    var crystal = list.getAttribute("data-crystal") || "";
    if (loading || !crystal || offset >= total) {{
      if (offset >= total) observer.disconnect();
      return;
    }}
    loading = true;
    var params = new URLSearchParams();
    params.set("offset", String(offset));
    params.set("limit", String({REVISION_LOG_PAGE}));
    fetch("/admin/api/crystals/" + encodeURIComponent(crystal) + "/revision-cards?" + params.toString())
      .then(function (response) {{
        if (!response.ok) throw new Error("cards");
        return response.text();
      }})
      .then(function (html) {{
        var wrap = document.createElement("div");
        wrap.innerHTML = html;
        while (wrap.firstChild) list.appendChild(wrap.firstChild);
        var next = offset + {REVISION_LOG_PAGE};
        list.setAttribute("data-rev-offset", String(Math.min(next, total)));
        loading = false;
        if (Number(list.getAttribute("data-rev-offset")) >= total) observer.disconnect();
      }})
      .catch(function () {{ loading = false; }});
  }}
  var root = list.closest(".wiki-article");
  var observer = new IntersectionObserver(function (entries) {{
    if (entries[0] && entries[0].isIntersecting) pump();
  }}, {{ root: root, rootMargin: "160px" }});
  observer.observe(sentinel);
}})();
</script>"""


def _revision_rail_script() -> str:
    return """
<script>
(function () {
  var sheet = document.querySelector(".rev-sheet");
  if (!sheet) return;
  var scroller = sheet.closest(".wiki-article");
  var rail = sheet.querySelector(".rev-rail");
  var ticks = Array.prototype.slice.call(sheet.querySelectorAll(".rev-tick"));
  function mark(id) {
    if (!id) return;
    ticks.forEach(function (tick) {
      tick.classList.toggle("on", tick.getAttribute("data-target") === id);
    });
  }
  function fit() {
    if (!rail || !scroller) return;
    var height = scroller.getBoundingClientRect().bottom - rail.getBoundingClientRect().top - 8;
    rail.style.height = Math.max(96, height) + "px";
  }
  function readingId() {
    if (!scroller) return "";
    var rootTop = scroller.getBoundingClientRect().top;
    var best = "";
    var bestTop = Infinity;
    sheet.querySelectorAll(".rev").forEach(function (el) {
      var rect = el.getBoundingClientRect();
      var top = rect.top - rootTop;
      var bottom = rect.bottom - rootTop;
      if (bottom <= 0 || top >= scroller.clientHeight) return;
      if (top < bestTop) {
        bestTop = top;
        best = el.id;
      }
    });
    return best;
  }
  function placeTip(tick) {
    var tip = tick.querySelector(".rev-tip");
    if (!tip || !scroller) return;
    var mid = scroller.getBoundingClientRect().top + scroller.clientHeight * 0.65;
    if (tick.getBoundingClientRect().top > mid) {
      tip.style.top = "auto";
      tip.style.bottom = "0";
    } else {
      tip.style.top = "0";
      tip.style.bottom = "auto";
    }
  }
  function wave(index) {
    ticks.forEach(function (tick, i) {
      var distance = Math.abs(i - index);
      tick.classList.toggle("hot", distance === 0);
      tick.classList.toggle("wave-1", distance === 1);
      tick.classList.toggle("wave-2", distance === 2);
      tick.classList.toggle("wave-3", distance === 3);
    });
  }
  var fromKeyboard = false;
  function restWave() {
    ticks.forEach(function (tick) {
      tick.classList.remove("hot", "wave-1", "wave-2", "wave-3");
    });
  }
  window.addEventListener("keydown", function (event) {
    if (event.key === "Tab") fromKeyboard = true;
  });
  window.addEventListener("pointerdown", function () { fromKeyboard = false; });
  ticks.forEach(function (tick, index) {
    tick.addEventListener("pointerenter", function () {
      placeTip(tick);
      wave(index);
    });
    tick.addEventListener("focus", function () {
      placeTip(tick);
      if (fromKeyboard) wave(index);
    });
    tick.addEventListener("blur", function (event) {
      var next = event.relatedTarget;
      if (next && rail && rail.contains(next)) return;
      if (rail && rail.matches(":hover")) return;
      restWave();
    });
    tick.addEventListener("click", function () {
      var el = document.getElementById(tick.getAttribute("data-target"));
      if (!el || !scroller) return;
      var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      var top = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 8;
      scroller.scrollTo({ top: Math.max(0, top), behavior: reduce ? "auto" : "smooth" });
      mark(tick.getAttribute("data-target"));
    });
  });
  if (rail) {
    rail.addEventListener("pointermove", function (event) {
      var node = event.target && event.target.closest ? event.target.closest(".rev-tick") : null;
      if (!node) return;
      var index = ticks.indexOf(node);
      if (index >= 0) wave(index);
    });
    rail.addEventListener("pointerleave", function () {
      if (fromKeyboard && rail.matches(":focus-within")) {
        var index = ticks.indexOf(document.activeElement);
        if (index >= 0) wave(index);
        else restWave();
        return;
      }
      restWave();
    });
  }
  if (scroller) scroller.addEventListener("scroll", function () { mark(readingId()); }, { passive: true });
  window.addEventListener("resize", fit);
  fit();
  mark(readingId());
  var detail = document.getElementById("rev-detail");
  var detailBody = document.getElementById("rev-detail-body");
  var detailClose = document.getElementById("rev-detail-close");
  var detailOpener = null;
  var detailToken = 0;
  function closeDetail() {
    detailToken += 1;
    if (!detail) return;
    detail.hidden = true;
    if (detailBody) detailBody.replaceChildren();
    if (detailOpener) {
      detailOpener.focus();
      detailOpener = null;
    }
  }
  function openDetail(button) {
    var crystal = button.getAttribute("data-crystal");
    var rev = button.getAttribute("data-rev");
    if (!detail || !detailBody || !crystal || !rev) return;
    var mine = ++detailToken;
    detailBody.textContent = "正在读取";
    detail.hidden = false;
    detailOpener = button;
    if (detailClose) detailClose.focus();
    fetch("/admin/api/crystals/" + encodeURIComponent(crystal) + "/revisions/" + encodeURIComponent(rev))
      .then(function (response) {
        if (!response.ok) throw new Error("missing");
        return response.text();
      })
      .then(function (html) {
        if (mine !== detailToken) return;
        detailBody.innerHTML = html;
        if (typeof formatSourceClocks === "function") formatSourceClocks(detailBody);
      })
      .catch(function () {
        if (mine !== detailToken) return;
        detailBody.textContent = "没有打开这一条。";
      });
  }
  sheet.addEventListener("click", function (event) {
    var button = event.target && event.target.closest ? event.target.closest(".rev-open") : null;
    if (!button) return;
    openDetail(button);
  });
  if (detailClose) detailClose.addEventListener("click", closeDetail);
  if (detail) {
    detail.addEventListener("click", function (event) {
      if (event.target === detail) closeDetail();
    });
  }
  window.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && detail && !detail.hidden) {
      event.preventDefault();
      closeDetail();
    }
  });
})();
</script>"""


_REVISION_CHANGE_PREVIEW = 200
_REVISION_BASIS_PREVIEW = 100


def _preview_text(text: str, limit: int) -> str:
    """The opening of a reading. The detail shows that same reading without this cut."""
    body = str(text or "")
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "…"


def _revision_facts(item: dict, actor: str) -> str:
    event = f"<span class='muted'>事件 {when(item.get('event_at'))}</span>" if item.get("event_at") else ""
    return (
        f"<span><span class='rev-k'>修订时间</span>{when(item.get('created_at')) or '未记下'}</span>"
        f"{event}"
        f"<span><span class='rev-k'>修订人</span><span class='rev-who'>{esc(actor)}</span></span>"
        f"<span><span class='rev-k'>修订范围</span>{esc(item.get('scope') or '未记下范围')}</span>"
    )


def _revision_basis_view(item: dict) -> tuple[str, str, bool]:
    quote = str(item.get("quote") or "")
    source_id = str(item.get("source_id") or "")
    statement_id = str(item.get("statement_id") or "")
    stored = str(item.get("basis") or "").strip()
    note = str(item.get("note") or "")
    if stored:
        link = ""
        if source_id:
            href = item.get("source_href") or item.get("session_href") or f"/admin/sources/{source_id}"
            link = f" <a href='{esc(href)}'>打开出处</a>"
        return stored, link, False
    if quote and source_id:
        href = item.get("session_href") or item.get("source_href") or ""
        if not href:
            href = f"/admin/sources/{esc(source_id)}?cite={esc(statement_id)}#cite-{esc(statement_id)}"
        label = "会话" if item.get("session_id") or item.get("session_href") else "原文"
        return quote.strip(), f" <a href='{esc(href)}'>{label}</a>", True
    if note.startswith("回滚"):
        return "撤销这一版之后的改动。已忘掉的原文不写回来。", "", False
    if note.startswith("撤回"):
        return "工程没有仓库身份，不能作为工程发布。", "", False
    if "已开工" in note:
        return "后文点名这项并写明已经开工，所以从计划中挪到开发中。", "", False
    if "特性已划掉" in note:
        return "后文写明这项已经交付或放弃。划掉的仍留在原处。", "", False
    if note.startswith("清扫"):
        return "概述只保留当前仍然有效的陈述。", "", False
    if note == "人工修订正文":
        return "这一笔是在词条正文里直接改的。", "", False
    if note == "预写真名与别名":
        return "记忆主人还没有真名和别名时，写入默认值。", "", False
    return "", "", False


def _basis_body(plain: str, link: str, quoted: bool, limit: int | None) -> str:
    if not plain:
        return "<span class='muted'>这一条没有记下依据。</span>"
    shown = plain if limit is None else _preview_text(plain, limit)
    text = f"<q>{esc(shown)}</q>" if quoted else esc(shown)
    return text + link


def _revision_detail_modal() -> str:
    return (
        "<div id='rev-detail' class='modal' hidden>"
        "<div class='modal-card wide' role='dialog' aria-modal='true' aria-labelledby='rev-detail-title'>"
        "<div class='rev-detail-bar'>"
        "<h2 id='rev-detail-title'>修订详情</h2>"
        "<button type='button' class='quiet' id='rev-detail-close'>关闭</button>"
        "</div>"
        "<div id='rev-detail-body'></div>"
        "</div></div>"
    )


def _revision_change(item: dict) -> str:
    return str(item.get("change") or "").strip() or "这一笔没有写下改了什么。"


def _first_diff_block(change: str) -> str:
    """The first deletion and the write that follows it. Later blocks stay in the detail."""
    lines = str(change or "").splitlines()
    seen_delete = False
    seen_add = False
    kept: list[str] = []
    for line in lines:
        if line == "删去：" and (seen_delete or seen_add):
            break
        if line == "写入：" and seen_add:
            break
        if line == "删去：":
            seen_delete = True
        elif line == "写入：":
            seen_add = True
        kept.append(line)
    return "\n".join(kept).strip()


def _revision_reading(item: dict, actor: str, *, change_limit: int | None, basis_limit: int | None) -> str:
    """One reading of a revision. The card keeps the first block; the detail keeps every block."""
    change = _revision_change(item)
    plain, link, quoted = _revision_basis_view(item)
    gist = _revision_gist(item)
    gist_html = ""
    if gist and gist != change and gist != " ".join(change.split()):
        gist_html = f"<p class='rev-gist'><span class='rev-k'>摘要</span>{esc(gist)}</p>"
    if change_limit is None:
        shown_change = change
    else:
        shown_change = _preview_text(_first_diff_block(change), change_limit)
    return (
        f"{gist_html}"
        f"<p class='rev-change'><span class='rev-k'>修订内容</span>{esc(shown_change)}</p>"
        f"<p class='rev-basis'><span class='rev-k'>修订依据</span>{_basis_body(plain, link, quoted, basis_limit)}</p>"
    )


def revision_detail(item: dict) -> str:
    """The full reading. The card shows the gist and the first block; this keeps every block."""
    actor = str(item.get("actor") or "").strip() or "未记下"
    return f"<div class='rev-meta'>{_revision_facts(item, actor)}</div>" + _revision_reading(
        item, actor, change_limit=None, basis_limit=None
    )


def _revision_item(crystal_id: str, item: dict, index: int = 0, name: str = "") -> str:
    actor = str(item.get("actor") or "").strip() or "未记下"
    change = _revision_change(item)
    brief = " ".join(change.split())
    if len(brief) > 42:
        brief = brief[:42].rstrip() + "…"
    named = f"「{name}」" if str(name).strip() else "这一页"
    touched = f"「{brief}」" if brief and brief != "这一笔没有写下改了什么。" else "这一条"
    button = confirm_button(
        f"确定撤销{named}里{touched}之后的修订？这一页会退回到这一条。已经忘掉的原文不写回来。",
        f"/admin/api/crystals/{crystal_id}/rollback",
        {"revision_id": int(item["id"])},
        label="撤销修订",
        confirm="确认撤销",
        quiet=True,
        title="退回到这一条，并撤销它之后的改动",
    )
    opener = (
        "<button type='button' class='quiet rev-open' aria-haspopup='dialog' "
        "aria-controls='rev-detail' title='查看完整修订' "
        f"data-crystal='{esc(crystal_id)}' data-rev='{int(item['id'])}'>详情</button>"
    )
    anchor = _revision_anchor(item, index)
    return (
        f"<li class='rev' id='{esc(anchor)}'>"
        "<div class='rev-head'>"
        f"{_revision_facts(item, actor)}"
        f"<span class='rev-actions'>{opener}{button}</span></div>"
        + _revision_reading(
            item,
            actor,
            change_limit=_REVISION_CHANGE_PREVIEW,
            basis_limit=_REVISION_BASIS_PREVIEW,
        )
        + "</li>"
    )


def _marked_body(row: dict, statements: list[dict]) -> str:
    body = row.get("body") or ""
    limit = document_view.TEXT_RENDER_LIMIT
    truncated = len(body) > limit
    shown = body[:limit] if truncated else body
    cite = row.get("_cite") or ""
    quote = ""
    for item in statements:
        if item.get("id") == cite:
            quote = item.get("quote") or ""
            break
    note = (
        "<p class='note'>这份材料正文超过 " + str(limit)
        + " 字符，文本解析只显示开头，避免整本载入拖垮页面；完整内容请用「文件预览」按片查看，或下载原文件。</p>"
        if truncated else ""
    )
    if not cite or not quote or quote not in shown:
        return f"<pre class='src-body'>{esc(shown)}</pre>" + note
    pre, _, post = shown.partition(quote)
    return f"<pre class='src-body'>{esc(pre)}<mark id='cite-{esc(cite)}'>{esc(quote)}</mark>{esc(post)}</pre>" + note


def _origin_links_html(item: dict) -> str:
    bits = []
    session_href = item.get("session_href") or ""
    source_href = item.get("source_href") or ""
    if session_href:
        bits.append(f"<a href='{esc(session_href)}'>会话</a>")
    if source_href:
        bits.append(f"<a href='{esc(source_href)}'>原文</a>")
    if not bits:
        return ""
    return " " + " ".join(bits)


def _session_cell(row: dict) -> str:
    origin = row.get("_origin") or {}
    session_id = origin.get("session_id") or row.get("session_id") or ""
    href = origin.get("session_href") or ""
    if not session_id:
        return _filled("")
    if href:
        return f"<a href='{esc(href)}'>{esc(session_id)}</a>"
    return _filled(session_id)


def _turn_cell(row: dict) -> str:
    origin = row.get("_origin") or {}
    turn_id = origin.get("turn_id") or row.get("turn_id") or ""
    href = origin.get("session_href") or ""
    if not turn_id:
        return _filled("")
    if href:
        return f"<a href='{esc(href)}'>{esc(turn_id)}</a>"
    return _filled(turn_id)


def _summary_table(summary: dict, marks: dict | None = None) -> str:
    items = list(summary.items())
    rows = []
    index = 0

    def cell(key: str, value: str, span: bool = False) -> str:
        quiet = " class='muted'" if value in {"尚无来源", "暂无来源"} else ""
        extra = " colspan='3'" if span else ""
        cited = _cite_marks((marks or {}).get(key) or [])
        return f"<th id='s-{esc(key)}'>{esc(key)}</th><td{extra}{quiet}>{esc(value)}{cited}</td>"

    wide = {"兴趣爱好", "决策风格", "沟通风格", "当前关注", "约定待办"}

    def long(key: str, value: str) -> bool:
        return key in wide or len(value) > 40

    while index < len(items):
        key, value = items[index]
        nxt = items[index + 1] if index + 1 < len(items) else None
        if long(key, value) or nxt is None or long(nxt[0], nxt[1]):
            rows.append(f"<tr>{cell(key, value, span=True)}</tr>")
            index += 1
        else:
            rows.append(f"<tr>{cell(key, value)}{cell(nxt[0], nxt[1])}</tr>")
            index += 2
    return f"<table class='summary'>{''.join(rows)}</table>"


def json_id(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


_LOCAL_INSTALLER = "http://127.0.0.1:18780"

_HOST_FLEET_SCRIPT = r"""
<script>
(function () {
  var origin = "__ORIGIN__";
  var pending = __PROMPT_TEMPLATE__;
  var grantMark = "___ROGMEM_GRANT___";
  var ticketMark = "___ROGMEM_TICKET___";
  var entrancesMark = "___ROGMEM_ENTRANCES___";
  var button = document.getElementById("add-host");
  var modal = document.getElementById("add-host-modal");
  var status = document.getElementById("add-host-status");
  function fallbackCopy(text, done, statusEl) {
    var area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      done();
    } catch (err) {
      (statusEl || status).textContent = "没有复制成功，请再试一次。";
    }
    area.remove();
  }
  if (button && modal) {
    document.getElementById("add-host-close").onclick = function () { modal.hidden = true; };
    var copy = document.getElementById("add-host-copy");
    var prompt = document.getElementById("add-host-prompt");
    function pendingText() {
      return pending
        .split(grantMark).join("（这一次的准入凭证正在签发）")
        .split(ticketMark).join("（接入票据正在签发）")
        .split(entrancesMark).join("（入口清单正在签发）");
    }
    button.onclick = function () {
      modal.hidden = false;
      if (prompt) prompt.value = pendingText();
      if (copy) {
        copy.disabled = true;
        copy.textContent = "复制提示";
      }
      if (status) status.textContent = "提示已经在上面。准入票据还在签发，写好之前先不要复制。";
      fetch("/admin/api/enroll-grants", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"}).then(function (response) {
        if (!response.ok) throw new Error("ticket");
        return response.json();
      }).then(function (data) {
        if (!data || !data.ticket || !data.prompt) throw new Error("ticket");
        if (prompt) prompt.value = data.prompt;
        if (copy) copy.disabled = false;
        if (status) status.textContent = "贴给本机任意一个已安装的宿主。票据自带入口清单，已经接入的不会重发钥匙。";
      }).catch(function () {
        if (status) status.textContent = "提示在上面，但这一次的准入票据没有签发成功，请再试一次。";
      });
    };
    if (copy && prompt) {
      copy.onclick = function () {
        var text = prompt.value || "";
        var done = function () { copy.textContent = "已复制"; };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text, done); });
        } else {
          fallbackCopy(text, done);
        }
      };
    }
  }
  var bundleRev = __HOST_BUNDLE_REV__;
  var updateTemplate = __UPDATE_TEMPLATE__;
  var updateNote = document.getElementById("update-hosts-note");
  var updateButton = document.getElementById("update-hosts");
  var updateModal = document.getElementById("update-hosts-modal");
  function bundleSeen() {
    try { return localStorage.getItem("rogmem-host-bundle-seen") || ""; } catch (err) { return ""; }
  }
  function markBundleSeen() {
    try { localStorage.setItem("rogmem-host-bundle-seen", String(bundleRev)); } catch (err) {}
  }
  // The banner is decided on the server from what each host reported, not from this browser.
  if (updateButton && updateModal) {
    document.getElementById("update-hosts-close").onclick = function () { updateModal.hidden = true; };
    var updatePrompt = document.getElementById("update-hosts-prompt");
    var updateCopy = document.getElementById("update-hosts-copy");
    var updateStatusEl = document.getElementById("update-hosts-status");
    function fillUpdate() { return updateTemplate; }
    updateButton.onclick = function () {
      if (updatePrompt) updatePrompt.value = fillUpdate();
      updateModal.hidden = false;
      if (updateCopy) updateCopy.textContent = "复制提示";
      if (updateStatusEl) updateStatusEl.textContent = "贴给本机任意一个已安装的宿主。它会把本机所有已支持的宿主一起更新。";
      markBundleSeen();
    };
    if (updateCopy && updatePrompt) {
      updateCopy.onclick = function () {
        var text = updatePrompt.value || fillUpdate();
        var done = function () { updateCopy.textContent = "已复制"; markBundleSeen(); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text, done, updateStatusEl); });
        } else {
          fallbackCopy(text, done, updateStatusEl);
        }
      };
    }
  }
  var fleet = document.getElementById("host-fleet");
  function paintProgress(hosts) {
    if (!fleet) return;
    hosts.forEach(function (item) {
      if (!item || item.importable === undefined || item.importable === null) return;
      var rows = [];
      fleet.querySelectorAll("tr[data-program]").forEach(function (row) {
        if (row.getAttribute("data-program") === item.program) rows.push(row);
      });
      var local = rows.filter(function (row) { return row.getAttribute("data-local") === "1"; });
      var targets = local;
      if (!targets.length && rows.length === 1) targets = rows;
      if (!targets.length) targets = rows.filter(function (row) { return row.getAttribute("data-scan") === "1"; });
      targets.forEach(function (row) {
        // 评审 §6：只写本机扫描到的 pending，不覆盖同 td 下的作业状态。
        var cell = row.querySelector(".import-progress .scan-pending");
        if (!cell) return;
        var imported = Number(row.getAttribute("data-imported") || "0");
        var total = Number(item.importable);
        if (!isFinite(imported) || imported < 0) imported = 0;
        if (!isFinite(total)) return;
        var pending = total - imported;
        cell.textContent = String(pending > 0 ? pending : 0);
      });
    });
  }
  function markLocal(info) {
    if (!fleet || !info) return;
    var id = info.device_id || "";
    var name = info.device_name || "";
    var lowered = String(name).toLowerCase();
    if (!name || name === "本机" || lowered === "localhost" || lowered === "local" || name === "未命名设备") name = "";
    var rows = fleet.querySelectorAll("tr[data-device-id]");
    var idHit = false;
    if (id) {
      rows.forEach(function (row) {
        if (row.getAttribute("data-device-id") === id) idHit = true;
      });
    }
    if (!idHit) id = "";
    if (!id && name) {
      var seen = {};
      rows.forEach(function (row) {
        if (row.getAttribute("data-device-name") === name) seen[row.getAttribute("data-device-id")] = 1;
      });
      if (Object.keys(seen).length !== 1) name = "";
    }
    if (!id && !name) return;
    rows.forEach(function (row) {
      var hit = (id && row.getAttribute("data-device-id") === id) || (!id && name && row.getAttribute("data-device-name") === name);
      row.setAttribute("data-local", hit ? "1" : "0");
      var cell = row.querySelector(".agent-name");
      if (!cell) return;
      var base = cell.getAttribute("data-base") || "";
      cell.textContent = "";
      cell.appendChild(document.createTextNode(base));
      if (hit) {
        var mark = document.createElement("span");
        mark.className = "local-mark";
        mark.textContent = "（本机）";
        cell.appendChild(mark);
      }
    });
  }
  function pullWhoami() {
    var request;
    try {
      request = new Request(origin + "/client/whoami", {method: "GET", mode: "cors", targetAddressSpace: "loopback"});
    } catch (err) {
      request = origin + "/client/whoami";
    }
    return fetch(request).then(function (response) {
      if (!response.ok) throw new Error("who");
      return response.json();
    }).then(markLocal);
  }
  function loopbackPage() {
    var host = String(location.hostname || "").toLowerCase();
    return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
  }
  function whoamiPermission() {
    if (loopbackPage() || !navigator.permissions || !navigator.permissions.query) return Promise.resolve("unknown");
    return navigator.permissions.query({name: "loopback-network"}).then(function (status) {
      return (status && status.state) || "unknown";
    }).catch(function () { return "unknown"; });
  }
  var localHosts = [];
  function pullLocal() {
    return fetch(origin + "/client/hosts").then(function (response) {
      if (!response.ok) throw new Error("scan");
      return response.json();
    }).then(function (data) {
      localHosts = (data && data.hosts) || [];
    }).catch(function () { localHosts = []; });
  }
  // 用一个 viewmodel 存每个 agent 的作业快照，避免 DOM 被各 Promise 互相覆盖。
  var jobsByAgent = {};
  function _displayedAgentIds() {
    var ids = {};
    document.querySelectorAll("button.host-import-job[data-agent]").forEach(function (btn) {
      var a = btn.getAttribute("data-agent");
      if (a) ids[a] = 1;
    });
    return Object.keys(ids);
  }
  function pullJobs() {
    // R5 B：不裸 fetch 全历史。两步有界查询：
    //  1) 全局 status=pending 拿所有在途作业（不随单设备历史堆积而被挤掉）；
    //  2) 对本次正在显示的每个 agent，各取其最近 1 条 terminal/latest 补充状态列。
    //  任何设备的当前作业都可见；单设备历史再多也不会把别人挤出 UI。
    var agentIds = _displayedAgentIds();
    var pendingFetch = fetch("/admin/api/hosts/jobs?status=pending&limit=200")
      .then(function (r) { if (!r.ok) throw new Error("pending"); return r.json(); })
      .then(function (data) { return (data && data.jobs) || []; })
      .catch(function () { return []; });
    var latestFetches = agentIds.map(function (aid) {
      return fetch("/admin/api/hosts/jobs?agent_id=" + encodeURIComponent(aid) + "&limit=1")
        .then(function (r) { if (!r.ok) throw new Error("latest"); return r.json(); })
        .then(function (data) { return (data && data.jobs) || []; })
        .catch(function () { return []; });
    });
    return Promise.all([pendingFetch, Promise.all(latestFetches)]).then(function (parts) {
      var pending = parts[0];
      var latests = parts[1];
      var next = {};
      pending.forEach(function (job) {
        (next[job.agent_id] = next[job.agent_id] || []).push(job);
      });
      latests.forEach(function (arr) {
        arr.forEach(function (job) {
          var bucket = next[job.agent_id] = next[job.agent_id] || [];
          // 避免重复：若 pending 列表里已有同 id，不再加
          if (!bucket.some(function (j) { return j.id === job.id; })) bucket.push(job);
        });
      });
      jobsByAgent = next;
      paintJobs();
    });
  }
  function failText(reason) {
    if (reason && /[\u4e00-\u9fff]/.test(reason)) return "没能开始：" + reason;
    return "没能开始";
  }
  function writeStatus(btn, text, fail, reason) {
    var row = btn.closest("tr");
    var statusCell = row && row.querySelector("span.job-status");
    if (!statusCell) return;
    if (!statusCell.getAttribute("data-idle-title")) {
      statusCell.setAttribute("data-idle-title", statusCell.title || "");
    }
    statusCell.textContent = text;
    statusCell.classList.toggle("is-fail", !!fail);
    if (fail) {
      statusCell.setAttribute("data-note", "fail");
      statusCell.setAttribute("data-reason", reason || "");
      if (reason) statusCell.title = reason;
    } else {
      statusCell.removeAttribute("data-note");
      statusCell.removeAttribute("data-reason");
      var idle = statusCell.getAttribute("data-idle-title");
      if (idle) statusCell.title = idle;
    }
  }
  function paintJobs() {
    document.querySelectorAll("tr[data-device-id]").forEach(function (row) {
      var btn = row.querySelector("button.host-import-job");
      if (!btn) return;
      var agentId = btn.getAttribute("data-agent");
      var list = jobsByAgent[agentId] || [];
      var active = list.filter(function (j) {
        return j.status === "pending" || j.status === "claimed" || j.status === "running" || j.status === "partial";
      });
      var latest = list[0];
      var statusCell = row.querySelector("span.job-status");
      if (!statusCell) return;
      // 刚才这次点击被拒绝时，旧的完成记录不能把这句话盖掉。只有新的在途作业才接过这一格。
      if (statusCell.getAttribute("data-note") === "fail" && !active.length) return;
      var parts = [];
      if (active.length) parts.push("在途 " + active.length);
      if (latest && latest.status === "completed") parts.push("完成 posted=" + latest.posted);
      if (latest && latest.status === "failed") parts.push("失败 " + (latest.last_error_class || "unknown"));
      if (latest && latest.status === "cancelled") parts.push("取消");
      if (latest && latest.status === "expired") parts.push("已过期（目标未在线）");
      if (parts.length) {
        statusCell.textContent = parts.join(" · ");
        statusCell.classList.remove("is-fail");
        statusCell.removeAttribute("data-note");
        statusCell.removeAttribute("data-reason");
        var idleTitle = statusCell.getAttribute("data-idle-title");
        if (idleTitle) statusCell.title = idleTitle;
      } else if (statusCell.getAttribute("data-note") !== "fail") {
        statusCell.textContent = "";
        statusCell.classList.remove("is-fail");
      }
    });
  }
  function createJob(agentId, program, requestId) {
    return fetch("/admin/api/hosts/jobs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({agent_id: agentId, program: program, request_id: requestId}),
    }).then(function (response) { return response.json().then(function (data) { return {ok: response.ok, data: data}; }); });
  }
  // 为每个按钮准备一个稳定 request_id，并在服务端没终结之前不重发。
  // 评审 §6：双击/快连按不得建两个 job；只有上一个终结才能启新一轮。
  var pendingButtonKey = {};
  document.querySelectorAll("button.host-import-job").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var agentId = btn.getAttribute("data-agent");
      var program = btn.getAttribute("data-program");
      var baseKey = agentId + ":" + program;
      // 当前 agent 有任何未终结作业就不发。按钮文案保持不动，进度写在待导入那一列。
      var open = (jobsByAgent[agentId] || []).some(function (j) {
        return j.status === "pending" || j.status === "claimed" || j.status === "running" || j.status === "partial";
      });
      if (open) {
        btn.setAttribute("data-state", "blocked");
        paintJobs();
        return;
      }
      if (pendingButtonKey[baseKey]) return;
      var reqId = pendingButtonKey[baseKey] = ("ui:" + agentId + ":" + program + ":" + Date.now());
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.setAttribute("data-state", "busy");
      createJob(agentId, program, reqId).then(function (res) {
        delete pendingButtonKey[baseKey];
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        if (res.ok && res.data && res.data.ok) {
          btn.setAttribute("data-state", "queued");
          writeStatus(btn, "已排队", false);
          pullJobs();
        } else {
          btn.setAttribute("data-state", "failed");
          var reason = (res.data && (res.data.refused || res.data.error)) || "没有返回原因";
          writeStatus(btn, failText(reason), true, reason);
        }
      }).catch(function () {
        delete pendingButtonKey[baseKey];
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        btn.setAttribute("data-state", "failed");
        writeStatus(btn, failText("网络"), true, "网络");
      });
    });
  });
  function pullImported() {
    if (!fleet) return Promise.resolve();
    return fetch("/admin/api/hosts/progress").then(function (response) {
      if (!response.ok) throw new Error("progress");
      return response.json();
    }).then(function (data) {
      ((data && data.rows) || []).forEach(function (item) {
        if (!item) return;
        var id = item.device_id || "";
        var program = item.program || "";
        fleet.querySelectorAll("tr[data-program]").forEach(function (row) {
          if (row.getAttribute("data-program") !== program) return;
          if (id && row.getAttribute("data-device-id") !== id) return;
          if (item.imported === undefined || item.imported === null) return;
          row.setAttribute("data-imported", String(item.imported));
        });
      });
    }).catch(function () {});
  }
  function refreshPending() {
    Promise.all([pullLocal(), pullImported(), pullJobs()]).then(function () { paintProgress(localHosts); });
  }
  // 暴露测试钩子（真实浏览器 verify 用它触发真 pullJobs，不手写 DOM 伪造）。
  window.rogmemPullJobs = pullJobs;
  window.rogmemRefreshPending = refreshPending;
  var identified = Promise.resolve();
  if (fleet && fleet.getAttribute("data-viewer") !== "known") {
    identified = whoamiPermission().then(function (state) {
      if (state === "denied") return;
      if (state === "prompt") {
        var once = function () {
          document.removeEventListener("pointerdown", once, true);
          document.removeEventListener("keydown", once, true);
          pullWhoami().then(refreshPending).catch(function () {});
        };
        document.addEventListener("pointerdown", once, true);
        document.addEventListener("keydown", once, true);
        return;
      }
      return pullWhoami();
    }).catch(function () {});
  }
  identified.then(refreshPending);
  setInterval(refreshPending, 20000);
})();
</script>
"""


_HOST_COLUMNS = (
    "智能体名",
    "接入状态",
    "接入时间",
    "最近活跃",
    "累计录入会话数",
    "累计调用次数",
    "插件版本",
    "待导入会话数",
    "操作",
)


_HOST_TIPS = {
    "插件版本": "这台电脑上这个宿主最近一次调用时报上来的 rogmem 插件版本，形如 P-V1-打包时间-DEV（钩子、规则、Hermes 记忆、本机门口是同一个版本）。打包时间是插件内容最后一次变化的那次发版；只改服务器不改插件时，插件版本不变。和中枢不一致就是落后，点「更新宿主插件」把提示贴给那台电脑上的任意宿主。「未上报」是旧插件，或钩子在那台电脑上没跑起来。",
    "设备名": "按设备名筛选各台电脑上的宿主。列表里设备名写在智能体名中。当前电脑上的宿主，智能体名后面会标（本机），只用于显示，不会写进设备名。认不出当前电脑时，仍然全部列出，只是不加这个标识。",
    "接入状态": "未安装、已发现未接入、待确认、待授权、已接入；已经在列表里的显示待放行、待领取、启用或停用。确认之前不会写入。没领到密钥不算连上。申请授权后，在这一行通过授权。",
    "接入时间": "接入时间是该智能体第一次领取授权的时间。更早启用、当时没单独记下的，先显示申请时间。",
    "最近活跃": "最近一次带着授权和记忆中枢通信的时间。",
    "累计录入会话数": "累计录入会话数把该智能体的输入和输出各算 1 条，从已有记录累加；文件和宿主记忆不算。",
    "累计调用次数": "累计调用次数只计成功读取记忆的次数：核心记忆、检索、读章节、读原文，用来看它对记忆的依赖。提交会话和内部萃取不计。",
    "待导入会话数": "这台电脑上已经扫到、还没有进入记忆中枢的历史会话条数。输入和输出各算 1 条。本地存量减去已经导入的，所以这个数会变少。还没扫到时显示 —；都导入完了显示 0。页面开着时会向本机通道重新要这个数。",
    "操作": "用来通过授权、停止授权，或让已经启用的宿主导入历史。启用中可以停止授权，停用后再通过仍用原来的密钥。待放行点通过授权后等待领取，没领到密钥不算连上。",
}


def _tip(label: str, tip: str = "") -> str:
    if not tip:
        return esc(label)
    return f"<span class='tip' title='{esc(tip)}'>{esc(label)}</span>"


_PLUGIN_LABELS = {"current": "最新", "outdated": "落后", "incompatible": "须更新", "unreported": "未上报"}


def _plugin_cell(row: dict) -> str:
    """The plugin this host last reported, against the hub's. Only enabled hosts are judged."""
    if row.get("status") != "启用":
        return "<td>—</td>"
    status = str(row.get("plugin_status") or "unreported")
    label = _PLUGIN_LABELS.get(status, status)
    reported = str(row.get("plugin_version") or "")
    label = str(row.get("plugin_label") or "") or ("旧版本" if reported else "")
    seen = str(row.get("plugin_seen_at") or "")
    hint = "；".join(part for part in (f"最近上报 {seen}" if seen else "", f"内容编号 {reported}" if reported else "") if part)
    title = f" title='{esc(hint)}'" if hint else ""
    shown = f"<code>{esc(label)}</code> " if label else ""
    label = _PLUGIN_LABELS.get(status, status)
    return f"<td class='plugin plugin-{esc(status)}'{title}>{shown}<span class='badge'>{esc(label)}</span></td>"


def plugin_fleet_summary(rows: list[dict]) -> dict:
    """How many enabled hosts run the hub's plugin. Drives the banner, not a browser flag."""
    enabled = [row for row in rows if row.get("status") == "启用"]
    behind = [row for row in enabled if (row.get("plugin_status") or "unreported") != "current"]
    devices = sorted({str(row.get("device_base") or row.get("device_name") or "") for row in behind} - {""})
    return {"enabled": len(enabled), "behind": len(behind), "devices": devices}


def _host_head() -> str:
    return "".join(f"<th>{_tip(label, _HOST_TIPS.get(label, ''))}</th>" for label in _HOST_COLUMNS)


def _host_stamp(value: str, title: str = "") -> str:
    if not value:
        return "—"
    extra = f" title='{esc(title)}'" if title else ""
    return f"<time datetime='{esc(value)}'{extra}>{esc(value)}</time>"


def _scan_buttons(report: dict | None) -> str:
    """只给扫描到还没接入的宿主列『确认接入』按钮。

    评审 PR #116 §1：历史导入必须按 agent_id 下达作业，program-only 旗标
    不得启动导出。原「确认导入」按钮因此移除；用户要开启导入，请到
    「宿主管理」里对具体设备那一行按「导入历史」。
    """
    if not report:
        return ""
    program = json.dumps(report.get("program") or "", ensure_ascii=False)
    detail = str(report.get("detail") or "").strip()
    parts = []
    if report.get("access") in {"已发现未接入", "待确认"}:
        tip = "列出差异之后是待确认。确认之前不会写入。"
        if detail:
            tip = f"{detail} {tip}"
        parts.append(
            f"<button type='button' title='{esc(tip)}' onclick='post(\"/admin/api/hosts/connect\", {{program: {program}, confirm: true}})'>确认接入</button>"
        )
    return " ".join(parts)


def _import_cell(report: dict | None, imported: int = 0) -> str:
    """待导入列。两件事分开展示，不互相覆盖（评审 PR #116 §6）：
    - 本机扫描到的「还未导入」量（由 JS pullLocal 刷新进 .scan-pending 子节点）。
    - 作业在途/完成/失败状态（由 JS pullJobs 刷新进 .job-status 子节点）。
    """
    total = (report or {}).get("importable") if report else None
    try:
        pending = max(int(total) - int(imported or 0), 0) if total not in (None, "") else None
    except (TypeError, ValueError):
        pending = None
    pending_str = "—" if pending is None else str(pending)
    return (
        "<td class='import-progress'>"
        f"<span class='scan-pending' title='本机扫到但还没入库的条数'>{pending_str}</span>"
        "<span class='job-status muted' aria-live='polite' title='这一行最近一次导入历史的进度'></span>"
        "</td>"
    )


def _status_cell(value: str, detail: str = "") -> str:
    text = value or "—"
    if not detail:
        return f"<td>{esc(text)}</td>"
    return f"<td>{_tip(text, detail)}</td>"


def _host_operation(row: dict) -> str:
    status = row.get("status") or ""
    agent_id = str(row.get("id") or "")
    if status == "待放行":
        code = esc(row.get("code") or "")
        code_html = f"<span class='muted'>核对码 {code}</span> " if code else ""
        return code_html + f"<button type='button' onclick='post(\"/admin/api/agents/{esc(agent_id)}/approve\")'>通过授权</button>"
    if status == "停用":
        return f"<button type='button' onclick='post(\"/admin/api/agents/{esc(agent_id)}/enable\")'>通过授权</button>"
    if status == "启用":
        who = str(row.get("agent_name") or "").strip()
        if not who:
            who = " · ".join(
                part
                for part in (
                    str(row.get("device_base") or row.get("device_name") or "").strip(),
                    str(row.get("program") or "").strip(),
                )
                if part
            )
        target = f"「{who}」" if who else "这一行"
        program = esc(row.get("program") or "")
        import_btn = (
            f"<button type='button' class='quiet host-import-job' "
            f"data-agent='{esc(agent_id)}' data-program='{program}' "
            f"title='让这台电脑导入还没进入记忆的历史会话。进度写在待导入那一列，开始不等于已经完成。'>"
            f"导入历史</button>"
        )
        disable_btn = confirm_button(
            f"确定停止{target}的授权？停止后新的调用会被拒绝。再通过仍用原来的密钥。",
            f"/admin/api/agents/{agent_id}/disable",
            label="停止授权",
            confirm="确认停止授权",
            quiet=True,
        )
        return f"<span class='host-actions'>{import_btn}{disable_btn}</span>"
    if status == "待领取":
        return "<span class='muted'>等待领取</span>"
    return "—"


def hosts_page(rows: list[dict], fleet: dict | None = None, filters: dict | None = None) -> str:
    fleet = fleet or {"rows": [], "facets": {"devices": [], "agents": []}, "viewer_known": False}
    filters = filters or {}
    device_options = "<option value=''>全部</option>"
    for item in (fleet.get("facets") or {}).get("devices") or []:
        label = item.get("label") or ""
        if item.get("local"):
            label = f"{label}（本机）"
        selected = " selected" if item.get("id") == filters.get("device", "") else ""
        device_options += f"<option value='{esc(item.get('id') or '')}'{selected}>{esc(label)}</option>"
    agent_options = _options((fleet.get("facets") or {}).get("agents") or [], filters.get("agent", ""))
    reports = {
        str(row.get("program") or ""): row
        for row in rows
        if str(row.get("reported_at") or "").strip()
    }
    imported_by_program = {str(row.get("program") or ""): int(row.get("imported") or 0) for row in rows}
    seen_once: dict[str, str] = {}
    repeated: set[str] = set()
    for row in fleet.get("rows") or []:
        program = str(row.get("program") or "")
        if program in seen_once:
            repeated.add(program)
        else:
            seen_once[program] = str(row.get("id") or "")
    owners: dict[str, str] = {}
    for row in fleet.get("rows") or []:
        program = str(row.get("program") or "")
        if program in reports and row.get("local"):
            owners[program] = str(row.get("id") or "")
    for program, row_id in seen_once.items():
        if program in reports and program not in owners and program not in repeated:
            owners[program] = row_id
    fleet_rows = []
    for row in fleet.get("rows") or []:
        program = str(row.get("program") or "")
        report = reports.get(program) if owners.get(program) == str(row.get("id") or "") else None
        base = esc(row.get("device_base") or "")
        agent = esc(row.get("agent_name") or "")
        mark = "<span class='local-mark'>（本机）</span>" if row.get("local") else ""
        connected = _host_stamp(
            row.get("connected_at") or "",
            "更早启用的，这里显示申请时间" if row.get("connected_estimated") else "",
        )
        detail = str((report or {}).get("detail") or "").strip()
        access = (report or {}).get("access") or row.get("status") or "—"
        op = _scan_buttons(report)
        auth = _host_operation(row)
        if op and auth != "—":
            op = f"{op} {auth}"
        elif not op:
            op = auth
        if "history_count" in row:
            imported = int(row.get("history_count") or 0)
        else:
            imported = imported_by_program.get(program, int((report or {}).get("imported") or 0))
        fleet_rows.append(
            "<tr data-device-id='{id}' data-device-name='{name}' data-program='{program}' data-imported='{imported}' data-local='{local}' data-plugin='{plugin_status}'>"
            "<td class='agent-name' data-base='{agent}'>{agent}{mark}</td>"
            "{access}<td>{connected}</td><td>{active}</td>"
            "<td>{sessions}</td><td>{calls}</td>{plugin}{progress}<td class='ops'>{op}</td></tr>".format(
                plugin_status=esc(row.get("plugin_status") or ""),
                plugin=_plugin_cell(row),
                id=esc(row.get("device_id") or ""),
                name=base,
                program=esc(program),
                imported=imported,
                local="1" if row.get("local") else "0",
                mark=mark,
                agent=agent,
                access=_status_cell(str(access), detail),
                connected=connected,
                active=_host_stamp(row.get("last_used") or ""),
                sessions=esc(row.get("session_count") or 0),
                calls=esc(row.get("call_count") or 0),
                progress=_import_cell(report, imported),
                op=op,
            )
        )
    if not (filters.get("device") or filters.get("agent")):
        for program, report in reports.items():
            if program in owners:
                continue
            detail = str(report.get("detail") or "").strip()
            imported = imported_by_program.get(program, int(report.get("imported") or 0))
            op = _scan_buttons(report) or "—"
            fleet_rows.append(
                f"<tr data-program='{esc(program)}' data-imported='{imported}' data-scan='1'>"
                f"<td class='agent-name' data-base='{esc(program)}'>{esc(program)}</td>"
                f"{_status_cell(str(report.get('access') or '—'), detail)}"
                "<td>—</td>"
                "<td>—</td><td>—</td><td>—</td><td>—</td>"
                f"{_import_cell(report, imported)}"
                f"<td class='ops'>{op}</td></tr>"
            )
    if not fleet_rows:
        fleet_rows.append(f"<tr><td class='empty' colspan='{len(_HOST_COLUMNS)}'>还没有接入的宿主。</td></tr>")
    viewer = "known" if fleet.get("viewer_known") else "unknown"
    from rogmem.engine import HOST_PROGRAMS, hub_id
    from rogmem.plugin import version as plugin_version
    from rogmem.onboard import GRANT_MARK, HUB_MARK, host_update_prompt, onboard_prompt

    from rogmem.engine import current_plugin_label

    HOST_BUNDLE_REV = current_plugin_label() or plugin_version()
    summary = plugin_fleet_summary(fleet.get("rows") or [])

    hub = hub_id()
    prompt_json = json.dumps(onboard_prompt(GRANT_MARK).replace(HUB_MARK, hub), ensure_ascii=False)
    update_json = json.dumps(host_update_prompt().replace(HUB_MARK, hub), ensure_ascii=False)
    supported = "、".join(HOST_PROGRAMS)
    hook_hosts = "、".join(name for name in HOST_PROGRAMS if name != "DSH")
    device_filter = _tip("设备名", _HOST_TIPS["设备名"])
    fleet_html = f"""<div class="page-head"><h1>宿主管理</h1></div>
<form class='filters' method='get' action='/admin/hosts'>
<label>{device_filter}<select name='device'>{device_options}</select></label>
<label>智能体<select name='agent'>{agent_options}</select></label>
<button class='quiet' type='submit'>筛选</button>
<button type="button" id="update-hosts" class="quiet row-end">更新宿主插件</button>
<button type="button" id="add-host">添加宿主</button>
</form>
<div class="note" id="update-hosts-note"{'' if summary['behind'] else ' hidden'}>中枢的 rogmem 插件是 <code>{HOST_BUNDLE_REV}</code>。{summary['behind']} 个已启用宿主不是这个版本{('（' + '、'.join(esc(name) for name in summary['devices']) + '）') if summary['devices'] else ''}。点「更新宿主插件」，把提示贴给那台电脑上任意一个宿主；或在那台电脑上运行 <code>python -m rogmem plugin update</code>。</div>
<div class="host-fleet-wrap" style="overflow-x:auto;width:100%;max-width:calc(100vw - 2rem);box-sizing:border-box;-webkit-overflow-scrolling:touch;"><table id="host-fleet" data-viewer="{viewer}" style="min-width:860px;"><tr>{_host_head()}</tr>{''.join(fleet_rows)}</table></div>
<div id="add-host-modal" class="modal" hidden>
<div class="modal-card wide" role="dialog" aria-modal="true" aria-labelledby="add-host-title">
<h2 id="add-host-title">添加宿主</h2>
<p class="note">现在可以适配这些宿主：{supported}。{hook_hosts} 装上同一份 hook。DSH 装上插件。</p>
<p class="note">复制下面这段，贴给本机任意一个已安装的宿主。扫描到多个时可以自己选要接哪几个；不选就接入全部已发现的这些宿主。已经接入的不会重发钥匙。以后新装的，再点一次。</p>
<p class="note">接入后看「待导入会话数」。它问要不要导入时，确认后再继续。</p>
<textarea id="add-host-prompt" readonly rows="10"></textarea>
<p><button type="button" id="add-host-copy">复制提示</button> <button type="button" class="quiet" id="add-host-close">关闭</button></p>
<p id="add-host-status" class="note"></p>
</div></div>
<div id="update-hosts-modal" class="modal" hidden>
<div class="modal-card wide" role="dialog" aria-modal="true" aria-labelledby="update-hosts-title">
<h2 id="update-hosts-title">更新宿主插件</h2>
<p class="note">把下面这段贴给本机任意一个已安装的宿主。它会刷新本机客户端，并把本机所有已支持的宿主（{supported}）的全局规则和 hook 更新到最新。</p>
<p class="note">不重新发钥匙，不改已有配置，也不动记忆数据。宿主端插件有新版本时执行一次即可。</p>
<textarea id="update-hosts-prompt" readonly rows="12"></textarea>
<p><button type="button" id="update-hosts-copy">复制提示</button> <button type="button" class="quiet" id="update-hosts-close">关闭</button></p>
<p id="update-hosts-status" class="note"></p>
</div></div>
{_HOST_FLEET_SCRIPT.replace("__ORIGIN__", _LOCAL_INSTALLER).replace("__PROMPT_TEMPLATE__", prompt_json).replace("__HOST_BUNDLE_REV__", json.dumps(HOST_BUNDLE_REV)).replace("__UPDATE_TEMPLATE__", update_json)}"""
    return page("宿主管理", "hosts", fleet_html)


# 图谱外壳：加载态、错误态、减动效、无 WebGL 兜底。取数这一层：一次慢、一次非 JSON 都不是
# 终局，会自己重试，并把上一次拿到的星图留在本会话里，重进这一页先画出来。
# 以普通字符串给出（非 f-string），花括号无需转义。
_GRAPH_SHELL_JS = """
var ROGMEM_GRAPH_FETCH_MS = 15000;
var ROGMEM_GRAPH_RENDER_WATCHDOG_MS = 15000;
// 一次取数的尝试次数，以及每次重试前的等待倍数。
var ROGMEM_GRAPH_FETCH_TRIES = 3;
var ROGMEM_GRAPH_BACKOFF_MS = 1200;
// 本会话里留一份上次拿到的星图。中枢慢或那一跳没连上时，重进这一页不再是死路。
var ROGMEM_GRAPH_CACHE_PREFIX = 'rogmem-graph:';
function rogmemClearBusy(el) {
  if (el && el.setAttribute) el.setAttribute('aria-busy', 'false');
}
function rogmemLoaderGone(el) {
  if (!el) return;
  var loader = el.querySelector('.graph-loader');
  if (!loader) return;
  loader.classList.add('is-gone');
  setTimeout(function () { if (loader && loader.parentNode) loader.remove(); }, 260);
}
function rogmemGraphCount(el) {
  // 活跃结晶叠在 #graph 里；换画面时必须带走，不能被 replaceChildren 清掉。
  if (!el || !el.querySelector) return null;
  return el.querySelector(':scope > #graph-count') || el.querySelector('#graph-count');
}
function rogmemReplaceGraphBody(el) {
  // 星图主体可以整页换掉；#graph-count 是沉浸式 chrome，始终留在容器末尾。
  if (!el || !el.replaceChildren) return;
  var tally = rogmemGraphCount(el);
  var kids = Array.prototype.slice.call(arguments, 1);
  if (tally) {
    for (var i = kids.length - 1; i >= 0; i--) if (kids[i] === tally) kids.splice(i, 1);
    kids.push(tally);
  }
  el.replaceChildren.apply(el, kids);
}
function rogmemGraphNote(el, text) {
  // 加载中改 loader 那句话；已经落到错误态就改错误卡片那句话。取数期间始终有话说。
  if (!el || !el.querySelector) return;
  var line = el.querySelector('.graph-loader p') || el.querySelector('.graph-error p');
  if (line) line.textContent = text;
}
function rogmemShowError(el, text, retry) {
  if (!el) return;
  var box = document.createElement('div');
  box.className = 'graph-error note';
  var para = document.createElement('p');
  para.textContent = text || '图谱没有载入。';
  box.appendChild(para);
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'quiet';
  btn.textContent = '重试';
  // 能就地重取就地重取。整页刷新只是没有取数入口时的退路。
  btn.onclick = typeof retry === 'function' ? function () { retry(); } : function () { location.reload(); };
  box.appendChild(btn);
  rogmemReplaceGraphBody(el, box);
  rogmemClearBusy(el);
}
function rogmemGraphCacheKey(el) {
  // 记忆星图和每一篇结晶的关联图谱不是同一份数据，本会话里的留存也不能互相顶掉。
  // 取本页地址当名字：同一页重进能命中，换一页不会串。
  var here = '';
  try { here = location.pathname + location.search; } catch (err) {}
  return ROGMEM_GRAPH_CACHE_PREFIX + (el.id || 'graph') + '@' + here;
}
function rogmemGraphStore(el, data) {
  if (!el) return;
  try {
    sessionStorage.setItem(rogmemGraphCacheKey(el), JSON.stringify({ nodes: data.nodes || [], edges: data.edges || [] }));
  } catch (err) {}
}
function rogmemGraphStored(el) {
  if (!el) return null;
  try {
    var raw = sessionStorage.getItem(rogmemGraphCacheKey(el));
    var data = raw ? JSON.parse(raw) : null;
    if (data && data.nodes && data.edges) return data;
  } catch (err) {}
  return null;
}
function rogmemGraphForget(el) {
  if (!el) return;
  try { sessionStorage.removeItem(rogmemGraphCacheKey(el)); } catch (err) {}
}
function rogmemGraphReason(status) {
  // 慢过 15 秒时 status 是 0；本机通道够不到中枢时回的是一段纯文本 502。
  // 所以先看状态码和 Content-Type，绝不把任何响应直接当 JSON 读。
  if (!status) return '（记忆中枢没有回应）';
  if (status >= 500) return '（记忆中枢暂时不可用，HTTP ' + status + '）';
  return '（记忆中枢回的不是图谱数据，HTTP ' + status + '）';
}
function rogmemGraphRequest(el, onOk, onFail) {
  // 一次请求只落地一次：超时、非 2xx、正文不是 JSON 都走同一条失败出口。
  var controller = (typeof AbortController === 'function') ? new AbortController() : null;
  var settled = false;
  var timer = setTimeout(function () { fail(0); }, ROGMEM_GRAPH_FETCH_MS);
  function fail(status) {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    if (controller) try { controller.abort(); } catch (err) {}
    onFail(status);
  }
  fetch('/admin/api/graph', controller ? {signal: controller.signal} : undefined).then(function (r) {
    if (settled) return null;
    if (r.status === 401) {
      // 会话过期：去登录页，不要把它说成载入失败。
      settled = true;
      clearTimeout(timer);
      rogmemGraphForget(el);
      location = '/admin/login';
      return null;
    }
    var type = (r.headers && r.headers.get && r.headers.get('content-type')) || '';
    if (!r.ok || type.indexOf('json') < 0) {
      fail(r.status);
      return null;
    }
    return r.json();
  }).then(function (data) {
    if (settled || !data) return;
    settled = true;
    clearTimeout(timer);
    onOk(data);
  }).catch(function () {
    fail(0);
  });
}
function rogmemDrawGraph(el, data, draw, note) {
  var nodes = data.nodes || [];
  var edges = data.edges || [];
  var tally = document.getElementById('graph-count');
  if (tally) tally.textContent = '活跃结晶：' + nodes.length + (note || '');
  try {
    draw(nodes, edges);
  } catch (err) {
    // 数据到手了、画面出不来，是渲染这一层的问题，不能算在取数头上。
    // 整页重开最干净；数据已在本会话里，重开会立刻画出上一份。
    rogmemShowError(el, '图谱画不出来，重开这一页可以再试一次。', null);
  }
}
function rogmemFetchGraph(el, draw, attempt) {
  el.rogmemGraphLoading = true;
  if (attempt === 1) rogmemGraphNote(el, '正在铺开星图');
  rogmemGraphRequest(el, function (data) {
    el.rogmemGraphLoading = false;
    rogmemGraphStore(el, data);
    rogmemDrawGraph(el, data, draw, '');
  }, function (status) {
    if (attempt < ROGMEM_GRAPH_FETCH_TRIES) {
      var wait = ROGMEM_GRAPH_BACKOFF_MS * attempt;
      rogmemGraphNote(
        el,
        '记忆中枢没有回应，' + Math.round(wait / 1000) + ' 秒后重取（第 ' + attempt + '/' + (ROGMEM_GRAPH_FETCH_TRIES - 1) + ' 次重试）'
      );
      setTimeout(function () { rogmemFetchGraph(el, draw, attempt + 1); }, wait);
      return;
    }
    el.rogmemGraphLoading = false;
    rogmemShowError(el, '图谱没有载入。' + rogmemGraphReason(status), function () {
      rogmemFetchGraph(el, draw, 1);
    });
  });
}
function rogmemLoadGraph(el, draw) {
  if (!el || typeof draw !== 'function') return;
  if (el.rogmemGraphLoading) return;
  var cached = rogmemGraphStored(el);
  if (cached) {
    // 先画上一份，再悄悄换新。中枢慢的时候，重进这一页也画得出来。
    rogmemDrawGraph(el, cached, draw, '');
    rogmemGraphRequest(el, function (data) { rogmemGraphStore(el, data); }, function () {});
    return;
  }
  rogmemFetchGraph(el, draw, 1);
}
function rogmemOpenBrowser() {
  var href = location.href;
  var note = document.createElement('p');
  note.className = 'note';
  note.innerHTML = '这台浏览器画不出星图。<a href="' + href + '" target="_blank" rel="noopener">用系统浏览器打开同一页</a>';
  fetch('/client/open-browser?url=' + encodeURIComponent(href)).catch(function () {});
  return note;
}
"""


# 3D 记忆星图：坐标用后端算好的球面位置，画面交给 Three.js，沿用登录页那套视效细节。
# 配色、发光、曲线波动、能量流动、点击脉冲、泛光全部照搬登录页的着色器；数据换成真实图谱。
_GRAPH_3D_JS_TEMPLATE = """
var ROGMEM_GRAPH_3D_MAX_POINTS = 200000;

function rogmemCommunity(count, linkSrc, linkDst, linkCount) {
  // 并查集。连通分量当一个社区；整图连通时退化到按节点类型分层，保证一定分出层来。
  var parent = new Int32Array(count);
  var i;
  for (i = 0; i < count; i++) parent[i] = i;
  function find(x) { while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; }
  for (i = 0; i < linkCount; i++) {
    var a = find(linkSrc[i]);
    var b = find(linkDst[i]);
    if (a !== b) parent[a] = b;
  }
  var label = new Int32Array(count);
  var map = Object.create(null);
  var next = 0;
  for (i = 0; i < count; i++) {
    var root = find(i);
    if (map[root] === undefined) map[root] = next++;
    label[i] = map[root];
  }
  return { label: label, size: next };
}

function rogmem3dPalette() {
  return [
    [0x2c / 255, 0x20 / 255, 0x24 / 255],
    [0x53 / 255, 0x34 / 255, 0x3e / 255],
    [0x8f / 255, 0x5a / 255, 0x67 / 255],
    [0xef / 255, 0x65 / 255, 0x76 / 255],
    [0xed / 255, 0xb4 / 255, 0xc2 / 255]
  ];
}

function rogmem3dSegments(edgeCount) {
  if (edgeCount <= 1500) return 20;
  if (edgeCount <= 8000) return 10;
  if (edgeCount <= 30000) return 5;
  return 2;
}

function rogmem3dStarSprite(THREE) {
  // Three.js 的 PointsMaterial 不指定 map 时把点画成实心方块。
  // 用一张 32x32 径向渐变把背景星裁成柔边圆点，和结晶节点用同一套发光语言。
  var side = 32;
  var half = side / 2;
  var canvas = document.createElement('canvas');
  canvas.width = canvas.height = side;
  var ctx = canvas.getContext('2d');
  var grad = ctx.createRadialGradient(half, half, 0, half, half, half);
  grad.addColorStop(0, 'rgba(255,255,255,1)');
  grad.addColorStop(0.38, 'rgba(255,255,255,.85)');
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, side, side);
  var texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function rogmemDraw3D(el, nodes, edges, focusMode) {
  var THREE = window.__rogmemThree;
  if (!THREE) { rogmemReplaceGraphBody(el, rogmemOpenBrowser()); rogmemClearBusy(el); return; }
  if (!nodes.length) {
    var empty = document.createElement('p');
    empty.className = 'note';
    empty.textContent = '还没有可以铺开的星点。';
    rogmemReplaceGraphBody(el, empty);
    rogmemClearBusy(el);
    return;
  }
  var count = nodes.length;
  var ids = new Array(count);
  var names = new Array(count);
  var types = new Array(count);
  var index = Object.create(null);
  var i;
  for (i = 0; i < count; i++) {
    ids[i] = nodes[i].id;
    names[i] = nodes[i].name || '';
    types[i] = nodes[i].type || '';
    index[nodes[i].id] = i;
  }
  var linkSrc = [], linkDst = [];
  for (i = 0; i < edges.length; i++) {
    var s = index[edges[i].src], d = index[edges[i].dst];
    if (s === undefined || d === undefined || s === d) continue;
    linkSrc.push(s);
    linkDst.push(d);
  }
  var linkCount = linkSrc.length;
  var srcArr = Uint32Array.from(linkSrc);
  var dstArr = Uint32Array.from(linkDst);

  // 坐标：后端已经把球面坐标算好了（半径=到根的跳数，角度=向量降维）。没带上就自己铺一层球壳兜底。
  var degree = new Uint32Array(count);
  for (i = 0; i < linkCount; i++) { degree[srcArr[i]] += 1; degree[dstArr[i]] += 1; }
  var maxDeg = 1;
  for (i = 0; i < count; i++) if (degree[i] > maxDeg) maxDeg = degree[i];
  var placed = new Float32Array(count * 3);
  var given = true;
  for (i = 0; i < count; i++) {
    var node = nodes[i];
    if (typeof node.x !== 'number' || typeof node.y !== 'number' || typeof node.z !== 'number') { given = false; break; }
    placed[i * 3] = node.x;
    placed[i * 3 + 1] = node.y;
    placed[i * 3 + 2] = node.z;
  }
  if (!given) {
    for (i = 0; i < count; i++) {
      var polar = Math.acos(Math.max(-1, Math.min(1, 1 - 2 * (i + 0.5) / count)));
      var azimuth = 2.399963229728653 * i;
      var radius = 60 + 130 * Math.sqrt(degree[i] / maxDeg);
      placed[i * 3] = radius * Math.sin(polar) * Math.cos(azimuth);
      placed[i * 3 + 1] = radius * Math.sin(polar) * Math.sin(azimuth);
      placed[i * 3 + 2] = radius * Math.cos(polar);
    }
  }
  rogmem3dBuild(el, {
    THREE: THREE, count: count, names: names, types: types, ids: ids,
    placed: placed, linkSrc: srcArr, linkDst: dstArr, linkCount: linkCount, focusMode: focusMode
  });
}

function rogmem3dBuild(el, ctx) {
  var THREE = ctx.THREE;
  var count = ctx.count;
  var ids = ctx.ids;
  var pos = ctx.placed;
  var linkSrc = ctx.linkSrc;
  var linkDst = ctx.linkDst;
  var linkCount = ctx.linkCount;
  var focusMode = ctx.focusMode;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var community = rogmemCommunity(count, linkSrc, linkDst, linkCount);
  var layers = community.size;
  var palette = rogmem3dPalette();
  var i;

  var scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x000000, 0.0016);
  // 画布尺寸跟容器走，不跟窗口走。容器在页面里只占剩余高度：
  // 用窗口高度会把渲染中心推到可见区下方，看着就像没绕球心转，而且球会被裁掉一截。
  var viewport = rogmem3dViewport(el);
  var camera = new THREE.PerspectiveCamera(62, viewport[0] / Math.max(1, viewport[1]), 0.5, 6000);
  var renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setSize(viewport[0], viewport[1]);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  // 纯黑底。任何非黑的灰在 composer + OutputPass 的色彩管线里都会被再编码一次，
  // 整块背景被抬成中灰，画面就糊了。黑是编码不变的。
  renderer.setClearColor(0x000000);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.className = 'graph-3d';
  rogmemReplaceGraphBody(el, renderer.domElement, rogmemGraphTip());

  var controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.06;
  controls.rotateSpeed = 0.6;
  controls.autoRotate = !reduce;
  controls.autoRotateSpeed = 0.25;
  controls.enablePan = false;

  // 星空
  var starCount = 2600;
  var starPos = new Float32Array(starCount * 3);
  for (i = 0; i < starCount; i++) {
    var r = 260 + Math.random() * 900;
    var phi = Math.acos(2 * Math.random() - 1);
    var theta = Math.random() * 6.283185307179586;
    starPos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    starPos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
    starPos[i * 3 + 2] = r * Math.cos(phi);
  }
  var starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  var stars = new THREE.Points(starGeo, new THREE.PointsMaterial({
    color: 0xffffff, size: 1.9, sizeAttenuation: true, map: rogmem3dStarSprite(THREE),
    transparent: true, opacity: 0.55, depthWrite: false, blending: THREE.AdditiveBlending
  }));
  stars.frustumCulled = false;
  scene.add(stars);

  // 节点：按社区取调色板，柔边发光点，呼吸
  var nodeColors = new Float32Array(count * 3);
  var nodeSizes = new Float32Array(count);
  var degree = new Uint32Array(count);
  for (i = 0; i < linkCount; i++) { degree[linkSrc[i]] += 1; degree[linkDst[i]] += 1; }
  var maxDeg = 1;
  for (i = 0; i < count; i++) if (degree[i] > maxDeg) maxDeg = degree[i];
  var maxNodeSize = 0;
  for (i = 0; i < count; i++) {
    var t = layers > 1 ? (community.label[i] % layers) / layers : Math.sqrt(degree[i] / maxDeg);
    var ci = Math.min(palette.length - 1, Math.floor(t * palette.length));
    nodeColors[i * 3] = palette[ci][0];
    nodeColors[i * 3 + 1] = palette[ci][1];
    nodeColors[i * 3 + 2] = palette[ci][2];
    // 节点大小：heat = √(连线数 / 最大值)，连得越多越大。
    var size = 0.4 + 1.75 * Math.sqrt(degree[i] / maxDeg);
    // 主人节点在基础尺寸上再放大，保证连接数相同时仍显著大于其他节点。
    if (ids[i] === 'owner') size *= 2.5;
    nodeSizes[i] = size;
    if (size > maxNodeSize) maxNodeSize = size;
  }

  var nodeGeo = new THREE.BufferGeometry();
  nodeGeo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  nodeGeo.setAttribute('nodeSize', new THREE.BufferAttribute(nodeSizes, 1));
  nodeGeo.setAttribute('nodeColor', new THREE.BufferAttribute(nodeColors, 3));
  var nodeUniforms = {
    uTime: { value: 0 },
    // 下面三个在取景完成后按 2D 星图的绝对像素口径覆写。
    uBaseNodeSize: { value: 1.6 },
    uMaxPointSize: { value: 26 },
    uPointScale: { value: 1 },
    uPulsePositions: { value: [new THREE.Vector3(1e5, 1e5, 1e5), new THREE.Vector3(1e5, 1e5, 1e5), new THREE.Vector3(1e5, 1e5, 1e5)] },
    uPulseTimes: { value: [-1e4, -1e4, -1e4] },
    uPulseSpeed: { value: 34 }
  };
  var nodeMat = new THREE.ShaderMaterial({
    uniforms: nodeUniforms,
    vertexShader: `
      attribute float nodeSize;
      attribute vec3 nodeColor;
      uniform float uTime;
      uniform float uBaseNodeSize;
      uniform float uMaxPointSize;
      uniform float uPointScale;
      uniform vec3 uPulsePositions[3];
      uniform float uPulseTimes[3];
      uniform float uPulseSpeed;
      varying vec3 vColor;
      varying float vGlow;
      varying float vPulse;
      float ring(vec3 w, vec3 p, float t) {
        if (t < 0.0) return 0.0;
        float dt = uTime - t;
        if (dt < 0.0 || dt > 4.0) return 0.0;
        float d = abs(distance(w, p) - dt * uPulseSpeed);
        return smoothstep(3.0, 0.0, d) * smoothstep(4.0, 0.0, dt);
      }
      void main() {
        vColor = nodeColor;
        vGlow = 0.5 + 0.5 * sin(uTime * 0.6 + position.x * 0.15);
        vec3 world = (modelMatrix * vec4(position, 1.0)).xyz;
        float total = 0.0;
        for (int k = 0; k < 3; k++) total += ring(world, uPulsePositions[k], uPulseTimes[k]);
        vPulse = min(total, 1.0);
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        float breathe = 1.0 + sin(uTime * 0.7 + position.z * 0.15) * 0.15;
        float size = nodeSize * breathe * uBaseNodeSize * (uPointScale / -mv.z) * (1.0 + vPulse * 2.5);
        gl_PointSize = min(size, uMaxPointSize);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      uniform float uTime;
      varying vec3 vColor;
      varying float vGlow;
      varying float vPulse;
      void main() {
        vec2 c = 2.0 * gl_PointCoord - 1.0;
        float d = length(c);
        if (d > 1.0) discard;
        float g1 = 1.0 - smoothstep(0.0, 0.5, d);
        float g2 = 1.0 - smoothstep(0.0, 1.0, d);
        float strength = pow(g1, 1.2) + g2 * 0.3;
        vec3 color = vColor * (1.0 + vGlow * 0.15);
        if (vPulse > 0.0) {
          color = mix(color, mix(vec3(1.0), vec3(0.937, 0.396, 0.463), 0.4), vPulse * 0.8) * (1.0 + vPulse * 1.2);
          strength *= (1.0 + vPulse);
        }
        float alpha = strength * (0.95 - 0.3 * d);
        gl_FragColor = vec4(color, alpha);
      }`,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  });
  var nodesMesh = new THREE.Points(nodeGeo, nodeMat);
  nodesMesh.frustumCulled = false;
  scene.add(nodesMesh);

  // 连线：自适应细分 + 贝塞尔中点 + 噪声波动 + 能量流动
  var seg = rogmem3dSegments(linkCount);
  var vCount = linkCount * seg;
  var edgePos = new Float32Array(vCount * 3);
  var edgeStart = new Float32Array(vCount * 3);
  var edgeEnd = new Float32Array(vCount * 3);
  var edgeColor = new Float32Array(vCount * 3);
  var edgePath = new Float32Array(vCount);
  var k;
  for (i = 0; i < linkCount; i++) {
    var a = linkSrc[i], b = linkDst[i];
    var t = layers > 1 ? (((community.label[a] % layers) + (community.label[b] % layers)) / 2) / layers : 0.5;
    var col = palette[Math.min(palette.length - 1, Math.floor(t * palette.length))];
    for (k = 0; k < seg; k++) {
      var vi = i * seg + k;
      var f = k / (seg - 1);
      edgePos[vi * 3] = f;
      edgeStart[vi * 3] = pos[a * 3]; edgeStart[vi * 3 + 1] = pos[a * 3 + 1]; edgeStart[vi * 3 + 2] = pos[a * 3 + 2];
      edgeEnd[vi * 3] = pos[b * 3]; edgeEnd[vi * 3 + 1] = pos[b * 3 + 1]; edgeEnd[vi * 3 + 2] = pos[b * 3 + 2];
      edgeColor[vi * 3] = col[0]; edgeColor[vi * 3 + 1] = col[1]; edgeColor[vi * 3 + 2] = col[2];
      edgePath[vi] = i;
    }
  }
  var edgeGeo = new THREE.BufferGeometry();
  edgeGeo.setAttribute('position', new THREE.BufferAttribute(edgePos, 3));
  edgeGeo.setAttribute('edgeStart', new THREE.BufferAttribute(edgeStart, 3));
  edgeGeo.setAttribute('edgeEnd', new THREE.BufferAttribute(edgeEnd, 3));
  edgeGeo.setAttribute('edgeColor', new THREE.BufferAttribute(edgeColor, 3));
  edgeGeo.setAttribute('edgePath', new THREE.BufferAttribute(edgePath, 1));
  var edgeUniforms = {
    uTime: { value: 0 },
    uEdgeAlpha: { value: 0.5 },
    uPulsePositions: nodeUniforms.uPulsePositions,
    uPulseTimes: nodeUniforms.uPulseTimes,
    uPulseSpeed: nodeUniforms.uPulseSpeed
  };
  var edgeMat = new THREE.ShaderMaterial({
    uniforms: edgeUniforms,
    vertexShader: `
      attribute vec3 edgeStart;
      attribute vec3 edgeEnd;
      attribute vec3 edgeColor;
      attribute float edgePath;
      uniform float uTime;
      uniform vec3 uPulsePositions[3];
      uniform float uPulseTimes[3];
      uniform float uPulseSpeed;
      varying vec3 vColor;
      varying float vT;
      varying float vEdge;
      varying float vPulse;
      vec3 hash3(vec3 p) {
        p = vec3(dot(p, vec3(127.1, 311.7, 74.7)), dot(p, vec3(269.5, 183.3, 246.1)), dot(p, vec3(113.5, 271.9, 124.6)));
        return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
      }
      float vnoise(vec3 p) {
        vec3 i = floor(p);
        vec3 f = fract(p);
        vec3 u = f * f * (3.0 - 2.0 * f);
        return mix(mix(mix(dot(hash3(i + vec3(0.0,0.0,0.0)), f - vec3(0.0,0.0,0.0)), dot(hash3(i + vec3(1.0,0.0,0.0)), f - vec3(1.0,0.0,0.0)), u.x),
                       mix(dot(hash3(i + vec3(0.0,1.0,0.0)), f - vec3(0.0,1.0,0.0)), dot(hash3(i + vec3(1.0,1.0,0.0)), f - vec3(1.0,1.0,0.0)), u.x), u.y),
                   mix(mix(dot(hash3(i + vec3(0.0,0.0,1.0)), f - vec3(0.0,0.0,1.0)), dot(hash3(i + vec3(1.0,0.0,1.0)), f - vec3(1.0,0.0,1.0)), u.x),
                       mix(dot(hash3(i + vec3(0.0,1.0,1.0)), f - vec3(0.0,1.0,1.0)), dot(hash3(i + vec3(1.0,1.0,1.0)), f - vec3(1.0,1.0,1.0)), u.x), u.y), u.z);
      }
      float ring(vec3 w, vec3 p, float t) {
        if (t < 0.0) return 0.0;
        float dt = uTime - t;
        if (dt < 0.0 || dt > 4.0) return 0.0;
        float d = abs(distance(w, p) - dt * uPulseSpeed);
        return smoothstep(3.0, 0.0, d) * smoothstep(4.0, 0.0, dt);
      }
      void main() {
        float f = position.x;
        vT = f;
        vEdge = edgePath;
        vec3 mid = mix(edgeStart, edgeEnd, 0.5);
        vec3 dir = normalize(edgeEnd - edgeStart + vec3(1e-4));
        vec3 perp = normalize(cross(dir, vec3(0.0, 1.0, 0.0)));
        if (length(perp) < 0.1) perp = vec3(1.0, 0.0, 0.0);
        mid += perp * (sin(f * 3.14159) * 0.15);
        vec3 p0 = mix(edgeStart, mid, f);
        vec3 p1 = mix(mid, edgeEnd, f);
        vec3 world = mix(p0, p1, f);
        world += perp * vnoise(vec3(edgePath * 0.08, f * 0.6, uTime * 0.15)) * 0.12;
        float total = 0.0;
        for (int j = 0; j < 3; j++) total += ring(world, uPulsePositions[j], uPulseTimes[j]);
        vPulse = min(total, 1.0);
        vColor = edgeColor;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(world, 1.0);
      }`,
    fragmentShader: `
      uniform float uTime;
      uniform float uEdgeAlpha;
      varying vec3 vColor;
      varying float vT;
      varying float vEdge;
      varying float vPulse;
      void main() {
        float flow1 = sin(vT * 25.0 - uTime * 4.0) * 0.5 + 0.5;
        float flow2 = sin(vT * 15.0 - uTime * 2.5 + 1.57) * 0.5 + 0.5;
        float flow = (flow1 + flow2 * 0.5) / 1.5;
        vec3 color = vColor * (0.8 + 0.2 * sin(uTime * 0.6 + vT * 12.0));
        float alpha = uEdgeAlpha * (0.45 + flow * 0.55);
        if (vPulse > 0.0) {
          color = mix(color, vec3(1.0), vPulse * 0.7);
          alpha = mix(alpha, 1.0, vPulse);
        }
        gl_FragColor = vec4(color, alpha);
      }`,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  });
  var edgesMesh = new THREE.LineSegments(edgeGeo, edgeMat);
  edgesMesh.frustumCulled = false;
  scene.add(edgesMesh);

  var composer = new THREE.EffectComposer(renderer);
  composer.addPass(new THREE.RenderPass(scene, camera));
  var bloom = new THREE.UnrealBloomPass(new THREE.Vector2(viewport[0], viewport[1]), 1.5, 0.6, 0.72);
  composer.addPass(bloom);
  composer.addPass(new THREE.OutputPass());

  // 取景：按实际球半径取，让整颗球落在画面里。
  var extent = 1;
  for (i = 0; i < count; i++) {
    var dist = Math.sqrt(pos[i * 3] * pos[i * 3] + pos[i * 3 + 1] * pos[i * 3 + 1] + pos[i * 3 + 2] * pos[i * 3 + 2]);
    if (dist > extent) extent = dist;
  }
  extent = Math.max(40, extent * 1.28);
  camera.position.set(extent * 0.35, extent * 0.5, extent * 1.5);
  controls.minDistance = extent * 0.3;
  controls.maxDistance = extent * 6;
  controls.target.set(0, 0, 0);
  controls.update();

  // 节点尺寸口径：默认机位下正好等于约定的像素尺寸。
  // gl_PointSize 是设备像素，所以要乘 pixelRatio；uPointScale = 基准距离 × pixelRatio，
  // 于是 size = nodeSize × star × (基准距离 / 当前距离)，近大远小且量纲一致。
  var pixelRatio = renderer.getPixelRatio();
  var star = Math.min(18, Math.max(2.2, 160 / Math.sqrt(Math.max(1, count))));
  var refDist = camera.position.length();
  nodeUniforms.uBaseNodeSize.value = star;
  nodeUniforms.uPointScale.value = refDist * pixelRatio;
  nodeUniforms.uMaxPointSize.value = Math.ceil(maxNodeSize * star * 2.2 * pixelRatio);

  // 通用帧扩展点：按需注入的层自己挂进来。核心不认识"标签"这个词，关态下不留任何痕迹。
  var hooks = el.rogmem3dHooks || [];
  var view = {
    el: el, THREE: THREE, camera: camera, pos: pos, names: ctx.names, count: count,
    degree: degree, extent: extent, reduce: reduce
  };

  // 拾取：悬停放大 + 名称，点击聚焦邻域
  var tip = document.getElementById('graph-tip') || rogmemGraphTip();
  var raycaster = new THREE.Raycaster();
  raycaster.params.Points.threshold = Math.max(6, extent / 60);
  var pointer = new THREE.Vector2(-10, -10);
  function neighborhood(idx) {
    var marked = new Uint8Array(count);
    if (idx < 0 || idx >= count) return marked;
    marked[idx] = 1;
    for (var n = 0; n < linkCount; n++) {
      if (linkSrc[n] === idx) marked[linkDst[n]] = 1;
      else if (linkDst[n] === idx) marked[linkSrc[n]] = 1;
    }
    return marked;
  }
  var baseColors = nodeColors.slice();
  function applyFocus(idx) {
    var marked = neighborhood(idx);
    for (var n = 0; n < count; n++) {
      var on = marked[n] || idx < 0;
      var c = on ? palette[palette.length - 1] : [0.28, 0.24, 0.26];
      nodeColors[n * 3] = baseColors[n * 3] * (on ? 1 : 0.5) + c[0] * (on ? 0.6 : 0);
      nodeColors[n * 3 + 1] = baseColors[n * 3 + 1] * (on ? 1 : 0.5) + c[1] * (on ? 0.6 : 0);
      nodeColors[n * 3 + 2] = baseColors[n * 3 + 2] * (on ? 1 : 0.5) + c[2] * (on ? 0.6 : 0);
    }
    nodeGeo.attributes.nodeColor.needsUpdate = true;
  }
  function pulseAt(point) {
    var t = clock.getElapsedTime();
    var slot = (pulseSlot = (pulseSlot + 1) % 3);
    nodeUniforms.uPulsePositions.value[slot].copy(point);
    nodeUniforms.uPulseTimes.value[slot] = t;
  }
  var pulseSlot = 0;
  renderer.domElement.addEventListener('mousemove', function (e) {
    var rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  });
  renderer.domElement.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('.glass-panel')) return;
    raycaster.setFromCamera(pointer, camera);
    var hits = raycaster.intersectObject(nodesMesh, false);
    if (!hits.length) { if (focusMode) applyFocus(-1); return; }
    var idx = hits[0].index;
    if (!focusMode) {
      var openId = ctx.ids[idx];
      if (openId) location = '/admin/crystals/' + encodeURIComponent(openId);
      return;
    }
    applyFocus(idx);
    pulseAt(hits[0].point);
    if (typeof el.rogmemOnFocus === 'function') { try { el.rogmemOnFocus(idx); } catch (err) {} }
  });

  var clock = new THREE.Clock();
  var framed = false;
  function frame() {
    requestAnimationFrame(frame);
    var t = clock.getElapsedTime();
    nodeUniforms.uTime.value = t;
    edgeUniforms.uTime.value = t;
    raycaster.setFromCamera(pointer, camera);
    var hits = raycaster.intersectObject(nodesMesh, false);
    if (hits.length) {
      tip.hidden = false;
      tip.textContent = ctx.names[hits[0].index] || '';
    } else if (tip.textContent) {
      tip.hidden = true;
      tip.textContent = '';
    }
    stars.rotation.y += 0.0002;
    controls.update();
    for (var hookIndex = 0; hookIndex < hooks.length; hookIndex++) hooks[hookIndex](view, performance.now());
    composer.render();
    if (!framed) {
      framed = true;
      el.rogmemReady = true;
      el.rogmemSimEnded = true;
      rogmemLoaderGone(el);
      rogmemClearBusy(el);
    }
  }
  // 尺寸以容器为准：窗口 resize 与容器自身变化都要重新贴合。
  // 加去重守卫，避免 setSize 反过来触发 ResizeObserver 造成回环。
  var lastW = viewport[0], lastH = viewport[1], lastPr = renderer.getPixelRatio();
  function fitViewport() {
    var size = rogmem3dViewport(el);
    var pr = Math.min(window.devicePixelRatio || 1, 2);
    if (size[0] === lastW && size[1] === lastH && pr === lastPr) return;
    lastW = size[0];
    lastH = size[1];
    lastPr = pr;
    renderer.setPixelRatio(pr);
    camera.aspect = size[0] / Math.max(1, size[1]);
    camera.updateProjectionMatrix();
    renderer.setSize(size[0], size[1]);
    composer.setSize(size[0], size[1]);
    bloom.resolution.set(size[0], size[1]);
    nodeUniforms.uPointScale.value = refDist * pr;
  }
  window.addEventListener('resize', fitViewport);
  var containerWatcher = window.ResizeObserver ? new ResizeObserver(fitViewport) : null;
  if (containerWatcher) containerWatcher.observe(el);
  window.addEventListener('pagehide', function () {
    if (containerWatcher) { try { containerWatcher.disconnect(); } catch (err) {} }
    try { controls.dispose(); renderer.dispose(); } catch (err) {}
  });
  frame();
}

// 渲染尺寸取容器而不是窗口：图形容器在页面里只占剩余高度。
function rogmem3dViewport(el) {
  var w = el.clientWidth || 0;
  var h = el.clientHeight || 0;
  if (w <= 0) w = window.innerWidth;
  if (h <= 0) h = window.innerHeight;
  return [w, h];
}

function rogmemGraphTip() {
  var old = document.getElementById('graph-tip');
  if (old) return old;
  var tip = document.createElement('div');
  tip.id = 'graph-tip';
  tip.hidden = true;
  document.body.appendChild(tip);
  return tip;
}
"""


# 三维的自动铺名层。只在大到一定倍数后出现，同屏数量有上限，只挑视野内的，且相互避让；
# 相机移动时已挑中的名字贴着节点走。开关关态下样式与脚本都不注入，浏览器不做任何新工作。
_GRAPH_3D_LABELS_CSS = (
    "<style>"
    ".graph-3d-labels{position:absolute;inset:0;z-index:3;pointer-events:none;overflow:hidden;}"
    ".graph-3d-label{position:absolute;top:0;left:0;font-size:12px;line-height:1;white-space:nowrap;"
    "color:#f4f4f4;text-shadow:0 0 4px #111,0 1px 2px #111;will-change:transform;}"
    "</style>"
)
_GRAPH_3D_LABELS_JS_TEMPLATE = """
var ROGMEM_3D_LABEL_BUDGET = __BUDGET__;
(function () {
  var mount = document.getElementById('graph');
  if (!mount) return;
  mount.rogmem3dHooks = mount.rogmem3dHooks || [];
  var layer = null;
  mount.rogmem3dHooks.push(function (view, now) {
    if (!layer) {
      layer = rogmem3dLabels(view.el, {
        THREE: view.THREE, camera: view.camera, pos: view.pos, names: view.names,
        count: view.count, degree: view.degree, extent: view.extent,
        budget: ROGMEM_3D_LABEL_BUDGET, calm: view.reduce
      });
    }
    layer.update(now);
  });
})();
function rogmem3dLabels(el, opts) {
  var THREE = opts.THREE;
  var host = document.createElement('div');
  host.className = 'graph-3d-labels';
  host.setAttribute('aria-hidden', 'true');
  el.appendChild(host);
  var budget = opts.budget;
  var names = opts.names;
  var positions = opts.pos;
  var count = opts.count;
  var degree = opts.degree;
  var camera = opts.camera;
  var extent = opts.extent;
  // 迟滞：近了才亮，退了才灭，免得在临界点来回闪。
  var Z_on = 1.1;
  var Z_off = 0.85;
  var EVERY_MS = opts.calm ? 900 : 700;
  var probe = new THREE.Vector3();
  var spans = [];
  var picked = [];
  var shown = false;
  var lastPick = 0;

  function zoomLevel() { return extent / Math.max(1e-3, camera.position.length()); }

  function box() {
    return [
      el.clientWidth || window.innerWidth,
      el.clientHeight || window.innerHeight
    ];
  }

  function project(i) {
    var size = box();
    probe.set(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]).project(camera);
    return {
      x: (probe.x * 0.5 + 0.5) * size[0],
      y: (-probe.y * 0.5 + 0.5) * size[1],
      z: probe.z
    };
  }

  function place(span, x, y) {
    var width = span.offsetWidth || 60;
    span.style.transform = 'translate(' + (x - width / 2) + 'px,' + (y - 9) + 'px)';
  }

  function drop() {
    for (var i = 0; i < spans.length; i++) if (spans[i].parentNode) spans[i].remove();
    spans = [];
    picked = [];
  }

  function pick() {
    drop();
    var size = box();
    var width = size[0];
    var height = size[1];
    var candidates = [];
    for (var i = 0; i < count; i++) {
      var name = names[i];
      if (!name) continue;
      var at = project(i);
      if (at.z > 1 || at.x < 0 || at.y < 0 || at.x > width || at.y > height) continue;
      candidates.push({ index: i, x: at.x, y: at.y, w: Math.min(260, name.length * 13 + 8), h: 18 });
    }
    // 连得多的先上，保证挤掉的是边缘的名字。
    candidates.sort(function(a, b) { return degree[b.index] - degree[a.index]; });
    var kept = [];
    for (var k = 0; k < candidates.length && kept.length < budget; k++) {
      var one = candidates[k];
      var clash = false;
      for (var r = 0; r < kept.length; r++) {
        var other = kept[r];
        if (Math.abs(other.x - one.x) * 2 < (other.w + one.w) &&
            Math.abs(other.y - one.y) * 2 < (other.h + one.h)) { clash = true; break; }
      }
      if (clash) continue;
      kept.push(one);
      var span = document.createElement('span');
      span.className = 'graph-3d-label';
      span.textContent = names[one.index];
      place(span, one.x, one.y);
      host.appendChild(span);
      place(span, one.x, one.y);
      spans.push(span);
      picked.push(one.index);
    }
  }

  function follow() {
    var size = box();
    var width = size[0];
    var height = size[1];
    for (var i = 0; i < picked.length; i++) {
      var at = project(picked[i]);
      if (at.z > 1 || at.x < -60 || at.y < -30 || at.x > width + 60 || at.y > height + 30) {
        spans[i].style.display = 'none';
      } else {
        spans[i].style.display = '';
        place(spans[i], at.x, at.y);
      }
    }
  }

  return {
    update: function (now) {
      var level = zoomLevel();
      if (!shown) {
        if (level < Z_on) return;
        shown = true;
        pick();
        lastPick = now;
        return;
      }
      if (level <= Z_off) { shown = false; drop(); return; }
      if (now - lastPick >= EVERY_MS) { pick(); lastPick = now; }
      else follow();
    },
    destroy: function () {
      drop();
      if (host.parentNode) host.parentNode.removeChild(host);
    }
  };
}
"""


def _graph_3d_document(
    element_id: str,
    restrict_js: str,
    focus_flag: str,
    shell_block: str,
    labels_block: str = "",
    labels_css: str = "",
) -> str:
    """3D 记忆星图。画面全部由 Three.js 出，坐标来自后端算好的球面位置（半径=跳数，角度=向量）。

    labels_block / labels_css 只在"自动展示词条名"开着时非空；关态下这一层一个字节都不发。
    """
    return f"""{labels_css}<script>{shell_block}{_GRAPH_3D_JS_TEMPLATE}{labels_block}</script>
<script type="importmap">{{"imports":{{"three":"/admin/static/three/build/three.module.js","three/addons/":"/admin/static/three/examples/jsm/"}}}}</script>
<script type="module">
import * as THREE from 'three';
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
import {{ EffectComposer }} from 'three/addons/postprocessing/EffectComposer.js';
import {{ RenderPass }} from 'three/addons/postprocessing/RenderPass.js';
import {{ UnrealBloomPass }} from 'three/addons/postprocessing/UnrealBloomPass.js';
import {{ OutputPass }} from 'three/addons/postprocessing/OutputPass.js';
window.__rogmemThree = Object.assign(Object.create(null), THREE, {{
  OrbitControls: OrbitControls, EffectComposer: EffectComposer, RenderPass: RenderPass,
  UnrealBloomPass: UnrealBloomPass, OutputPass: OutputPass
}});
var rogmemGraphEl = document.getElementById('{element_id}');
// 渲染看门狗：脚本没起来、或球没画出来，都有界撤 loader，让位真图，不空白。
// 正在取数时不撤：重试有话说，撤了就等于把话说给空气。
setTimeout(function renderWatchdog() {{
  if (rogmemGraphEl && !rogmemGraphEl.rogmemReady && !rogmemGraphEl.rogmemGraphLoading) {{
    rogmemLoaderGone(rogmemGraphEl);
    rogmemClearBusy(rogmemGraphEl);
  }}
}}, ROGMEM_GRAPH_RENDER_WATCHDOG_MS);
(function loadGraph3D() {{
  rogmemLoadGraph(document.getElementById('{element_id}'), function (nodes, edges) {{
    {restrict_js}
    rogmemDraw3D(document.getElementById('{element_id}'), nodes, edges, {focus_flag});
  }});
}})();
</script>"""


def graph_mount(
    element_id: str,
    restrict_js: str = "",
    focus: bool = False,
    auto_labels: bool = False,
    label_budget: int = 50,
) -> str:
    """Draw the 3D starfield, or offer the system browser when it cannot be drawn.

    When focus is on, a click colors that point and the points directly tied to it.
    A click on empty space clears that color. A double click opens the page.
    Otherwise a click opens it.

    When auto_labels is on, a separate IIFE attaches the viewport-driven name layer. In the off
    state this function emits zero bytes of that feature so the browser does no new work.
    """
    focus_flag = "true" if focus else "false"
    try:
        budget = int(label_budget)
    except (TypeError, ValueError):
        budget = 50
    if budget < 1:
        budget = 1
    if budget > 200:
        budget = 200
    labels_block = ""
    labels_css = ""
    if auto_labels:
        labels_block = _GRAPH_3D_LABELS_JS_TEMPLATE.replace("__BUDGET__", str(budget))
        labels_css = _GRAPH_3D_LABELS_CSS
    shell_block = _GRAPH_SHELL_JS
    markup = f"""<div id="{element_id}" aria-busy="true"><div class="graph-loader" role="status" aria-live="polite"><div class="graph-loader-dots"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div><p>正在铺开星图</p></div></div>"""
    return markup + _graph_3d_document(
        element_id, restrict_js, focus_flag, shell_block, labels_block, labels_css
    )


def shared_graph(
    restrict_js: str = "",
    *,
    auto_labels: bool = False,
    label_budget: int = 50,
) -> str:
    """The 3D starfield both pages draw. restrict_js only decides which points remain."""
    return graph_mount(
        "graph",
        restrict_js,
        focus=True,
        auto_labels=auto_labels,
        label_budget=label_budget,
    )


# ---- 全站背景音乐 ---------------------------------------------------------------
# 只播这一首随包自带的 BGM。不加载第三方音频，不加交互音效，也不做音频处理。
GRAPH_AUDIO_BGM = "bgm.mp3"
GRAPH_AUDIO_FILES = frozenset({GRAPH_AUDIO_BGM})
_GRAPH_AUDIO_FILE = Path(__file__).with_name("vendor") / "audio" / GRAPH_AUDIO_BGM
_GRAPH_AUDIO_FINGERPRINT: dict[tuple[int, int], tuple[str, str, int]] = {}


def graph_audio_fingerprint() -> tuple[str, str, int]:
    """内容指纹 (ETag, Last-Modified, 字节数)。按 mtime+size 缓存，换曲自动失效。"""
    try:
        stat = _GRAPH_AUDIO_FILE.stat()
    except OSError:
        return "", "", 0
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _GRAPH_AUDIO_FINGERPRINT.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with _GRAPH_AUDIO_FILE.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    value = (
        '"' + digest.hexdigest() + '"',
        email.utils.formatdate(stat.st_mtime, usegmt=True),
        stat.st_size,
    )
    _GRAPH_AUDIO_FINGERPRINT.clear()
    _GRAPH_AUDIO_FINGERPRINT[key] = value
    return value


def graph_audio_src() -> str:
    """URL 带内容指纹：曲目不变，浏览器就一直用本地副本，不必重新下载。"""
    etag, _, _ = graph_audio_fingerprint()
    version = etag.strip('"')[:12]
    return "/admin/static/audio/" + GRAPH_AUDIO_BGM + ("?v=" + version if version else "")

_AUDIO_CSS = """<style>
header .rga-toggle{position:relative;display:grid;place-items:center;flex:none;box-sizing:border-box;aspect-ratio:1;width:20px;height:20px;min-width:20px;min-height:20px;line-height:0;padding:0;margin-right:4px;border:1px solid var(--border);border-radius:50%;background:transparent;color:var(--text-muted);cursor:pointer}
header .rga-toggle::before{content:"";position:absolute;inset:-10px -6px;border-radius:50%}
header .rga-toggle:hover{background:var(--surface-hover);color:var(--text-primary)}
header .rga-toggle:focus-visible{outline:2px solid var(--focus-color);outline-offset:2px}
.rga-note{display:block;flex:none;width:12px;height:12px;aspect-ratio:1;transform-origin:50% 50%}
.rga-toggle[data-state="playing"] .rga-note{animation:rga-spin 4s linear infinite}
.rga-toggle[data-state="loading"] .rga-note{animation:rga-breathe 1100ms ease-in-out infinite}
.rga-toggle[data-state="loading"]::after{content:"";position:absolute;inset:-2px;border-radius:50%;border:1.5px solid transparent;border-top-color:var(--text-muted);animation:rga-scan 900ms linear infinite;pointer-events:none}
.rga-toggle[data-state="error"]::after{content:"";position:absolute;inset:-2px;border-radius:50%;border:1.5px dashed var(--border-strong);pointer-events:none}
.rga-toggle[data-state="paused"] .rga-note,.rga-toggle[data-state="error"] .rga-note{animation-play-state:paused;opacity:.45}
.rga-toggle[aria-pressed="false"] .rga-note{animation-play-state:paused;opacity:.45}
.rga-toggle[data-state="playing"] .rga-core{fill:var(--brand)}
@keyframes rga-spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
@keyframes rga-scan{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
@keyframes rga-breathe{0%,100%{opacity:.42}50%{opacity:.9}}
@media (max-width: 860px){header .rga-toggle{margin-right:8px}}
@media (prefers-reduced-motion: reduce){.rga-note,.rga-toggle::after{animation:none}.rga-toggle[data-state="loading"] .rga-note{opacity:.7}}
</style>"""

_AUDIO_BUTTON = """<button type="button" class="rga-toggle" id="rga-toggle" aria-pressed="true" data-state="loading" aria-label="暂停背景音乐" title="暂停背景音乐">
  <svg class="rga-note" viewBox="0 0 24 24" preserveAspectRatio="xMidYMid meet" fill="none" aria-hidden="true" focusable="false">
    <g transform="translate(1.6 0)">
      <path d="M9 17.5V6.4l8-1.9v11.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
      <ellipse class="rga-core" cx="6.4" cy="17.5" rx="2.6" ry="2" fill="currentColor"/>
      <ellipse cx="14.4" cy="15.6" rx="2.6" ry="2" fill="currentColor"/>
    </g>
  </svg>
</button>"""

_AUDIO_MEDIA = """<audio id="rogmem-graph-bgm" loop preload="none" data-bgm-src="__BGM_SRC__"></audio>"""

_AUDIO_JS = """<script>
(function () {
  'use strict';
  var STORE = "rogmem-graph-bgm-v1";
  var POS = "rogmem-bgm-position-v1";
  var VOLUME = 0.45;
  var bgm = document.getElementById("rogmem-graph-bgm");
  var bgmSrc = bgm ? bgm.getAttribute("data-bgm-src") : "";
  var bgmLoaded = false;
  var toggle = document.getElementById("rga-toggle");
  var playing = true;
  var state = "loading";
  var mutedForAutoplay = false;
  var blocked = false;
  var resumeAt = 0;
  var lastSaved = 0;
  try { playing = localStorage.getItem(STORE) !== "off"; } catch (e) {}
  try { resumeAt = parseFloat(sessionStorage.getItem(POS)) || 0; } catch (e) {}
  if (bgm) bgm.volume = VOLUME;

  function paint() {
    if (!toggle) return;
    toggle.setAttribute("aria-pressed", playing ? "true" : "false");
    var label = state === "error" ? "背景音乐载入失败"
      : state === "loading" ? (blocked ? "点一下开启背景音乐" : "背景音乐载入中")
      : playing ? "暂停背景音乐" : "播放背景音乐";
    toggle.setAttribute("aria-label", label);
    toggle.setAttribute("title", label);
  }
  function setState(next) {
    if (state === next) return;
    state = next;
    if (toggle) toggle.setAttribute("data-state", next);
    paint();
  }
  function savePos() {
    if (!bgm) return;
    try { sessionStorage.setItem(POS, String(bgm.currentTime || 0)); } catch (e) {}
  }
  function resume() {
    if (!bgm || !(resumeAt > 0)) return;
    var span = bgm.duration;
    var target = (isFinite(span) && span > 0) ? Math.min(resumeAt, span - 0.25) : resumeAt;
    try { bgm.currentTime = Math.max(0, target); } catch (e) {}
    resumeAt = 0;
  }
  function fadeIn() {
    if (!bgm) return;
    bgm.volume = 0;
    var step = VOLUME / 14;
    var timer = setInterval(function () {
      if (!playing || bgm.paused) { clearInterval(timer); return; }
      var next = Math.min(VOLUME, bgm.volume + step);
      bgm.volume = next;
      if (next >= VOLUME) clearInterval(timer);
    }, 50);
  }
  function attempt() {
    if (!bgm || !playing) return;
    var pending = bgm.play();
    if (pending && pending.catch) pending.catch(function () {
      // 自动播放被策略挡住：先静音把时间轴跑起来，任一次交互再淡入，不必专点按钮。
      if (mutedForAutoplay || !playing || !bgm.paused) return;
      mutedForAutoplay = true;
      blocked = true;
      paint();
      bgm.muted = true;
      var retry = bgm.play();
      if (retry && retry.catch) retry.catch(function () {});
    });
  }
  function unlock() {
    if (!playing || !bgm) return;
    if (mutedForAutoplay) {
      mutedForAutoplay = false;
      blocked = false;
      bgm.muted = false;
      fadeIn();
      setState("playing");
    } else if (bgmLoaded && bgm.paused) {
      attempt();
    }
  }
  function startBgm() {
    if (bgmLoaded || !bgm || !bgmSrc) return;
    bgmLoaded = true;
    bgm.src = bgmSrc;
    bgm.load();
    if (playing) attempt();
  }
  function afterPageAssets() {
    // window.load 已等完 CSS / three.module.js / addons / 图片；再让出一次空闲帧，
    // 不让整首 BGM 和星图资产抢下载带宽。
    if (window.requestIdleCallback) window.requestIdleCallback(startBgm, {timeout: 1500});
    else setTimeout(startBgm, 120);
  }
  if (bgm) {
    bgm.addEventListener("loadedmetadata", resume);
    bgm.addEventListener("timeupdate", function () {
      var now = bgm.currentTime || 0;
      if (Math.abs(now - lastSaved) >= 1) { lastSaved = now; savePos(); }
    });
    bgm.addEventListener("loadstart", function () { setState("loading"); });
    bgm.addEventListener("waiting", function () { setState("loading"); });
    bgm.addEventListener("stalled", function () { setState("loading"); });
    bgm.addEventListener("playing", function () { if (!mutedForAutoplay) setState("playing"); });
    bgm.addEventListener("pause", function () { if (!mutedForAutoplay) setState("paused"); });
    bgm.addEventListener("error", function () { setState("error"); });
  }
  window.addEventListener("pagehide", savePos);
  window.addEventListener("beforeunload", savePos);
  document.addEventListener("pointerdown", unlock, {passive: true});
  document.addEventListener("keydown", unlock);
  document.addEventListener("touchstart", unlock, {passive: true});
  if (toggle) toggle.addEventListener("click", function () {
    playing = !playing;
    try { localStorage.setItem(STORE, playing ? "on" : "off"); } catch (e) {}
    if (playing) {
      mutedForAutoplay = false;
      blocked = false;
      if (bgm) { bgm.muted = false; bgm.volume = VOLUME; }
      if (bgmLoaded) attempt(); else startBgm();
    } else if (bgm) {
      bgm.pause();
      setState("paused");
    }
    paint();
  });
  if (toggle) toggle.setAttribute("data-state", state);
  paint();
  // 命中缓存时 loadedmetadata 可能早于本脚本；补一次断点续播。
  if (bgm && bgm.readyState >= 1) resume();
  // 上次关过音乐：直接落暂停态，别一直显示"载入中"。
  if (!playing) { setState("paused"); }
  // 页面其它资产全部就位后才开始下载 BGM；下载完立即尝试出声，不需要用户先点一下。
  else if (document.readyState === "complete") { afterPageAssets(); }
  else { window.addEventListener("load", afterPageAssets, {once:true}); }
})();
</script>"""


def audio_layer() -> str:
    """全站背景音乐：样式 + <audio> + 状态机。控制台每一页都带，跨页从断点续播。

    音频文件等到 window.load 之后才开始下载，不与星图资源抢带宽。
    """
    return (
        _AUDIO_CSS
        + _AUDIO_MEDIA.replace("__BGM_SRC__", graph_audio_src())
        + _AUDIO_JS
    )


def graph_page(auto_labels: bool = False, label_budget: int = 50) -> str:
    """沉浸式星空：主导航以下 edge-to-edge，活跃结晶计数叠在星图内。"""
    mount = shared_graph(auto_labels=auto_labels, label_budget=label_budget)
    count = "<p class='graph-count' id='graph-count'></p>"
    mount = mount.replace(
        '<div id="graph" aria-busy="true">',
        '<div id="graph" aria-busy="true">' + count,
        1,
    )
    return page("记忆星图", "graph", mount)


def _section_page(key: str, title: str, inner: str) -> str:
    """页只报自己的键。栏、顶栏高亮和侧栏都从这把键推出来。"""
    return page(title, key, f"<h1>{esc(title)}</h1>{inner}")


def _lamp(provider: dict) -> str:
    health = provider.get("health") or ""
    if health == "ok":
        kind, label = "ok", "正常"
    elif health == "warn":
        kind, label = "warn", "降级"
    elif health == "down":
        kind, label = "down", "异常"
    else:
        kind, label = "", "未检测"
    detail = provider.get("health_detail") or label
    when = provider.get("health_at") or ""
    stamp = f" data-utc='{esc(when)}'" if when else ""
    return f"<span class='lamp {kind}'{stamp} title='{esc(detail)}'><i></i>{esc(label)}</span>"


def _provider_label(provider: dict) -> str:
    if provider.get("builtin") or provider.get("id") == "local-embed":
        return "中枢服务器本地"
    return str(provider.get("name") or "")


def _provider_endpoint(provider: dict) -> str:
    url = str(provider.get("base_url") or "").strip()
    if url:
        return url
    if provider.get("builtin") or provider.get("id") == "local-embed":
        return "中枢服务器本机"
    return "本机"


def _remark_cell(provider: dict) -> str:
    text = str(provider.get("remark") or "").strip()
    if not text and provider.get("health") in {"warn", "down"}:
        text = str(provider.get("health_detail") or "").strip()
    if not text:
        return "<span class='muted'>—</span>"
    return esc(text)


def _kind_name(provider: dict, model: str) -> str:
    kinds = provider.get("model_kinds") if isinstance(provider.get("model_kinds"), dict) else {}
    return "向量嵌入" if kinds.get(model) == "embed" else "文本对话"


def _resident_ids() -> set[str]:
    from rogmem.engine import DEFAULT_MINIMAX_ID, LOCAL_EMBED_ID, SILICONFLOW_ID

    return {LOCAL_EMBED_ID, DEFAULT_MINIMAX_ID, SILICONFLOW_ID}


def _provider_key_form(provider: dict) -> str:
    if provider.get("builtin") or provider.get("id") == "local-embed":
        return "<span class='muted'>不需要密钥</span>"
    ident = esc(provider.get("id") or "")
    shown = str(provider.get("key_hint") or "")
    if shown:
        field = (
            f"<input name='api_key' type='text' value='{esc(shown)}' "
            "autocomplete='off' spellcheck='false' aria-label='密钥'>"
        )
    else:
        field = "<input name='api_key' type='password' placeholder='填写密钥' autocomplete='off' aria-label='密钥'>"
    save = ""
    if provider.get("id") not in _resident_ids():
        save = "<button type='submit'>保存密钥</button>"
    return (
        f"<form id='key-{ident}' onsubmit=\"event.preventDefault(); "
        f"post('/admin/api/settings/models/key', {{id:'{ident}', api_key:this.api_key.value}});\">"
        f"{field}{save}</form>"
    )


def models_page(providers: list[dict], assigned: dict, effective: dict, embed: dict | None = None) -> str:
    del assigned, effective, embed

    def row(provider: dict) -> str:
        names = provider.get("models") or []
        listed = "<br>".join(esc(name) for name in names) or "—"
        kinds = "<br>".join(esc(_kind_name(provider, name)) for name in names) or "—"
        action = ""
        ident = str(provider.get("id") or "")
        if ident in _resident_ids() and ident != "local-embed" and not provider.get("builtin"):
            action = f"<button type='submit' form='key-{esc(ident)}' data-key-save disabled>保存</button>"
        elif ident not in _resident_ids() and not provider.get("builtin"):
            action = delete_button(
                "确定删除供应商「" + _provider_label(provider) + "」？已经分配到它的模型会一起失效。",
                "/admin/api/settings/models/delete",
                {"id": provider["id"]},
            )
        return (
            f"<tr><td>{esc(_provider_label(provider))}</td><td class='url'>{esc(_provider_endpoint(provider))}</td>"
            f"<td>{listed}</td><td>{kinds}</td><td class='key'>{_provider_key_form(provider)}</td>"
            f"<td class='health'>{_lamp(provider)}</td>"
            f"<td>{_remark_cell(provider)}</td><td class='ops'>{action}</td></tr>"
        )

    ordered = sorted(providers, key=lambda item: 0 if item.get("builtin") or item.get("id") == "local-embed" else 1)
    rows = "".join(row(provider) for provider in ordered)
    draft = """<tr class="draft">
<td><input form="new-provider" name="name" placeholder="名称" required></td>
<td><input form="new-provider" name="base_url" placeholder="https://主机/v1" required></td>
<td><input form="new-provider" name="models" placeholder="模型名，逗号分隔" required></td>
<td><select form="new-provider" name="kind"><option value="chat">文本对话</option><option value="embed">向量嵌入</option></select></td>
<td class="key"><input form="new-provider" name="api_key" type="password" placeholder="密钥" autocomplete="off"></td>
<td class="health muted">—</td>
<td class="muted">—</td>
<td class="ops"><button type="submit" form="new-provider">保存</button></td>
</tr>"""
    inner = f"""<div class='panel'>
<form id="new-provider" onsubmit="event.preventDefault(); post('/admin/api/settings/models', {{name:this.elements.name.value, base_url:this.elements.base_url.value, api_key:this.elements.api_key.value, models:this.elements.models.value, kind:this.elements.kind.value}});"></form>
<table class="models"><tr><th>供应商</th><th>接口</th><th>模型</th><th>模型类型</th><th class="key">密钥</th><th class="health">健康</th><th>备注</th><th class="ops"></th></tr>{rows}{draft}</table>
<p class='note'>保存时立刻测一次。之后按管线设置里的探活间隔再测，默认 60 秒。连不上、鉴权被拒或服务端 5xx 记为异常（红）。限流、额度、参数被拒，或成功响应里带了错误码，记为降级（黄）。出现这些情况时，运行日志留下一条未结案告警；同一状态不重复记，恢复后结案。文本对话和向量嵌入在 <a href='/admin/settings/pipeline'>管线设置</a> 里分配给对应的工人。</p>
</div>
<script>
(function () {{
  document.querySelectorAll("button[data-key-save]").forEach(function (button) {{
    var form = document.getElementById(button.getAttribute("form"));
    var field = form && form.elements.api_key;
    if (!field) return;
    var initial = field.value.trim();
    function paint() {{
      var value = field.value.trim();
      button.disabled = !value || value === initial;
    }}
    field.addEventListener("input", paint);
    form.addEventListener("submit", function (event) {{
      if (button.disabled) {{
        event.preventDefault();
        event.stopImmediatePropagation();
      }}
    }}, true);
    paint();
  }});
}})();
</script>"""
    return _section_page("models", "模型管理", inner)


_COST_SPANS = (("all", "历史至今"), ("24h", "近 24 小时"), ("7d", "近 7 天"), ("30d", "近 30 天"))
_COST_DETAILS = (("24h", "近 24 小时"), ("30d", "近 30 天"), ("all", "全部"))
COST_OBSERVE_HEADER = "X-Cost-Observe"
_COST_PENDING_SCRIPT = r"""
<script>
(function () {
  var root = document.getElementById("cost");
  if (!root) return;
  var tries = 0;
  function tokenOf(state, epoch, asOf) {
    return String(state || "") + "|" + (epoch == null ? "" : String(epoch)) + "|" + (asOf || "");
  }
  var rendered = tokenOf(root.getAttribute("data-cost-state"), root.getAttribute("data-cost-epoch"), root.getAttribute("data-cost-asof"));
  try {
    var here = location.pathname + location.search;
    if (sessionStorage.getItem("rogmem-cost-url") !== here) {
      sessionStorage.setItem("rogmem-cost-url", here);
      sessionStorage.setItem("rogmem-cost-reloads", "0");
    }
  } catch (err) {}
  function reloadCards() {
    var n = 0;
    try { n = Number(sessionStorage.getItem("rogmem-cost-reloads") || 0); } catch (err) {}
    if (n >= 6) {
      var cap = document.getElementById("ov-caption");
      if (cap) cap.textContent = "这一时段还在汇总，刷新页面可再试";
      return;
    }
    try { sessionStorage.setItem("rogmem-cost-reloads", String(n + 1)); } catch (err2) {}
    var url = new URL(location.href);
    url.searchParams.delete("page");
    url.searchParams.set("part", "overview");
    fetch(url.pathname + url.search, {credentials: "same-origin", cache: "no-store"})
      .then(function (response) {
        var kind = response.headers.get("content-type") || "";
        if (!response.ok || kind.indexOf("json") < 0) throw new Error("overview");
        return response.json();
      })
      .then(function (data) {
        if (!data || window.__costLeave) return;
        var money = document.querySelector("#ov-money-box .money");
        var token = document.querySelector("#ov-token-box .money");
        var split = document.querySelector("#ov-token-box .ov-split");
        var caption = document.getElementById("ov-caption");
        var fx = document.getElementById("fx");
        var openPrices = document.getElementById("open-prices");
        root.setAttribute("data-cost-observed", data.observed ? "1" : "0");
        root.setAttribute("data-cost-state", data.state || "");
        root.setAttribute("data-cost-epoch", data.epoch == null ? "" : String(data.epoch));
        root.setAttribute("data-cost-asof", data.as_of || "");
        if (data.state === "pending" || data.state === "stale") root.setAttribute("aria-busy", "true");
        else root.removeAttribute("aria-busy");
        if (money) {
          money.textContent = data.money_text || "";
          money.classList.toggle("gathering", !!data.gathering);
        }
        if (token) {
          token.textContent = data.token_text || "";
          token.classList.toggle("gathering", !!data.gathering);
        }
        if (split) split.textContent = data.split_text || "";
        if (caption) {
          caption.textContent = data.caption_text || "";
          caption.classList.toggle("gathering", !!data.gathering);
        }
        if (fx) {
          if (data.rate_text != null) fx.value = data.rate_text;
          fx.disabled = !data.observed;
        }
        if (openPrices) openPrices.disabled = !data.observed;
        if (data.chart && typeof window.__rogmemCostChartBoot === "function") {
          window.__rogmemCostChartBoot(data.chart);
        }
        rendered = tokenOf(data.state, data.epoch, data.as_of);
        if (data.state === "pending" || data.state === "stale") setTimeout(arm, 400);
      })
      .catch(function () { if (!window.__costLeave) location.reload(); });
  }
  document.addEventListener("click", function (event) {
    var anchor = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (anchor && !anchor.hasAttribute("data-cost-filter")) window.__costLeave = true;
  }, true);
  function arm() {
    if (window.__costLeave) return;
    tries += 1;
    var delay = tries < 20 ? 400 : 1500;
    if (tries === 20 && root.getAttribute("data-cost-state") === "pending") {
      var cap = document.getElementById("ov-caption");
      if (cap) cap.textContent = "这一时段还在汇总，完成后会换上";
    }
    var modal = document.getElementById("price-modal");
    if (modal && !modal.hidden) { setTimeout(arm, delay); return; }
    var probeUrl = new URL(location.href);
    probeUrl.searchParams.delete("page");
    probeUrl.searchParams.delete("part");
    probeUrl.searchParams.set("observe", "1");
    var probe = probeUrl.pathname + probeUrl.search;
    if (probe.indexOf("observe=1") < 0) probe += (probe.indexOf("?") >= 0 ? "&" : "?") + "observe=1";
    fetch(probe, {
      headers: {"__HEADER__": "1"},
      cache: "no-store",
      credentials: "same-origin"
    }).then(function (response) {
      if (window.__costLeave) return null;
      var kind = response.headers.get("content-type") || "";
      if (!response.ok || kind.indexOf("json") < 0) throw new Error("observe");
      return response.json();
    }).then(function (data) {
      if (!data || window.__costLeave) return;
      if (data.state === "failed") { reloadCards(); return; }
      var next = tokenOf(data.state, data.epoch, data.as_of);
      if (next !== rendered) { reloadCards(); return; }
      setTimeout(arm, delay);
    }).catch(function () { if (!window.__costLeave) reloadCards(); });
  }
  window.__costResume = function () {
    window.__costLeave = false;
    setTimeout(arm, 50);
  };
  setTimeout(arm, 50);
})();
</script>
""".replace("__HEADER__", COST_OBSERVE_HEADER)
_COST_SCRIPT = r"""
<script>
(function () {
  var OVERVIEW_KEYS = {span: 1, worker: 1, model: 1};
  var DETAIL_KEYS = {detail: 1, dworker: 1, dmodel: 1};
  var FILTER_KEYS = ["span", "detail", "worker", "model", "dworker", "dmodel"];
  var overviewAbort = null;
  var callsAbort = null;
  var filterSeq = 0;

  function comboFromUrl(url) {
    var parsed = new URL(url, location.href);
    var combo = {span: "30d", detail: "30d", worker: "", model: "", dworker: "", dmodel: ""};
    for (var i = 0; i < FILTER_KEYS.length; i++) {
      var key = FILTER_KEYS[i];
      if (parsed.searchParams.has(key)) combo[key] = parsed.searchParams.get(key) || "";
    }
    return combo;
  }
  function hrefFromCombo(combo, page) {
    var url = new URL("/admin/costs", location.href);
    for (var i = 0; i < FILTER_KEYS.length; i++) {
      var key = FILTER_KEYS[i];
      var value = combo[key] || "";
      if (key === "span" || key === "detail" || value) url.searchParams.set(key, value);
    }
    if (page && Number(page) > 1) url.searchParams.set("page", String(page));
    return url.pathname + url.search;
  }
  function applyKey(combo, key, value, resetPage) {
    var next = {
      span: combo.span || "30d",
      detail: combo.detail || "30d",
      worker: combo.worker || "",
      model: combo.model || "",
      dworker: combo.dworker || "",
      dmodel: combo.dmodel || ""
    };
    next[key] = value == null ? "" : String(value);
    return {combo: next, page: resetPage ? 1 : (Number(new URL(location.href).searchParams.get("page") || 1) || 1)};
  }
  function markTabs(combo) {
    document.querySelectorAll("a[data-cost-filter][data-cost-key]").forEach(function (anchor) {
      var key = anchor.getAttribute("data-cost-key") || "";
      var value = anchor.getAttribute("data-cost-value") || "";
      anchor.classList.toggle("on", (combo[key] || "") === value);
      var built = applyKey(combo, key, value, !!DETAIL_KEYS[key]);
      anchor.setAttribute("href", hrefFromCombo(built.combo, built.page));
    });
    document.querySelectorAll("select[data-cost-key]").forEach(function (select) {
      var key = select.getAttribute("data-cost-key") || "";
      var current = combo[key] || "";
      Array.prototype.forEach.call(select.options, function (option) {
        var value = option.getAttribute("data-cost-value");
        if (value == null) value = "";
        var built = applyKey(combo, key, value, !!DETAIL_KEYS[key]);
        option.value = hrefFromCombo(built.combo, built.page);
        option.selected = value === current;
      });
    });
  }
  function setBusy(root, on) {
    if (!root) return;
    if (window.RogmemLoad && typeof RogmemLoad.list === "function") RogmemLoad.list(root, on);
    else {
      root.classList.toggle("list-busy", !!on);
      if (on) root.setAttribute("aria-busy", "true");
      else root.removeAttribute("aria-busy");
    }
  }
  function paintCalls(html) {
    var root = document.getElementById("calls");
    if (!root) return;
    var doc = new DOMParser().parseFromString(html, "text/html");
    var nextBody = doc.querySelector("tbody");
    var nextFoot = doc.querySelector(".list-foot");
    var body = root.querySelector("tbody");
    var current = root.querySelector(".list-foot");
    if (nextBody && body) body.replaceWith(nextBody);
    if (nextFoot && current) current.replaceWith(nextFoot);
  }
  function paintOverview(data) {
    var root = document.getElementById("cost");
    if (!root || !data) return;
    root.setAttribute("data-cost-observed", data.observed ? "1" : "0");
    root.setAttribute("data-cost-state", data.state || "");
    root.setAttribute("data-cost-epoch", data.epoch == null ? "" : String(data.epoch));
    root.setAttribute("data-cost-asof", data.as_of || "");
    if (data.state === "pending" || data.state === "stale") root.setAttribute("aria-busy", "true");
    else root.removeAttribute("aria-busy");
    var money = document.querySelector("#ov-money-box .money");
    var token = document.querySelector("#ov-token-box .money");
    var split = document.querySelector("#ov-token-box .ov-split");
    var caption = document.getElementById("ov-caption");
    var fx = document.getElementById("fx");
    var openPrices = document.getElementById("open-prices");
    if (money) {
      money.textContent = data.money_text || "";
      money.classList.toggle("gathering", !!data.gathering);
    }
    if (token) {
      token.textContent = data.token_text || "";
      token.classList.toggle("gathering", !!data.gathering);
    }
    if (split) split.textContent = data.split_text || "";
    if (caption) {
      caption.textContent = data.caption_text || "";
      caption.classList.toggle("gathering", !!data.gathering);
    }
    if (fx) {
      if (data.rate_text != null) fx.value = data.rate_text;
      fx.disabled = !data.observed;
    }
    if (openPrices) openPrices.disabled = !data.observed;
    if (data.chart && typeof window.__rogmemCostChartBoot === "function") {
      window.__rogmemCostChartBoot(data.chart);
    }
  }
  function refreshCalls(href, seq) {
    var root = document.getElementById("calls");
    var url = new URL(href, location.href);
    url.searchParams.set("part", "calls");
    if (callsAbort) callsAbort.abort();
    callsAbort = typeof AbortController === "function" ? new AbortController() : null;
    setBusy(root, true);
    fetch(url.pathname + url.search, {
      credentials: "same-origin",
      cache: "no-store",
      headers: {"X-Rogmem-List": "1"},
      signal: callsAbort ? callsAbort.signal : undefined
    }).then(function (response) {
      if (seq !== filterSeq) return null;
      var type = response.headers.get("content-type") || "";
      if (!response.ok || type.indexOf("html") < 0) throw new Error("list");
      return response.text();
    }).then(function (html) {
      if (html == null || seq !== filterSeq) return;
      paintCalls(html);
      setBusy(root, false);
    }).catch(function (err) {
      if (err && err.name === "AbortError") return;
      setBusy(root, false);
      if (seq === filterSeq) location.href = href;
    });
  }
  function refreshOverview(href, seq) {
    var root = document.getElementById("cost");
    var url = new URL(href, location.href);
    url.searchParams.delete("page");
    url.searchParams.set("part", "overview");
    if (overviewAbort) overviewAbort.abort();
    overviewAbort = typeof AbortController === "function" ? new AbortController() : null;
    setBusy(root, true);
    fetch(url.pathname + url.search, {
      credentials: "same-origin",
      cache: "no-store",
      signal: overviewAbort ? overviewAbort.signal : undefined
    }).then(function (response) {
      if (seq !== filterSeq) return null;
      var type = response.headers.get("content-type") || "";
      if (!response.ok || type.indexOf("json") < 0) throw new Error("overview");
      return response.json();
    }).then(function (data) {
      if (data == null || seq !== filterSeq) return;
      paintOverview(data);
      setBusy(root, false);
      if (data.state === "pending" || data.state === "stale") {
        window.__costLeave = false;
        if (typeof window.__costResume === "function") window.__costResume();
      }
    }).catch(function (err) {
      if (err && err.name === "AbortError") return;
      setBusy(root, false);
      if (seq === filterSeq) location.href = href;
    });
  }
  function saveCostFilterPartial(combo, href, kinds) {
    filterSeq += 1;
    var seq = filterSeq;
    window.__costLeave = true;
    markTabs(combo);
    if (window.history && typeof history.pushState === "function") history.pushState({}, "", href);
    fetch("/admin/api/costs/filters", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      body: JSON.stringify(combo)
    }).then(function (response) {
      if (response.status === 401) { location = "/admin/login"; return null; }
      if (!response.ok) throw new Error("filters");
      return response.json();
    }).then(function () {
      if (seq !== filterSeq) return;
      if (kinds.overview) refreshOverview(href, seq);
      if (kinds.detail) refreshCalls(href, seq);
      window.__costLeave = false;
    }).catch(function () {
      window.__costLeave = false;
      if (seq === filterSeq) location.href = href;
    });
  }
  window.saveCostFilter = function (control) {
    if (!control) return;
    var key = control.getAttribute("data-cost-key") || "";
    var selected = control.options ? control.options[control.selectedIndex] : null;
    var value = selected ? (selected.getAttribute("data-cost-value") || "") : "";
    if (!key) {
      var fallback = control.value ? String(control.value) : "";
      if (fallback) saveCostFilterThen(fallback, control.getAttribute("data-cost-scope") || "");
      return;
    }
    var built = applyKey(comboFromUrl(location.href), key, value, !!DETAIL_KEYS[key]);
    var href = hrefFromCombo(built.combo, built.page);
    saveCostFilterPartial(built.combo, href, {
      overview: !!OVERVIEW_KEYS[key],
      detail: !!DETAIL_KEYS[key]
    });
  };
  window.saveCostFilterThen = function (url, scopeHint) {
    if (!url) return;
    var combo = comboFromUrl(url);
    var current = comboFromUrl(location.href);
    var overview = false;
    var detail = false;
    for (var key in OVERVIEW_KEYS) if ((combo[key] || "") !== (current[key] || "")) overview = true;
    for (var dkey in DETAIL_KEYS) if ((combo[dkey] || "") !== (current[dkey] || "")) detail = true;
    if (scopeHint === "overview") overview = true;
    if (scopeHint === "detail") detail = true;
    if (!overview && !detail) detail = true;
    var page = Number(new URL(url, location.href).searchParams.get("page") || 1) || 1;
    if (detail) page = page || 1;
    saveCostFilterPartial(combo, hrefFromCombo(combo, detail ? page : (Number(new URL(location.href).searchParams.get("page") || 1) || 1)), {
      overview: overview,
      detail: detail
    });
  };
})();
document.addEventListener("click", function (event) {
  var anchor = event.target.closest ? event.target.closest("a[data-cost-filter]") : null;
  if (!anchor) return;
  window.__costLeave = true;
  event.preventDefault();
  var key = anchor.getAttribute("data-cost-key") || "";
  var value = anchor.getAttribute("data-cost-value");
  if (key) {
    var DETAIL = {detail: 1, dworker: 1, dmodel: 1};
    var OVERVIEW = {span: 1, worker: 1, model: 1};
    var combo = (function () {
      var parsed = new URL(location.href);
      var out = {span: "30d", detail: "30d", worker: "", model: "", dworker: "", dmodel: ""};
      ["span", "detail", "worker", "model", "dworker", "dmodel"].forEach(function (name) {
        if (parsed.searchParams.has(name)) out[name] = parsed.searchParams.get(name) || "";
      });
      out[key] = value == null ? "" : String(value);
      return out;
    })();
    var page = DETAIL[key] ? 1 : (Number(new URL(location.href).searchParams.get("page") || 1) || 1);
    var href = (function () {
      var url = new URL("/admin/costs", location.href);
      ["span", "detail", "worker", "model", "dworker", "dmodel"].forEach(function (name) {
        var item = combo[name] || "";
        if (name === "span" || name === "detail" || item) url.searchParams.set(name, item);
      });
      if (page > 1) url.searchParams.set("page", String(page));
      return url.pathname + url.search;
    })();
    saveCostFilterThen(href, OVERVIEW[key] ? "overview" : (DETAIL[key] ? "detail" : ""));
    return;
  }
  saveCostFilterThen(anchor.getAttribute("href") || "");
});
(function () {
  var modal = document.getElementById("price-modal");
  var fx = document.getElementById("fx");
  var save = document.getElementById("save-prices");
  function field(row, name) { return row.querySelector("[data-field='" + name + "']"); }
  function read() {
    var rows = {}, list = document.querySelectorAll(".rate-row"), i;
    for (i = 0; i < list.length; i++) {
      var row = list[i];
      rows[row.getAttribute("data-provider") + "\t" + row.getAttribute("data-model")] = {
        currency: field(row, "currency").value,
        input: field(row, "input").value,
        output: field(row, "output").value,
        cache: field(row, "cache").value
      };
    }
    return {rate: fx ? fx.value : "", rows: rows};
  }
  var saved = read();
  function numberOrBlank(value) {
    if (String(value).trim() === "") return null;
    var number = Number(value);
    if (!isFinite(number) || number < 0) return NaN;
    return number;
  }
  function valid() {
    if (!fx || Number.isNaN(numberOrBlank(fx.value))) return false;
    var list = document.querySelectorAll(".rate-row"), i;
    for (i = 0; i < list.length; i++) {
      var parts = ["input", "output", "cache"].map(function (name) { return numberOrBlank(field(list[i], name).value); });
      if (parts.some(function (part) { return Number.isNaN(part); })) return false;
      var filled = parts.filter(function (part) { return part !== null; }).length;
      if (filled !== 0 && filled !== 3) return false;
    }
    return true;
  }
  function paintSave() {
    if (!save) return;
    save.disabled = JSON.stringify(read()) === JSON.stringify(saved) || !valid();
  }
  function restore() {
    var rows = saved.rows, list = document.querySelectorAll(".rate-row"), i;
    if (fx) fx.value = saved.rate;
    for (i = 0; i < list.length; i++) {
      var row = list[i];
      var item = rows[row.getAttribute("data-provider") + "\t" + row.getAttribute("data-model")];
      if (!item) continue;
      field(row, "currency").value = item.currency;
      field(row, "input").value = item.input;
      field(row, "output").value = item.output;
      field(row, "cache").value = item.cache;
    }
    paintSave();
  }
  function closePrices() { restore(); modal.hidden = true; }
  function items() {
    var found = [], list = document.querySelectorAll(".rate-row"), i;
    for (i = 0; i < list.length; i++) {
      var row = list[i];
      found.push({
        provider: row.getAttribute("data-provider"),
        model: row.getAttribute("data-model"),
        currency: field(row, "currency").value,
        input: field(row, "input").value.trim(),
        output: field(row, "output").value.trim(),
        cache: field(row, "cache").value.trim()
      });
    }
    return found;
  }
  document.getElementById("open-prices").onclick = function () { modal.hidden = false; paintSave(); };
  document.getElementById("close-prices").onclick = closePrices;
  document.getElementById("cancel-prices").onclick = closePrices;
  modal.addEventListener("click", function (event) { if (event.target === modal) closePrices(); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && modal && !modal.hidden) closePrices();
  });
  if (fx) fx.addEventListener("input", paintSave);
  var editors = document.querySelectorAll(".rate-row input, .rate-row select"), e;
  for (e = 0; e < editors.length; e++) {
    editors[e].addEventListener("input", paintSave);
    editors[e].addEventListener("change", paintSave);
  }
  if (save) save.onclick = function () {
    if (save.disabled) return;
    post("/admin/api/costs", {rate: fx.value.trim(), items: items()});
  };
  paintSave();
})();
</script>
"""


_AVAIL_JS = r"""
  var availByAt = {};
  function availIndex(pack) {
    var map = {};
    var rows = (pack && pack.buckets) || [];
    for (var i = 0; i < rows.length; i++) map[rows[i].at] = rows[i];
    return map;
  }
  function clippedSeconds(at, grainMs, win) {
    var start = new Date(at).getTime();
    if (!isFinite(start)) return 0;
    var end = start + grainMs;
    if (win && win.start) {
      var w0 = new Date(win.start).getTime();
      if (isFinite(w0)) start = Math.max(start, w0);
    }
    if (win && win.end) {
      var w1 = new Date(win.end).getTime();
      if (isFinite(w1)) end = Math.min(end, w1);
    }
    return Math.max(0, (end - start) / 1000);
  }
  function availRead(at, fallback) {
    var hit = availByAt[at];
    if (hit) {
      return {
        ok: +hit.ok || 0,
        warn: +hit.warn || 0,
        down: +hit.down || 0,
        unknown: +hit.unknown || 0,
        span: +hit.span || 0,
        who: (hit.who || []).map(function (p) {
          return {name: p.name || "", seconds: +p.seconds || 0};
        })
      };
    }
    var span = fallback > 0 ? fallback : 0;
    return {ok: 0, warn: 0, down: 0, unknown: span, span: span, who: []};
  }
  function availAdd(into, extra) {
    into.ok += extra.ok;
    into.warn += extra.warn;
    into.down += extra.down;
    into.unknown += extra.unknown;
    into.span += extra.span;
    if (!into.who) into.who = [];
    var extraWho = extra.who || [];
    for (var w = 0; w < extraWho.length; w++) {
      var piece = extraWho[w];
      if (!piece || !piece.name) continue;
      var found = null;
      for (var k = 0; k < into.who.length; k++) {
        if (into.who[k].name === piece.name) { found = into.who[k]; break; }
      }
      if (found) found.seconds += piece.seconds || 0;
      else into.who.push({name: piece.name, seconds: piece.seconds || 0});
    }
  }
  // Same rule as rogmem.availability.tone: depth is that state's share of the slot.
  function availTone(a) {
    a = a || {};
    var ok = +a.ok || 0, warn = +a.warn || 0, down = +a.down || 0, unknown = +a.unknown || 0;
    var span = ok + warn + down + unknown;
    if (!(span > 0) || !(ok + warn + down > 0)) return {tone: "unknown", depth: 0};
    if (down <= 0 && warn <= 0) return {tone: "ok", depth: ok / span};
    if (down >= warn) return {tone: "down", depth: down / span};
    return {tone: "warn", depth: warn / span};
  }
  // Same charcoal-ember formula as rogmem.availability.fill.
  function availFill(kind, depth) {
    var d = depth > 0 ? depth : 0;
    if (d > 1) d = 1;
    var hue = kind === "down" ? 352 : kind === "ok" ? 152 : 42;
    var sat = (kind === "ok" ? 48 : 70) + (kind === "ok" ? 22 : 25) * d;
    var light = (kind === "down" ? 78 : kind === "ok" ? 80 : 82) - (kind === "down" ? 34 : 38) * d;
    return "hsl(" + hue + " " + sat.toFixed(1) + "% " + light.toFixed(1) + "%)";
  }
  function availWho(pack) {
    pack = pack || {};
    if (pack.name && pack.model) return pack.name + " · " + pack.model;
    return pack.name || "当时调用的模型";
  }
  function availPhrase(a, name) {
    a = a || {};
    var ok = +a.ok || 0, warn = +a.warn || 0, down = +a.down || 0, unknown = +a.unknown || 0;
    function mins(sec) {
      if (!(sec > 0)) return "";
      var whole = Math.round(sec / 60);
      if (whole < 1) return "不到 1 分钟";
      if (whole < 60) return whole + " 分钟";
      var hours = Math.floor(whole / 60);
      var rest = whole % 60;
      return rest ? (hours + " 小时 " + rest + " 分钟") : (hours + " 小时");
    }
    var rows = a.who || [];
    var speaker = "";
    if (rows.length === 1 && rows[0].name) speaker = rows[0].name;
    else if (rows.length > 1) {
      var named = [];
      for (var i = 0; i < rows.length; i++) {
        if (rows[i] && rows[i].name) named.push(rows[i].name + " " + mins(rows[i].seconds));
      }
      speaker = named.join("，");
    }
    if (!speaker) speaker = name || "当时调用的模型";
    var parts = [];
    if (ok) parts.push("可用 " + mins(ok));
    if (warn) parts.push("降级 " + mins(warn));
    if (down) parts.push("不可用 " + mins(down));
    if (unknown) parts.push("未探活 " + mins(unknown));
    if (!parts.length) parts.push("未探活");
    return speaker + " · " + parts.join(" · ");
  }
  function availEscape(value) {
    return String(value == null ? "" : value).replace(/[&<>"]/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch];
    });
  }
  function paintAvail(host, slots, axisY, who) {
    if (!slots.length) return;
    var height = 8;
    var y = axisY + 26;
    var row = document.createElementNS(SVG_NS, "g");
    row.setAttribute("class", "avail-row");
    row.setAttribute("aria-label", who);
    for (var i = 0; i < slots.length; i++) {
      var slot = slots[i];
      var record = (slot.bucket && slot.bucket.avail) || {ok: 0, warn: 0, down: 0, unknown: 0, span: 0};
      var painted = availTone(record);
      var width = slot.barW > 0 ? slot.barW : (slot.hitW || 1);
      var x = slot.barX != null ? slot.barX : slot.hitX;
      var mark = document.createElementNS(SVG_NS, "rect");
      mark.setAttribute("class", painted.tone === "unknown" ? "avail unknown" : "avail");
      mark.setAttribute("data-tone", painted.tone);
      mark.setAttribute("data-depth", String(Math.round(painted.depth * 1000) / 1000));
      mark.setAttribute("x", x);
      mark.setAttribute("y", y);
      mark.setAttribute("width", width);
      mark.setAttribute("height", height);
      if (painted.tone !== "unknown") mark.setAttribute("fill", availFill(painted.tone, painted.depth));
      var title = document.createElementNS(SVG_NS, "title");
      title.textContent = availPhrase(record, who);
      mark.appendChild(title);
      row.appendChild(mark);
    }
    host.appendChild(row);
  }
"""

_CHART_SCRIPT = r"""
<script>
window.__rogmemCostChartBoot = function (COST) {
  if (window.__rogmemCostChartCleanup) {
    try { window.__rogmemCostChartCleanup(); } catch (err) {}
    window.__rogmemCostChartCleanup = null;
  }
  var container = document.querySelector(".ov-chart");
  var svg = document.getElementById("ov-svg");
  var tip = document.getElementById("ov-tip");
  var caption = document.getElementById("ov-caption");
  if (!svg || !container) return;

  var SVG_NS = "http://www.w3.org/2000/svg";
__AVAIL_JS__
  var CHAR_PX = 7.2;
  var MIN_BAR_PITCH = 4;
  var SHIFT_MS = 8 * 3600 * 1000;
  var unit = "money";
  var activeIndex = -1;
  var displayBuckets = [];
  var layout = [];

  function text(v) {
    return String(v == null ? "" : v).replace(/[&<>"]/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch];
    });
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function fmtMD(d) { return pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()); }
  function fmtHM(d) { return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()); }
  function fmtYMD(d) { return d.getUTCFullYear() + "-" + fmtMD(d); }
  function toCST(iso) { return new Date(new Date(iso).getTime() + SHIFT_MS); }
  function grainDeltaMs(grain) { return grain === "hour" ? 3600e3 : 86400e3; }

  // Chart copy only. One scale for money and tokens: millions, two decimals, unit written out.
  // Exact counts stay in the call table; this formatter never prints them.
  function fmtMillion(n, money) {
    var v = Number(n);
    if (!isFinite(v)) v = 0;
    var body = (v / 1e6).toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return (money ? "¥" : "") + body + " 百万";
  }

  // 1) Data domain --------------------------------------------------------
  function buildDisplayBuckets(series, windowStart, windowEnd, maxBars) {
    if (!series || series.length === 0) return [];
    var delta = grainDeltaMs(COST.grain);
    if (series.length <= maxBars) {
      return series.map(function (item) {
        var startMs = new Date(item.at).getTime();
        // moneyKnown comes from the backend _public_sum.money_known — it is the priced subset
        // sum for this bucket, kept even when money=null (bucket has unpriced calls).
        return {
          startMs: startMs,
          endMs: startMs + delta,
          money: item.money,
          moneyKnown: item.money_known != null ? item.money_known : (item.money != null ? item.money : 0),
          tokens: item.tokens || 0,
          input: item.input || 0,
          output: item.output || 0,
          cache: item.cache || 0,
          unpriced: item.unpriced || 0,
          bare: item.bare || 0,
          mergedCount: 1,
          grain: item.grain || COST.grain,
          avail: availRead(item.at, clippedSeconds(item.at, delta, COST.window))
        };
      });
    }
    var k = Math.ceil(series.length / maxBars);
    var out = [];
    for (var i = 0; i < series.length; i += k) {
      var chunk = series.slice(i, i + k);
      var startMs = new Date(chunk[0].at).getTime();
      var lastMs = new Date(chunk[chunk.length - 1].at).getTime();
      // Mirrors cost._public_sum: unpriced>0 ⇒ money is None; else sum of priced money (0 when none).
      var merged = {
        startMs: startMs,
        endMs: lastMs + delta,
        money: 0,
        moneyKnown: 0,
        tokens: 0,
        input: 0,
        output: 0,
        cache: 0,
        unpriced: 0,
        bare: 0,
        mergedCount: chunk.length,
        grain: COST.grain,
        avail: {ok: 0, warn: 0, down: 0, unknown: 0, span: 0}
      };
      for (var j = 0; j < chunk.length; j++) {
        var it = chunk[j];
        // Always accumulate the priced subset from every child, including children that have
        // their own money=null but a non-zero money_known (mixed single-bucket case).
        if (it.money_known != null) merged.moneyKnown += it.money_known;
        else if (it.money != null) merged.moneyKnown += it.money;
        merged.tokens += it.tokens || 0;
        merged.input += it.input || 0;
        merged.output += it.output || 0;
        merged.cache += it.cache || 0;
        merged.unpriced += it.unpriced || 0;
        merged.bare += it.bare || 0;
        availAdd(merged.avail, availRead(it.at, clippedSeconds(it.at, delta, COST.window)));
      }
      merged.money = merged.unpriced > 0 ? null : merged.moneyKnown;
      out.push(merged);
    }
    return out;
  }

  // 2) Scale domain -------------------------------------------------------
  function niceNum(x) {
    if (!(x > 0)) return 1;
    var exp = Math.floor(Math.log10(x));
    var f = x / Math.pow(10, exp);
    var nf;
    if (f < 1.5) nf = 1;
    else if (f < 3) nf = 2;
    else if (f < 7) nf = 5;
    else nf = 10;
    return nf * Math.pow(10, exp);
  }
  function formatAxisValue(v, u) {
    return fmtMillion(v, u === "money");
  }
  function buildYScale(max, u) {
    if (!(max > 0)) {
      return {max: 0, step: 0, ticks: [{value: 0, label: formatAxisValue(0, u)}]};
    }
    var step = niceNum(max / 4);
    var top = Math.ceil(max / step) * step;
    var ticks = [];
    for (var v = 0; v <= top + step * 1e-9; v += step) {
      ticks.push({value: v, label: formatAxisValue(v, u)});
    }
    return {max: top, step: step, ticks: ticks};
  }

  function buildXTicks(buckets, chartLeft, chartRight, grain, windowStart, windowEnd) {
    var n = buckets.length;
    if (n === 0) return [];
    var trackW = chartRight - chartLeft;
    var step = trackW / n;
    var winStartDt = toCST(windowStart);
    var winEndDt = toCST(windowEnd);
    var crossDay = fmtYMD(winStartDt) !== fmtYMD(winEndDt);
    var crossYear = winStartDt.getUTCFullYear() !== winEndDt.getUTCFullYear();
    var totalDays = (winEndDt.getTime() - winStartDt.getTime()) / 86400e3;
    var candidates = [];
    for (var i = 0; i < n; i++) {
      var d = new Date(buckets[i].startMs + SHIFT_MS);
      var x = chartLeft + (i + 0.5) * step;
      var label = "";
      if (grain === "hour") {
        if (d.getUTCHours() % 2 !== 0) continue;
        label = fmtHM(d);
      } else {
        if (totalDays > 365) {
          if (d.getUTCDate() !== 1) continue;
          label = d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1);
        } else if (totalDays > 60) {
          if (d.getUTCDay() !== 1 && d.getUTCDate() !== 1) continue;
          label = fmtMD(d);
        } else {
          label = fmtMD(d);
        }
      }
      candidates.push({index: i, x: x, label: label, d: d});
    }
    if (grain === "hour" && crossDay) {
      var lastDay = null;
      for (var c = 0; c < candidates.length; c++) {
        var today = fmtYMD(candidates[c].d);
        if (today !== lastDay) {
          candidates[c].label = fmtMD(candidates[c].d) + " " + candidates[c].label;
          lastDay = today;
        }
      }
    } else if (grain === "day" && crossYear && totalDays <= 365) {
      var lastYear = null;
      for (var c2 = 0; c2 < candidates.length; c2++) {
        var y = candidates[c2].d.getUTCFullYear();
        if (y !== lastYear) {
          candidates[c2].label = fmtYMD(candidates[c2].d);
          lastYear = y;
        }
      }
    }
    return candidates;
  }

  function measureLabels(candidates) {
    // Prefer real getBBox widths so we react to the actual font/zoom instead of a hard-coded CHAR_PX.
    var tmpGroup = document.createElementNS(SVG_NS, "g");
    tmpGroup.setAttribute("aria-hidden", "true");
    tmpGroup.setAttribute("visibility", "hidden");
    svg.appendChild(tmpGroup);
    for (var i = 0; i < candidates.length; i++) {
      var t = document.createElementNS(SVG_NS, "text");
      t.setAttribute("class", "axis");
      t.setAttribute("x", 0);
      t.setAttribute("y", 0);
      t.setAttribute("text-anchor", "middle");
      t.textContent = candidates[i].label;
      tmpGroup.appendChild(t);
      candidates[i].__node = t;
    }
    for (var j = 0; j < candidates.length; j++) {
      try {
        var w = candidates[j].__node.getBBox().width;
        if (w > 0) candidates[j].width = w;
      } catch (err) {
        // getBBox can throw on detached SVGs; fall through to CHAR_PX estimate.
      }
      if (!candidates[j].width) candidates[j].width = candidates[j].label.length * CHAR_PX;
      candidates[j].__node = null;
    }
    tmpGroup.parentNode.removeChild(tmpGroup);
    return candidates;
  }

  function dodgeXTicks(candidates) {
    function halfWidth(t) { return (t.width || (t.label.length * CHAR_PX)) / 2 + 8; }
    function collides(a, b) { return (a.x + halfWidth(a)) > (b.x - halfWidth(b)); }
    var out = [];
    for (var i = 0; i < candidates.length; i++) {
      var cur = candidates[i];
      if (out.length === 0) { out.push(cur); continue; }
      var prev = out[out.length - 1];
      if (collides(prev, cur)) {
        if (i === candidates.length - 1) {
          while (out.length > 0 && collides(out[out.length - 1], cur)) out.pop();
          out.push(cur);
        }
        continue;
      }
      out.push(cur);
    }
    return out;
  }

  // 3) Layout domain ------------------------------------------------------
  function measureTextWidth(sample, group) {
    // Real bbox of a sample label with the current axis font. Width is sized against the live
    // yScale ticks (not a hard-coded "¥10.00M" sample that never occurs in real labels).
    var tmp = document.createElementNS(SVG_NS, "text");
    tmp.setAttribute("class", "axis");
    tmp.setAttribute("x", 0);
    tmp.setAttribute("y", 0);
    tmp.setAttribute("visibility", "hidden");
    tmp.textContent = sample || "";
    (group || svg).appendChild(tmp);
    var w = 0;
    try { w = tmp.getBBox().width; } catch (err) { w = (sample || "").length * CHAR_PX; }
    tmp.parentNode.removeChild(tmp);
    return w;
  }

  function envelope() {
    // Pure geometric frame (without the y gutter). The y gutter is decided later in render()
    // once the real tick labels exist and can be measured.
    var rect = svg.getBoundingClientRect();
    var w = rect.width || container.getBoundingClientRect().width;
    var h = rect.height || 244;
    var narrow = w < 520;
    return {
      w: w,
      h: h,
      chartRight: w - 16,
      chartTop: 12,
      chartBottom: h - (narrow ? 56 : 52),
      narrow: narrow
    };
  }
  function valueOf(b) { return unit === "money" ? (b.money == null ? 0 : b.money) : (b.tokens || 0); }
  function layoutBars(buckets, geom, max) {
    var trackW = geom.chartRight - geom.chartLeft;
    var trackH = geom.chartBottom - geom.chartTop;
    var n = buckets.length;
    var step = n > 0 ? trackW / n : trackW;
    var barW = Math.max(1, step * 0.72);
    var out = [];
    for (var i = 0; i < n; i++) {
      var b = buckets[i];
      var v = valueOf(b);
      var h = (max > 0 && v > 0) ? (v / max) * trackH : 0;
      out.push({
        index: i,
        hitX: geom.chartLeft + i * step,
        hitY: geom.chartTop,
        hitW: step,
        hitH: trackH,
        barX: geom.chartLeft + i * step + (step - barW) / 2,
        barY: geom.chartBottom - h,
        barW: barW,
        barH: h,
        bucket: b
      });
    }
    return out;
  }

  // 4) Interaction domain -------------------------------------------------
  function formatBucketRange(bucket) {
    var winStartMs = new Date(COST.window.start).getTime();
    var winEndMs = new Date(COST.window.end).getTime();
    var s = Math.max(bucket.startMs, winStartMs);
    var e = Math.min(bucket.endMs, winEndMs);
    var sd = new Date(s + SHIFT_MS);
    var ed = new Date(e + SHIFT_MS);
    var fullDay = bucket.grain === "day" && bucket.mergedCount === 1 && s === bucket.startMs && e === bucket.endMs;
    if (fullDay) return fmtYMD(sd) + " 00:00 – 23:59 · UTC+8";
    var sameDay = fmtYMD(sd) === fmtYMD(ed);
    if (bucket.mergedCount > 1) {
      return fmtYMD(sd) + " " + fmtHM(sd) + " – " + fmtYMD(ed) + " " + fmtHM(ed) + " · UTC+8 · 合并 " + bucket.mergedCount + " 桶";
    }
    if (sameDay) {
      return fmtYMD(sd) + " " + fmtHM(sd) + " – " + fmtHM(ed) + " · UTC+8";
    }
    return fmtYMD(sd) + " " + fmtHM(sd) + " – " + fmtYMD(ed) + " " + fmtHM(ed) + " · UTC+8";
  }

  function moneyLineText(b) {
    if (b.money == null) {
      if (b.unpriced > 0 && b.moneyKnown > 0) {
        return "金额未完整计入（含 " + b.unpriced + " 条未计价，已计价部分 " + fmtMillion(b.moneyKnown, true) + "）";
      }
      return "金额未计入";
    }
    return fmtMillion(b.money, true);
  }
  function renderTooltip(index) {
    if (index < 0 || index >= displayBuckets.length) { tip.hidden = true; return; }
    var b = displayBuckets[index];
    var html = "<b>" + text(formatBucketRange(b)) + "</b>";
    if (COST.scope) html += "<span class='scope'>" + text(COST.scope) + "</span>";
    html += "<p>" + (unit === "money" ? moneyLineText(b) : fmtMillion(b.tokens || 0, false)) + "</p>";
    html += "<p>输入 " + fmtMillion(b.input || 0, false) + " · 输出 " + fmtMillion(b.output || 0, false) + " · 缓存 " + fmtMillion(b.cache || 0, false) + "</p>";
    if (unit === "token") html += "<p>" + moneyLineText(b) + "</p>";
    if (b.bare > 0) html += "<p class='scope'>" + b.bare + " 条调用未返回用量</p>";
    html += "<p>" + text(availPhrase(b.avail, availWho(COST.availability))) + "</p>";
    tip.innerHTML = html;
    tip.hidden = false;
  }

  function moveTip(x, y) {
    var gap = 14, width = tip.offsetWidth, height = tip.offsetHeight;
    var tx = x + gap, ty = y + gap;
    if (tx + width > window.innerWidth - 8) tx = x - width - gap;
    if (ty + height > window.innerHeight - 8) ty = y - height - gap;
    tip.style.left = tx + "px";
    tip.style.top = ty + "px";
  }

  function highlight(index) {
    var bars = svg.querySelectorAll("rect.bar");
    for (var i = 0; i < bars.length; i++) bars[i].classList.toggle("on", i === index);
    var marks = svg.querySelectorAll("rect.avail");
    for (var m = 0; m < marks.length; m++) marks[m].classList.toggle("on", m === index);
  }

  function setActive(index, screenX, screenY) {
    activeIndex = index;
    if (index < 0) { tip.hidden = true; highlight(-1); svg.removeAttribute("aria-activedescendant"); return; }
    renderTooltip(index);
    highlight(index);
    if (screenX != null) moveTip(screenX, screenY);
    else {
      var L = layout[index];
      var rect = svg.getBoundingClientRect();
      moveTip(rect.left + L.barX + L.barW / 2, rect.top + L.barY);
    }
    svg.setAttribute("aria-activedescendant", "ov-bar-" + index);
  }

  function bindPointerAndKeyboard() {
    svg.addEventListener("pointermove", function (ev) {
      if (layout.length === 0) return;
      var rect = svg.getBoundingClientRect();
      var lx = ev.clientX - rect.left;
      var idx = -1;
      for (var i = 0; i < layout.length; i++) {
        var L = layout[i];
        if (lx >= L.hitX && lx < L.hitX + L.hitW) { idx = i; break; }
      }
      if (idx >= 0) setActive(idx, ev.clientX, ev.clientY);
      else setActive(-1);
    });
    svg.addEventListener("pointerdown", function (ev) {
      if (layout.length === 0) return;
      var rect = svg.getBoundingClientRect();
      var lx = ev.clientX - rect.left;
      var idx = -1;
      for (var i = 0; i < layout.length; i++) {
        var L = layout[i];
        if (lx >= L.hitX && lx < L.hitX + L.hitW) { idx = i; break; }
      }
      if (idx >= 0) setActive(idx, ev.clientX, ev.clientY);
    });
    svg.addEventListener("pointerleave", function (ev) {
      // Touch pointers always fire `pointerleave` immediately after `pointerdown` releases, which
      // would wipe the tip the user just tapped. Only mouse-tracked hovers trigger the auto-dismiss.
      if (ev && ev.pointerType === "touch") return;
      setActive(-1);
    });
    svg.addEventListener("keydown", function (ev) {
      if (layout.length === 0) return;
      var moved = true;
      if (ev.key === "ArrowRight") setActive(Math.min(layout.length - 1, (activeIndex < 0 ? 0 : activeIndex + 1)));
      else if (ev.key === "ArrowLeft") setActive(Math.max(0, (activeIndex < 0 ? 0 : activeIndex - 1)));
      else if (ev.key === "Home") setActive(0);
      else if (ev.key === "End") setActive(layout.length - 1);
      else if (ev.key === "Escape") setActive(-1);
      else moved = false;
      if (moved) ev.preventDefault();
    });
    svg.addEventListener("blur", function () { setActive(-1); });
    document.addEventListener("pointerdown", function (ev) {
      if (!svg.contains(ev.target)) setActive(-1);
    });
  }

  // 5) Orchestration ------------------------------------------------------
  function render() {
    // Reset interaction state across every redraw so unit toggles / resize cannot leave a stale
    // tooltip pointing at a bucket index that no longer exists after merge changes the count.
    availByAt = availIndex(COST.availability);
    setActive(-1);
    var env = envelope();
    svg.setAttribute("width", env.w);
    svg.setAttribute("height", env.h);
    svg.innerHTML = "";
    layout = [];

    if (COST.observation && COST.observation.state === "failed") {
      caption.textContent = "汇总没有完成";
      svg.setAttribute("aria-label", "成本趋势：汇总没有完成");
      return;
    }
    if (COST.observed === false) {
      caption.textContent = "正在汇总这一时段";
      svg.setAttribute("aria-label", "成本趋势：正在汇总");
      return;
    }

    if (!COST.window || COST.window.available === false || !COST.window.start || !COST.window.end) {
      // The chart refuses to invent query edges; caller must pass a real window from cost.overview.
      caption.textContent = "窗口信息缺失，无法绘制图表";
      return;
    }

    // Provisional track to decide the merge budget. 44px is just a floor so we can start
    // computing display buckets; the real chartLeft comes later from the actual tick labels.
    var provisionalLeft = 44;
    var provisionalTrackW = env.chartRight - provisionalLeft;
    if (provisionalTrackW < 120) {
      caption.textContent = "图表区域过窄，请缩小筛选范围";
      return;
    }
    var maxBars = Math.max(8, Math.floor(provisionalTrackW / MIN_BAR_PITCH));
    displayBuckets = buildDisplayBuckets(COST.series, COST.window.start, COST.window.end, maxBars);
    var n = displayBuckets.length;

    if (n === 0) {
      caption.textContent = "所选时段无数据";
      svg.setAttribute("aria-label", "成本趋势：无数据");
      return;
    }
    caption.textContent = "所选时段总用量 · UTC+8 · 悬停或方向键查看";

    var vals = displayBuckets.map(valueOf);
    var max = 0;
    for (var vi = 0; vi < vals.length; vi++) if (vals[vi] > max) max = vals[vi];

    // Y labels are millions only. Measure those labels and let the gutter follow the real width.
    var yScale = buildYScale(max, unit);
    var tmpGroup = document.createElementNS(SVG_NS, "g");
    tmpGroup.setAttribute("visibility", "hidden");
    svg.appendChild(tmpGroup);
    function widestTickWidth(ticks) {
      var wm = 0;
      for (var ti = 0; ti < ticks.length; ti++) {
        var tw = measureTextWidth(ticks[ti].label, tmpGroup);
        if (tw > wm) wm = tw;
      }
      return wm;
    }
    var widest = widestTickWidth(yScale.ticks);
    tmpGroup.parentNode.removeChild(tmpGroup);

    var chartLeft = Math.ceil(widest + 12);
    if (chartLeft < 32) chartLeft = 32;
    if (chartLeft > env.w / 2) chartLeft = Math.floor(env.w / 2);
    var finalTrackW = env.chartRight - chartLeft;
    if (finalTrackW < 160) {
      caption.textContent = "图表区域过窄，请缩小筛选范围";
      return;
    }
    var newMaxBars = Math.max(8, Math.floor(finalTrackW / MIN_BAR_PITCH));
    if (newMaxBars < displayBuckets.length) {
      displayBuckets = buildDisplayBuckets(COST.series, COST.window.start, COST.window.end, newMaxBars);
      n = displayBuckets.length;
      vals = displayBuckets.map(valueOf);
      max = 0;
      for (var vj = 0; vj < vals.length; vj++) if (vals[vj] > max) max = vals[vj];
      yScale = buildYScale(max, unit);
    }
    var geom = {
      w: env.w, h: env.h, chartLeft: chartLeft, chartRight: env.chartRight,
      chartTop: env.chartTop, chartBottom: env.chartBottom, narrow: env.narrow
    };
    for (var yi = 0; yi < yScale.ticks.length; yi++) {
      var t = yScale.ticks[yi];
      var ratio = yScale.max > 0 ? t.value / yScale.max : 0;
      var y = geom.chartBottom - ratio * (geom.chartBottom - geom.chartTop);
      var ln = document.createElementNS(SVG_NS, "line");
      ln.setAttribute("class", "grid");
      ln.setAttribute("x1", geom.chartLeft);
      ln.setAttribute("x2", geom.chartRight);
      ln.setAttribute("y1", y);
      ln.setAttribute("y2", y);
      svg.appendChild(ln);
      var lab = document.createElementNS(SVG_NS, "text");
      lab.setAttribute("class", "axis");
      lab.setAttribute("x", geom.chartLeft - 8);
      lab.setAttribute("y", y + 4);
      lab.setAttribute("text-anchor", "end");
      lab.textContent = t.label;
      svg.appendChild(lab);
    }

    layout = layoutBars(displayBuckets, geom, yScale.max);

    for (var i = 0; i < layout.length; i++) {
      var L = layout[i];
      var bar = document.createElementNS(SVG_NS, "rect");
      bar.setAttribute("id", "ov-bar-" + i);
      bar.setAttribute("class", "bar");
      bar.setAttribute("x", L.barX);
      bar.setAttribute("y", L.barY);
      bar.setAttribute("width", L.barW);
      bar.setAttribute("height", L.barH);
      var title = document.createElementNS(SVG_NS, "title");
      title.textContent = formatBucketRange(L.bucket) + " — " + (unit === "money" ? moneyLineText(L.bucket) : fmtMillion(L.bucket.tokens || 0, false));
      bar.appendChild(title);
      svg.appendChild(bar);
      var hit = document.createElementNS(SVG_NS, "rect");
      hit.setAttribute("class", "hit");
      hit.setAttribute("data-i", i);
      hit.setAttribute("x", L.hitX);
      hit.setAttribute("y", L.hitY);
      hit.setAttribute("width", L.hitW);
      hit.setAttribute("height", L.hitH);
      svg.appendChild(hit);
    }

    var candidates = buildXTicks(displayBuckets, geom.chartLeft, geom.chartRight, COST.grain, COST.window.start, COST.window.end);
    if (n > 0) {
      var step = (geom.chartRight - geom.chartLeft) / n;
      var winStartDt2 = toCST(COST.window.start);
      var winEndDt2 = toCST(COST.window.end);
      var totalDays2 = (winEndDt2.getTime() - winStartDt2.getTime()) / 86400e3;
      function defaultTickLabel(d) {
        if (COST.grain === "hour") return fmtHM(d);
        if (totalDays2 > 365) return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1);
        if (totalDays2 > 60) return fmtMD(d);
        return fmtMD(d);
      }
      if (candidates.length === 0 || candidates[0].index !== 0) {
        var d0 = new Date(displayBuckets[0].startMs + SHIFT_MS);
        candidates.unshift({index: 0, x: geom.chartLeft + 0.5 * step, label: defaultTickLabel(d0), d: d0});
      }
      if (candidates[candidates.length - 1].index !== n - 1) {
        var dN = new Date(displayBuckets[n - 1].startMs + SHIFT_MS);
        candidates.push({index: n - 1, x: geom.chartLeft + (n - 0.5) * step, label: defaultTickLabel(dN), d: dN});
      }
    }
    measureLabels(candidates);
    var ticks = dodgeXTicks(candidates);
    svg.setAttribute("aria-label", "成本趋势，共 " + n + " 个数据桶，纵轴单位百万");
    for (var xi = 0; xi < ticks.length; xi++) {
      var tk = ticks[xi];
      var tline = document.createElementNS(SVG_NS, "line");
      tline.setAttribute("class", "tick");
      tline.setAttribute("x1", tk.x);
      tline.setAttribute("x2", tk.x);
      tline.setAttribute("y1", geom.chartBottom);
      tline.setAttribute("y2", geom.chartBottom + 4);
      svg.appendChild(tline);
      var tlab = document.createElementNS(SVG_NS, "text");
      tlab.setAttribute("class", "axis");
      var halfW = (tk.width || (tk.label.length * CHAR_PX)) / 2;
      // Prevent the first/last label from extending past the svg edges when the chart is tight.
      var anchor = "middle";
      var xpos = tk.x;
      if (tk.x - halfW < 2) { anchor = "start"; xpos = Math.max(tk.x, 2); }
      else if (tk.x + halfW > geom.w - 2) { anchor = "end"; xpos = Math.min(tk.x, geom.w - 2); }
      tlab.setAttribute("x", xpos);
      tlab.setAttribute("y", geom.chartBottom + 18);
      tlab.setAttribute("text-anchor", anchor);
      tlab.textContent = tk.label;
      svg.appendChild(tlab);
    }

    var base = document.createElementNS(SVG_NS, "line");
    base.setAttribute("class", "axis");
    base.setAttribute("x1", geom.chartLeft);
    base.setAttribute("x2", geom.chartRight);
    base.setAttribute("y1", geom.chartBottom);
    base.setAttribute("y2", geom.chartBottom);
    svg.appendChild(base);
    paintAvail(svg, layout, geom.chartBottom, availWho(COST.availability));

    if (!svg.__bound) {
      bindPointerAndKeyboard();
      svg.__bound = true;
    }
    lastRenderWidth = geom.w;
    if (COST.observation && COST.observation.state === "stale") {
      caption.textContent = "记录有变，正在重算这一时段";
    }
  }

  var buttons = document.querySelectorAll("#ov-unit button");
  for (var b = 0; b < buttons.length; b++) {
    buttons[b].onclick = function () {
      unit = this.getAttribute("data-unit");
      document.getElementById("ov-money-box").classList.toggle("is-secondary", unit !== "money");
      document.getElementById("ov-token-box").classList.toggle("is-secondary", unit !== "token");
      for (var ii = 0; ii < buttons.length; ii++) buttons[ii].classList.toggle("on", buttons[ii].getAttribute("data-unit") === unit);
      render();
    };
  }

  var lastRenderWidth = -1;
  var rerenderTimer = null;
  function schedule() {
    if (rerenderTimer) clearTimeout(rerenderTimer);
    rerenderTimer = setTimeout(function () {
      rerenderTimer = null;
      // ResizeObserver can fire spuriously after fonts load / dialog opens / scroll even when the
      // width is unchanged. Re-rendering here would clear a tooltip the user just tapped. Skip.
      var w = svg.getBoundingClientRect().width;
      if (Math.round(w) === Math.round(lastRenderWidth)) return;
      render();
    }, 50);
  }
  var resizeObserver = null;
  if (window.ResizeObserver) {
    resizeObserver = new ResizeObserver(schedule);
    resizeObserver.observe(container);
  } else window.addEventListener("resize", schedule);
  window.__rogmemCostChartCleanup = function () {
    if (rerenderTimer) clearTimeout(rerenderTimer);
    if (resizeObserver) resizeObserver.disconnect();
    else window.removeEventListener("resize", schedule);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
  // Read-only test probe. Not a public API: only snapshot() is exposed, and it returns a frozen deep
  // copy of state so callers cannot mutate internal buckets/layout. Needed so Playwright can assert
  // bucket conservation and layout bounds without intrusive hooks; nothing in product code reads it.
  function snapshot() {
    var buckets = displayBuckets.map(function (b) {
      return Object.freeze({
        startMs: b.startMs, endMs: b.endMs, money: b.money, moneyKnown: b.moneyKnown,
        tokens: b.tokens, input: b.input, output: b.output, cache: b.cache,
        unpriced: b.unpriced, bare: b.bare, mergedCount: b.mergedCount, grain: b.grain,
        avail: b.avail || null
      });
    });
    var bars = layout.map(function (L) {
      return Object.freeze({index: L.index, barX: L.barX, barY: L.barY, barW: L.barW, barH: L.barH, hitX: L.hitX, hitW: L.hitW});
    });
    return Object.freeze({
      displayBuckets: Object.freeze(buckets),
      layout: Object.freeze(bars),
      unit: unit,
      activeIndex: activeIndex,
      ready: true
    });
  }
  window.__COST_CHART__ = Object.freeze({render: render, snapshot: snapshot});
};
window.__rogmemCostChartBoot(__COST__);
</script>
"""


def _cost_params(board: dict, **changes) -> dict:
    state = {
        "span": str(board.get("span") or "30d"),
        "worker": str(board.get("worker") or ""),
        "model": str(board.get("model") or ""),
        "detail": str(board.get("detail") or "30d"),
        "dworker": str(board.get("dworker") or ""),
        "dmodel": str(board.get("dmodel") or ""),
    }
    state.update({key: value for key, value in changes.items() if key != "page"})
    params = {"span": str(state["span"]), "detail": str(state["detail"])}
    for key in ("worker", "model", "dworker", "dmodel"):
        if state[key]:
            params[key] = str(state[key])
    return params


def _cost_href(board: dict, **changes) -> str:
    try:
        page_no = int(changes.get("page", board.get("page") or 1))
    except (TypeError, ValueError):
        page_no = 1
    return _href("/admin/costs", _cost_params(board, **changes), page_no)


def _yuan(value) -> str:
    if value is None:
        return "未计入"
    number = float(value)
    digits = 2 if number == 0 or abs(number) >= 0.01 else 4
    return "¥" + f"{number:,.{digits}f}"


def _million(value) -> str:
    million = float(value or 0) / 1_000_000
    if million >= 1:
        body = f"{million:,.2f}"
    else:
        body = f"{million:,.2f}".rstrip("0").rstrip(".") or "0"
    return body + " 百万"


def _grouped(value) -> str:
    return f"{int(value):,}"


def _plain_price(value: float) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".") or "0"


def _cost_headline(summary: dict) -> str:
    if summary.get("money") is None and summary.get("unpriced"):
        return "未计入"
    return _yuan(summary.get("money") or 0)


def _cost_catalog(providers: list[dict], prices: dict, called: list[dict]) -> list[tuple[str, str, str, str]]:
    """Every model that can be priced or has already been called, grouped by supplier."""

    names = {}
    for provider in providers or []:
        names[str(provider.get("id") or "")] = _provider_label(provider) or str(provider.get("id") or "")
    found: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(provider_id: str, model: str) -> None:
        key = (provider_id, model)
        if not model or key in seen:
            return
        seen.add(key)
        vendor = names.get(provider_id) or provider_id or "未命名"
        found.append((provider_id, model, provider_id + "\t" + model, vendor))

    for provider in providers or []:
        provider_id = str(provider.get("id") or "")
        for model in provider.get("models") or []:
            add(provider_id, str(model))
    for item in called or []:
        add(str(item.get("provider") or ""), str(item.get("model") or ""))
    for provider_id, model in prices or {}:
        add(str(provider_id), str(model))
    return found


def _cost_tabs(board: dict, key: str, labels: tuple[tuple[str, str], ...], *, reset_page: bool) -> str:
    current = str(board.get(key) or "")
    scope = "detail" if key in {"detail", "dworker", "dmodel"} else "overview"
    parts = []
    for value, label in labels:
        changes: dict = {key: value}
        if reset_page:
            changes["page"] = 1
        mark = " class='on'" if current == value else ""
        parts.append(
            f"<a{mark} data-cost-filter data-cost-scope='{scope}' data-cost-key='{esc(key)}' "
            f"data-cost-value='{esc(value)}' href='{esc(_cost_href(board, **changes))}'>{label}</a>"
        )
    return "".join(parts)


def _cost_select(board: dict, key: str, label: str, aria: str, options: list[tuple[str, str]], *, reset_page: bool) -> str:
    current = str(board.get(key) or "")
    scope = "detail" if key in {"detail", "dworker", "dmodel"} else "overview"
    reset = {"page": 1} if reset_page else {}
    choices = [
        f"<option data-cost-value='' value='{esc(_cost_href(board, **{key: ''}, **reset))}'"
        f"{'' if current else ' selected'}>全部</option>"
    ]
    for value, text in options:
        selected = " selected" if current == value else ""
        choices.append(
            f"<option data-cost-value='{esc(value)}' value='{esc(_cost_href(board, **{key: value}, **reset))}'"
            f"{selected}>{esc(text)}</option>"
        )
    return (
        f"<label>{label} <select aria-label='{esc(aria)}' data-cost-key='{esc(key)}' "
        f"data-cost-scope='{scope}' onchange='saveCostFilter(this)'>"
        f"{''.join(choices)}</select></label>"
    )


def _currency_options(model: str, currency: str) -> str:
    current = currency if currency in {"USD", "CNY"} else "CNY"
    return "".join(
        f"<option value='{code}'{(' selected' if code == current else '')}>{label}</option>"
        for code, label in (("USD", "美元"), ("CNY", "元"))
    )


def _rate_row(provider_id: str, model: str, price) -> str:
    currency = price.currency if price is not None else "CNY"
    values = {
        "input": "" if price is None else _plain_price(price.input),
        "output": "" if price is None else _plain_price(price.output),
        "cache": "" if price is None else _plain_price(price.cache),
    }
    boxes = "".join(
        f"<input data-field='{field}' value='{esc(values[field])}' inputmode='decimal' autocomplete='off' "
        f"aria-label='{esc(model + ' ' + label)}'>"
        for field, label in (("input", "输入单价"), ("output", "输出单价"), ("cache", "缓存命中单价"))
    )
    return (
        f"<div class='rate-row' data-provider='{esc(provider_id)}' data-model='{esc(model)}'>"
        f"<span class='name' title='{esc(model)}'>{esc(model)}</span>"
        f"<select data-field='currency' aria-label='{esc(model)} 币种'>{_currency_options(model, currency)}</select>"
        f"{boxes}</div>"
    )


def _price_groups(catalog: list[tuple[str, str, str, str]], prices: dict) -> str:
    if not catalog:
        return "<p class='empty'>还没有模型</p>"
    groups: list[dict] = []
    for provider_id, model, _value, vendor in catalog:
        if not groups or groups[-1]["id"] != provider_id:
            groups.append({"id": provider_id, "vendor": vendor, "rows": []})
        groups[-1]["rows"].append((provider_id, model))
    html = []
    for group in groups:
        rows = "".join(
            _rate_row(provider_id, model, (prices or {}).get((provider_id, model)))
            for provider_id, model in group["rows"]
        )
        html.append(
            "<section class='price-group'>"
            f"<h3>{esc(group['vendor'])}</h3>"
            "<div class='rate-sheet'><div class='rate-labels'><span>模型</span><span>币种</span><span>输入</span><span>输出</span><span>缓存命中</span></div>"
            f"{rows}</div></section>"
        )
    return "".join(html)


def _call_row(row: dict) -> str:
    bare = row.get("input") is None and row.get("output") is None

    def words(value) -> str:
        return esc(value) if value else "—"

    def num(value) -> str:
        if bare or value is None:
            return "—"
        return _grouped(value)

    money = "没有返回用量" if bare else ("未计入" if row.get("money") is None else _yuan(row.get("money")))
    return (
        "<tr>"
        f"<td>{esc(row.get('at') or '')}</td>"
        f"<td>{esc(row.get('worker') or '')}</td>"
        f"<td>{esc(row.get('model') or '—')}</td>"
        f"<td class='clip' title='{esc(row.get('prompt') or '')}'>{words(row.get('prompt'))}</td>"
        f"<td class='clip' title='{esc(row.get('answer') or '')}'>{words(row.get('answer'))}</td>"
        f"<td class='num'>{num(row.get('input'))}</td>"
        f"<td class='num'>{num(row.get('output'))}</td>"
        f"<td class='num'>{num(row.get('cache'))}</td>"
        f"<td class='num'>{num(row.get('total'))}</td>"
        f"<td class='num'>{esc(money)}</td>"
        "</tr>"
    )


def cost_page(board: dict, providers: list[dict] | None = None, prices: dict | None = None) -> str:
    """成本明细。金额不入库，打开这一页时按当前单价现算。"""

    providers = providers or []
    prices = prices or {}
    summary = board.get("summary") or {}
    catalog = _cost_catalog(providers, prices, board.get("models") or [])
    if board.get("model") and board["model"] not in {item[2] for item in catalog}:
        provider_id, _, model = str(board["model"]).partition("\t")
        catalog.append((provider_id, model, str(board["model"]), provider_id or "未命名"))
    workers = list(board.get("workers") or [])
    if board.get("worker") and board["worker"] not in workers:
        workers.append(board["worker"])
    worker_options = [(name, name) for name in workers]
    model_options = [(value, f"{vendor} · {model}" if vendor else model) for _pid, model, value, vendor in catalog]
    chosen = next((label for value, label in model_options if value == board.get("model")), "")
    scope = " · ".join(part for part in (board.get("worker") or "", chosen) if part)
    raw_series = board.get("series") or []
    board_window = board.get("window") if isinstance(board.get("window"), dict) else {}
    first_grain = next((str(item.get("grain") or "") for item in raw_series if item.get("grain")), "")
    grain = str(board_window.get("grain") or first_grain or ("hour" if str(board.get("span") or "") == "24h" else "day"))
    grain_delta = timedelta(hours=1) if grain == "hour" else timedelta(days=1)
    series = [
        {
            "at": str(item.get("at") or ""),
            "grain": str(item.get("grain") or grain),
            "label": item.get("label") or "",
            "money": item.get("money"),
            # money_known: 已计价子集金额之和，即使 money=None 仍保留（cost._public_sum R2 新增）。
            # 用它让 tooltip 在混合桶里显示"已计价部分"而不是丢数据。
            "money_known": float(item.get("money_known") or 0.0),
            "tokens": item.get("tokens") or 0,
            "input": item.get("input") or 0,
            "output": item.get("output") or 0,
            "cache": item.get("cache") or 0,
            "unpriced": item.get("unpriced") or 0,
            "bare": item.get("bare") or 0,
        }
        for item in raw_series
    ]
    # Window boundaries come from the overview snapshot that ran span_bounds(span, now, earliest)
    # at a single moment. The UI is NOT allowed to re-derive query edges from bucket timestamps:
    # a hand-built board that omits window triggers an explicit "unknown window" payload and the
    # chart will degrade to a caption rather than silently showing invented times (see R2 review).
    window_start = str(board_window.get("start") or "")
    window_end = str(board_window.get("end") or "")
    window_tz = str(board_window.get("tz") or "UTC+8")
    window_available = bool(window_start and window_end)
    rows = board.get("rows") or []
    observation = board.get("observation") if isinstance(board.get("observation"), dict) else {}
    if observation.get("state"):
        view_state = str(observation.get("state"))
    elif board.get("observed") is False:
        view_state = "pending"
    else:
        view_state = "ready"
    show_numbers = view_state in {"ready", "stale"} or (view_state == "failed" and board.get("observed") is not False)
    working = " gathering" if view_state == "pending" else ""
    # Call list is its own read model: card pending must not blank or freeze the table.
    body_rows = "".join(_call_row(row) for row in rows) or "<tr><td class='empty' colspan='10'>还没有调用</td></tr>"
    if not show_numbers:
        money_text = "汇总没有完成" if view_state == "failed" else "正在汇总"
        token_text = money_text
        split_text = "这一时段还没有可用的汇总" if view_state == "failed" else "汇总完成后显示这一时段的用量"
        caption_text = "汇总没有完成" if view_state == "failed" else "正在汇总这一时段"
        observed_attr = "0"
        busy_attr = "" if view_state == "failed" else " aria-busy='true'"
        price_disabled = " disabled"
    else:
        money_text = _cost_headline(summary)
        token_text = _million(summary.get("tokens") or 0)
        split_text = (
            f"输入 {_million(summary.get('input') or 0)}"
            f" · 输出 {_million(summary.get('output') or 0)}"
            f" · 缓存 {_million(summary.get('cache') or 0)}"
        )
        if view_state == "stale":
            caption_text = "记录有变，正在重算这一时段"
        elif view_state == "failed":
            caption_text = "这一时段的汇总停了，稍后再打开"
        else:
            caption_text = "所选时段总用量 · UTC+8 · 悬停或方向键查看"
        observed_attr = "1"
        busy_attr = " aria-busy='true'" if view_state == "stale" else ""
        price_disabled = ""
    epoch_attr = "" if observation.get("epoch") is None else str(observation.get("epoch"))
    asof_attr = str(observation.get("as_of") or "")
    pending_script = _COST_PENDING_SCRIPT if view_state in {"pending", "stale"} else ""
    page_no = int(board.get("page") or 1)
    from rogmem.cost import PAGE as _COST_PAGE

    foot = list_pager(
        "/admin/costs",
        _cost_params(board),
        total=int(board.get("count") or 0),
        page=page_no,
        page_size=board.get("page_size") or _COST_PAGE,
        partial="calls",
    )
    rate = board.get("rate")
    rate_text = "" if rate is None else _plain_price(float(rate))
    avail = _chart_availability(
        series,
        {
            "start": window_start if window_available else "",
            "end": window_end if window_available else "",
            "grain": grain,
        },
    )
    payload = json.dumps(
        {
            "scope": scope,
            "span": str(board.get("span") or ""),
            "grain": grain,
            "unit": {
                "money": {"currency": "CNY"},
                "token": {"name": "token"},
            },
            "window": {
                "start": window_start if window_available else None,
                "end": window_end if window_available else None,
                "grain": grain,
                "tz": window_tz,
                "available": window_available,
            },
            "series": series,
            "availability": avail,
            "observed": show_numbers,
            "observation": {"state": view_state, "epoch": observation.get("epoch"), "as_of": observation.get("as_of")},
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    body = f"""<h1>成本明细</h1>
<div class="board">
<section class="panel" id="cost" data-cost-observed="{observed_attr}" data-cost-state="{view_state}" data-cost-epoch="{esc(epoch_attr)}" data-cost-asof="{esc(asof_attr)}"{busy_attr}>
<header class="setting-head">
<h2>成本概览</h2>
<div class="board-tools">
<label class="fx">1 美元 = <input id="fx" value="{esc(rate_text)}" inputmode="decimal" aria-label="1 美元折合人民币" autocomplete="off"{price_disabled}> 元</label>
<button type="button" class="quiet" id="open-prices"{price_disabled}>单价配置</button>
</div>
</header>
<div class="ov-filters">
<div class="ov-left">
<div class="tabs">{_cost_tabs(board, "span", _COST_SPANS, reset_page=False)}</div>
{_cost_select(board, "worker", "工人", "概览按工人筛选", worker_options, reset_page=False)}
{_cost_select(board, "model", "模型", "概览按模型筛选", model_options, reset_page=False)}
</div>
<div class="tabs unit" id="ov-unit">
<button type="button" class="on" data-unit="money">金额</button>
<button type="button" data-unit="token">Token</button>
</div>
</div>
<div class="ov-figures">
<div id="ov-money-box"><p class="k">金额</p><p class="money{working}">{esc(money_text)}</p></div>
<div id="ov-token-box" class="is-secondary"><p class="k">Token</p><p class="money{working}">{esc(token_text)}</p>
<p class="ov-split">{esc(split_text)}</p></div>
</div>
<figure class="ov-chart">
<svg id="ov-svg" role="img" aria-label="成本趋势" aria-describedby="ov-caption" aria-roledescription="柱状图" tabindex="0"></svg>
<div id="ov-tip" class="ov-tip" hidden role="status" aria-live="polite"></div>
<figcaption id="ov-caption" class="ov-caption{working}">{esc(caption_text)}</figcaption>
{_avail_legend()}
</figure>
</section>
<section class="panel" id="calls">
<header class="setting-head"><h2>调用明细</h2></header>
<div class="detail">
<div class="filters">
<div class="tabs">{_cost_tabs(board, "detail", _COST_DETAILS, reset_page=True)}</div>
{_cost_select(board, "dworker", "工人", "按工人筛选", worker_options, reset_page=True)}
{_cost_select(board, "dmodel", "模型", "按模型筛选", model_options, reset_page=True)}
</div>
<div class="table-scroll">
<table class="calls">
<thead><tr>
<th>时间</th><th>工人</th><th>模型</th><th>输入</th><th>输出</th>
<th class="num">输入 token</th><th class="num">输出 token</th><th class="num">缓存命中</th><th class="num">总 token</th><th class="num">成本</th>
</tr></thead>
<tbody>{body_rows}</tbody>
</table>
</div>
{foot}
</div>
</section>
</div>
<div class="modal" id="price-modal" hidden>
<div class="price-dialog" role="dialog" aria-modal="true" aria-labelledby="price-title">
<header class="setting-head">
<div class="price-head"><h2 id="price-title">单价配置</h2><span class="price-unit">每百万token</span></div>
<button type="button" class="quiet" id="close-prices" aria-label="关闭">×</button>
</header>
<div class="price-body">{_price_groups(catalog, prices)}</div>
<footer class="setting-foot">
<button type="button" class="quiet" id="cancel-prices">取消</button>
<button type="button" id="save-prices" disabled>保存</button>
</footer>
</div>
</div>
{_COST_SCRIPT}
{_CHART_SCRIPT.replace("__COST__", payload)}
{pending_script}
{_LIST_PARTIAL_SCRIPT}"""
    return page("成本明细", "costs", body)


def cost_calls_fragment(board: dict) -> str:
    """The call list alone. Card filters on the board are echoed, not applied here."""

    from rogmem.cost import PAGE as _COST_PAGE

    rows = "".join(_call_row(row) for row in (board.get("rows") or [])) or (
        "<tr><td class='empty' colspan='10'>还没有调用</td></tr>"
    )
    foot = list_pager(
        "/admin/costs",
        _cost_params(board),
        total=int(board.get("count") or 0),
        page=int(board.get("page") or 1),
        page_size=board.get("page_size") or _COST_PAGE,
        partial="calls",
    )
    return f"<tbody>{rows}</tbody>{foot}"


def cost_overview_payload(board: dict, providers: list[dict] | None = None, prices: dict | None = None) -> dict:
    """JSON for a card-only partial refresh. Same numbers as a full page paint."""

    providers = providers or []
    prices = prices or {}
    summary = board.get("summary") or {}
    catalog = _cost_catalog(providers, prices, board.get("models") or [])
    if board.get("model") and board["model"] not in {item[2] for item in catalog}:
        provider_id, _, model = str(board["model"]).partition("\t")
        catalog.append((provider_id, model, str(board["model"]), provider_id or "未命名"))
    chosen = next(
        (
            f"{vendor} · {model}" if vendor else model
            for _pid, model, value, vendor in catalog
            if value == board.get("model")
        ),
        "",
    )
    scope = " · ".join(part for part in (board.get("worker") or "", chosen) if part)
    raw_series = board.get("series") or []
    board_window = board.get("window") if isinstance(board.get("window"), dict) else {}
    first_grain = next((str(item.get("grain") or "") for item in raw_series if item.get("grain")), "")
    grain = str(board_window.get("grain") or first_grain or ("hour" if str(board.get("span") or "") == "24h" else "day"))
    series = [
        {
            "at": str(item.get("at") or ""),
            "grain": str(item.get("grain") or grain),
            "label": item.get("label") or "",
            "money": item.get("money"),
            "money_known": float(item.get("money_known") or 0.0),
            "tokens": item.get("tokens") or 0,
            "input": item.get("input") or 0,
            "output": item.get("output") or 0,
            "cache": item.get("cache") or 0,
            "unpriced": item.get("unpriced") or 0,
            "bare": item.get("bare") or 0,
        }
        for item in raw_series
    ]
    window_start = str(board_window.get("start") or "")
    window_end = str(board_window.get("end") or "")
    window_tz = str(board_window.get("tz") or "UTC+8")
    window_available = bool(window_start and window_end)
    observation = board.get("observation") if isinstance(board.get("observation"), dict) else {}
    if observation.get("state"):
        view_state = str(observation.get("state"))
    elif board.get("observed") is False:
        view_state = "pending"
    else:
        view_state = "ready"
    show_numbers = view_state in {"ready", "stale"} or (view_state == "failed" and board.get("observed") is not False)
    if not show_numbers:
        money_text = "汇总没有完成" if view_state == "failed" else "正在汇总"
        token_text = money_text
        split_text = "这一时段还没有可用的汇总" if view_state == "failed" else "汇总完成后显示这一时段的用量"
        caption_text = "汇总没有完成" if view_state == "failed" else "正在汇总这一时段"
    else:
        money_text = _cost_headline(summary)
        token_text = _million(summary.get("tokens") or 0)
        split_text = (
            f"输入 {_million(summary.get('input') or 0)}"
            f" · 输出 {_million(summary.get('output') or 0)}"
            f" · 缓存 {_million(summary.get('cache') or 0)}"
        )
        if view_state == "stale":
            caption_text = "记录有变，正在重算这一时段"
        elif view_state == "failed":
            caption_text = "这一时段的汇总停了，稍后再打开"
        else:
            caption_text = "所选时段总用量 · UTC+8 · 悬停或方向键查看"
    rate = board.get("rate")
    avail = _chart_availability(
        series,
        {
            "start": window_start if window_available else "",
            "end": window_end if window_available else "",
            "grain": grain,
        },
    )
    chart = {
        "scope": scope,
        "span": str(board.get("span") or ""),
        "grain": grain,
        "unit": {"money": {"currency": "CNY"}, "token": {"name": "token"}},
        "window": {
            "start": window_start if window_available else None,
            "end": window_end if window_available else None,
            "grain": grain,
            "tz": window_tz,
            "available": window_available,
        },
        "series": series,
        "availability": avail,
        "observed": show_numbers,
        "observation": {"state": view_state, "epoch": observation.get("epoch"), "as_of": observation.get("as_of")},
    }
    return {
        "state": view_state,
        "epoch": observation.get("epoch"),
        "as_of": observation.get("as_of"),
        "observed": show_numbers,
        "money_text": money_text,
        "token_text": token_text,
        "split_text": split_text,
        "caption_text": caption_text,
        "rate_text": "" if rate is None else _plain_price(float(rate)),
        "gathering": view_state == "pending",
        "chart": chart,
    }


def _choice_html(providers: list[dict], kind: str = "") -> str:
    choices = ["<option value=''>未选择</option>"]
    for provider in providers:
        recorded = provider.get("model_kinds") if isinstance(provider.get("model_kinds"), dict) else {}
        for model in provider.get("models") or []:
            model_kind = "embed" if recorded.get(model) == "embed" else "chat"
            if kind and model_kind != kind:
                continue
            token = f"{provider['id']}|{model}"
            choices.append(f"<option value='{esc(token)}'>{esc(provider['name'])} · {esc(model)}</option>")
    return "".join(choices)


def _assigned_select(providers: list[dict], assigned: dict, key: str, kind: str = "") -> str:
    current = assigned.get(key) or ""
    options = _choice_html(providers, kind)
    if current:
        options = options.replace(f"value='{esc(current)}'", f"value='{esc(current)}' selected", 1)
    return f"<select name='{esc(key)}'>{options}</select>"


def _byte_size(value) -> str:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        size = 0
    if size < 0:
        size = 0
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def _settings_plate(title: str, body: str, submit: str) -> str:
    return (
        '<form class="panel settings-form plate" onsubmit="event.preventDefault(); '
        + submit
        + '"><header class="setting-head"><h2>'
        + title
        + "</h2></header>"
        + body
        + '<div class="setting-foot"><button type="submit" disabled>保存</button></div></form>'
    )


_SETTINGS_DIRTY = """<script>
(function () {
  function valueOf(el) {
    return el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
  }
  function snap(form) {
    return Array.prototype.map.call(form.elements, function (el) {
      return el.name ? valueOf(el) : "";
    }).join("\\n");
  }
  Array.prototype.forEach.call(document.querySelectorAll("form.settings-form"), function (form) {
    var button = form.querySelector("button[type=submit]");
    if (!button) return;
    var initial = snap(form);
    function paint() { button.disabled = snap(form) === initial; }
    form.addEventListener("input", paint);
    form.addEventListener("change", paint);
    paint();
  });
})();
</script>"""


_PEEK_ICON = (
    '<svg class="eye-on" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2">'
    '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>'
    '<svg class="eye-off" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2">'
    '<path d="M3 3l18 18"/><path d="M10.6 10.6a3 3 0 004.2 4.2"/>'
    '<path d="M9.9 5.1A10.8 10.8 0 0112 5c6.5 0 10 7 10 7a18.5 18.5 0 01-3.2 4.3"/>'
    '<path d="M6.1 6.1C3.8 7.8 2 12 2 12s3.5 7 10 7a10.8 10.8 0 004.1-.8"/></svg>'
)


def _password_peek(name: str) -> str:
    """Show/hide control. Same markup on every password field so the eyes share one column."""
    return (
        f'<button type="button" class="peek" data-show="显示{name}" data-hide="隐藏{name}" '
        f'aria-label="显示{name}" aria-pressed="false">{_PEEK_ICON}</button>'
    )


def basic_page(cfg: dict) -> str:
    from rogmem.engine import GRAPH_AUTO_LABELS_DEFAULT, INITIAL_ADMIN_PASSWORD, TURN_WAIT_DEFAULT

    path = str(cfg.get("store_path") or "").strip() or "—"
    # 设置行缺这项时，复选框要跟引擎的默认值一致，而不是掉回关。
    auto = " checked" if str(cfg.get("graph_auto_labels") or ("1" if GRAPH_AUTO_LABELS_DEFAULT else "0")) == "1" else ""
    budget = str(cfg.get("graph_label_budget") or "50")
    wait = str(cfg.get("turn_wait") or TURN_WAIT_DEFAULT)
    stored = cfg.get("passwordless")
    opened = "" if str(stored if stored is not None else "1").strip() in {"0", "off", "false", "no"} else " checked"
    peek_current = _password_peek("当前密码")
    peek_new = _password_peek("新密码")
    peek_again = _password_peek("确认新密码")
    login_settings = f"""<section class="panel plate" id="login-password">
<header class="setting-head"><h2>登录密码设置</h2></header>
<form id="login-password-form" onsubmit="event.preventDefault(); post('/admin/api/password', {{current:this.current_password.value, password:this.new_password.value, confirm:this.confirm_password.value}})">
<div class="setting-row login-password-block">
<div class="login-password-grid">
<p class="setting-name">当前密码</p>
<div class="password-stack">
<label class="password-field">
<input name="current_password" type="password" autocomplete="current-password" aria-label="当前密码">
{peek_current}
</label>
<p class="initial-password">初始密码: {esc(INITIAL_ADMIN_PASSWORD)}</p>
</div>
<p class="setting-name">新密码</p>
<label class="password-field">
<input name="new_password" type="password" autocomplete="new-password" aria-label="新密码" aria-describedby="password-gap">
{peek_new}
</label>
<p class="setting-name">确认新密码</p>
<label class="password-field">
<input name="confirm_password" type="password" autocomplete="new-password" aria-label="确认新密码" aria-describedby="password-gap">
{peek_again}
</label>
<p id="password-gap" class="initial-password" aria-live="polite"></p>
<div class="login-password-save"><button type="submit">保存</button></div>
</div>
</div>
</form>
<section class="setting-row setting-line login-password-switch">
<div><p class="setting-name">免密登录</p><p class="note">开启后，登录记忆中枢时不再需要输入密码。建议仅当确信自己的 NAS 安全时开启。</p></div>
<label class="switch"><span class="knob"><input type="checkbox" name="passwordless" role="switch"{opened} aria-label="免密登录" onchange="post('/admin/api/settings/basic', {{passwordless:this.checked ? '1' : '0'}})"><i></i></span></label>
</section>
<script>
(function () {{
  var form = document.getElementById("login-password-form");
  if (!form) return;
  var button = form.querySelector("button[type=submit]");
  function gaps(value) {{
    var missing = [];
    if (value.length < 8) missing.push("至少 8 位");
    if (!/[A-Z]/.test(value)) missing.push("大写");
    if (!/[a-z]/.test(value)) missing.push("小写");
    if (!/[0-9]/.test(value)) missing.push("数字");
    if (!/[^A-Za-z0-9]/.test(value)) missing.push("符号");
    return missing;
  }}
  function paint() {{
    var next = form.new_password.value;
    var again = form.confirm_password.value;
    var missing = gaps(next);
    var note = document.getElementById("password-gap");
    var parts = [];
    if (next && missing.length) parts.push("还缺" + missing.join("、"));
    if ((next || again) && next !== again) parts.push("两次输入不一致");
    note.textContent = parts.join("。");
    button.disabled = !next || missing.length > 0 || next !== again;
  }}
  form.addEventListener("input", paint);
  paint();
  Array.prototype.forEach.call(form.querySelectorAll(".peek"), function (peek) {{
    peek.addEventListener("click", function () {{
      var input = peek.parentNode.querySelector("input");
      var shown = input.type === "text";
      input.type = shown ? "password" : "text";
      peek.setAttribute("aria-pressed", shown ? "false" : "true");
      peek.setAttribute("aria-label", shown ? peek.getAttribute("data-show") : peek.getAttribute("data-hide"));
    }});
  }});
}})();
</script>
</section>"""
    owner = _settings_plate(
        "主人称呼",
        f"""<section class="setting-row">
<p class="note">交给模型，多个名字一行。</p>
<input name="owner_appellation" aria-label="主人称呼" value="{esc(cfg.get('owner_appellation') or '')}">
</section>""",
        "post('/admin/api/settings/basic', {owner_appellation:this.owner_appellation.value})",
    )
    limits = _settings_plate(
        "记忆预算上限",
        f"""<section class="setting-row setting-line">
<div><p class="setting-name">结晶速写字数上限</p><p class="note">已有的不改。</p></div>
<label class="job-field"><input name="sketch_char_limit" aria-label="结晶速写字数上限" value="{esc(cfg.get('sketch_char_limit') or '')}" inputmode="numeric"> 字</label>
</section>
<section class="setting-row setting-line">
<div><p class="setting-name">新会话核心记忆预算</p><p class="note">已开会话不改。</p></div>
<label class="job-field"><input name="core_budget" value="{esc(cfg.get('core_budget') or '15000')}" aria-label="新会话核心记忆预算" inputmode="numeric"> 字</label>
</section>""",
        "post('/admin/api/settings/basic', {sketch_char_limit:this.sketch_char_limit.value, core_budget:this.core_budget.value})",
    )
    graph = _settings_plate(
        "记忆星图视效",
        f"""<section class="setting-row setting-line">
<div><p class="setting-name">自动展示词条名</p><p class="note">放大到一定程度后，在星点上显示词条名。</p></div>
<label class="switch"><span class="knob"><input type="checkbox" name="graph_auto_labels" role="switch"{auto} aria-label="自动展示词条名"><i></i></span></label>
</section>
<section class="setting-row setting-line">
<p class="setting-name">同屏最多名称数</p>
<label class="job-field"><input type="number" name="graph_label_budget" min="1" max="200" step="1" inputmode="numeric" aria-label="同屏最多名称数" value="{esc(budget)}"></label>
</section>""",
        "post('/admin/api/settings/basic', {graph_auto_labels:this.graph_auto_labels.checked ? '1' : '0', graph_label_budget:this.graph_label_budget.value})",
    )
    mount = _settings_plate(
        "个人空间",
        f"""<section class="setting-row">
<p class="setting-name">挂载路径</p>
<p class="note">知识导入从这里读。</p>
<input name="nas_path" aria-label="个人空间挂载路径" value="{esc(cfg.get('nas_path') or '')}">
</section>""",
        "post('/admin/api/settings/basic', {nas_path:this.nas_path.value})",
    )
    collect = _settings_plate(
        "自动采集方案",
        f"""<section class="setting-row">
<p class="setting-name">定时扫描目录</p>
<input name="scan_path" aria-label="定时扫描目录" value="{esc(cfg.get('scan_path') or '')}">
{_active_scan_fact(cfg)}
</section>
<section class="setting-row">
<p class="setting-name">支持文件格式</p>
<fieldset class="types">{_type_boxes(str(cfg.get('file_types') or ''))}</fieldset>
</section>
<section class="setting-row setting-line">
<div><p class="setting-name">会话回合等待时长</p><p class="note">只到一边时先等。</p></div>
<label class="job-field"><input name="turn_wait" aria-label="会话回合等待时长" value="{esc(wait)}" inputmode="numeric"> 秒</label>
</section>""",
        "var types=[]; this.querySelectorAll('input[name=file_type]:checked').forEach(function(node){ types.push(node.value); }); post('/admin/api/settings/collect', {scan_path:this.scan_path.value, file_types:types.join(','), turn_wait:this.turn_wait.value})",
    )
    store = f"""<section class="panel settings-form plate">
<header class="setting-head"><h2>记忆数据文件</h2></header>
<section class="setting-row">
<p class="note">{_byte_size(cfg.get('store_bytes'))}</p>
<input class="locked" aria-label="记忆数据文件" value="{esc(path)}" readonly tabindex="-1">
</section>
</section>"""
    theme = """<section class="panel plate" id="theme-mode">
<header class="setting-head"><h2>配色模式</h2></header>
<section class="setting-row setting-line">
<div><p class="setting-name">界面配色</p><p class="note">只保存在本机浏览器，不写入服务器。</p></div>
<div class="theme-mode" role="group" aria-label="配色模式">
<button type="button" class="quiet" data-theme-pick="light" aria-pressed="false">日间</button>
<button type="button" class="quiet" data-theme-pick="dark" aria-pressed="false">暗夜</button>
</div>
</section>
</section>"""
    inner = f"""<div class="settings-stack">
{theme}
{login_settings}
{owner}
{limits}
{graph}
{mount}
{collect}
{store}
</div>
{_SETTINGS_DIRTY}"""
    return _section_page("basic", "基础设置", inner)


def _knob_panel(groups: list[dict] | None) -> str:
    """萃取成本与质量：每组一个小标题，开关是勾选框，其余是整数框，右边写单位和默认值。"""
    if not groups:
        return ""
    blocks = []
    names = []
    for group in groups:
        rows = []
        for item in group.get("items") or []:
            key = str(item["key"])
            names.append((key, bool(item.get("switch"))))
            label = esc(item.get("label") or key)
            unit = esc(item.get("unit") or "")
            if item.get("switch"):
                checked = " checked" if int(item.get("value") or 0) == 1 else ""
                field = f"<input type='checkbox' name='{esc(key)}'{checked} aria-label='{label}'> 勾上启用"
            else:
                field = (
                    f"<input name='{esc(key)}' value='{esc(str(item.get('value')))}' inputmode='numeric' "
                    f"aria-label='{label}'> {unit}<span class='sub'>默认 {esc(str(item.get('default')))}</span>"
                )
            rows.append(
                "<section class='setting-row setting-line'>"
                f"<h2>{label}</h2><label class='job-field'>{field}</label></section>"
            )
        blocks.append(
            f"<h3 class='knob-group'>{esc(group.get('title') or '')}</h3>"
            f"<p class='note'>{esc(group.get('note') or '')}</p>" + "".join(rows)
        )
    pairs = ", ".join(
        f"{key}:this.{key}.checked" if switch else f"{key}:this.{key}.value" for key, switch in names
    )
    return (
        "<form class='panel settings-form' onsubmit=\"event.preventDefault(); "
        f"post('/admin/api/settings/knobs', {{{pairs}}});\">"
        "<header class='setting-head'><h2>萃取成本与质量</h2></header>"
        "<p class='note'>这些数字决定花多少钱、记下什么。留空的数字不改；超出范围会被拒绝。改完立刻生效，不用重启。</p>"
        + "".join(blocks)
        + "<div class='setting-foot'><button type='submit'>保存</button></div></form>"
    )


def pipeline_page(
    providers: list[dict], assigned: dict, effective: dict, schedule: dict, knobs: list[dict] | None = None
) -> str:
    workers = (
        ("model_distill", "草稿", "chat"),
        ("model_page", "并页", "chat"),
        ("model_sketch", "速写", "chat"),
        ("model_proper", "专名核对", "chat"),
        ("model_merge", "身份合并", "chat"),
        ("model_embed", "向量嵌入", "embed"),
    )

    def reasoning_select(role: str) -> str:
        current = schedule.get(f"thinking_{role}") or "max"
        options = []
        for value, label in (
            ("off", "关"),
            ("low", "低"),
            ("medium", "中"),
            ("high", "高"),
            ("xhigh", "极高"),
            ("max", "最高"),
        ):
            selected = " selected" if value == current else ""
            options.append(f"<option value='{value}'{selected}>{label}</option>")
        return f"<select name='thinking_{role}'>{''.join(options)}</select>"

    def worker_row(key: str, label: str, kind: str) -> str:
        think = ""
        if kind != "embed":
            think = (
                "<label class='worker-field think'>推理强度"
                f"{reasoning_select(key.removeprefix('model_'))}</label>"
            )
        env = ""
        if key == "model_distill" and effective.get("source") == "env" and effective.get("model"):
            env = f"<span class='worker-env'>容器环境 · {esc(effective.get('model'))}</span>"
        return (
            f"<div class='worker'><div class='worker-name'>{esc(label)}{env}</div>"
            "<div class='worker-controls'>"
            f"<label class='worker-field model'>模型选择{_assigned_select(providers, assigned, key, kind)}</label>"
            f"{think}</div></div>"
        )

    rows = "".join(worker_row(key, label, kind) for key, label, kind in workers)
    jobs = (
        ("目录扫描", "scan_interval", "秒", ""),
        ("供应商探活", "probe_interval", "秒", ""),
        ("词条整合", "reconcile_hours", "小时", ""),
        ("每日清扫", "sweep_at", "世界时间", "03:00"),
    )
    gate = (
        ("额度阶梯", "gate_quota_steps", "秒，逗号分隔，逐档上升", "300,900,1800,3600,7200,18000"),
        ("限流等待", "gate_throttle_seconds", "秒", ""),
        ("探活后限流条数", "gate_probation_units", "条，同时最多几条在跑；0 表示关掉", ""),
        ("探活后限流时长", "gate_probation_seconds", "秒", ""),
        ("萃取并发", "distill_concurrency", "个，调低立即生效，调高重启后生效", ""),
    )

    def job_row(label: str, name: str, unit: str, placeholder: str) -> str:
        hold = f' placeholder="{placeholder}"' if placeholder else ""
        # 额度阶梯是一串逗号值，默认给更宽的输入框，免得看不清里面写了什么。
        wide = " wide" if name == "gate_quota_steps" else ""
        return (
            "<section class='setting-row setting-line'>"
            f"<h2>{label}</h2>"
            f'<label class="job-field{wide}"><input name="{name}" value="{esc(schedule.get(name) or "")}"{hold} aria-label="{label}"> {unit}</label>'
            "</section>"
        )

    job_rows = "".join(job_row(*item) for item in jobs)
    gate_rows = "".join(job_row(*item) for item in gate)
    # The switch stops the whole distill line without losing where it was: no lease is
    # taken while it is on, so lifting it continues from the same queue position.
    pause_checked = " checked" if schedule.get("distill_paused") else ""
    pause_row = (
        "<section class='setting-row setting-line'>"
        "<h2>暂停萃取</h2>"
        "<label class='job-field'>"
        f"<input type='checkbox' name='distill_paused'{pause_checked}> "
        "勾上立即停止派单；队列进度原地保留，取消勾选即从原处继续"
        "</label>"
        "</section>"
    )
    gate_names = ", ".join(f"{name}:this.{name}.value" for _label, name, _unit, _hold in gate)
    assign_pairs = ", ".join(f"{key}:this.{key}.value" for key, _label, _kind in workers)
    think_pairs = ", ".join(
        f"thinking_{role}:this.thinking_{role}.value"
        for role in ("distill", "page", "sketch", "proper", "merge")
    )
    assign_payload = "{" + assign_pairs + ", " + think_pairs + "}"
    inner = f"""<div class="settings-stack">
<form class="panel settings-form" onsubmit="event.preventDefault(); post('/admin/api/settings/assign', {assign_payload});">
<header class="setting-head"><h2>工人设置</h2></header>
<div class="workers">
{rows}
</div>
<div class="setting-foot"><button type="submit">保存</button></div>
</form>
<form class="panel settings-form" onsubmit="event.preventDefault(); post('/admin/api/settings/pipeline', {{scan_interval:this.scan_interval.value, probe_interval:this.probe_interval.value, reconcile_hours:this.reconcile_hours.value, sweep_at:this.sweep_at.value, distill_paused:this.distill_paused.checked, {gate_names}}});">
<header class="setting-head"><h2>定时任务与闸门</h2></header>
{pause_row}
{job_rows}
<p class="note">供应商报额度窗口时，阶梯决定派单停多久；探活不受它影响，一直按上面的探活间隔看一眼，一有回应就立刻开闸。开闸之后先按「探活后限流条数」放行，过了限流时长再完全放开——探活只花几个字，撑不住一整轮真实任务。</p>
{gate_rows}
<div class="setting-foot"><button type="submit">保存</button></div>
</form>
{_knob_panel(knobs)}
</div>"""
    return _section_page("pipeline", "管线设置", inner)


def _type_boxes(raw: str) -> str:
    picked = {part.strip().lower().lstrip(".") for part in str(raw or "").replace("，", ",").split(",") if part.strip()}
    choices = list(FILE_TYPES)
    for extra in sorted(picked - set(FILE_TYPES)):
        choices.append(extra)
    if not picked:
        picked = set(FILE_TYPES)
    boxes = []
    for name in choices:
        checked = " checked" if name in picked else ""
        boxes.append(
            f"<label><input type='checkbox' name='file_type' value='{esc(name)}'{checked}> {esc(name)}</label>"
        )
    return "".join(boxes)


def _active_scan_fact(cfg: dict) -> str:
    wanted = str(cfg.get("scan_path") or "").rstrip("/")
    active = str(cfg.get("active_scan") or "").rstrip("/")
    if not active or active == wanted:
        return ""
    return f"<p class='setting-fact'>正在扫描 {esc(active)}</p>"


def _log_filters(kinds: list[str], selected: str) -> str:
    links = []
    for label in ("全部", *kinds):
        on = " class='on'" if label == selected else ""
        href = "/admin/settings" if label == "全部" else f"/admin/settings?kind={esc(label)}"
        links.append(f"<a{on} href='{href}'>{esc(label)}</a>")
    return f"<nav class='tabs log-kinds'>{''.join(links)}</nav>"


def _log_rows(entries: list[dict]) -> str:
    if not entries:
        return "<tr><td colspan='6' class='empty'>这一类还没有记录。</td></tr>"
    rows = []
    for item in entries:
        subject = esc(item.get("subject") or "")
        href = item.get("href") or ""
        if href:
            subject = f"<a href='{esc(href)}'>{subject}</a>"
        detail = str(item.get("detail") or "").strip()
        extra = f"<span class='sub'>{esc(detail)}</span>" if detail else ""
        rows.append(
            "<tr>"
            f"<td class='when'>{when(item.get('at'))}</td>"
            f"<td class='kind'>{esc(item.get('kind'))}</td>"
            f"<td class='step'>{esc(item.get('step') or '')}</td>"
            f"<td>{subject}</td>"
            f"<td class='out'>{esc(item.get('outcome'))}</td>"
            f"<td class='rec'><div class='body'>{esc(item.get('summary') or '')}{extra}</div></td>"
            "</tr>"
        )
    return "".join(rows)


def logs_page(
    entries: list[dict],
    kinds: list[str],
    selected: str,
    webhook: str = "",
    *,
    page: int = 1,
    total: int | None = None,
    page_size: int | None = None,
) -> str:
    params = {}
    if selected and selected != "全部":
        params["kind"] = selected
    foot = list_pager(
        "/admin/settings",
        params,
        total=len(entries) if total is None else int(total),
        page=int(page or 1),
        page_size=page_size,
    )
    inner = f"""<div class='panel'>
{_log_filters(kinds, selected)}
<div class='table-scroll'><table class='logs'>
<tr><th class='when'>时间</th><th class='kind'>类型</th><th class='step'>步骤</th><th>对象</th><th class='out'>结果</th><th>记录</th></tr>
{_log_rows(entries)}
</table></div>
{foot}
<p class='note'>最新的在最上面，保留最近 200 条。萃取只记没走完的那一步。页面上真正改过的字在修订里。</p>
<h2>告警通知</h2>
<form onsubmit="event.preventDefault(); post('/admin/api/settings/webhook', {{url:this.url.value}});">
<label>Webhook 地址<br><input name="url" value="{esc(webhook)}" size="48" placeholder="https://example.com/hook"></label>
<p class='note'>只发时间和一句原因。本机、内网、链路本地和云元数据地址不会保存，也不会去请求。</p>
<p><button type="submit">保存通知地址</button></p>
</form>
</div>"""
    return _section_page("logs", "运行日志", inner)


def _about_line_html(release: dict) -> str:
    """Render the inner HTML of `.about-line` from a view dict.

    Rules enforced here (mirror of release_parser.present):

    - A failure never shows '已是最新/有新版本/本地较新' as a current
      relation. The stale historical tag renders separately with an
      explicit "上次成功" label.
    - Hint and timestamps are block-level so "已是最新2026-..." never
      collapses onto the same line.
    - UTC-Z timestamps carry a ``data-utc`` attribute so the page-wide
      Shanghai-time formatter converts them to UTC+8.
    """
    shown = str(release.get("display") or "未核对")
    kind = str(release.get("error_kind") or "never_checked")
    freshness = str(release.get("freshness") or "none")
    fail_detail = str(release.get("fail_detail") or "")
    last_fail = str(release.get("last_fail_at") or "")
    last_success = str(release.get("last_success_at") or release.get("checked_at") or "")
    last_success_tag = str(release.get("last_success_tag") or release.get("latest") or "")

    def _code_span(prefix: str, tag: str) -> str:
        return f"{esc(prefix)}<code>{esc(tag)}</code>"

    if freshness == "stale" and last_success_tag and shown.startswith("上次成功 "):
        primary = (
            "<span class='about-primary about-stale'>"
            + _code_span("上次成功 ", last_success_tag)
            + "</span>"
        )
    elif re.fullmatch(r"V[1-9]\d*-\d{10}-PUB", shown):
        primary = f"<span class='about-primary'><code>{esc(shown)}</code></span>"
    elif shown.startswith("请求失败") or kind == "config_invalid":
        primary = f"<span class='about-primary about-fail'>{esc(shown)}</span>"
    else:
        primary = f"<span class='about-primary'>{esc(shown)}</span>"
    state = ""
    relation_text = release.get("relation") or ""
    if freshness == "fresh" and relation_text:
        state_kind = " due" if relation_text == "有新版本" else ""
        state = f"<span class='about-state{state_kind}'>{esc(relation_text)}</span>"
    refresh = (
        "<button type='button' class='quiet' data-release-refresh "
        "onclick=\"refreshRelease(event)\">检查更新</button>"
    )
    hint_parts = []
    if freshness == "stale" and fail_detail:
        if last_fail:
            hint_parts.append(
                "<span class='about-hint about-fail'>"
                f"<time data-utc=\"{esc(last_fail)}\">{esc(last_fail)}</time> "
                f"{esc(fail_detail)}</span>"
            )
        else:
            hint_parts.append(f"<span class='about-hint about-fail'>{esc(fail_detail)}</span>")
        if last_success:
            hint_parts.append(
                "<span class='about-hint about-muted'>最后成功核对 "
                f"<time data-utc=\"{esc(last_success)}\">{esc(last_success)}</time>"
                "</span>"
            )
    elif freshness == "fresh" and last_success:
        hint_parts.append(
            "<span class='about-hint about-muted'>核对于 "
            f"<time data-utc=\"{esc(last_success)}\">{esc(last_success)}</time>"
            "</span>"
        )
    elif kind == "config_missing":
        hint_parts.append(
            "<span class='about-hint about-muted'>在基础设置里填入发布核对源 URL 以开启核对。</span>"
        )
    hint = "".join(hint_parts)
    return (
        "<span class='about-line-row'>"
        + primary
        + refresh
        + state
        + "</span>"
        + hint
    )


def about_release_script() -> str:
    """JS that replaces the `.about-line` HTML after a refresh, without reloading."""
    return """<script>(function(){
window.refreshRelease = async function(event){
  if (event && event.preventDefault) event.preventDefault();
  var btn = document.querySelector('[data-release-refresh]');
  if (btn) btn.disabled = true;
  try {
    var resp = await fetch('/admin/api/settings/release-refresh', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
    });
    if (resp.status === 401) { location = '/admin/login'; return; }
    var data = await resp.json();
    if (data && data.html) {
      var line = document.querySelector('.about-line');
      if (line) {
        line.innerHTML = data.html;
        if (typeof window.formatShanghaiTimes === 'function') {
          window.formatShanghaiTimes(line);
        }
      }
    }
  } finally {
    btn = document.querySelector('[data-release-refresh]');
    if (btn) btn.disabled = false;
  }
};
})();</script>"""


_VERSION_NOTE = (
    "版本号写法：<code>S-V1-打包时间-后缀</code> 是服务器，<code>P-V1-打包时间-后缀</code> 是 rogmem 插件。"
    "打包时间是北京时间年月日时分（10 位）；后缀 DEV 为私有仓生产、PUB 为公开仓生产、PRE 为预发布。"
    "插件的打包时间是它内容最后一次变化的那次发版，只改服务器时插件版本不变。"
)


def about_page(version: str, release: dict | None = None, plugin: str = "") -> str:
    release = release or {
        "display": "未核对", "relation": "", "detail": "", "checked_at": "",
        "error_kind": "never_checked", "last_fail_at": "", "last_success_at": "",
        "fail_detail": "", "source_url": "",
    }
    line_html = _about_line_html(release)
    inner = f"""<section class='panel about'>
<div class='about-hero'>
<p class='about-kicker'>私人记忆中枢</p>
<h2>ROGMEM</h2>
<p class='about-lead'>ROGMEM 是可在极空间 NAS 或私人服务器部署的私人记忆中枢，可通过 MCP 为用户在不同设备上的不同 Agent 提供一站式记忆和知识管理服务，致力于解决跨设备、跨 Agent 工作时的记忆、知识和技能孤岛问题。</p>
</div>
<div class='about-versions'>
<div><p class='about-label'>服务器版本 · 本机运行</p><p class='about-value'><code>{esc(version)}</code></p>
<p class='about-hint'>记忆中枢本身：管理台、MCP、萃取，跑在这台 NAS 的容器里。</p></div>
<div><p class='about-label'>服务器版本 · 最新发布</p><p class='about-line'>{line_html}</p></div>
<div><p class='about-label'>插件版本 · 中枢当前提供</p><p class='about-value'><code>{esc(plugin or "—")}</code></p>
<p class='about-hint'>装在每台电脑上的 rogmem 插件：钩子、全局规则、Hermes 记忆、本机门口。各电脑装的是哪一版，看<a href='/admin/hosts'>宿主管理</a>的「插件版本」列；落后的电脑运行 <code>python -m rogmem plugin update</code>。</p></div>
<div><p class='about-label'>版本号怎么读</p><p class='about-hint about-note'>{_VERSION_NOTE}</p></div>
</div>
<ul class='about-links'>
<li><span>GitHub</span><a href='https://github.com/rogdance/rogmem' target='_blank' rel='noopener'>https://github.com/rogdance/rogmem</a></li>
</ul>
<div class='about-credits'>
<p class='about-label'>致谢</p>
<ul>
<li><span>WeKnora</span><span class='about-credit-meta'>Tencent · <a href='https://github.com/Tencent/WeKnora' target='_blank' rel='noopener'>https://github.com/Tencent/WeKnora</a></span></li>
<li><span>Techartist</span><span class='about-credit-meta'>Techartist · <a href='https://codepen.io/VoXelo/pen/dPMeGze' target='_blank' rel='noopener'>https://codepen.io/VoXelo/pen/dPMeGze</a></span></li>
</ul>
</div>
</section>
{about_release_script()}"""
    return _section_page("about", "关于我们", inner)


def _queue_spans() -> tuple[tuple[str, str], ...]:
    return (("24h", "近 24 小时"), ("7d", "近 7 天"), ("30d", "近 30 天"), ("all", "历史至今"))


def _queue_href(board: dict, **changes) -> str:
    params = {"span": str(board.get("span") or "24h"), "tab": str(board.get("tab") or "all")}
    params.update(changes)
    kept = {key: value for key, value in params.items() if value not in ("", None)}
    return "/admin/settings/queue?" + urlencode(kept)


def _queue_rate(value) -> str:
    number = float(value or 0)
    if number >= 10:
        text = f"{number:.0f}"
    elif number >= 1:
        text = f"{number:.1f}"
    else:
        text = f"{number:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _queue_clip(text: str, limit: int = 30) -> str:
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    return body[:limit] + "…"


_QUEUE_CHART = r"""<script>
(function () {
  var QUEUE = __QUEUE__;
  var svg = document.getElementById("q-svg");
  var tip = document.getElementById("q-tip");
  var container = svg ? svg.closest(".ov-chart") : null;
  var buttons = document.querySelectorAll("[data-qunit]");
  if (!svg || !container || !QUEUE) return;

  var SVG_NS = "http://www.w3.org/2000/svg";
__AVAIL_JS__
  var CHAR_PX = 7.2;
  var MIN_BAR_PITCH = 4;
  var SHIFT_MS = 8 * 3600 * 1000;
  var unit = "tasks";
  var activeIndex = -1;
  var displayBuckets = [];
  var layout = [];
  var lastRenderWidth = -1;

  function pad(n) { return String(n).padStart(2, "0"); }
  function fmtMD(d) { return pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()); }
  function fmtHM(d) { return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()); }
  function fmtYMD(d) { return d.getUTCFullYear() + "-" + fmtMD(d); }
  function toCST(iso) { return new Date(new Date(iso).getTime() + SHIFT_MS); }
  function grainOf() { return QUEUE.grain === "hour" ? "hour" : "day"; }
  function grainDeltaMs() { return grainOf() === "hour" ? 3600e3 : 86400e3; }
  function grouped(n) {
    var v = Math.round(Number(n) || 0);
    var sign = v < 0 ? "-" : "";
    var body = String(Math.abs(v));
    var out = "";
    while (body.length > 3) {
      out = "," + body.slice(-3) + out;
      body = body.slice(0, -3);
    }
    return sign + body + out;
  }
  function trimMagnitude(n) {
    var digits = Math.abs(n) >= 10 ? 1 : 2;
    return n.toFixed(digits).replace(/\.0+$/, "").replace(/(\.\d*[1-9])0+$/, "$1");
  }
  function formatCount(v, compact) {
    var n = Math.abs(v - Math.round(v)) < 1e-6 ? Math.round(v) : Number(v) || 0;
    var abs = Math.abs(n);
    if (compact && abs >= 1e9) return trimMagnitude(n / 1e9) + "B";
    if (compact && abs >= 1e6) return trimMagnitude(n / 1e6) + "M";
    if (compact && abs >= 1e3) return trimMagnitude(n / 1e3) + "K";
    return grouped(n);
  }
  function valueOf(bucket) { return unit === "chars" ? (bucket.chars || 0) : (bucket.tasks || 0); }
  function noun() { return unit === "chars" ? "字" : "项"; }

  function buildDisplayBuckets(series, maxBars) {
    if (!series || !series.length) return [];
    var delta = grainDeltaMs();
    function one(item, count, endMs) {
      var startMs = new Date(item.at).getTime();
      if (!isFinite(startMs)) return null;
      return {
        startMs: startMs,
        endMs: endMs == null ? startMs + delta : endMs,
        tasks: item.tasks || 0,
        chars: item.chars || 0,
        mergedCount: count,
        grain: grainOf(),
        avail: availRead(item.at, clippedSeconds(item.at, delta, QUEUE.window))
      };
    }
    if (series.length <= maxBars) {
      var plain = [];
      for (var p = 0; p < series.length; p++) {
        var single = one(series[p], 1);
        if (single) plain.push(single);
      }
      return plain;
    }
    var k = Math.ceil(series.length / maxBars);
    var merged = [];
    for (var i = 0; i < series.length; i += k) {
      var chunk = series.slice(i, i + k);
      var lastMs = new Date(chunk[chunk.length - 1].at).getTime();
      var bucket = one(chunk[0], chunk.length, (isFinite(lastMs) ? lastMs : new Date(chunk[0].at).getTime()) + delta);
      if (!bucket) continue;
      bucket.tasks = 0;
      bucket.chars = 0;
      bucket.avail = {ok: 0, warn: 0, down: 0, unknown: 0, span: 0};
      for (var j = 0; j < chunk.length; j++) {
        bucket.tasks += chunk[j].tasks || 0;
        bucket.chars += chunk[j].chars || 0;
        availAdd(bucket.avail, availRead(chunk[j].at, clippedSeconds(chunk[j].at, delta, QUEUE.window)));
      }
      merged.push(bucket);
    }
    return merged;
  }

  function niceNum(x) {
    if (!(x > 0)) return 1;
    var exp = Math.floor(Math.log10(x));
    var f = x / Math.pow(10, exp);
    var nf = f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10;
    return nf * Math.pow(10, exp);
  }
  function buildYScale(max, compact) {
    if (!(max > 0)) return {max: 0, step: 1, compact: false, ticks: [{value: 0, label: "0"}]};
    var step = niceNum(max / 4);
    if (step < 1) step = 1;
    var top = Math.ceil((max - 1e-9) / step) * step;
    if (!(top > 0)) top = step;
    var ticks = [];
    for (var v = 0; v <= top + step * 1e-6; v += step) {
      ticks.push({value: v, label: formatCount(v, compact)});
    }
    return {max: top, step: step, compact: !!compact, ticks: ticks};
  }

  function buildXTicks(buckets, chartLeft, chartRight, windowStart, windowEnd) {
    var n = buckets.length;
    if (!n) return [];
    var step = (chartRight - chartLeft) / n;
    var winStartDt = toCST(windowStart);
    var winEndDt = toCST(windowEnd);
    var totalDays = (winEndDt.getTime() - winStartDt.getTime()) / 86400e3;
    var grain = grainOf();
    var candidates = [];
    for (var i = 0; i < n; i++) {
      var d = new Date(buckets[i].startMs + SHIFT_MS);
      var label = "";
      if (grain === "hour") {
        if (d.getUTCHours() % 2 !== 0) continue;
        label = fmtHM(d);
      } else if (totalDays > 365) {
        if (d.getUTCDate() !== 1) continue;
        label = d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1);
      } else if (totalDays > 60) {
        if (d.getUTCDay() !== 1 && d.getUTCDate() !== 1) continue;
        label = fmtMD(d);
      } else {
        label = fmtMD(d);
      }
      candidates.push({index: i, x: chartLeft + (i + 0.5) * step, label: label, d: d});
    }
    return candidates;
  }

  function measureLabels(candidates) {
    var group = document.createElementNS(SVG_NS, "g");
    group.setAttribute("aria-hidden", "true");
    group.setAttribute("visibility", "hidden");
    svg.appendChild(group);
    for (var i = 0; i < candidates.length; i++) {
      var node = document.createElementNS(SVG_NS, "text");
      node.setAttribute("class", "axis");
      node.textContent = candidates[i].label;
      group.appendChild(node);
      var width = 0;
      try { width = node.getBBox().width; } catch (err) { width = 0; }
      candidates[i].width = width > 0 ? width : candidates[i].label.length * CHAR_PX;
    }
    group.parentNode.removeChild(group);
  }
  function measureTextWidth(sample) {
    var node = document.createElementNS(SVG_NS, "text");
    node.setAttribute("class", "axis");
    node.setAttribute("visibility", "hidden");
    node.textContent = sample || "";
    svg.appendChild(node);
    var width = 0;
    try { width = node.getBBox().width; } catch (err) { width = 0; }
    node.parentNode.removeChild(node);
    return width > 0 ? width : (sample || "").length * CHAR_PX;
  }
  function labelBox(tick, chartW) {
    var width = tick.width || (tick.label || "").length * CHAR_PX;
    var half = width / 2;
    var pad = 8;
    if (tick.x - half < 2) {
      var start = Math.max(tick.x, 2);
      return {anchor: "start", x: start, left: start, right: start + width + pad};
    }
    if (tick.x + half > chartW - 2) {
      var end = Math.min(tick.x, chartW - 2);
      return {anchor: "end", x: end, left: end - width - pad, right: end};
    }
    return {anchor: "middle", x: tick.x, left: tick.x - half - pad, right: tick.x + half + pad};
  }
  function dodgeXTicks(candidates, chartW) {
    function hits(a, b) { return labelBox(a, chartW).right > labelBox(b, chartW).left; }
    var kept = [];
    for (var i = 0; i < candidates.length; i++) {
      var cur = candidates[i];
      if (!kept.length) { kept.push(cur); continue; }
      if (hits(kept[kept.length - 1], cur)) {
        if (i === candidates.length - 1) {
          while (kept.length && hits(kept[kept.length - 1], cur)) kept.pop();
          kept.push(cur);
        }
        continue;
      }
      kept.push(cur);
    }
    return kept;
  }
  function decorateTicks(ticks, crossDay, crossYear, totalDays) {
    var prevDay = null;
    var prevYear = null;
    for (var i = 0; i < ticks.length; i++) {
      var d = ticks[i].d;
      var day = fmtYMD(d);
      var year = d.getUTCFullYear();
      if (grainOf() === "hour") {
        ticks[i].label = crossDay && (prevDay == null || day !== prevDay) ? fmtMD(d) + " " + fmtHM(d) : fmtHM(d);
      } else if (totalDays > 365) {
        ticks[i].label = year + "-" + pad(d.getUTCMonth() + 1);
      } else if (crossYear && (prevYear == null || year !== prevYear)) {
        ticks[i].label = fmtYMD(d);
      } else {
        ticks[i].label = fmtMD(d);
      }
      prevDay = day;
      prevYear = year;
    }
  }
  function settleXTicks(buckets, chartLeft, chartRight, bounds, chartW) {
    var n = buckets.length;
    if (!n) return [];
    var ticks = buildXTicks(buckets, chartLeft, chartRight, bounds.start, bounds.end);
    var step = (chartRight - chartLeft) / n;
    var winStartDt = toCST(bounds.start);
    var winEndDt = toCST(bounds.end);
    var known = isFinite(winStartDt.getTime()) && isFinite(winEndDt.getTime());
    var crossDay = known && fmtYMD(winStartDt) !== fmtYMD(winEndDt);
    var crossYear = known && winStartDt.getUTCFullYear() !== winEndDt.getUTCFullYear();
    var totalDays = known ? (winEndDt.getTime() - winStartDt.getTime()) / 86400e3 : 0;
    function edge(index) {
      return {
        index: index,
        x: chartLeft + (index + 0.5) * step,
        label: "",
        d: new Date(buckets[index].startMs + SHIFT_MS)
      };
    }
    if (!ticks.length || ticks[0].index !== 0) ticks.unshift(edge(0));
    if (ticks[ticks.length - 1].index !== n - 1) ticks.push(edge(n - 1));
    var guard = ticks.length + 1;
    while (guard-- > 0) {
      decorateTicks(ticks, crossDay, crossYear, totalDays);
      measureLabels(ticks);
      var kept = dodgeXTicks(ticks, chartW);
      if (kept.length === ticks.length) return kept;
      ticks = kept;
    }
    return ticks;
  }

  function layoutBars(buckets, geom, max) {
    var trackW = geom.chartRight - geom.chartLeft;
    var trackH = geom.chartBottom - geom.chartTop;
    var n = buckets.length;
    var step = n ? trackW / n : trackW;
    var barW = Math.max(1, step * 0.72);
    var bars = [];
    for (var i = 0; i < n; i++) {
      var value = valueOf(buckets[i]);
      var h = max > 0 && value > 0 ? (value / max) * trackH : 0;
      bars.push({
        index: i,
        hitX: geom.chartLeft + i * step,
        hitW: step,
        barX: geom.chartLeft + i * step + (step - barW) / 2,
        barY: geom.chartBottom - h,
        barW: barW,
        barH: h,
        bucket: buckets[i]
      });
    }
    return bars;
  }

  function windowBounds(buckets) {
    var start = QUEUE.window && QUEUE.window.start;
    var end = QUEUE.window && QUEUE.window.end;
    if (!start && buckets.length) start = new Date(buckets[0].startMs).toISOString();
    if (!end && buckets.length) end = new Date(buckets[buckets.length - 1].endMs).toISOString();
    return {start: start, end: end};
  }
  function formatBucketRange(bucket, bounds) {
    var winStart = new Date(bounds.start).getTime();
    var winEnd = new Date(bounds.end).getTime();
    if (!isFinite(winStart)) winStart = bucket.startMs;
    if (!isFinite(winEnd)) winEnd = bucket.endMs;
    var s = Math.max(bucket.startMs, winStart);
    var e = Math.min(bucket.endMs, winEnd);
    if (!(e > s)) { s = bucket.startMs; e = bucket.endMs; }
    var sd = new Date(s + SHIFT_MS);
    var ed = new Date(e + SHIFT_MS);
    var fullDay = bucket.grain === "day" && bucket.mergedCount === 1 && s === bucket.startMs && e === bucket.endMs;
    if (fullDay) return fmtYMD(sd) + " 00:00 – 23:59 · UTC+8";
    if (bucket.mergedCount > 1) {
      return fmtYMD(sd) + " " + fmtHM(sd) + " – " + fmtYMD(ed) + " " + fmtHM(ed) + " · UTC+8 · 合并 " + bucket.mergedCount + " 桶";
    }
    if (fmtYMD(sd) === fmtYMD(ed)) return fmtYMD(sd) + " " + fmtHM(sd) + " – " + fmtHM(ed) + " · UTC+8";
    return fmtYMD(sd) + " " + fmtHM(sd) + " – " + fmtYMD(ed) + " " + fmtHM(ed) + " · UTC+8";
  }
  function renderTooltip(index, bounds) {
    if (!tip || index < 0 || index >= displayBuckets.length) {
      if (tip) tip.hidden = true;
      return;
    }
    var bucket = displayBuckets[index];
    tip.innerHTML = "<b>" + formatBucketRange(bucket, bounds) + "</b><p>" + grouped(valueOf(bucket)) + " " + noun() + "</p><p>" + availEscape(availPhrase(bucket.avail, availWho(QUEUE.availability))) + "</p>";
    tip.hidden = false;
  }
  function moveTip(x, y) {
    if (!tip) return;
    var gap = 14;
    var tx = x + gap;
    var ty = y + gap;
    if (tx + tip.offsetWidth > window.innerWidth - 8) tx = x - tip.offsetWidth - gap;
    if (ty + tip.offsetHeight > window.innerHeight - 8) ty = y - tip.offsetHeight - gap;
    tip.style.left = tx + "px";
    tip.style.top = ty + "px";
  }
  function highlight(index) {
    var bars = svg.querySelectorAll("rect.bar");
    for (var i = 0; i < bars.length; i++) bars[i].classList.toggle("on", i === index);
    var marks = svg.querySelectorAll("rect.avail");
    for (var m = 0; m < marks.length; m++) marks[m].classList.toggle("on", m === index);
  }
  var frame = {bounds: {start: "", end: ""}};
  function localX(clientX, clientY) {
    if (svg.createSVGPoint && svg.getScreenCTM) {
      var point = svg.createSVGPoint();
      point.x = clientX;
      point.y = clientY;
      var matrix = svg.getScreenCTM();
      if (matrix) {
        try { return point.matrixTransform(matrix.inverse()).x; } catch (err) {}
      }
    }
    return clientX - svg.getBoundingClientRect().left;
  }
  function hitIndex(clientX, clientY) {
    var x = localX(clientX, clientY);
    for (var i = 0; i < layout.length; i++) {
      if (x >= layout[i].hitX && x < layout[i].hitX + layout[i].hitW) return i;
    }
    return -1;
  }
  function setActive(index, screenX, screenY) {
    activeIndex = index;
    if (index < 0 || !layout[index]) {
      if (tip) tip.hidden = true;
      highlight(-1);
      svg.removeAttribute("aria-activedescendant");
      return;
    }
    renderTooltip(index, frame.bounds);
    highlight(index);
    if (screenX == null) {
      var rect = svg.getBoundingClientRect();
      var bar = layout[index];
      moveTip(rect.left + bar.barX + bar.barW / 2, rect.top + bar.barY);
    } else {
      moveTip(screenX, screenY);
    }
    svg.setAttribute("aria-activedescendant", "q-bar-" + index);
  }

  function render() {
    availByAt = availIndex(QUEUE.availability);
    activeIndex = -1;
    if (tip) tip.hidden = true;
    svg.removeAttribute("aria-activedescendant");
    var rect = svg.getBoundingClientRect();
    var w = rect.width || container.getBoundingClientRect().width;
    var h = rect.height || 244;
    if (!(w > 0) || !(h > 0)) return;
    svg.setAttribute("width", w);
    svg.setAttribute("height", h);
    svg.removeAttribute("viewBox");
    var narrow = w < 520;
    var chartRight = w - 16;
    var chartTop = 12;
    var chartBottom = h - (narrow ? 56 : 52);
    var series = QUEUE.series || [];
    var chartLeft = 44;
    var yScale = buildYScale(0, false);
    var n = 0;
    var guard = 6;
    while (guard-- > 0) {
      var trackW = chartRight - chartLeft;
      if (!(trackW > 0)) return;
      displayBuckets = buildDisplayBuckets(series, Math.max(8, Math.floor(trackW / MIN_BAR_PITCH)));
      n = displayBuckets.length;
      var max = 0;
      for (var i = 0; i < n; i++) if (valueOf(displayBuckets[i]) > max) max = valueOf(displayBuckets[i]);
      yScale = buildYScale(max, false);
      var widest = 0;
      for (var t = 0; t < yScale.ticks.length; t++) widest = Math.max(widest, measureTextWidth(yScale.ticks[t].label));
      var budget = Math.min(140, w * 0.3);
      if (widest > budget) {
        yScale = buildYScale(max, true);
        widest = 0;
        for (var t2 = 0; t2 < yScale.ticks.length; t2++) widest = Math.max(widest, measureTextWidth(yScale.ticks[t2].label));
      }
      var nextLeft = Math.ceil(widest + 12);
      if (nextLeft < 32) nextLeft = 32;
      if (nextLeft > w / 2) nextLeft = Math.floor(w / 2);
      if (nextLeft === chartLeft) break;
      chartLeft = nextLeft;
    }
    var geom = {w: w, h: h, chartLeft: chartLeft, chartRight: chartRight, chartTop: chartTop, chartBottom: chartBottom};
    var trackH = chartBottom - chartTop;
    frame.bounds = windowBounds(displayBuckets);
    svg.innerHTML = "";
    layout = [];
    for (var yi = 0; yi < yScale.ticks.length; yi++) {
      var tick = yScale.ticks[yi];
      var ratio = yScale.max > 0 ? tick.value / yScale.max : 0;
      var y = chartBottom - ratio * trackH;
      var grid = document.createElementNS(SVG_NS, "line");
      grid.setAttribute("class", "grid");
      grid.setAttribute("x1", chartLeft);
      grid.setAttribute("x2", chartRight);
      grid.setAttribute("y1", y);
      grid.setAttribute("y2", y);
      svg.appendChild(grid);
      var label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("class", "axis");
      label.setAttribute("x", chartLeft - 8);
      label.setAttribute("y", y + 4);
      label.setAttribute("text-anchor", "end");
      label.textContent = tick.label;
      svg.appendChild(label);
    }
    layout = layoutBars(displayBuckets, geom, yScale.max);
    for (var b = 0; b < layout.length; b++) {
      var bar = layout[b];
      var rectBar = document.createElementNS(SVG_NS, "rect");
      rectBar.setAttribute("id", "q-bar-" + b);
      rectBar.setAttribute("class", "bar");
      rectBar.setAttribute("x", bar.barX);
      rectBar.setAttribute("y", bar.barY);
      rectBar.setAttribute("width", bar.barW);
      rectBar.setAttribute("height", bar.barH);
      rectBar.setAttribute("data-tasks", bar.bucket.tasks || 0);
      rectBar.setAttribute("data-chars", bar.bucket.chars || 0);
      var title = document.createElementNS(SVG_NS, "title");
      title.textContent = formatBucketRange(bar.bucket, frame.bounds) + " " + grouped(valueOf(bar.bucket)) + " " + noun();
      rectBar.appendChild(title);
      svg.appendChild(rectBar);
      var hit = document.createElementNS(SVG_NS, "rect");
      hit.setAttribute("class", "hit");
      hit.setAttribute("x", bar.hitX);
      hit.setAttribute("y", chartTop);
      hit.setAttribute("width", bar.hitW);
      hit.setAttribute("height", trackH);
      svg.appendChild(hit);
    }
    var ticks = n ? settleXTicks(displayBuckets, chartLeft, chartRight, frame.bounds, w) : [];
    for (var xi = 0; xi < ticks.length; xi++) {
      var mark = ticks[xi];
      var placed = labelBox(mark, w);
      var stem = document.createElementNS(SVG_NS, "line");
      stem.setAttribute("class", "tick");
      stem.setAttribute("x1", mark.x);
      stem.setAttribute("x2", mark.x);
      stem.setAttribute("y1", chartBottom);
      stem.setAttribute("y2", chartBottom + 4);
      svg.appendChild(stem);
      var text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("class", "axis");
      text.setAttribute("x", placed.x);
      text.setAttribute("y", chartBottom + 18);
      text.setAttribute("text-anchor", placed.anchor);
      text.textContent = mark.label;
      svg.appendChild(text);
    }
    var baseline = document.createElementNS(SVG_NS, "line");
    baseline.setAttribute("class", "axis");
    baseline.setAttribute("x1", chartLeft);
    baseline.setAttribute("x2", chartRight);
    baseline.setAttribute("y1", chartBottom);
    baseline.setAttribute("y2", chartBottom);
    svg.appendChild(baseline);
    paintAvail(svg, layout, chartBottom, availWho(QUEUE.availability));
    svg.setAttribute("aria-label", "萃取完成走势，共 " + n + " 个时段");
    lastRenderWidth = w;
  }
  svg.addEventListener("pointermove", function (ev) {
    if (!layout.length) return;
    var found = hitIndex(ev.clientX, ev.clientY);
    if (found >= 0) setActive(found, ev.clientX, ev.clientY);
    else setActive(-1);
  });
  svg.addEventListener("pointerdown", function (ev) {
    var found = hitIndex(ev.clientX, ev.clientY);
    if (found >= 0) setActive(found, ev.clientX, ev.clientY);
  });
  svg.addEventListener("pointerleave", function (ev) {
    if (ev && ev.pointerType === "touch") return;
    setActive(-1);
  });
  svg.addEventListener("keydown", function (ev) {
    if (!layout.length) return;
    var next = activeIndex;
    if (ev.key === "ArrowRight") next = Math.min(layout.length - 1, activeIndex < 0 ? 0 : activeIndex + 1);
    else if (ev.key === "ArrowLeft") next = Math.max(0, activeIndex < 0 ? 0 : activeIndex - 1);
    else if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = layout.length - 1;
    else if (ev.key === "Escape") next = -1;
    else return;
    ev.preventDefault();
    setActive(next);
  });
  document.addEventListener("pointerdown", function (ev) {
    if (!svg.contains(ev.target)) setActive(-1);
  });

  for (var b = 0; b < buttons.length; b++) {
    buttons[b].addEventListener("click", function () {
      unit = this.getAttribute("data-qunit") || "tasks";
      for (var k = 0; k < buttons.length; k++) buttons[k].classList.toggle("on", buttons[k] === this);
      render();
    });
  }
  var timer = null;
  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () {
      timer = null;
      var width = svg.getBoundingClientRect().width;
      if (Math.round(width) === Math.round(lastRenderWidth)) return;
      render();
    }, 50);
  }
  if (window.ResizeObserver) new ResizeObserver(schedule).observe(container);
  else window.addEventListener("resize", schedule);
  window.__QUEUE_CHART__ = Object.freeze({render: render});
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", render);
  else render();
})();
</script>"""

_CHART_SCRIPT = _CHART_SCRIPT.replace("__AVAIL_JS__", _AVAIL_JS)
_QUEUE_CHART = _QUEUE_CHART.replace("__AVAIL_JS__", _AVAIL_JS)


def _chart_availability(series: list, window: dict | None) -> dict:
    from rogmem.availability import for_chart

    return for_chart(series if isinstance(series, list) else [], window if isinstance(window, dict) else {})


def _avail_legend() -> str:
    return (
        "<p class='ov-avail-legend'>"
        "模型探活状态：位于时间刻度下方。绿是可用，黄是降级，红不可用，颜色越深表示该状态占比越久。"
        "两种异常同时出现时，色块用更久的那一种。空心是这格暂无探活记录。</p>"
    )


def _queue_activity_note(reading: dict) -> str:
    """此刻有哪些模型调用在跑。

    队列行只是萃取作业；向量补齐和词条整合不写队列，却同样在调模型。
    这条读数出自模型的唯一收口，所以队列状态和成本明细不会再各说各话。
    """

    live = reading.get("open") or []
    if live:
        parts = "、".join(
            f"{esc(str(item.get('worker') or ''))}（已 {max(0, int(item.get('seconds') or 0))} 秒）"
            for item in live
        )
        return f"此刻在调用模型：{parts}。"
    recent = reading.get("recent") or []
    if recent:
        head = recent[0]
        worker = esc(str(head.get("worker") or ""))
        return f"此刻没有模型调用；最近一次在 {max(0, int(head.get('ago') or 0))} 秒前（{worker}）。"
    return "此刻没有模型调用。"


def queue_page(board: dict) -> str:
    """队列状态。已完成跟着时段。排队中是此刻的队列。"""

    summary = board.get("summary") or {}
    counts = board.get("counts") or {}
    activity_note = _queue_activity_note(board.get("activity") or {})
    untimed = int(summary.get("untimed_tasks") or 0)
    untimed_note = f"未标时间 {_grouped(untimed)} 项。" if untimed else ""
    span_tabs = []
    for value, label in _queue_spans():
        mark = " class='on'" if board.get("span") == value else ""
        span_tabs.append(f"<a{mark} href='{esc(_queue_href(board, span=value, page=1))}'>{label}</a>")
    detail_tabs = []
    for value, label in (("all", "全部"), ("done", "已完成"), ("queued", "排队中"), ("leased", "进行中")):
        mark = " class='on'" if board.get("tab") == value else ""
        count = int(counts.get(value) or 0)
        detail_tabs.append(
            f"<a{mark} href='{esc(_queue_href(board, tab=value, page=1))}'>{label} {_grouped(count)}</a>"
        )
    body_rows = []
    for row in board.get("rows") or []:
        full = str(row.get("name") or "")
        note = str(row.get("note") or "")
        body_rows.append(
            "<tr>"
            f"<td><a class='clip' href='/admin/sources/{esc(row.get('source_id') or '')}' title='{esc(full)}'>{esc(_queue_clip(full))}</a></td>"
            f"<td>{esc(row.get('imported_at') or '—')}</td>"
            f"<td>{esc(row.get('enqueued_at') or '—')}</td>"
            f"<td>{esc(row.get('stage') or '')}</td>"
            f"<td>{esc(row.get('state_label') or '')}</td>"
            f"<td>{esc(row.get('priority') or '')}</td>"
            f"<td>{esc(row.get('spent') or '—')}</td>"
            f"<td class='clip' title='{esc(note)}'>{esc(_queue_clip(note, 40) or '—')}</td>"
            "</tr>"
        )
    if not body_rows:
        body_rows.append("<tr><td class='empty' colspan='8'>这一页还没有任务</td></tr>")
    window = board.get("window") if isinstance(board.get("window"), dict) else {}
    grain = str(window.get("grain") or ("hour" if str(board.get("span") or "") == "24h" else "day"))
    avail = _chart_availability(board.get("series") or [], {**window, "grain": grain})
    payload = json.dumps(
        {
            "span": str(board.get("span") or ""),
            "grain": grain,
            "window": {
                "start": str(window.get("start") or ""),
                "end": str(window.get("end") or ""),
                "grain": grain,
                "tz": str(window.get("tz") or "UTC+8"),
            },
            "series": board.get("series") or [],
            "availability": avail,
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    from rogmem.queue_board import PAGE_SIZE as _QUEUE_PAGE

    foot = list_pager(
        "/admin/settings/queue",
        {"span": board.get("span") or "24h", "tab": board.get("tab") or "all"},
        total=int(board.get("total") or 0),
        page=int(board.get("page") or 1),
        page_size=board.get("page_size") or _QUEUE_PAGE,
        partial="queue",
    )
    inner = f"""<div class="board">
<section class="panel" id="queue-overview">
<header class="setting-head"><h2>任务概览</h2></header>
<div class="ov-filters">
<div class="tabs">{''.join(span_tabs)}</div>
<div class="tabs unit">
<button type="button" class="on" data-qunit="tasks">任务数</button>
<button type="button" data-qunit="chars">字符数</button>
</div>
</div>
<div class="ov-figures three">
<div><p class="k">已完成</p><p class="money">{_grouped(summary.get('done_tasks') or 0)} 项</p><p class="ov-split">{_grouped(summary.get('done_chars') or 0)} 字</p></div>
<div><p class="k">排队中</p><p class="money">{_grouped(summary.get('queued_tasks') or 0)} 项</p><p class="ov-split">{_grouped(summary.get('queued_chars') or 0)} 字</p></div>
<div><p class="k">平均每小时</p><p class="money">{esc(_queue_rate(summary.get('per_hour_tasks')))} 项</p><p class="ov-split">{esc(_queue_rate(summary.get('per_hour_chars')))} 字</p></div>
</div>
<p class="ov-activity">{activity_note}</p>
<figure class="ov-chart">
<svg id="q-svg" role="img" aria-label="萃取完成走势" aria-describedby="q-caption" aria-roledescription="柱状图" tabindex="0"></svg>
<div id="q-tip" class="ov-tip" hidden role="status" aria-live="polite"></div>
<figcaption id="q-caption" class="ov-caption">所选时段里完成的任务 · 北京时间 · 悬停或方向键查看。{untimed_note}此刻进行中 {int(summary.get('running') or 0)} 项，未跑完 {int(summary.get('stopped') or 0)} 项。</figcaption>
{_avail_legend()}
</figure>
</section>
<section class="panel" id="queue-tasks">
<header class="setting-head"><h2>任务明细</h2></header>
<div class="detail">
<div class="filters"><div class="tabs">{''.join(detail_tabs)}</div></div>
<p class="note">排队、进行中和未跑完是此刻的整条队列。已完成跟着上面的时段。排队按优先级从高到低，同一优先级里还没试过的在前。</p>
<div class="table-scroll">
<table class="calls">
<thead><tr>
<th>文件名</th><th>导入时间</th><th>入列时间</th><th>任务阶段</th><th>任务状态</th><th>优先级</th><th>完成耗时</th><th>备注</th>
</tr></thead>
<tbody>{''.join(body_rows)}</tbody>
</table>
</div>
{foot}
</div>
</section>
</div>
{_QUEUE_CHART.replace("__QUEUE__", payload)}
{_LIST_PARTIAL_SCRIPT}"""
    return _section_page("queue", "队列状态", inner)


def queue_list_fragment(board: dict) -> str:
    """The queue list alone. Overview figures are not part of this response."""

    from rogmem.queue_board import PAGE_SIZE as _QUEUE_PAGE

    body_rows = []
    for row in board.get("rows") or []:
        full = str(row.get("name") or "")
        note = str(row.get("note") or "")
        body_rows.append(
            "<tr>"
            f"<td><a class='clip' href='/admin/sources/{esc(row.get('source_id') or '')}' title='{esc(full)}'>{esc(_queue_clip(full))}</a></td>"
            f"<td>{esc(row.get('imported_at') or '—')}</td>"
            f"<td>{esc(row.get('enqueued_at') or '—')}</td>"
            f"<td>{esc(row.get('stage') or '')}</td>"
            f"<td>{esc(row.get('state_label') or '')}</td>"
            f"<td>{esc(row.get('priority') or '')}</td>"
            f"<td>{esc(row.get('spent') or '—')}</td>"
            f"<td class='clip' title='{esc(note)}'>{esc(_queue_clip(note, 40) or '—')}</td>"
            "</tr>"
        )
    if not body_rows:
        body_rows.append("<tr><td class='empty' colspan='8'>这一页还没有任务</td></tr>")
    foot = list_pager(
        "/admin/settings/queue",
        {"span": board.get("span") or "24h", "tab": board.get("tab") or "all"},
        total=int(board.get("total") or 0),
        page=int(board.get("page") or 1),
        page_size=board.get("page_size") or _QUEUE_PAGE,
        partial="queue",
    )
    return f"<tbody>{''.join(body_rows)}</tbody>{foot}"
