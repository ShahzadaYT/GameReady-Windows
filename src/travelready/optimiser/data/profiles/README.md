# ROG Ally Life profiles

This directory holds recommended-settings profiles. **It ships empty on
purpose.**

`rogallylife.com` was not reachable from the environment in which this code was
written (the network policy denied the host), so no real profile could be
captured. Writing plausible-looking settings would have been inventing data and
presenting it as a ROG Ally Life recommendation, which `profiles.py` exists to
prevent.

`travelready.optimiser.profiles.profile_count()` reports the real number
installed, and the GUI says "No ROG Ally Life profile found" rather than
offering anything made up.

## Adding a profile

1. Open the game's page on <https://rogallylife.com/>.
2. `python -m travelready.cli profile-template "<Game Name>" > game.json`
3. Fill in the values **from that page**, and set `source_url` to the exact
   page URL and `source_date` to the date you read it.
4. `python -m travelready.cli profile-import game.json`

The importer validates against the schema and refuses a profile that is missing
attribution or that names a setting TravelReady does not recognise.

## Rules

* `source` and `source_url` are mandatory.
* Only settings listed in `KNOWN_GAME_SETTINGS` / `KNOWN_DEVICE_SETTINGS` are
  accepted.
* A field the source does not state is left out. Absent means "not
  recommended", never "guess a value".
* ROG Ally Life is a community source. It is **not** ASUS, and TravelReady
  never labels it as official ASUS guidance.
* Device matters: a profile for `rog_ally`, `rog_xbox_ally`, `steam_deck` or
  `legion_go` is shown but never applied automatically on a `rog_ally_x`.
