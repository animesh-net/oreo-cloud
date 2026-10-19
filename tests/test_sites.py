import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from oreo import sites
from oreo.checks import Check


@pytest.fixture
def server():
    """A local site that answers with whatever status the test asks for."""
    status = {"code": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status["code"])
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/api/server/ping", status
    httpd.shutdown()


def test_answering_site_passes(server):
    url, _ = server
    assert sites.check_answer(url) == Check("site 127.0.0.1", True, "answers HTTP 200")


def test_password_protected_site_counts_as_up(server):
    url, status = server
    status["code"] = 401
    assert sites.check_answer(url).ok


def test_server_error_fails(server):
    url, status = server
    status["code"] = 502
    assert sites.check_answer(url) == Check("site 127.0.0.1", False, "answers HTTP 502")


def test_site_that_does_not_answer_fails():
    check = sites.check_answer("http://127.0.0.1:9/")
    assert not check.ok
    assert check.detail.startswith("no answer")


def test_certificate_with_time_left_passes():
    check = sites.check_certificate("https://cloud.example/", days_left=lambda host: 60.2)
    assert check == Check("certificate cloud.example", True, "valid for 60 more days")


def test_certificate_close_to_expiry_fails(monkeypatch):
    monkeypatch.setenv("OREO_CERT_MIN_DAYS", "14")
    check = sites.check_certificate("https://cloud.example/", days_left=lambda host: 9)
    assert not check.ok
    assert check.detail == "expires in 9 days; Caddy should have renewed it"


def test_unreadable_certificate_fails():
    def broken(host):
        raise OSError("connection refused")

    assert not sites.check_certificate("https://cloud.example/", days_left=broken).ok


def test_sites_come_from_the_setting(monkeypatch):
    monkeypatch.setenv("OREO_SITES", "https://a.example/, https://b.example/x")
    assert sites.configured() == ["https://a.example/", "https://b.example/x"]
    assert len(sites.DEFAULT_SITES.split(",")) == 2
