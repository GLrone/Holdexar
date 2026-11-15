# Community price history plugin

A data source plugin of kind `datasource.community_history`. The package carries
`manifest.json` plus one data file, `observations.json`, and contains no
executable content: Holdexar validates every observation at install time and
merges them through its own unified write path when a backfill is dispatched.

`observations.json` describes one `appid` + `region` series with daily points:

```json
{"appid": 10, "region": "cn",
 "observations": [{"date": "YYYY-MM-DD", "priceMinor": 8800}]}
```

Dates must be unique, not in the future, and priced points need a positive
`priceMinor` equal to the region currency's major-unit amount multiplied by 100
(including currencies without fractional units). The core calculates CNY values
from historical exchange rates; source-supplied conversions are not used.
These packages carry no verifiable product identity, so every point renders in
the unknown-product series and never merges into standard-edition history. At most
5000 points and 256 KiB per package. Data source plugins declare no network
endpoints, domains, headers, or configuration.

Run `python pack.py example.history.hlx` and install the archive from
Settings → Plugins. Enabling it selects the active data source (only one can be
enabled at a time); dispatch a backfill from a game's history view or
`POST /api/v1/games/{appid}/history/backfill?region=cn`. Uninstalling the
plugin keeps every merged observation. Select the unknown-product series in the
history view to see these points. Package observations and installation state are
saved together in Holdexar's existing settings storage. To distribute updates, serve an index
like the notification plugin example and keep the archive checksum in it.
