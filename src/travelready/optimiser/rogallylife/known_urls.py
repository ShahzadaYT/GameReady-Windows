"""known_urls.py — settings-post URLs observed on ROG Ally Life.

These are **real URLs** confirmed to exist on the site during development. They
are used only as a discovery hint: a starting set for
:func:`travelready.optimiser.rogallylife.sync.discover` when the REST API, the
sitemap and the index pages are all unreachable, and as a corpus for testing
the matcher against genuine titles.

They carry **no settings data**. A URL here still has to be fetched and parsed
before TravelReady knows anything about the game, and the title shown is
derived from the URL slug, not from the page. Seeding the cache from this list
without fetching would be inventing data, and :mod:`sync` never does it.

The list is inherently partial — it is what was observed, not the site's
catalogue. Treat any coverage figure computed from it as a lower bound.
"""

from __future__ import annotations

from typing import List, Tuple

from .model import clean_game_title, device_family_from_url, title_from_slug

#: Observed post URLs. Kept as plain strings so nothing here can imply a
#: recommendation exists until the page has actually been read.
OBSERVED_POST_URLS: Tuple[str, ...] = (
    # ROG Ally / ROG Ally X
    "https://rogallylife.com/2025/04/24/clair-obscur-expedition-33-rog-ally/",
    "https://rogallylife.com/2026/02/27/resident-evil-requiem-rog-ally-game/",
    "https://rogallylife.com/2026/05/15/forza-horizon-6-rog-ally-game-settings/",
    "https://rogallylife.com/2023/06/22/forza-horizon-5-rog-ally-game-settings/",
    "https://rogallylife.com/2023/10/10/forza-motorsport-rog-ally-game-settings/",
    "https://rogallylife.com/2024/01/08/a-plague-tale-requiem-rog-ally/",
    "https://rogallylife.com/2026/08/26/resonance-a-plague-tale-legacy-rog-ally/",
    "https://rogallylife.com/2026/09/02/the-blood-of-dawnwalker-rog-ally-game-settings/",
    "https://rogallylife.com/2026/09/22/silent-hill-townfall-rog-ally-game-settings/",
    "https://rogallylife.com/2026/08/27/star-wars-zero-company-rog-ally-game-settings/",
    "https://rogallylife.com/2026/05/11/directive-8020-rog-ally-game-settings/",
    "https://rogallylife.com/2026/08/31/onimusha-way-of-the-sword-rog-ally/",
    "https://rogallylife.com/2026/09/24/control-resonant-rog-ally-game-settings/",
    "https://rogallylife.com/2026/09/28/mxgp-26-the-official-game-rog-ally-game-settings/",
    "https://rogallylife.com/2026/04/14/replaced-rog-ally-game-settings/",
    "https://rogallylife.com/2026/03/13/everwind-rog-ally-game-settings/",
    # ROG Xbox Ally / X / X20
    "https://rogallylife.com/2025/10/18/clair-obscur-expedition-33-xbox-ally-x/",
    "https://rogallylife.com/2026/02/27/resident-evil-requiem-rog-xbox-ally-x/",
    "https://rogallylife.com/2026/01/12/resident-evil-4-rog-xbox-ally-x/",
    "https://rogallylife.com/2026/05/15/forza-horizon-6-rog-xbox-ally-x-game-settings/",
    "https://rogallylife.com/2025/10/17/forza-horizon-5-rog-xbox-ally-x/",
    "https://rogallylife.com/2026/08/26/resonance-a-plague-tale-legacy-xbox-all/",
    "https://rogallylife.com/2026/09/17/runescape-dragonwilds-rog-xbox-ally-x-game-settings/",
    "https://rogallylife.com/2026/09/02/the-blood-of-dawnwalker-rog-xbox-ally-x-game-settings/",
)


def seed_urls(device_family: str = "") -> List[str]:
    """Observed URLs, optionally limited to one device family."""
    if not device_family:
        return list(OBSERVED_POST_URLS)
    return [u for u in OBSERVED_POST_URLS if device_family_from_url(u) == device_family]


def seed_titles(device_family: str = "") -> List[Tuple[str, str]]:
    """``(title, url)`` derived from the slug. Titles only — never settings."""
    return [(clean_game_title(title_from_slug(u).title()), u)
            for u in seed_urls(device_family)]
