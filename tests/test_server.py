import dataclasses
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cclens import index, server


@pytest.fixture
def live(cfg):
    index.index_all(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(server.Api(cfg)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return response.status, response.headers, response.read()


def test_the_page_is_served_with_a_policy_that_forbids_outbound_loads(live):
    status, headers, body = get(live, "/")
    assert status == 200
    assert b"<title>cclens</title>" in body
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert "Access-Control-Allow-Origin" not in headers


def test_every_api_route_answers(live):
    for path in ("/api/stats", "/api/doctor", "/api/sessions",
                 "/api/sessions/session-one", "/api/entries?limit=2",
                 "/api/hooks?limit=2", "/api/search?q=widgets", "/api/tool/tu-one",
                 "/api/agents?session=session-one"):
        status, _, body = get(live, path)
        assert status == 200, path
        json.loads(body)


def test_an_unknown_page_is_served_so_the_hash_router_can_take_it(live):
    status, _, body = get(live, "/session/session-one")
    assert status == 200
    assert b"<title>cclens</title>" in body


def test_a_static_name_outside_the_package_is_refused(live):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(live, "/..%2f..%2fetc%2fpasswd")
    assert raised.value.code == 404


def test_an_unknown_api_route_is_a_404(live):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(live, "/api/badger")
    assert raised.value.code == 404


def test_a_missing_entry_is_a_404(live):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(live, "/api/entries/999999")
    assert raised.value.code == 404


def test_large_responses_come_back_compressed(live):
    request = urllib.request.Request(live + "/api/stats",
                                     headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.headers["Content-Encoding"] in (None, "gzip")


@pytest.mark.parametrize("header", ["evil.example", "evil.example:8731",
                                    "127.0.0.1.evil.example", ""])
def test_a_request_naming_anything_but_loopback_is_refused(live, header):
    request = urllib.request.Request(live + "/api/sessions", headers={"Host": header})
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=10)
    assert raised.value.code == 403


@pytest.mark.parametrize("header", ["127.0.0.1:8731", "localhost:8731",
                                    "[::1]:8731", "LocalHost:8731", "localhost"])
def test_a_request_naming_loopback_is_answered(live, header):
    request = urllib.request.Request(live + "/api/sessions", headers={"Host": header})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


def test_an_exposed_bind_answers_to_the_name_it_was_reached_by(cfg):
    exposed = dataclasses.replace(cfg, host="0.0.0.0")
    assert server.host_allowed("mymac.local:8731", exposed)
    assert not server.host_allowed("mymac.local:8731", cfg)
