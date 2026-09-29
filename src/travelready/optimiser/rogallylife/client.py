"""client.py — read-only HTTP access to ROG Ally Life.

Retrieval order, most structured first:

1. **WordPress REST API** — ``/wp-json/wp/v2/posts``, ``/pages``,
   ``/categories``, ``/search``. Public and official, returns JSON with
   ``modified_gmt`` for change detection and ``X-WP-TotalPages`` for
   pagination. Preferred over scraping.
2. **Sitemap** — ``/wp-sitemap.xml`` / ``/sitemap.xml``, for URL discovery
   with ``lastmod``.
3. **Category archive HTML** — ``/category/rog-ally-game-settings/page/N/``.
4. **A single post's HTML**, when only one game is wanted.

Good-citizen behaviour, all of it mandatory rather than optional:

* ``robots.txt`` is fetched once and honoured — a disallowed path is not
  requested, and ``Crawl-delay`` raises the inter-request delay;
* a descriptive User-Agent identifies the tool and links to the project;
* requests are rate-limited and retried with backoff on 429/5xx, honouring
  ``Retry-After``;
* ``If-None-Match`` / ``If-Modified-Since`` are sent so a refresh of unchanged
  content costs a 304.

Nothing here authenticates, follows a paywall, solves a challenge or works
around an access control. If the network denies the host, that is reported as
a blocked fetch — it is never routed via a mirror or translation proxy.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import SOURCE_BASE
from .model import device_family_from_url

USER_AGENT = (
    "TravelReady/0.3 (+https://github.com/ShahzadaYT/GameReady-Windows; "
    "ROG Ally settings sync; contact via repository issues)"
)

DEFAULT_TIMEOUT = 20
DEFAULT_DELAY = 1.0            # seconds between requests
MAX_RETRIES = 3

#: Index pages, per device family.
INDEX_PATHS = {
    "rog_ally_family": ("rog-ally-game-settings/", "category/rog-ally-game-settings/"),
    "rog_xbox_ally_family": ("rog-xbox-ally-x-game-settings/",
                             "category/rog-xbox-ally-x-game-settings/"),
}

REST_ROOT = "wp-json/wp/v2/"
SITEMAP_PATHS = ("wp-sitemap.xml", "sitemap.xml", "sitemap_index.xml")


class FetchError(Exception):
    """A fetch failed. ``blocked`` distinguishes a network/policy denial."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 blocked: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.blocked = blocked


@dataclass
class Response:
    """One fetched resource."""

    url: str
    status: int
    body: str
    etag: str = ""
    last_modified: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def json(self):
        return json.loads(self.body) if self.body else None


