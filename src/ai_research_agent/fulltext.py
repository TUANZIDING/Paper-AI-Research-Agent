from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import signal
import threading
from typing import Mapping, Protocol, Sequence
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

from .license_policy import (
    LicenseDecision,
    ManuscriptVersion,
    assess_fulltext_license,
)


class FulltextDownloadError(RuntimeError):
    """Base error for a blocked or failed full-text download."""


class FulltextPolicyError(FulltextDownloadError):
    """The license, version, URL, or robots policy did not permit downloading."""


class UnsafeNetworkTarget(FulltextDownloadError):
    """DNS or URL validation found an unsafe network target."""


class FulltextResponseError(FulltextDownloadError):
    """The HTTP response was unsafe, invalid, or larger than permitted."""


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class DownloadedFulltext:
    path: Path
    final_url: str
    sha256: str
    byte_length: int
    content_type: str
    normalized_license: str
    manuscript_version: ManuscriptVersion
    retrieved_at: str
    redirect_chain: tuple[str, ...]


class Resolver(Protocol):
    def __call__(self, hostname: str, port: int) -> Sequence[str]: ...


class Transport(Protocol):
    def request(
        self,
        *,
        url: str,
        pinned_ip: str,
        timeout: float,
        max_bytes: int,
        headers: Mapping[str, str],
    ) -> TransportResponse: ...


def _default_resolver(hostname: str, port: int) -> list[str]:
    try:
        answers = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise UnsafeNetworkTarget(f"DNS resolution failed for {hostname}.") from exc
    return sorted({str(answer[4][0]) for answer in answers})


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, pinned_ip: str, **kwargs: object):
        self._pinned_ip = pinned_ip
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, **kwargs: object):
        self._pinned_ip = pinned_ip
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


