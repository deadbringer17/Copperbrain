from io import BytesIO
from pathlib import Path

import pytest

from copperbrain.adapters.downloads import DownloadAdapter, validate_download_url
from copperbrain.errors import CopperbrainError


class Headers(dict[str, str]):
    pass


class FakeResponse:
    def __init__(self, payload: bytes, url: str, content_type: str = "application/pdf") -> None:
        self.stream = BytesIO(payload)
        self.url = url
        self.headers = Headers({"Content-Type": content_type, "Content-Length": str(len(payload))})

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass


def test_validate_download_url() -> None:
    assert (
        validate_download_url("https://wmsc.lcsc.com/a.pdf", ("lcsc.com",)).hostname
        == "wmsc.lcsc.com"
    )
    for url in ("http://lcsc.com/a", "https://evil.example/a", "https://user:x@lcsc.com/a"):
        with pytest.raises(CopperbrainError, match="not allowed"):
            validate_download_url(url, ("lcsc.com",))


def test_download_validates_and_writes_atomically(tmp_path: Path) -> None:
    adapter = DownloadAdapter(
        ("lcsc.com",),
        timeout=1,
        max_bytes=100,
        opener=lambda request, timeout: FakeResponse(b"pdf", "https://wmsc.lcsc.com/a.pdf"),
    )
    destination = tmp_path / "a.pdf"
    assert (
        adapter.download(
            "https://lcsc.com/a.pdf", destination, allowed_content_types=("application/pdf",)
        )
        == destination
    )
    assert destination.read_bytes() == b"pdf"


def test_download_rejects_type_and_size(tmp_path: Path) -> None:
    wrong_type = DownloadAdapter(
        ("lcsc.com",),
        timeout=1,
        max_bytes=10,
        opener=lambda request, timeout: FakeResponse(b"x", "https://lcsc.com/a", "text/html"),
    )
    with pytest.raises(CopperbrainError, match="content type"):
        wrong_type.download(
            "https://lcsc.com/a", tmp_path / "a.pdf", allowed_content_types=("application/pdf",)
        )
    too_large = DownloadAdapter(
        ("lcsc.com",),
        timeout=1,
        max_bytes=2,
        opener=lambda request, timeout: FakeResponse(b"123", "https://lcsc.com/a"),
    )
    with pytest.raises(CopperbrainError, match="size limit"):
        too_large.download(
            "https://lcsc.com/a", tmp_path / "a.pdf", allowed_content_types=("application/pdf",)
        )
