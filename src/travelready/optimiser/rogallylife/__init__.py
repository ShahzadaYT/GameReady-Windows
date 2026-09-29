"""ROG Ally Life — the external recommendation source for TravelReady.

ROG Ally Life (https://rogallylife.com/) publishes per-game, per-device
settings recommendations for the ROG Ally family. This package retrieves them,
represents them faithfully, matches them to the user's installed games, and
hands them to the rest of the optimiser as inert, attributed data.

Layout::

    model      SourceGame / SourceProfile — the site's data model, extensible
    matcher    library title -> source title, with a confidence and a reason
    parser     rendered post -> SourceGame; carries PARSER_VERSION
    client     read-only HTTP: WP REST API first, sitemap and HTML as fallback
    cache      on-disk cache keyed by content hash, with change detection
    capability what TravelReady can actually detect / apply / verify
    select     which profile suits the user's operating mode, transparently
    sync       update, diff and summarise

Nothing in this package invents a recommendation. If the site has no entry for
a game, the answer is "no profile found" — never an extrapolation from another
game, another device or a generic engine default.
"""

from .model import (  # noqa: F401
    DEVICE_FAMILIES, SourceGame, SourceProfile, SourceSetting,
)

PARSER_VERSION = 1
SOURCE_ID = "rogallylife"
SOURCE_NAME = "ROG Ally Life"
SOURCE_BASE = "https://rogallylife.com/"