class RobotsPolicy:
    """The parts of ``robots.txt`` that apply to us."""

    def __init__(self, text: str = "", *, fetched: bool = False) -> None:
        self.fetched = fetched
        self.crawl_delay: Optional[float] = None
        self._disallow: List[str] = []
        self._allow: List[str] = []
        self._parse(text)

    def _parse(self, text: str) -> None:
        applies = False
        for raw in str(text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            field_name, value = field_name.strip().lower(), value.strip()
            if field_name == "user-agent":
                applies = value in ("*", "travelready")
            elif not applies:
                continue
            elif field_name == "disallow" and value:
                self._disallow.append(value)
            elif field_name == "allow" and value:
                self._allow.append(value)
            elif field_name == "crawl-delay":
                try:
                    self.crawl_delay = float(value)
                except ValueError:
                    pass

    def allows(self, url: str) -> bool:
        """Is ``url`` permitted? Unknown robots means allowed, longest rule wins."""
        path = urllib.parse.urlsplit(str(url or "")).path or "/"
        best_allow = max((len(r) for r in self._allow if path.startswith(r)), default=-1)
        best_deny = max((len(r) for r in self._disallow if path.startswith(r)), default=-1)
        return best_allow >= best_deny


class RogAllyLifeClient:
    """Read-only client. Performs no writes of any kind."""

    def __init__(self, base_url: str = SOURCE_BASE, *, timeout: int = DEFAULT_TIMEOUT,
                 delay: float = DEFAULT_DELAY, opener: Optional[Callable] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 respect_robots: bool = True) -> None:
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.delay = delay
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._respect_robots = respect_robots
        self._robots: Optional[RobotsPolicy] = None
        self._last_request = 0.0
        self.requests_made = 0

    # -- plumbing ----------------------------------------------------------

    def _absolute(self, path: str) -> str:
        """Resolve ``path`` against the base URL, pinned to its host.

        URLs reaching this client come from the site's own sitemap and index
        pages, which are external input. Pinning the host means a stray or
        hostile link cannot turn a settings sync into a request to an internal
        address or a third party.
        """
        raw = str(path or "")
        url = raw if raw.startswith(("http://", "https://")) else \
            urllib.parse.urljoin(self.base_url, raw.lstrip("/"))
        host = urllib.parse.urlsplit(url).hostname or ""
        allowed = urllib.parse.urlsplit(self.base_url).hostname or ""
        if host.lower() not in (allowed.lower(), f"www.{allowed.lower()}"):
            raise FetchError(f"refusing to fetch {url}: host is not {allowed}")
        return url

    def _throttle(self) -> None:
        delay = self.delay
        if self._robots and self._robots.crawl_delay:
            delay = max(delay, self._robots.crawl_delay)
        elapsed = time.time() - self._last_request
        if self._last_request and elapsed < delay:
            self._sleep(delay - elapsed)
        self._last_request = time.time()

    def robots(self) -> RobotsPolicy:
        if self._robots is None:
            try:
                response = self._raw(self._absolute("robots.txt"))
                self._robots = RobotsPolicy(response.body, fetched=True)
            except FetchError:
                self._robots = RobotsPolicy("")
        return self._robots

    def _raw(self, url: str, *, etag: str = "", last_modified: str = "") -> Response:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
            "Accept-Encoding": "gzip",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                self.requests_made += 1
                with self._opener(request, timeout=self.timeout) as raw:
                    payload = raw.read()
                    if (raw.headers.get("Content-Encoding") or "").lower() == "gzip":
                        payload = gzip.decompress(payload)
                    charset = "utf-8"
                    content_type = raw.headers.get("Content-Type") or ""
                    if "charset=" in content_type:
                        charset = content_type.split("charset=", 1)[1].split(";")[0].strip()
                    return Response(
                        url=url, status=getattr(raw, "status", 200),
                        body=payload.decode(charset, errors="replace"),
                        etag=raw.headers.get("ETag") or "",
                        last_modified=raw.headers.get("Last-Modified") or "",
                        headers={k.lower(): v for k, v in raw.headers.items()},
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return Response(url=url, status=304, body="")
                last_error = exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                    wait = float(exc.headers.get("Retry-After") or 0) or (2 ** attempt)
                    self._sleep(min(wait, 30))
                    continue
                raise FetchError(f"{url}: HTTP {exc.code}", status=exc.code,
                                 blocked=exc.code in (401, 403)) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                reason = str(getattr(exc, "reason", exc))
                if attempt < MAX_RETRIES - 1:
                    self._sleep(2 ** attempt)
                    continue
                raise FetchError(f"{url}: {reason}", blocked=True) from exc
            except OSError as exc:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    self._sleep(2 ** attempt)
                    continue
                raise FetchError(f"{url}: {exc}", blocked=True) from exc
        raise FetchError(f"{url}: {last_error}", blocked=True)

    def get(self, path: str, *, etag: str = "", last_modified: str = "") -> Response:
        """Fetch a path, refusing anything ``robots.txt`` disallows."""
        url = self._absolute(path)
        if self._respect_robots and not self.robots().allows(url):
            raise FetchError(f"{url}: disallowed by robots.txt")
        return self._raw(url, etag=etag, last_modified=last_modified)

    # -- WordPress REST API ------------------------------------------------

    def rest(self, endpoint: str, **params) -> Response:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}, doseq=True)
        path = REST_ROOT + endpoint.lstrip("/") + (f"?{query}" if query else "")
        return self.get(path)

    def rest_available(self) -> bool:
        """Is the REST API reachable and returning JSON?"""
        try:
            response = self.rest("types")
            return response.status == 200 and response.body.lstrip().startswith("{")
        except (FetchError, ValueError):
            return False

    def list_posts(self, *, per_page: int = 50, max_pages: int = 40,
                   search: str = "", modified_after: str = "") -> List[dict]:
        """Every settings post via the REST API, following pagination.

        Posts are filtered to those whose URL identifies a device family, so
        news and guides are ignored.
        """
        out: List[dict] = []
        for page in range(1, max_pages + 1):
            response = self.rest("posts", per_page=per_page, page=page,
                                 search=search, modified_after=modified_after,
                                 orderby="modified", order="desc",
                                 _fields="id,slug,link,title,date_gmt,modified_gmt,"
                                         "content,excerpt,categories,featured_media")
            try:
                batch = response.json() or []
            except ValueError as exc:
                raise FetchError(f"REST API returned non-JSON: {exc}") from exc
            if not isinstance(batch, list) or not batch:
                break
            out.extend(item for item in batch
                       if device_family_from_url(
                           (item.get("link") or item.get("slug") or "")) is not None)
            total_pages = int(response.headers.get("x-wp-totalpages") or 0)
            if total_pages and page >= total_pages:
                break
            if len(batch) < per_page:
                break
        return out

    def search_posts(self, term: str, *, limit: int = 20) -> List[dict]:
        response = self.rest("search", search=term, per_page=limit, type="post")
        try:
            return response.json() or []
        except ValueError:
            return []

    # -- sitemap -----------------------------------------------------------

    def sitemap_urls(self) -> List[Tuple[str, str]]:
        """``(url, lastmod)`` for settings posts, from the sitemap."""
        for candidate in SITEMAP_PATHS:
            try:
                response = self.get(candidate)
            except FetchError:
                continue
            if response.status != 200 or "<" not in response.body:
                continue
            found = _parse_sitemap(response.body)
            nested = [u for u, _ in found if u.endswith(".xml")]
            if nested and not any(device_family_from_url(u) for u, _ in found):
                collected: List[Tuple[str, str]] = []
                for child in nested[:20]:
                    try:
                        child_response = self.get(child)
                    except FetchError:
                        continue
                    collected.extend(_parse_sitemap(child_response.body))
                found = collected
            posts = [(u, m) for u, m in found if device_family_from_url(u) is not None]
            if posts:
                return posts
        return []

    # -- HTML fallbacks ----------------------------------------------------

    def index_urls(self, device_family: str = "rog_ally_family",
                   *, max_pages: int = 30) -> List[Tuple[str, str]]:
        """``(title, url)`` from the index and category archive pages."""
        from .parser import parse_index

        out: List[Tuple[str, str]] = []
        seen = set()
        for path in INDEX_PATHS.get(device_family, ()):
            for page in range(1, max_pages + 1):
                target = path if page == 1 else f"{path.rstrip('/')}/page/{page}/"
                try:
                    response = self.get(target)
                except FetchError as exc:
                    # A 404 just means the archive ended. A blocked host means
                    # we learned nothing, and the caller must be able to tell
                    # those apart or a denied network looks like an empty site.
                    if exc.blocked:
                        raise
                    break
                found = [(t, u) for t, u in parse_index(response.body, base_url=self.base_url)
                         if device_family_from_url(u) == device_family]
                fresh = [(t, u) for t, u in found if u not in seen]
                if not fresh:
                    break
                for title, url in fresh:
                    seen.add(url)
                    out.append((title, url))
        return out

    def fetch_post(self, url: str, *, etag: str = "", last_modified: str = ""):
        """Fetch one post's HTML."""
        return self.get(url, etag=etag, last_modified=last_modified)


def _parse_sitemap(xml: str) -> List[Tuple[str, str]]:
    """``(loc, lastmod)`` pairs from a sitemap or sitemap index."""
    out: List[Tuple[str, str]] = []
    for block in re.findall(r"<(?:url|sitemap)\b[^>]*>(.*?)</(?:url|sitemap)>",
                            str(xml or ""), re.DOTALL | re.IGNORECASE):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.DOTALL | re.IGNORECASE)
        mod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, re.DOTALL | re.IGNORECASE)
        if loc:
            out.append((loc.group(1).strip(), mod.group(1).strip() if mod else ""))
    return out
