"""Rate limits for enroll and admin writes."""

import json

from rogmem import rate_limit
from tests.test_auth_boundary import Hub


def test_sliding_window_blocks_then_allows_again():
    rate_limit.reset()
    assert rate_limit.allow("enroll-apply:1.2.3.4", 2, 60, now=100) is True
    assert rate_limit.allow("enroll-apply:1.2.3.4", 2, 60, now=101) is True
    assert rate_limit.allow("enroll-apply:1.2.3.4", 2, 60, now=102) is False
    assert rate_limit.allow("enroll-apply:9.9.9.9", 2, 60, now=102) is True
    assert rate_limit.allow("enroll-claim:1.2.3.4", 2, 60, now=102) is True
    assert rate_limit.allow("enroll-apply:1.2.3.4", 2, 60, now=160) is True


def test_default_quotas_are_finite_and_small():
    enroll_limit, enroll_window = rate_limit.default_quota("enroll")
    admin_limit, admin_window = rate_limit.default_quota("admin")
    assert enroll_window == 60 and 1 <= enroll_limit <= 30
    assert admin_window == 60 and 1 <= admin_limit <= 120


def test_non_positive_env_keeps_default(monkeypatch):
    monkeypatch.setenv("ROGMEM_RATE_ENROLL", "0/60")
    monkeypatch.setenv("ROGMEM_RATE_ADMIN", "unlimited")
    assert rate_limit.configured_quota("enroll") == rate_limit.default_quota("enroll")
    assert rate_limit.configured_quota("admin") == rate_limit.default_quota("admin")


def test_http_enroll_and_admin_writes_return_429(tmp_path, monkeypatch):
    from rogmem import crypto

    rate_limit.reset()
    hub = Hub(tmp_path, monkeypatch, enroll_quota="2/60", admin_quota="2/60")
    try:
        codes = []
        for i in range(3):
            _secret, public = crypto.new_keypair()
            status, _body, _headers = hub.req(
                "POST",
                "/enroll/apply",
                {"device_id": f"dev-{i}", "device_name": "qa", "program": "Cursor", "pubkey": crypto.b64encode(public)},
            )
            codes.append(status)
        assert codes[:2] == [200, 200]
        assert codes[2] == 429
        rate_limit.reset()
        from rogmem import auth, engine

        engine.set_setting("admin_password", auth.hash_password("rate-limit-pass"))
        engine.db().commit()
        cookie = hub.login("rate-limit-pass")
        first = hub.req("POST", "/admin/api/settings/webhook", {"url": ""}, {"Cookie": cookie})
        second = hub.req("POST", "/admin/api/settings/webhook", {"url": ""}, {"Cookie": cookie})
        third = hub.req("POST", "/admin/api/settings/webhook", {"url": "http://169.254.169.254/"}, {"Cookie": cookie})
        assert first[0] == 200, first[1]
        assert second[0] == 200, second[1]
        assert third[0] == 429, third[1]
        saved = json.loads(first[1])
        assert saved.get("ok") is True
    finally:
        hub.close()
