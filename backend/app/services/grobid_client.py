"""HTTP client for a GROBID service (Day 6). The ONLY module that performs HTTP.

GROBID turns a PDF into TEI XML (header metadata, section structure, paragraphs
with PDF coordinates, bibliography). It is optional: every problem is reported as
an outcome string, never raised, so the caller can fall back to page-based
structure and still finish the extraction.

Outcomes returned by `process_fulltext`:
    ok                -> `tei` holds the response body (still to be validated by the parser)
    disabled          -> GROBID_ENABLED=false (the client is never called; see get_grobid_client)
    unavailable       -> connection refused / DNS failure / connect timeout / still busy (503) after one retry
    timeout           -> no complete response within GROBID_TIMEOUT_SECONDS
    invalid_response  -> 204 No Content, empty body or body larger than MAX_RESPONSE_BYTES
    error             -> any other HTTP status (GROBID could not process the PDF) or protocol error

Request parameters (all fixed, nothing comes from the client of our API):
    consolidateHeader=0, consolidateCitations=0
        No calls from GROBID to external services (CrossRef/biblio-glutton): the
        paper's metadata never leaves this machine, and results are reproducible.
    includeRawCitations=1   keep the raw reference string next to the parsed fields.
    segmentSentences=1      <s> elements inside paragraphs (used for chunk splitting).
    teiCoordinates=p,head,s,biblStruct,figure,formula
        coords="page,x,y,w,h;..." (1-based page numbers) for page-level citations,
        table-text detection and figure/formula placement.

The dependency is httpx2 (pinned in requirements.txt). It is imported only here, so
replacing it later means rewriting this one module.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx2 as httpx

logger = logging.getLogger(__name__)

FULLTEXT_PATH = "/api/processFulltextDocument"
ALIVE_PATH = "/api/isalive"
# A TEI document is typically 0.2-3 MB; anything far larger is not a paper we can use.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 5.0
BUSY_RETRY_DELAY_SECONDS = 3.0
# figure and formula coordinates (Day 6 quality pass) place captions and equations on the right page.
TEI_COORDINATE_ELEMENTS = ("p", "head", "s", "biblStruct", "figure", "formula")

ClientOutcome = Literal["ok", "unavailable", "timeout", "invalid_response", "error"]


@dataclass(frozen=True, slots=True)
class GrobidResponse:
    outcome: ClientOutcome
    tei: bytes | None
    # Internal diagnostic for server logs only. Never returned to API clients.
    detail: str
    elapsed_ms: float


class _ResponseTooLarge(Exception):
    pass


class GrobidClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        busy_retry_delay_seconds: float = BUSY_RETRY_DELAY_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport
        self._busy_retry_delay = busy_retry_delay_seconds
        self._max_response_bytes = max_response_bytes

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout_seconds, connect=min(CONNECT_TIMEOUT_SECONDS, self._timeout_seconds)),
            # GROBID is a local service: never route it through HTTP(S)_PROXY or read .netrc.
            trust_env=False,
            follow_redirects=False,
        )

    async def process_fulltext(self, pdf_path: Path) -> GrobidResponse:
        """Send one PDF to GROBID. Never raises for network/HTTP problems."""
        started = time.perf_counter()
        try:
            pdf_bytes = await asyncio.to_thread(pdf_path.read_bytes)
        except OSError as exc:
            return self._result("error", None, f"cannot read the stored PDF: {type(exc).__name__}", started)
        files = {"input": ("paper.pdf", pdf_bytes, "application/pdf")}  # fixed name: never the user's filename
        data = {
            "consolidateHeader": "0",
            "consolidateCitations": "0",
            "includeRawCitations": "1",
            "segmentSentences": "1",
            "teiCoordinates": list(TEI_COORDINATE_ELEMENTS),
        }
        try:
            # Total deadline for the whole exchange, including one busy retry.
            async with asyncio.timeout(self._timeout_seconds), self._client() as client:
                status, body = await self._post(client, files, data)
                if status == 503:
                    await asyncio.sleep(self._busy_retry_delay)
                    status, body = await self._post(client, files, data)
        except httpx.ConnectTimeout:
            return self._result("unavailable", None, "connect timeout", started)
        except (TimeoutError, httpx.TimeoutException):
            return self._result("timeout", None, f"no response within {self._timeout_seconds:.0f}s", started)
        except (httpx.ConnectError, httpx.UnsupportedProtocol) as exc:
            return self._result("unavailable", None, f"cannot connect: {type(exc).__name__}", started)
        except _ResponseTooLarge:
            return self._result("invalid_response", None, "response larger than the size limit", started)
        except httpx.HTTPError as exc:
            return self._result("error", None, f"transport error: {type(exc).__name__}", started)

        if status == 200:
            if not body.strip():
                return self._result("invalid_response", None, "empty 200 response", started)
            return self._result("ok", body, "", started)
        if status == 204:
            return self._result("invalid_response", None, "204 no content", started)
        if status == 503:
            return self._result("unavailable", None, "busy (503) after one retry", started)
        return self._result("error", None, f"HTTP {status}", started)

    async def _post(self, client: httpx.AsyncClient, files: dict, data: dict) -> tuple[int, bytes]:
        async with client.stream("POST", FULLTEXT_PATH, files=files, data=data) as response:
            if response.status_code != 200:
                return response.status_code, b""
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > self._max_response_bytes:
                raise _ResponseTooLarge
            chunks: list[bytes] = []
            size = 0
            async for piece in response.aiter_bytes():
                size += len(piece)
                if size > self._max_response_bytes:
                    raise _ResponseTooLarge
                chunks.append(piece)
            return 200, b"".join(chunks)

    async def is_alive(self) -> tuple[bool, float]:
        """GET /api/isalive -> (alive, latency_ms). Never raises for network problems."""
        started = time.perf_counter()
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS), self._client() as client:
                response = await client.get(ALIVE_PATH)
                alive = response.status_code == 200 and response.text.strip().lower() == "true"
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.info("GROBID liveness check failed: %s", type(exc).__name__)
            alive = False
        return alive, round((time.perf_counter() - started) * 1000, 2)

    def _result(self, outcome: ClientOutcome, tei: bytes | None, detail: str, started: float) -> GrobidResponse:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if outcome != "ok":
            logger.warning("GROBID request to %s failed: %s (%s, %.0f ms)", self._base_url, outcome, detail, elapsed_ms)
        return GrobidResponse(outcome=outcome, tei=tei, detail=detail, elapsed_ms=elapsed_ms)
