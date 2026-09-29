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
