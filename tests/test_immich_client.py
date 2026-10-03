import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import ImmichClient, ImmichError


class FakeResponse:
    def __init__(self, ok, status_code=200, text="", headers=None, chunks=(b"abc",)):
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._chunks = chunks

    def iter_content(self, chunk_size=65536):
        return iter(self._chunks)


class FakeSession:
    """Stands in for requests.Session -- only implements .get(), since
    download_asset_original deliberately bypasses ImmichClient._request
    (which assumes a JSON body) in favor of a direct streaming call."""

    def __init__(self, response):
        self.headers = {}
        self._response = response
        self.requested_url = None
        self.requested_kwargs = None

    def get(self, url, **kwargs):
        self.requested_url = url
        self.requested_kwargs = kwargs
        return self._response

    def request(self, method, url, **kwargs):
        raise AssertionError("download_asset_original should use .get(), not ._request()")


def test_download_asset_original_infers_jpg_extension(tmp_path):
    response = FakeResponse(True, headers={"content-type": "image/jpeg"}, chunks=(b"hello", b"world"))
    session = FakeSession(response)
    client = ImmichClient("http://immich", "key", session=session)

    dest_path = client.download_asset_original("abc123", str(tmp_path))

    assert dest_path == str(tmp_path / "abc123.jpg")
    with open(dest_path, "rb") as fh:
        assert fh.read() == b"helloworld"
    assert session.requested_url == "http://immich/api/assets/abc123/original"
    assert session.requested_kwargs["stream"] is True


def test_download_asset_original_infers_png_extension_ignoring_charset(tmp_path):
    response = FakeResponse(True, headers={"content-type": "image/png; charset=binary"})
    session = FakeSession(response)
    client = ImmichClient("http://immich", "key", session=session)

    dest_path = client.download_asset_original("xyz", str(tmp_path))

    assert dest_path.endswith("xyz.png")


def test_download_asset_original_defaults_to_jpg_for_unrecognized_content_type(tmp_path):
    response = FakeResponse(True, headers={"content-type": "application/octet-stream"})
    session = FakeSession(response)
    client = ImmichClient("http://immich", "key", session=session)

    dest_path = client.download_asset_original("qqq", str(tmp_path))

    assert dest_path.endswith("qqq.jpg")


def test_download_asset_original_raises_immich_error_on_failed_response(tmp_path):
    response = FakeResponse(False, status_code=404, text="not found")
    session = FakeSession(response)
    client = ImmichClient("http://immich", "key", session=session)

    try:
        client.download_asset_original("missing", str(tmp_path))
        assert False, "expected ImmichError"
    except ImmichError as exc:
        assert "404" in str(exc)
