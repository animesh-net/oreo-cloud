import pytest

from oreo import notify


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


@pytest.fixture
def sent(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    return requests


def test_send_posts_message_with_headers(monkeypatch, sent):
    monkeypatch.setenv("OREO_NTFY_URL", "https://ntfy.example/oreo")
    monkeypatch.setenv("OREO_NTFY_TOKEN", "tk_test")
    notify.send("disk full", title="alert", priority="high", tags="warning")

    [request] = sent
    assert request.full_url == "https://ntfy.example/oreo"
    assert request.get_method() == "POST"
    assert request.data == b"disk full"
    assert request.get_header("Title") == "alert"
    assert request.get_header("Priority") == "high"
    assert request.get_header("Tags") == "warning"
    assert request.get_header("Authorization") == "Bearer tk_test"


def test_send_without_optional_headers(monkeypatch, sent):
    monkeypatch.delenv("OREO_NTFY_TOKEN", raising=False)
    notify.send("hello", url="https://ntfy.example/oreo")

    [request] = sent
    assert request.get_header("Title") is None
    assert request.get_header("Authorization") is None


def test_send_requires_url(monkeypatch):
    monkeypatch.delenv("OREO_NTFY_URL", raising=False)
    with pytest.raises(notify.NotifyError, match="OREO_NTFY_URL"):
        notify.send("hello")


def test_send_wraps_network_errors(monkeypatch):
    def fail(request, timeout):
        raise notify.urllib.error.URLError("unreachable")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fail)
    with pytest.raises(notify.NotifyError, match="unreachable"):
        notify.send("hello", url="https://ntfy.example/oreo")
