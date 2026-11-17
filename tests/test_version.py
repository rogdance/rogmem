"""Product version is one pack stamp, not a live clock."""

from datetime import datetime, timezone

from rogmem.ui import about_page
from rogmem.version import product_version, production_suffix, shanghai_stamp


def test_production_suffix_follows_the_repository():
    """Private rogmem-dev stamps DEV. Public rogmem stamps PUB."""
    assert production_suffix("https://github.com/rogdance/rogmem-dev.git") == "DEV"
    assert production_suffix("git@github.com:rogdance/rogmem-dev.git") == "DEV"
    assert production_suffix("rogdance/rogmem-dev") == "DEV"
    assert production_suffix("https://github.com/rogdance/rogmem.git") == "PUB"
    assert production_suffix("rogdance/rogmem") == "PUB"
    try:
        production_suffix("rogdance/other")
    except ValueError as error:
        assert "other" in str(error)
    else:
        raise AssertionError("unknown repository was given a suffix")


def test_stamp_uses_shanghai_clock():
    moment = datetime(2026, 10, 27, 10, 57, tzinfo=timezone.utc)
    assert shanghai_stamp(moment) == "2610271857"


def test_unpacked_run_does_not_invent_a_clock(monkeypatch):
    monkeypatch.delenv("ROGMEM_BUILD_STAMP", raising=False)
    monkeypatch.setattr("rogmem.version.pack_stamp", lambda: "")
    assert product_version() == "V1-未打包"


def test_pack_stamp_is_the_version(monkeypatch):
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "2610271857")
    monkeypatch.delenv("ROGMEM_RELEASE", raising=False)
    assert product_version() == "V1-2610271857"


def test_release_channel_marks_production_and_pre(monkeypatch):
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "2611072056")
    monkeypatch.setenv("ROGMEM_RELEASE", "dev")
    assert product_version() == "V1-2611072056-DEV"
    monkeypatch.setenv("ROGMEM_RELEASE", "pub")
    assert product_version() == "V1-2611072056-PUB"
    monkeypatch.setenv("ROGMEM_RELEASE", "PRE")
    assert product_version() == "V1-2611072056-PRE"
    monkeypatch.setenv("ROGMEM_RELEASE", "beta")
    assert product_version() == "V1-2611072056"


def test_bad_stamp_is_ignored(monkeypatch):
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "yesterday")
    monkeypatch.setattr("rogmem.version.Path.is_file", lambda _self: False)
    assert product_version() == "V1-未打包"


def test_about_page_shows_the_single_version():
    html = about_page("V1-2610271857")
    assert "关于ROGMEM" in html
    assert "V1-2610271857" in html
    assert "/admin/settings/about" in html
    assert "S-V1" not in html
    assert "C-V1" not in html
    assert "致谢" in html
    assert "Tencent · <a href='https://github.com/Tencent/WeKnora'" in html
    assert "Techartist · <a href='https://codepen.io/VoXelo/pen/dPMeGze'" in html
    assert "cosmos" not in html.lower()


def test_nav_puts_the_version_left_of_the_theme_icon(monkeypatch):
    """版本号靠右，主题切换是描边图标，贴在版本号右侧。"""
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "2610271857")
    html = about_page("V1-2610271857")
    header = html.split("<main", 1)[0]
    assert '<span class="ver">V1-2610271857</span>' in header
    assert header.index("</nav>") < header.index('class="ver"') < header.index('id="theme"')
    assert 'savedTheme !== "light"' in header
    assert 'setAttribute("data-theme", "dark")' in header
    assert 'aria-label="切换到日间"' in header
    assert ">日间</button>" not in header
    assert ">暗夜</button>" not in header
    assert "theme-moon" in header and "theme-sun" in header
    style = " ".join(html.split("</style>", 1)[0].split())
    assert "header .ver { margin-left: auto;" in style
    assert 'html[data-theme="dark"] header .theme .theme-sun { display: block; }' in style
