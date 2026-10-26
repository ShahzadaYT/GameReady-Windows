"""Sync must be cancellable, bounded, and cheap the second time.

The reported symptom was a GUI stuck on "Updating…" with a Cancel button that
did nothing. The cause was not a timeout that was too short: ``sync()`` took no
stop event, had no overall deadline, and never sent the conditional-request
headers the client already supported, so every run re-fetched and re-parsed
every post at one request per second.
"""
from __future__ import annotations

import io
import threading
import urllib.error

import pytest

from conftest import FIXTURES
from travelready.optimiser.rogallylife.budget import Budget, Cancelled
from travelready.optimiser.rogallylife.cache import ProfileCache
from travelready.optimiser.rogallylife.client import FetchError, RogAllyLifeClient
from travelready.optimiser.rogallylife.parser import parse_post
from travelready.optimiser.rogallylife.sync import DEFAULT_BUDGET_SECONDS, sync

RAL = FIXTURES / "rogallylife"
POST = "https://rogallylife.com/2026/02/27/example-adventure-rog-ally-game-settings/"


def post_html() -> str:
    return (RAL / "two_profiles_table.html").read_text()


def sitemap(urls) -> str:
    items = "".join(
        f"<url><loc>{u}</loc><lastmod>2026-02-27</lastmod></url>" for u in urls)
    return f"<urlset>{items}</urlset>"


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body.encode("utf-8"))
        self.status = status
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.headers.update(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHTTP:
    """Serves canned responses and records every request made."""

    def __init__(self, routes, *, headers=None, on_request=None):
        self.routes = routes
        self.headers = headers or {}
        self.requests = []
        self.on_request = on_request

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append((url, dict(request.headers), timeout))
        if self.on_request:
            self.on_request(url, dict(request.headers))
        # Honour conditional requests the way a real server does.
        sent = {k.lower(): v for k, v in request.headers.items()}
        if self.headers.get("ETag") and sent.get("if-none-match") == self.headers["ETag"]:
            raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)
        for prefix, body in self.routes.items():
            if prefix in url:
                return FakeResponse(body, headers=self.headers)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def client(routes, **kw):
    return RogAllyLifeClient(opener=FakeHTTP(routes, **kw), delay=0,
                             sleep=lambda s: None)


def client_with(http):
    return RogAllyLifeClient(opener=http, delay=0, sleep=lambda s: None)


ROUTES = {
    "robots.txt": "",
    "wp-json": "",                       # REST unavailable -> falls to sitemap
    "sitemap": sitemap([POST]),
    POST: post_html(),
}


# -- cancellation -----------------------------------------------------------

def test_sync_accepts_a_stop_event():
    """Regression: the GUI's Cancel button had nothing to set."""
    import inspect
    assert "stop_event" in inspect.signature(sync).parameters


def test_a_cancelled_sync_stops_and_says_so(tmp_path):
    stop = threading.Event()
    stop.set()                                   # cancelled before it starts
    cache = ProfileCache(tmp_path / "cache")
    report = sync(client(ROUTES), cache, stop_event=stop)
    assert report.cancelled
    assert not report.ok
    assert "Cancelled" in report.describe()


def test_cancelling_midway_keeps_what_was_already_fetched(tmp_path):
    urls = [POST.replace("example-adventure", f"game-{n}") for n in range(5)]
    routes = dict(ROUTES)
    routes["sitemap"] = sitemap(urls)
    for url in urls:
        routes[url] = post_html()

    stop = threading.Event()
    fetched = []

    def watch(url, headers):
        if "game-" in url:
            fetched.append(url)
            if len(fetched) == 2:
                stop.set()                       # user presses Cancel

    cache = ProfileCache(tmp_path / "cache")
    report = sync(client_with(FakeHTTP(routes, on_request=watch)), cache,
                  stop_event=stop)

    assert report.cancelled
    assert report.skipped > 0, "the report must say the run was partial"
    assert cache.index.entries, "work already done is kept, not discarded"
    # A partial run has not looked at the rest of the source, so it may not
    # claim anything disappeared from it.
    assert report.missing == []


