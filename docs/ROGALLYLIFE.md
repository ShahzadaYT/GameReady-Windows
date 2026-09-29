# ROG Ally Life integration

## 1. Access from this environment

`rogallylife.com` is denied by this environment's egress proxy
(`CONNECT tunnel failed, response 403` — organization network policy). Every
route was tried:

| Route | Result |
|---|---|
| `https://rogallylife.com/` | `403` at the proxy |
| `https://rogallylife.com/wp-json/wp/v2/posts` | `403` at the proxy |
| `https://rogallylife.com/sitemap.xml` | `403` at the proxy |
| `https://rogallylife.com/feed/` | `403` at the proxy |
| `WebFetch` tool | `EGRESS_BLOCKED` |
| `WebSearch` tool | **works** — returns real URLs, titles and page summaries |

Two routes that would have worked were **deliberately not used**: the
`translate.google.com` and `rogallylife-com.translate.goog` mirrors that appear
in search results. Using a translation proxy to reach a host the operator's
policy denies is circumventing an access control, which the brief rules out.

So the structural research below comes from `WebSearch` (real result URLs,
titles and extracted summaries), and the HTTP client is written against the
real endpoint shapes and exercised on Windows, where the network is available.
**No profile content has been invented to fill the gap.** The bundled fixtures
are explicitly synthetic and are labelled as such in-file; they exercise the
parser, not the recommendations.

To unblock live sync from a cloud session, add `rogallylife.com` to the allowed
domains under the environment's Network access settings (cloud environment menu
in the session title bar → Edit).

## 2. What the site actually is

WordPress. Evidence: date-based permalinks (`/2026/02/27/<slug>/`), a
`/category/<slug>/` archive, and a custom post type at `/iscu-games/<slug>/`.

### Page families

| Family | URL shape | Holds |
|---|---|---|
| Editorial, ROG Ally / Ally X | `/YYYY/MM/DD/<game>-rog-ally-game-settings/` | the primary recommendations |
| Editorial, ROG Xbox Ally X / X20 | `/YYYY/MM/DD/<game>-rog-xbox-ally-x-game-settings/` | a **separate post** for that device |
| Index, ROG Ally | `/rog-ally-game-settings/` | the game index for Ally / Ally X |
| Index, ROG Xbox Ally X | `/rog-xbox-ally-x-game-settings/` | the game index for the Xbox Ally family |
| Category archive | `/category/rog-ally-game-settings/` | paginated post listing |
| Community | `/community-game-settings/`, `/iscu-games/<game>/` | user-submitted settings |

Observed slug suffixes vary and must all be recognised: `-rog-ally-game-settings`,
`-rog-ally-game`, `-rog-ally`, `-rog-xbox-ally-x-game-settings`,
`-rog-xbox-ally-x`, `-xbox-ally-x`, `-xbox-all` (truncated slugs occur).

**Device is encoded in the URL, not inside the page.** The same game has two
posts:

```
/2025/04/24/clair-obscur-expedition-33-rog-ally/        → ROG Ally, ROG Ally X
/2025/10/18/clair-obscur-expedition-33-xbox-ally-x/     → ROG Xbox Ally X
```

This matters: TravelReady targets the **ROG Ally X**, so it must prefer the
`-rog-ally-` family and never silently apply an Xbox Ally X post.

Per the site, all editorial settings are tested on the original ROG Ally
(Z1 Extreme) **and** the ROG Ally X, so one post covers both.

### Multiple performance profiles per game

A game does not have one profile. Observed, verbatim from the site:

* *Resident Evil Requiem* — "900P and 1080P resolution profiles and 15/18W and
  18/25/30W power modes for handheld play"
* *Forza Horizon 6* — "1080P and 900P resolution profiles and 18/25/30W and
  18/25W power modes"
* *Clair Obscur: Expedition 33* — "900P and 1080P resolution profiles and
  25/30W power modes"; "Increase the VRAM to 6GB or above"

So a profile is `(resolution, TDP set, VRAM)` plus its own graphics table, and
the graphics table differs between profiles. A profile label reads like
`15/18W • 900p • 8GB VRAM`.

### Performance rating

A star rating, defined on the site: 5 excellent, 4.5 great, 4 good, 3.5
average, 3 playable, ≤2.5 unplayable.

### Setting vocabulary observed

FSR (Quality / Balanced / Performance / Ultra Performance), FSR 4 / FSR 4.02c,
Frame Generation, Anti-Aliasing, Shadows, Hair Strands, Screen Space
Reflections, Resolution, VRAM, TDP, Armoury Crate Operating Mode
(Manual / Turbo).

Site-wide guidance: 7W for best battery life on light titles, 18W the balance
point, 25–30W for demanding AAA at 1080p; set *Memory Assigned to GPU* to 8GB
or higher in Armoury Crate.

The vocabulary is open-ended — *Hair Strands* appears on one game and not
others — so the internal schema stores settings as an extensible map and
classifies each key against a capability registry, rather than fixing a
schema that only fits one game.

## 3. Retrieval strategy

In preference order, all read-only:

1. **WordPress REST API** — `/wp-json/wp/v2/posts`, `/wp-json/wp/v2/pages`,
   `/wp-json/wp/v2/categories`, `/wp-json/wp/v2/search`. Structured JSON, with
   `modified_gmt` for change detection and `Link` headers for pagination. This
   is a public, official endpoint; preferred over scraping.
