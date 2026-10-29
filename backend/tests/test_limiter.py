from flashsale.limiter import FixedWindowLimiter


def test_allows_up_to_limit_then_blocks():
    limiter = FixedWindowLimiter()
    assert [limiter.allow("k", 3) for _ in range(5)] == [True, True, True, False, False]


def test_keys_are_independent():
    limiter = FixedWindowLimiter()
    assert limiter.allow("a", 1) and limiter.allow("b", 1)
    assert not limiter.allow("a", 1)


def test_non_positive_limit_disables_check():
    limiter = FixedWindowLimiter()
    assert all(limiter.allow("k", 0) for _ in range(100))
