"""Cache, client and sync tests — with the network stubbed out."""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from conftest import FIXTURES
from travelready.optimiser.rogallylife.cache import ProfileCache, entry_key
from travelready.optimiser.rogallylife.client import (
    FetchError, RobotsPolicy, RogAllyLifeClient, _parse_sitemap,
)
from travelready.optimiser.rogallylife.model import (
    SourceGame, SourceProfile, SourceSetting,
)
from travelready.optimiser.rogallylife.parser import PARSER_VERSION, parse_post
from travelready.optimiser.rogallylife import sync as ral_sync

RAL = FIXTURES / "rogallylife"
ALLY_URL = "https://rogallylife.com/2026/02/27/example-adventure-rog-ally-game-settings/"
RACER_URL = "https://rogallylife.com/2026/03/01/example-racer-rog-ally-game-settings/"


def _game(title="Example Adventure", url=ALLY_URL, content_hash="h1",
          last_updated="2026-02-27", parser_version=PARSER_VERSION):
    from travelready.optimiser.rogallylife.model import device_family_from_url

    return SourceGame(
        title=title, source_url=url,
        device_family=device_family_from_url(url) or "rog_ally_family",
        slug=url.rstrip("/").rsplit("/", 1)[-1],
        content_hash=content_hash, last_updated=last_updated,
        parser_version=parser_version,
        profiles=[SourceProfile(name="900P 15/18W", settings=[
            SourceSetting("Texture Quality", "Medium", "texture_quality")])],
    )


# -- cache basics ------------------------------------------------------------

def test_initial_import_round_trips(tmp_path):
    cache = ProfileCache(tmp_path)
    key = cache.put(_game())
    cache.save_index()
    reloaded = ProfileCache(tmp_path).get(key)
    assert reloaded.title == "Example Adventure"
    assert reloaded.profiles[0].settings[0].canonical == "texture_quality"


def test_unchanged_source_is_not_stale(tmp_path):
    cache = ProfileCache(tmp_path)
    key = cache.put(_game())
    assert not cache.is_stale(key, parser_version=PARSER_VERSION,
                              content_hash="h1", last_updated="2026-02-27")


def test_changed_content_hash_is_stale(tmp_path):
    cache = ProfileCache(tmp_path)
    key = cache.put(_game())
    assert cache.is_stale(key, parser_version=PARSER_VERSION, content_hash="h2")


def test_changed_last_updated_is_stale(tmp_path):
    cache = ProfileCache(tmp_path)
    key = cache.put(_game())
    assert cache.is_stale(key, parser_version=PARSER_VERSION, last_updated="2026-03-01")


def test_missing_entry_is_stale(tmp_path):
    assert ProfileCache(tmp_path).is_stale("nothing", parser_version=PARSER_VERSION)


def test_parser_version_migration_invalidates_old_entries(tmp_path):
    cache = ProfileCache(tmp_path)
    key = cache.put(_game(parser_version=PARSER_VERSION))
    assert cache.needs_reparse(PARSER_VERSION) == []
    assert cache.needs_reparse(PARSER_VERSION + 1) == [key]
    assert cache.is_stale(key, parser_version=PARSER_VERSION + 1)


def test_deleting_an_entry_removes_file_and_index_row(tmp_path):
    cache = ProfileCache(tmp_path)
    key = cache.put(_game())
    cache.save_index()
    assert cache.remove(key)
    assert not cache.has(key)
    assert key not in cache.index.entries
    assert not cache.remove(key)


def test_corrupt_index_is_moved_aside_and_rebuilt(tmp_path):
    cache = ProfileCache(tmp_path)
    cache.put(_game())
    cache.save_index()
    (tmp_path / "index.json").write_text("{not json")
    rebuilt = ProfileCache(tmp_path)
    assert (tmp_path / "index.corrupt").exists()
    assert len(rebuilt.all_games()) == 1, "game files survive a corrupt index"


def test_corrupt_game_file_is_skipped_not_fatal(tmp_path):
    cache = ProfileCache(tmp_path)
    cache.put(_game())
    (tmp_path / "games" / "broken.json").write_text("{nope")
    assert len(ProfileCache(tmp_path).all_games()) == 1


def test_device_filtering(tmp_path):
    cache = ProfileCache(tmp_path)
    cache.put(_game())
    cache.put(_game(title="Xbox Only",
                    url="https://rogallylife.com/2026/01/01/x-rog-xbox-ally-x/"))
    assert [g.title for g in cache.games_for_device("rog_ally_x")] == ["Example Adventure"]
    assert [g.title for g in cache.games_for_device("rog_xbox_ally_x")] == ["Xbox Only"]


# -- robots and HTTP ---------------------------------------------------------

def test_robots_disallow_is_honoured():
    policy = RobotsPolicy("User-agent: *\nDisallow: /wp-admin/\n")
    assert not policy.allows("https://rogallylife.com/wp-admin/x")
    assert policy.allows("https://rogallylife.com/2026/01/01/a-rog-ally/")


