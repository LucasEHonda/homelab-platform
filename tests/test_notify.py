import logging

import httpx

from deployer.notify import NtfyNotifier

URL = "https://ntfy.example.com/secret-topic"


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_posts_body_to_url():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    NtfyNotifier(URL, client=make_client(handler)).send("games v1.0.0: live")
    assert len(requests) == 1
    assert str(requests[0].url) == URL
    assert requests[0].content == b"games v1.0.0: live"


def test_send_without_url_does_nothing():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    NtfyNotifier(None, client=make_client(handler)).send("hello")
    assert requests == []


def test_send_failure_is_logged_without_url(caplog):
    def handler(request):
        return httpx.Response(500)

    with caplog.at_level(logging.WARNING):
        NtfyNotifier(URL, client=make_client(handler)).send("hello")
    assert "ntfy notification failed: HTTPStatusError" in caplog.text
    assert URL not in caplog.text
