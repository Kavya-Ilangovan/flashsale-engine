from flashsale.auth import issue_token, verify_token


def test_round_trip(settings):
    token = issue_token(settings, "alice_01")
    assert verify_token(settings, token) == "alice_01"


def test_tampered_token_is_rejected(settings):
    body, _, signature = issue_token(settings, "alice_01").partition(".")
    assert verify_token(settings, f"{body}.{signature[::-1]}") is None
    assert verify_token(settings, f"{body}x.{signature}") is None


def test_expired_token_is_rejected(settings):
    token = issue_token(settings, "alice_01", now=1_000)
    assert verify_token(settings, token, now=1_000 + settings.session_ttl_sec + 1) is None


def test_malformed_input_is_rejected(settings):
    for bad in (None, "", "nodot", "a.b", "....", "e30.e30"):
        assert verify_token(settings, bad) is None