def test_robots_allow_overrides_a_longer_disallow():
    policy = RobotsPolicy("User-agent: *\nDisallow: /wp-admin/\n"
                          "Allow: /wp-admin/admin-ajax.php\n")
    assert policy.allows("https://rogallylife.com/wp-admin/admin-ajax.php")


def test_robots_crawl_delay_is_read():
    assert RobotsPolicy("User-agent: *\nCrawl-delay: 5").crawl_delay == 5.0


def test_rules_for_other_agents_are_ignored():
    policy = RobotsPolicy("User-agent: BadBot\nDisallow: /\n")
    assert policy.allows("https://rogallylife.com/anything/")


def test_sitemap_parsing():
    entries = _parse_sitemap(
        "<urlset><url><loc>https://rogallylife.com/2026/01/01/a-rog-ally/</loc>"
        "<lastmod>2026-01-01</lastmod></url>"
        "<url><loc>https://rogallylife.com/about/</loc></url></urlset>")
    assert entries[0] == ("https://rogallylife.com/2026/01/01/a-rog-ally/", "2026-01-01")
    assert entries[1][1] == ""


class FakeHTTP:
    """A stubbed urlopen. Records requests; serves canned responses."""

    def __init__(self, routes, *, status=200):
        self.routes = routes
        self.requests = []
        self.status = status

    def __call__(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.requests.append((url, dict(request.headers)))
        for prefix, body in self.routes.items():
            if prefix in url:
                return _FakeResponse(body, self.status)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class _FakeResponse(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body.encode("utf-8"))
        self.status = status
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client(routes, **kwargs):
    return RogAllyLifeClient(opener=FakeHTTP(routes), delay=0,
                             sleep=lambda s: None, **kwargs)


def test_client_sends_a_descriptive_user_agent():
    http = FakeHTTP({"robots.txt": "", "index": "<html></html>"})
    client = RogAllyLifeClient(opener=http, delay=0, sleep=lambda s: None)
    client.get("index")
    agents = [headers.get("User-agent") or headers.get("User-Agent")
              for _, headers in http.requests]
    assert any(a and "TravelReady" in a for a in agents)


def test_client_refuses_a_path_robots_disallows():
    client = _client({"robots.txt": "User-agent: *\nDisallow: /secret/"})
    with pytest.raises(FetchError, match="robots.txt"):
        client.get("/secret/thing")


def test_client_sends_conditional_headers():
    http = FakeHTTP({"robots.txt": "", "page": "<html></html>"})
    client = RogAllyLifeClient(opener=http, delay=0, sleep=lambda s: None)
    client.get("page", etag='"abc"', last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    headers = http.requests[-1][1]
    normalised = {k.lower(): v for k, v in headers.items()}
    assert normalised.get("if-none-match") == '"abc"'
    assert "if-modified-since" in normalised


def test_304_is_reported_as_not_modified():
    class NotModified(FakeHTTP):
        def __call__(self, request, timeout=None):
            url = request.full_url
            if "robots" in url:
                return _FakeResponse("")
            raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)

    client = RogAllyLifeClient(opener=NotModified({}), delay=0, sleep=lambda s: None)
    assert client.get("page").not_modified


def test_403_is_reported_as_blocked():
    class Forbidden(FakeHTTP):
        def __call__(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    client = RogAllyLifeClient(opener=Forbidden({}), delay=0, sleep=lambda s: None,
                               respect_robots=False)
    with pytest.raises(FetchError) as excinfo:
        client.get("anything")
    assert excinfo.value.blocked


def test_server_errors_are_retried_then_raised():
    attempts = []

    class Flaky(FakeHTTP):
        def __call__(self, request, timeout=None):
            attempts.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 503, "Busy",
                                         {"Retry-After": "0"}, None)

    client = RogAllyLifeClient(opener=Flaky({}), delay=0, sleep=lambda s: None,
                               respect_robots=False)
    with pytest.raises(FetchError):
        client.get("page")
    assert len(attempts) > 1, "a 503 should be retried"


# -- sync --------------------------------------------------------------------

def _sync_routes():
    return {
        "robots.txt": "User-agent: *\nDisallow: /wp-admin/\n",
        "wp-json/wp/v2/types": "{}",
        "wp-json/wp/v2/posts": json.dumps([
            {"link": ALLY_URL, "slug": "example-adventure-rog-ally-game-settings",
             "title": {"rendered": "Example Adventure ROG Ally Game Settings"},
             "modified_gmt": "2026-02-27T00:00:00",
             "content": {"rendered": (RAL / "two_profiles_table.html").read_text()}},
            {"link": RACER_URL, "slug": "example-racer-rog-ally-game-settings",
             "title": {"rendered": "Example Racer ROG Ally Game Settings"},
             "modified_gmt": "2026-03-01T00:00:00",
             "content": {"rendered": (RAL / "single_profile_list.html").read_text()}},
        ]),
    }


def test_sync_imports_via_the_rest_api(tmp_path):
    cache = ProfileCache(tmp_path)
    report = ral_sync.sync(_client(_sync_routes()), cache)
    assert report.route == "wp-json REST API"
    assert len(report.new) == 2
    assert report.ok
    titles = {g.title for g in cache.all_games()}
    assert titles == {"Example Adventure", "Example Racer"}


def test_second_sync_reports_everything_unchanged(tmp_path):
    cache = ProfileCache(tmp_path)
    ral_sync.sync(_client(_sync_routes()), cache)
    report = ral_sync.sync(_client(_sync_routes()), ProfileCache(tmp_path))
    assert report.new == []
    assert len(report.unchanged) == 2
    assert report.fetched == 0, "unchanged posts should not be re-fetched"


def test_changed_source_is_detected_and_updated(tmp_path):
    cache = ProfileCache(tmp_path)
    ral_sync.sync(_client(_sync_routes()), cache)
    routes = _sync_routes()
    payload = json.loads(routes["wp-json/wp/v2/posts"])
    payload[0]["content"]["rendered"] = \
        payload[0]["content"]["rendered"].replace("<td>Medium</td>", "<td>High</td>")
    payload[0]["modified_gmt"] = "2026-04-01T00:00:00"
    routes["wp-json/wp/v2/posts"] = json.dumps(payload)

    report = ral_sync.sync(_client(routes), ProfileCache(tmp_path))
    assert report.updated == ["Example Adventure"]
    refreshed = ProfileCache(tmp_path).get("example-adventure-rog-ally-game-settings")
    assert refreshed.profiles[0].setting("texture_quality").value == "High"


def test_a_post_that_disappears_is_reported_not_deleted(tmp_path):
    cache = ProfileCache(tmp_path)
    ral_sync.sync(_client(_sync_routes()), cache)
    routes = _sync_routes()
    payload = json.loads(routes["wp-json/wp/v2/posts"])[:1]
    routes["wp-json/wp/v2/posts"] = json.dumps(payload)

    report = ral_sync.sync(_client(routes), ProfileCache(tmp_path))
    assert report.missing == ["Example Racer"]
    assert ProfileCache(tmp_path).get("example-racer-rog-ally-game-settings") is not None


def test_pruning_is_explicit(tmp_path):
    cache = ProfileCache(tmp_path)
    ral_sync.sync(_client(_sync_routes()), cache)
    removed = ral_sync.prune(cache, ["example-racer-rog-ally-game-settings"])
    assert removed == ["example-racer-rog-ally-game-settings"]
    assert ProfileCache(tmp_path).get("example-racer-rog-ally-game-settings") is None


def test_sync_falls_back_to_the_sitemap(tmp_path):
    routes = {
        "robots.txt": "",
        "wp-sitemap.xml": (f"<urlset><url><loc>{ALLY_URL}</loc>"
                           f"<lastmod>2026-02-27</lastmod></url></urlset>"),
        ALLY_URL: (RAL / "two_profiles_table.html").read_text(),
    }
    report = ral_sync.sync(_client(routes), ProfileCache(tmp_path))
    assert report.route == "sitemap.xml"
    assert report.new == ["Example Adventure"]


def test_sync_falls_back_to_index_pages(tmp_path):
    routes = {
        "robots.txt": "",
        "rog-ally-game-settings/": (RAL / "index_page.html").read_text(),
        ALLY_URL: (RAL / "two_profiles_table.html").read_text(),
        RACER_URL: (RAL / "single_profile_list.html").read_text(),
    }
    report = ral_sync.sync(_client(routes), ProfileCache(tmp_path), limit=2)
    assert report.route == "index pages"
    assert report.new


def test_a_blocked_source_leaves_the_cache_intact(tmp_path):
    cache = ProfileCache(tmp_path)
    ral_sync.sync(_client(_sync_routes()), cache)

    class Blocked(FakeHTTP):
        def __call__(self, request, timeout=None):
            raise urllib.error.URLError("CONNECT tunnel failed, response 403")

    client = RogAllyLifeClient(opener=Blocked({}), delay=0, sleep=lambda s: None)
    report = ral_sync.sync(client, ProfileCache(tmp_path))
    assert report.blocked
    assert not report.ok
    assert "could not be reached" in report.describe()
    assert len(ProfileCache(tmp_path).all_games()) == 2, "cache must survive"


def test_a_page_with_no_settings_is_recorded_as_such(tmp_path):
    routes = {
        "robots.txt": "",
        "wp-json/wp/v2/types": "{}",
        "wp-json/wp/v2/posts": json.dumps([{
            "link": ALLY_URL, "slug": "example-prose-rog-ally-game-settings",
            "title": {"rendered": "Example Prose ROG Ally Game Settings"},
            "modified_gmt": "2026-02-27T00:00:00",
            "content": {"rendered": (RAL / "no_profiles.html").read_text()}}]),
    }
    report = ral_sync.sync(_client(routes), ProfileCache(tmp_path))
    assert report.unparsed == ["Example Prose"]


def test_force_refetches_everything(tmp_path):
    cache = ProfileCache(tmp_path)
    ral_sync.sync(_client(_sync_routes()), cache)
    report = ral_sync.sync(_client(_sync_routes()), ProfileCache(tmp_path), force=True)
    assert len(report.updated) == 2