2. **Sitemap** — `/sitemap.xml` / `/wp-sitemap.xml`, for URL discovery with
   `lastmod`.
3. **Category archive HTML** — `/category/rog-ally-game-settings/page/N/`.
4. **Individual post HTML** — parsed only for the settings tables the REST API
   returns as rendered content anyway.

`robots.txt` is fetched and honoured before any crawl, a descriptive
User-Agent is sent, and requests are rate-limited with conditional
`If-None-Match` / `If-Modified-Since` so a refresh costs almost nothing.
Nothing authenticates, and no access control is bypassed.

## 4. Internal data model

```
SourceGame
 ├── title, slug, source_url, device_family
 ├── published_at, last_updated, retrieved_at
 ├── performance_rating (+ the site's word for it), performance_summary
 ├── store_links, image_url, notes
 ├── content_hash, parser_version
 └── profiles[]
      ├── name           the heading verbatim, e.g. "900P 15/18W"
      ├── tdp_watts[]    [15, 18]
      ├── resolution     "900p"
      ├── vram_gb        8
      ├── notes
      └── settings[]
           ├── label      the site's own words, e.g. "Hair Strands"
           ├── value      verbatim
           ├── canonical  TravelReady's key, or "" when unrecognised
           └── section
```

`settings` is a list, not a fixed record, because the vocabulary is open-ended.
An unrecognised setting keeps its label and value and gets `canonical=""`, so it
is shown to the user and never applied. Nothing is dropped, and nothing a page
does not state is filled in.

## 5. Matching

`matcher.score()` runs an ordered ladder and stops at the first rule that fires,
so the reason names the rule that decided it:

| Confidence | Rule |
|---|---|
| 1.00 | exact title, or exact normalised title |
| 0.94 | normalised title + edition suffix |
| 0.92 | normalised title ignoring a leading article |
| 0.88 | title prefix (the source adds a subtitle) |
| 0.90 | near-identical normalised title |
| 0.72–0.90 | similar normalised title — **review only** |
| 0.00 | different numbering, or different distinguishing words |

Normalisation folds accents, removes trademark marks *before* NFKD (which would
otherwise turn `™` into the letters "TM"), removes apostrophes rather than
turning them into separators, unifies the separators launchers disagree about,
collapses dotted acronyms, and expands roman numerals and known abbreviations.

Two guards prevent the failure that matters — matching the wrong game:

* **numbering** — differing numbers score 0, so `Forza Horizon 5` never matches
  `Forza Horizon 6`, and `DOOM` never matches `DOOM II`;
* **distinguishing words** — if each title keeps a substantial word the other
  lacks, they are siblings: `Need for Speed Unbound` vs `Need for Speed Heat`
  scores 0.

Only ≥ 0.90 is used automatically; 0.72–0.90 is offered for review; two
different titles scoring equally is marked ambiguous and forced to review. A
test scores every pair of distinct titles in the real `games.json` against each
other and fails on any automatic match.

## 6. Profile selection

| Mode | Target | Rule |
|---|---|---|
| `battery` | 13W | lowest published wattage |
| `balanced` | 18W | closest published wattage to 18W |
| `performance` | 30W | highest published wattage |

`--max-watts` caps the choice. Every selection returns the reason and the
alternatives. When the source publishes nothing suitable the reason says so
explicitly — *"the source publishes nothing below 25W for this game, so this is
the lowest available rather than a battery profile"* — instead of interpolating
a profile that was never published.

## 7. Cache and refresh

```
<data>/cache/rogallylife/
    index.json
    games/<slug>.json
```

Each entry carries `content_hash`, `retrieved_at`, `last_updated` and
`parser_version`. A refresh re-fetches only what changed; raising
`PARSER_VERSION` invalidates everything so a parser fix re-reads pages rather
than trusting an old extraction. A corrupt index is moved aside and rebuilt from
the game files. A post that disappears is **reported, not deleted** — vanishing
from one discovery run is usually a listing quirk, not a retraction.

The content hash covers the settings tables, not just the prose. (It did not at
first, which meant a changed recommendation produced an identical hash — the
exact case change detection exists for. A test now pins it.)

## 8. Capability matrix

`capability.py` records, per setting, whether TravelReady can **detect**,
**apply** and **verify** it:

| Scope | Examples | Applied |
|---|---|---|
| game, automatable | resolution, texture/shadow/effects quality, VSync, frame limit, FSR mode, anti-aliasing | yes, transactionally |
| game, recognised but unmapped | Frame Generation, ray tracing, Hair Strands, Screen Space Reflections | no — reported, manual |
| device | TDP, VRAM allocation, CPU Boost, Armoury Crate mode, fan profile | no — reported, manual |
| informational | average FPS, playability commentary | no — it is not a setting |
| unrecognised | anything else the source names | no — shown with the source's own label |

The registry says "TravelReady knows how"; `optimiser/safety.py` independently
says "and is it allowed to, here, in this file, on this device". Both must agree
before anything is written, and the existing transactional write path —
preconditions, hash re-verification, verified backup, dry-run, explicit
approval, atomic write, read-back verification, rollback — is unchanged.

A resolution published as `1600x900` is split into `resolution_width` and
`resolution_height`, the two keys engines actually store, so it is genuinely
applicable rather than stuck at RESEARCH_REQUIRED.