def test_a_cancelled_sync_never_reports_an_empty_source(tmp_path):
    """'We stopped early' must never read as 'there is nothing there'."""
    stop = threading.Event()
    stop.set()
    cache = ProfileCache(tmp_path / "cache")
    report = sync(client(ROUTES), cache, stop_event=stop)
    assert report.cancelled and not report.blocked
    assert "no recommendations" not in report.describe().lower()


# -- the deadline -----------------------------------------------------------

def test_sync_has_a_bounded_default_deadline():
    import inspect
    default = inspect.signature(sync).parameters["budget_seconds"].default
    assert default == DEFAULT_BUDGET_SECONDS
    assert 0 < DEFAULT_BUDGET_SECONDS <= 600


def test_an_overrunning_sync_stops_at_its_deadline(tmp_path):
    """A slow source ends the run tidily instead of spinning forever."""
    now = [0.0]
    urls = [POST.replace("example-adventure", f"game-{n}") for n in range(20)]
    routes = dict(ROUTES)
    routes["sitemap"] = sitemap(urls)
    for url in urls:
        routes[url] = post_html()

    def slow(url, headers):
        now[0] += 3.0                            # every request takes 3s

    cache = ProfileCache(tmp_path / "cache")
    report = sync(client_with(FakeHTTP(routes, on_request=slow)), cache,
                  budget_seconds=10.0, clock=lambda: now[0])

    assert report.timed_out and not report.complete
    assert not report.cancelled, "a deadline is not a user cancellation"
    assert report.skipped > 0
    assert "Stopped after" in report.describe()
    assert report.duration >= 10.0


def test_the_deadline_can_be_lifted_for_the_command_line(tmp_path):
    cache = ProfileCache(tmp_path / "cache")
    report = sync(client(ROUTES), cache, budget_seconds=None)
    assert report.complete and report.ok


# -- incremental refresh ----------------------------------------------------

def test_the_second_sync_sends_conditional_request_headers(tmp_path):
    """Regression: validators were stored but never sent, so nothing was cheap."""
    cache_dir = tmp_path / "cache"
    routes = dict(ROUTES)
    # Without a lastmod the listing cannot settle it, so the request itself
    # must carry the validator.
    routes["sitemap"] = f"<urlset><url><loc>{POST}</loc></url></urlset>"

    http = FakeHTTP(routes, headers={"ETag": '"abc123"'})
    first = sync(client_with(http), ProfileCache(cache_dir))
    assert first.fetched >= 1

    http2 = FakeHTTP(routes, headers={"ETag": '"abc123"'})
    sync(client_with(http2), ProfileCache(cache_dir))
    sent = [h for url, h, _ in http2.requests if POST in url]
    assert sent, "the post must have been requested"
    assert any("abc123" in str(h) for h in sent), "If-None-Match must be sent"


def test_forcing_a_refresh_deliberately_ignores_the_validators(tmp_path):
    """--force means re-read the source, so a 304 would defeat the point."""
    cache_dir = tmp_path / "cache"
    routes = dict(ROUTES)
    routes["sitemap"] = f"<urlset><url><loc>{POST}</loc></url></urlset>"
    sync(client_with(FakeHTTP(routes, headers={"ETag": '"abc123"'})),
         ProfileCache(cache_dir))

    http = FakeHTTP(routes, headers={"ETag": '"abc123"'})
    forced = sync(client_with(http), ProfileCache(cache_dir), force=True)
    sent = [h for url, h, _ in http.requests if POST in url]
    assert not any("abc123" in str(h) for h in sent)
    assert forced.fetched >= 1 and forced.not_modified == 0


