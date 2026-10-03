from app.health import HealthStore


def test_starts_unhealthy_with_not_checked_yet_reason():
    health = HealthStore()
    snap = health.snapshot()
    assert snap.claude_auth_ok is False
    assert snap.last_error == "not checked yet"


def test_set_auth_ok_clears_error():
    health = HealthStore()
    health.set_auth_failed("no token found")
    health.set_auth_ok()
    snap = health.snapshot()
    assert snap.claude_auth_ok is True
    assert snap.last_error is None


def test_set_auth_failed_records_reason():
    health = HealthStore()
    health.set_auth_ok()
    health.set_auth_failed("claude invocation timed out")
    snap = health.snapshot()
    assert snap.claude_auth_ok is False
    assert snap.last_error == "claude invocation timed out"
