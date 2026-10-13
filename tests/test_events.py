import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.library import LibraryStore, Photo
from app.webui import events
from app.webui.server import create_app


def test_the_token_changes_whenever_the_file_is_written(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    assert events.token(store.path) == "none"
    store.update(lambda lib: lib.photos.update(a=Photo(id="a", home="inbox")))
    first = events.token(store.path)
    assert first != "none" and events.token(store.path) == first
    store.update(lambda lib: lib.photos.update(b=Photo(id="b", home="inbox")))
    assert events.token(store.path) != first


def test_the_stream_sends_the_state_then_each_change_then_a_keep_alive(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    store.update(lambda lib: lib.photos.update(a=Photo(id="a", home="inbox")))
    now = [0.0]
    steps = []

    def sleep(seconds):
        now[0] += seconds
        steps.append(now[0])
        if len(steps) == 2:                         # a writer changes the library meanwhile
            store.update(lambda lib: lib.photos.update(b=Photo(id="b", home="inbox")))

    ticks = iter(range(7))
    out = list(events.stream(store.path, poll=1, heartbeat=3, sleep=sleep, clock=lambda: now[0],
                             alive=lambda: next(ticks, None) is not None))
    assert out[0].startswith("retry: 3000\ndata: ") and out[0].endswith("\n\n")
    assert out[1].startswith("data: ") and out[1] != out[0].split("retry: 3000\n")[1]
    assert ": keep-alive\n\n" in out
    assert all(chunk.endswith("\n\n") for chunk in out)


def test_the_route_streams_events_with_headers_that_stop_proxies_buffering(tmp_path, monkeypatch):
    store = LibraryStore(str(tmp_path / "library.json"))
    monkeypatch.setattr(events, "stream", lambda path: iter(["data: x\n\n"]))
    resp = create_app(None, None, store, None).test_client().get("/api/events")
    assert resp.mimetype == "text/event-stream"
    assert resp.headers["X-Accel-Buffering"] == "no" and resp.headers["Cache-Control"] == "no-cache"
    assert resp.data == b"data: x\n\n"


def test_the_page_updates_pieces_instead_of_reloading():
    page = open(os.path.join(os.path.dirname(__file__), "..", "app", "webui", "templates", "index.html"), encoding="utf-8").read()
    assert 'new EventSource("/api/events")' in page and "syncCounts" in page and "syncGrid" in page
    assert "location.reload" not in page
