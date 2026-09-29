# ROG Ally Life parser fixtures

**These files are synthetic.** They reproduce the *structure* of the real site
(WordPress markup, profile headings, settings tables, per-device post URLs) so
the parser can be tested, but the settings values in them are invented test
data and are **not** ROG Ally Life recommendations.

They are never loaded into the profile cache by the application — only by
`tests/test_rogallylife_parser.py`. Nothing in `src/` reads this directory.

Real recommendations only ever enter TravelReady through
`travelready settings update`, which fetches them from rogallylife.com and
records the URL, retrieval time and content hash.
