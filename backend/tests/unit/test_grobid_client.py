"""GrobidClient: request contents and outcome classification, with no real network."""

import asyncio
import logging

import httpx2 as httpx
import pytest

from app.services.grobid_client import FULLTEXT_PATH, GrobidClient

TEI = b'<?xml version="1.0"?><TEI xmlns="http://www.tei-c.org/ns/1.0"><text/></TEI>'


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "secret-user-name.pdf"
    path.write_bytes(b"%PDF-1.7\n%fake\n")
    return path


def client_for(handler, **kwargs) -> GrobidClient:
    kwargs.setdefault("timeout_seconds", 5)
    kwargs.setdefault("busy_retry_delay_seconds", 0)
    return GrobidClient("http://grobid.test:8070", transport=httpx.MockTransport(handler), **kwargs)


async def test_successful_response_returns_tei(pdf_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=TEI, headers={"content-type": "application/xml"})

    result = await client_for(handler).process_fulltext(pdf_path)
    assert result.outcome == "ok"
    assert result.tei == TEI
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == FULLTEXT_PATH


async def test_request_uses_fixed_privacy_preserving_parameters(pdf_path):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode("latin-1"))
        return httpx.Response(200, content=TEI)

    await client_for(handler).process_fulltext(pdf_path)
    body = bodies[0]
    for name, value in [
        ("consolidateHeader", "0"),
        ("consolidateCitations", "0"),
        ("includeRawCitations", "1"),
        ("segmentSentences", "1"),
    ]:
        assert f'name="{name}"\r\n\r\n{value}\r\n' in body
    for element in ("p", "head", "s", "biblStruct", "figure", "formula"):
        assert f'name="teiCoordinates"\r\n\r\n{element}\r\n' in body
    # The stored file name (and the user's original name) never leave the server.
    assert 'name="input"; filename="paper.pdf"' in body
    assert "secret-user-name" not in body
    assert "%PDF-1.7" in body


@pytest.mark.parametrize(
    ("status", "content", "outcome"),
    [
        (204, b"", "invalid_response"),
        (200, b"   \n", "invalid_response"),
        (500, b"[GENERAL] An exception occurred", "error"),
        (400, b"bad request", "error"),
        (404, b"", "error"),
    ],
)
async def test_http_status_classification(pdf_path, status, content, outcome):
    result = await client_for(lambda r: httpx.Response(status, content=content)).process_fulltext(pdf_path)
    assert result.outcome == outcome
    assert result.tei is None


async def test_busy_service_is_retried_once_then_succeeds(pdf_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, content=TEI)

    result = await client_for(handler).process_fulltext(pdf_path)
    assert result.outcome == "ok"
    assert len(calls) == 2


async def test_service_still_busy_after_retry_is_unavailable(pdf_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    result = await client_for(handler).process_fulltext(pdf_path)
    assert result.outcome == "unavailable"
    assert len(calls) == 2  # exactly one retry, no retry storm


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (httpx.ConnectError("refused"), "unavailable"),
        (httpx.ConnectTimeout("connect timed out"), "unavailable"),
        (httpx.ReadTimeout("read timed out"), "timeout"),
        (httpx.RemoteProtocolError("garbled"), "error"),
        (httpx.ReadError("reset"), "error"),
    ],
)
async def test_transport_errors_are_classified_not_raised(pdf_path, error, outcome):
    def handler(request):
        raise error

    result = await client_for(handler).process_fulltext(pdf_path)
    assert result.outcome == outcome
    assert result.tei is None


async def test_total_deadline_applies_even_if_the_server_trickles(pdf_path):
    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, content=TEI)

    result = await client_for(handler, timeout_seconds=0.2).process_fulltext(pdf_path)
    assert result.outcome == "timeout"


async def test_oversized_response_is_rejected(pdf_path):
    result = await client_for(
        lambda r: httpx.Response(200, content=b"<TEI>" + b"x" * 100 + b"</TEI>"), max_response_bytes=50
    ).process_fulltext(pdf_path)
    assert result.outcome == "invalid_response"
    assert result.tei is None


async def test_failure_log_contains_no_file_path(pdf_path, caplog):
    def handler(request):
        raise httpx.ConnectError(f"refused while sending {pdf_path}")

    with caplog.at_level(logging.WARNING, logger="app.services.grobid_client"):
        result = await client_for(handler).process_fulltext(pdf_path)
    assert result.outcome == "unavailable"
    assert str(pdf_path) not in caplog.text
    assert str(pdf_path) not in result.detail


def test_client_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    client = GrobidClient("http://grobid.test:8070", timeout_seconds=5)._client()
    assert client.trust_env is False


@pytest.mark.parametrize(
    ("response", "alive"),
    [
        (httpx.Response(200, text="true"), True),
        (httpx.Response(200, text="false"), False),
        (httpx.Response(503, text=""), False),
    ],
)
async def test_is_alive(response, alive):
    result, latency = await client_for(lambda r: response).is_alive()
    assert result is alive
    assert latency >= 0


async def test_is_alive_connection_refused():
    def handler(request):
        raise httpx.ConnectError("refused")

    alive, _ = await client_for(handler).is_alive()
    assert alive is False


async def test_unreadable_pdf_is_an_error_not_an_exception(tmp_path):
    def handler(request):
        raise AssertionError("must not be called")

    result = await client_for(handler).process_fulltext(tmp_path / "missing.pdf")
    assert result.outcome == "error"
    assert "missing.pdf" not in result.detail


async def test_oversized_streamed_response_without_length_is_rejected(pdf_path):
    async def body():
        for _ in range(10):
            yield b"<TEI>" + b"x" * 20  # chunked transfer: no Content-Length to check up front

    result = await client_for(lambda r: httpx.Response(200, content=body()), max_response_bytes=100).process_fulltext(
        pdf_path
    )
    assert result.outcome == "invalid_response"
