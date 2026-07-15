"""Restricted downloader for datasheets and resolved component assets."""

from __future__ import annotations

import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode


class Response(Protocol):
    headers: object

    def read(self, size: int = -1) -> bytes: ...
    def geturl(self) -> str: ...
    def __enter__(self) -> Response: ...
    def __exit__(self, *args: object) -> None: ...


def validate_download_url(url: str, allowed_hosts: tuple[str, ...]) -> urllib.parse.ParseResult:
    """Allow HTTPS only and exact hosts/subdomains from an explicit allowlist."""
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    allowed = any(hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts)
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise CopperbrainError(ErrorCode.INVALID_INPUT, "Download URL is not allowed")
    return parsed


class DownloadAdapter:
    def __init__(
        self,
        allowed_hosts: tuple[str, ...],
        *,
        timeout: float,
        max_bytes: int,
        opener: object = urllib.request.urlopen,
    ) -> None:
        self.allowed_hosts = tuple(host.casefold().rstrip(".") for host in allowed_hosts)
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = opener

    def download(
        self,
        url: str,
        destination: Path,
        *,
        allowed_content_types: tuple[str, ...],
    ) -> Path:
        """Download atomically with redirect, type, and bounded-size validation."""
        validate_download_url(url, self.allowed_hosts)
        request = urllib.request.Request(url, headers={"User-Agent": "Copperbrain/0.1"})
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        os.close(descriptor)
        try:
            with self.opener(request, timeout=self.timeout) as response:  # type: ignore[operator]
                validate_download_url(response.geturl(), self.allowed_hosts)
                content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0]
                if content_type not in allowed_content_types:
                    raise CopperbrainError(
                        ErrorCode.VALIDATION_FAILED,
                        "Downloaded asset has an unexpected content type",
                        details={"content_type": content_type},
                    )
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > self.max_bytes:
                    raise CopperbrainError(
                        ErrorCode.VALIDATION_FAILED, "Download exceeds size limit"
                    )
                total = 0
                with Path(temporary).open("wb") as stream:
                    while chunk := response.read(min(65536, self.max_bytes + 1 - total)):
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise CopperbrainError(
                                ErrorCode.VALIDATION_FAILED, "Download exceeds size limit"
                            )
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except CopperbrainError:
            raise
        except (OSError, ValueError) as exc:
            raise CopperbrainError(ErrorCode.NETWORK_ERROR, "Asset download failed") from exc
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return destination