def test_an_unchanged_post_costs_a_304_not_a_reparse(tmp_path):
    cache_dir = tmp_path / "cache"
    routes = dict(ROUTES)
    # No lastmod, so the cheap listing check cannot short-circuit; the only
    # way to avoid a re-parse is a conditional request.
    routes["sitemap"] = f"<urlset><url><loc>{POST}</loc></url></urlset>"

    http = FakeHTTP(routes, headers={"ETag": '"v1"'})
    sync(client_with(http), ProfileCache(cache_dir))

    http2 = FakeHTTP(routes, headers={"ETag": '"v1"'})
    second = sync(client_with(http2), ProfileCache(cache_dir))
    assert second.not_modified >= 1
    assert second.fetched == 0, "a 304 must not count as a fetch"
    assert second.ok


def test_a_new_parser_version_ignores_the_cached_validators(tmp_path):
    """Bytes unchanged but our parser improved: we must re-parse, not 304."""
    cache = ProfileCache(tmp_path / "cache")
    game = parse_post(post_html(), url=POST, title="Example", last_updated="2026-02-27")
    cache.put(game, etag='"v1"', last_modified="Thu, 27 Feb 2026 00:00:00 GMT")
    cache.save_index()
    etag, modified = cache.validators_for(game_key(cache), parser_version=999)
    assert (etag, modified) == ("", ""), "an outdated parse must not be revalidated"


def game_key(cache: ProfileCache) -> str:
    return next(iter(cache.index.entries))


def test_validators_survive_a_response_that_omits_them(tmp_path):
    cache = ProfileCache(tmp_path / "cache")
    game = parse_post(post_html(), url=POST, title="Example")
    cache.put(game, etag='"v1"', last_modified="")
    cache.put(game)                               # a later response with no ETag
    etag, _ = cache.validators_for(game_key(cache))
    assert etag == '"v1"', "a server that sometimes omits an ETag must not lose it"


# -- the report -------------------------------------------------------------

def test_the_report_counts_and_times_the_run(tmp_path):
    cache = ProfileCache(tmp_path / "cache")
    report = sync(client(ROUTES), cache)
    text = report.describe()
    assert "Posts discovered:" in text
    assert "Duration:" in text
    assert "Not modified:" in text
    assert report.duration >= 0


def test_blocked_is_distinct_from_cancelled_and_from_empty(tmp_path):
    class Forbidden:
        def __call__(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    cache = ProfileCache(tmp_path / "cache")
    blocked = sync(RogAllyLifeClient(opener=Forbidden(), delay=0,
                                     sleep=lambda s: None, respect_robots=False),
                   cache)
    assert blocked.blocked and not blocked.cancelled
    assert "could not be reached" in blocked.describe()


# -- the budget itself ------------------------------------------------------

def test_budget_check_raises_once_cancelled():
    stop = threading.Event()
    budget = Budget(seconds=None, stop_event=stop)
    budget.check()
    stop.set()
    with pytest.raises(Cancelled):
        budget.check()


def test_budget_sleep_gives_up_when_cancelled():
    stop = threading.Event()
    now = [0.0]
    budget = Budget(seconds=None, stop_event=stop, clock=lambda: now[0])
    slept = []

    def sleeper(seconds):
        slept.append(seconds)
        stop.set()                                # cancelled during the wait

    with pytest.raises(Cancelled):
        budget.sleep(30.0, sleeper)
    assert sum(slept) < 1.0, "a 30s wait must not run to completion after Cancel"


def test_budget_sleep_never_outlasts_the_deadline():
    now = [0.0]
    budget = Budget(seconds=1.0, clock=lambda: now[0])
    slept = []
    with pytest.raises(Cancelled):
        budget.sleep(30.0, lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    assert sum(slept) <= 1.5, "backoff must be clipped to the remaining budget"


def test_a_request_timeout_is_clipped_to_the_remaining_budget():
    now = [0.0]
    http = FakeHTTP(ROUTES)
    c = RogAllyLifeClient(opener=http, delay=0, sleep=lambda s: None, timeout=30)
    budget = Budget(seconds=5.0, clock=lambda: now[0])
    c.get("robots.txt", budget=budget)
    timeouts = [t for _, _, t in http.requests]
    assert timeouts and max(timeouts) <= 5.0