class PinnedHTTPTransport:
    """HTTP transport that connects to the already validated DNS answer."""

    def __init__(self, *, tls_context: ssl.SSLContext | None = None):
        self.tls_context = tls_context or ssl.create_default_context()

    def request(
        self,
        *,
        url: str,
        pinned_ip: str,
        timeout: float,
        max_bytes: int,
        headers: Mapping[str, str],
    ) -> TransportResponse:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHTTPSConnection(
                parsed.hostname or "",
                pinned_ip,
                port=port,
                timeout=timeout,
                context=self.tls_context,
            )
        elif parsed.scheme == "http":
            connection = _PinnedHTTPConnection(
                parsed.hostname or "", pinned_ip, port=port, timeout=timeout
            )
        else:  # Validation should make this unreachable.
            raise UnsafeNetworkTarget("Only HTTP(S) transport is supported.")
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            connection.request("GET", target, headers=dict(headers))
            response = connection.getresponse()
            response_headers = {key.casefold(): value for key, value in response.getheaders()}
            declared = response_headers.get("content-length")
            if declared is not None:
                try:
                    declared_length = int(declared)
                except ValueError as exc:
                    raise FulltextResponseError("Invalid Content-Length header.") from exc
                if declared_length < 0 or declared_length > max_bytes:
                    raise FulltextResponseError(
                        f"Response exceeds the {max_bytes}-byte limit."
                    )
            deadline = time.monotonic() + timeout
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise FulltextResponseError("HTTP response exceeded the total timeout.")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining_time)
                chunk = response.read(min(65_536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise FulltextResponseError(
                        f"Response exceeds the {max_bytes}-byte limit."
                    )
            body = b"".join(chunks)
            return TransportResponse(response.status, response_headers, body)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise FulltextResponseError(f"HTTP transport failed: {type(exc).__name__}.") from exc
        finally:
            connection.close()


class SafeFulltextDownloader:
    """Fail-closed downloader for explicitly licensed public full text.

    It never sends credentials or cookies, never follows automatic redirects,
    and never attempts to authenticate or bypass access controls.
    """

    _REDIRECTS = frozenset({301, 302, 303, 307, 308})
    _LOGIN_OR_PAYWALL = frozenset({401, 402, 403, 407})

    def __init__(
        self,
        *,
        resolver: Resolver = _default_resolver,
        transport: Transport | None = None,
        timeout: float = 20.0,
        max_bytes: int = 25_000_000,
        max_redirects: int = 5,
        require_https: bool = True,
        user_agent: str = "AIResearchAgent-Fulltext/0.1",
        robots_max_bytes: int = 256_000,
        pdf_validator=None,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_bytes < 1 or robots_max_bytes < 1:
            raise ValueError("byte limits must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        self.resolver = resolver
        self.transport = transport or PinnedHTTPTransport()
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.require_https = require_https
        self.user_agent = user_agent
        self.robots_max_bytes = robots_max_bytes
        self.pdf_validator = pdf_validator or _validate_pdf_with_pdfinfo

    def download(
        self,
        *,
        url: str,
        license: str | None,
        version: str | None,
        destination: str | Path,
        accepted_versions: frozenset[ManuscriptVersion] | None = None,
        overwrite: bool = False,
    ) -> DownloadedFulltext:
        assessment = assess_fulltext_license(
            url=url, license=license, version=version
        )
        allowed_versions = (
            accepted_versions
            if accepted_versions is not None
            else frozenset(
                {
                    ManuscriptVersion.SUBMITTED,
                    ManuscriptVersion.ACCEPTED,
                    ManuscriptVersion.PUBLISHED,
                }
            )
        )
        if assessment.decision is not LicenseDecision.ALLOWED_MACHINE_READ:
            raise FulltextPolicyError(
                "An explicit allowlisted machine-readable license is required."
            )
        if assessment.version not in allowed_versions:
            raise FulltextPolicyError("The manuscript version is unknown or not accepted.")
        target = Path(destination)
        if not target.is_absolute():
            raise FulltextPolicyError("destination must be an absolute path")
        if target.exists() and not overwrite:
            raise FulltextPolicyError("destination already exists")

        response, final_url, chain = self._fetch_document(url)
        content_type = _base_content_type(_header(response.headers, "content-type"))
        _validate_document(response.body, content_type, pdf_validator=self.pdf_validator)
        digest = hashlib.sha256(response.body).hexdigest()
        _atomic_write(target, response.body, overwrite=overwrite)
        return DownloadedFulltext(
            path=target,
            final_url=final_url,
            sha256=digest,
            byte_length=len(response.body),
            content_type=content_type,
            normalized_license=assessment.normalized_license or "",
            manuscript_version=assessment.version,
            retrieved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            redirect_chain=tuple(chain),
        )

    def _fetch_document(
        self, url: str
    ) -> tuple[TransportResponse, str, list[str]]:
        current = url
        initial = urlsplit(url)
        licensed_origin = (
            initial.scheme.casefold(), initial.hostname.casefold() if initial.hostname else None,
            initial.port or (443 if initial.scheme.casefold() == "https" else 80),
        )
        chain: list[str] = []
        robots_cache: dict[tuple[str, str], bool] = {}
        for redirect_count in range(self.max_redirects + 1):
            parsed, pinned_ip = self._validate_and_pin(current)
            origin = (parsed.scheme, parsed.netloc)
            if origin not in robots_cache:
                robots_cache[origin] = self._robots_allows(current, parsed)
            if not robots_cache[origin]:
                raise FulltextPolicyError("robots policy does not allow this download")
            chain.append(current)
            response = self._timed_request(
                url=current, pinned_ip=pinned_ip, max_bytes=self.max_bytes,
                headers=self._headers(parsed),
            )
            if response.status in self._LOGIN_OR_PAYWALL:
                raise FulltextPolicyError(
                    f"HTTP {response.status} requires access or payment; no bypass is attempted."
                )
            if response.status in self._REDIRECTS:
                location = _header(response.headers, "location")
                if not location:
                    raise FulltextResponseError("Redirect response lacks Location.")
                if redirect_count >= self.max_redirects:
                    raise FulltextResponseError("Too many redirects.")
                next_url = urljoin(current, location)
                if self.require_https and urlsplit(next_url).scheme.casefold() != "https":
                    raise UnsafeNetworkTarget("HTTPS downgrade redirect is blocked.")
                # Resolve and classify the redirect target before considering it.
                self._validate_and_pin(next_url)
                next_parsed = urlsplit(next_url)
                next_origin = (
                    next_parsed.scheme.casefold(),
                    next_parsed.hostname.casefold() if next_parsed.hostname else None,
                    next_parsed.port or (443 if next_parsed.scheme.casefold() == "https" else 80),
                )
                if next_origin != licensed_origin:
                    raise FulltextPolicyError(
                        "Cross-origin redirect is not covered by the supplied license evidence."
                    )
                current = next_url
                continue
            if response.status != 200:
                raise FulltextResponseError(f"Unexpected HTTP status {response.status}.")
            if len(response.body) > self.max_bytes:
                raise FulltextResponseError(
                    f"Response exceeds the {self.max_bytes}-byte limit."
                )
            return response, current, chain
        raise FulltextResponseError("Too many redirects.")

    def _validate_and_pin(self, url: str) -> tuple[SplitResult, str]:
        try:
            parsed = urlsplit(url)
            port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        except ValueError as exc:
            raise UnsafeNetworkTarget("Malformed URL.") from exc
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise UnsafeNetworkTarget("Only absolute HTTP(S) URLs are allowed.")
        if self.require_https and scheme != "https":
            raise UnsafeNetworkTarget("HTTPS is required for automatic full-text download.")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeNetworkTarget("URL userinfo is forbidden.")
        hostname = parsed.hostname.casefold().rstrip(".")
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            raise UnsafeNetworkTarget("Local hostnames are forbidden.")
        addresses = list(self.resolver(hostname, port))
        if not addresses:
            raise UnsafeNetworkTarget("DNS returned no addresses.")
        validated: list[str] = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise UnsafeNetworkTarget("Resolver returned a non-IP address.") from exc
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            if not address.is_global:
                raise UnsafeNetworkTarget(
                    "DNS answer contains a private, local, reserved, or non-global address."
                )
            validated.append(str(address))
        return parsed, sorted(validated)[0]

    def _robots_allows(self, url: str, parsed: SplitResult) -> bool:
        scheme = parsed.scheme
        netloc = parsed.netloc
        robots_url = urlunsplit((scheme, netloc, "/robots.txt", "", ""))
        robots_parsed, pinned_ip = self._validate_and_pin(robots_url)
        response = self._timed_request(
            url=robots_url, pinned_ip=pinned_ip, max_bytes=self.robots_max_bytes,
            headers=self._headers(robots_parsed, accept="text/plain"),
        )
        if response.status in {404, 410}:
            return True
        if response.status != 200 or len(response.body) > self.robots_max_bytes:
            return False
        content_type = _base_content_type(_header(response.headers, "content-type"))
        if content_type not in {"text/plain", "text/robots"}:
            return False
        try:
            text = response.body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        if not any(
            line.split(":", 1)[0].strip().casefold() == "user-agent"
            for line in text.splitlines() if ":" in line
        ):
            return False
        parser = RobotFileParser(robots_url)
        parser.parse(text.splitlines())
        return parser.can_fetch(self.user_agent, url)

    def _timed_request(
        self, *, url: str, pinned_ip: str, max_bytes: int,
        headers: Mapping[str, str],
    ) -> TransportResponse:
        if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
            raise FulltextResponseError(
                "Wall-clock request deadline cannot be enforced in this execution context."
            )

        def expired(signum, frame):
            raise TimeoutError("wall-clock request deadline exceeded")

        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        started = time.monotonic()
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, self.timeout)
        try:
            return self.transport.request(
                url=url, pinned_ip=pinned_ip, timeout=self.timeout,
                max_bytes=max_bytes, headers=headers,
            )
        except TimeoutError as exc:
            raise FulltextResponseError("HTTP request exceeded the wall-clock timeout.") from exc
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                remaining = max(0.000001, previous_timer[0] - (time.monotonic() - started))
                signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])

    def _headers(self, parsed: SplitResult, *, accept: str | None = None) -> dict[str, str]:
        hostname = parsed.hostname
        port = parsed.port
        scheme = parsed.scheme
        default_port = 443 if scheme == "https" else 80
        host = hostname if port in (None, default_port) else f"{hostname}:{port}"
        return {
            "Accept": accept or "application/pdf, application/xml, text/xml;q=0.9",
            "Connection": "close",
            "Host": host,
            "User-Agent": self.user_agent,
        }


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.casefold()
    for key, value in headers.items():
        if key.casefold() == expected:
            return value
    return None


def _base_content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().casefold()


def _validate_document(body: bytes, content_type: str, *, pdf_validator=None) -> None:
    if content_type == "application/pdf":
        stripped = body.rstrip(b"\x00\t\r\n \f")
        if (
            not body.startswith(b"%PDF-")
            or not stripped.endswith(b"%%EOF")
            or b"/Type /Page" not in body
            or b"trailer" not in body
            or re.search(br"startxref\s+[0-9]+\s+%%EOF$", stripped) is None
            or (b"\nxref" not in body and b"/Type /XRef" not in body)
        ):
            raise FulltextResponseError("PDF content signature is invalid.")
        (pdf_validator or _validate_pdf_with_pdfinfo)(body)
        return
    if content_type in {"application/xml", "text/xml"} or content_type.endswith("+xml"):
        probe = body.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
        if not probe.startswith((b"<?xml", b"<")):
            raise FulltextResponseError("XML content signature is invalid.")
        upper = body.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise FulltextResponseError("DTD and entity declarations are forbidden.")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise FulltextResponseError("XML document is not well formed.") from exc
        local_name = root.tag.rsplit("}", 1)[-1].casefold()
        if local_name not in {"article", "pmc-articleset"}:
            raise FulltextResponseError("XML root is not a supported full-text article container.")
        namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
        if namespace:
            namespace_url = urlsplit(namespace)
            if (
                namespace_url.scheme not in {"http", "https"}
                or namespace_url.hostname != "jats.nlm.nih.gov"
            ):
                raise FulltextResponseError("XML namespace is not an allowlisted JATS namespace.")
        local_names = [node.tag.rsplit("}", 1)[-1].casefold() for node in root.iter()]
        required_metadata = {"front", "article-meta", "title-group", "article-title"}
        if not required_metadata.issubset(local_names):
            raise FulltextResponseError("XML lacks required JATS article metadata structure.")
        body_nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].casefold() == "body"]
        body_text = " ".join(
            value.strip() for node in body_nodes for value in node.itertext() if value.strip()
        )
        if (
            not body_nodes
            or len(body_text) < 200
            or not any(name in {"p", "sec"} for name in local_names)
        ):
            raise FulltextResponseError("XML lacks a substantive article body.")
        return
    raise FulltextResponseError(
        "Content-Type must explicitly identify PDF or XML full text."
    )


def _validate_pdf_with_pdfinfo(body: bytes) -> None:
    binary = shutil.which("pdfinfo")
    if binary is None:
        raise FulltextResponseError(
            "PDF validation requires the local pdfinfo parser; PDF download is blocked."
        )
    with tempfile.NamedTemporaryFile("wb", suffix=".pdf") as stream:
        stream.write(body)
        stream.flush()
        try:
            result = subprocess.run(
                [binary, stream.name], capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FulltextResponseError("PDF parser validation failed.") from exc
    if result.returncode != 0:
        raise FulltextResponseError("PDF parser rejected the document.")


def _atomic_write(path: Path, body: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary_name = stream.name
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary = Path(temporary_name)
        if overwrite:
            os.replace(temporary, path)
            temporary_name = None
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise FulltextPolicyError("destination already exists") from exc
            temporary.unlink()
            temporary_name = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
