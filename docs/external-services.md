# External services

miles calls a handful of public services. Each has a usage policy; this page
records what miles uses each one for, where the policy lives, what miles
enforces on itself, and how to point it at your own instance instead. All
outbound requests go through `miles/http_client.py`, which holds the limits
below process-wide (every request handler and background job shares one budget
per service), retries transient failures with exponential backoff, honors
`Retry-After` (a request is abandoned, not retried early, when the server asks
for more than five minutes), and gives up after a bounded number of attempts.

Every request, and its outcome, is counted per day by service and host in the
database (the `get_outbound_requests` MCP tool and `/metrics`;
see the README), so "how much traffic do we send to each service" has an answer
that includes short-lived CLI runs.

Policies change; the links are the source of truth, and the numbers below were
read from them in October 2026.

## Identifying yourself: `MILES_CONTACT`

Every request carries `User-Agent: miles/<version> (+self-hosted)`. Several
operators ask for a way to reach whoever is generating the traffic, so set
`MILES_CONTACT` (an email address or a URL) and it is appended:
`miles/<version> (+self-hosted; contact: <value>)`. Nothing is sent by default
beyond the application name and version.

## Caching and refresh

Every public service's answers are cached, and nothing refreshes by age except
where noted. Data is re-fetched only when someone asks (`refresh`), so the
traffic miles generates follows what the operator does, not the calendar.
`/metrics` (see the README) counts cache hits and misses per cache.

| Cache | Lifetime | Refresh on request |
| --- | --- | --- |
| Geocodes (`geocode_cache`) | forever | `import_area(..., refresh_geocode=true)`, `miles-area-refresh --geocode PLACE` |
| Geofabrik extracts | forever | `import_area(..., refresh=true)`, `miles-area-refresh LABEL`; a conditional GET, at most once a day per region |
| Geofabrik region index | 30 days | refreshed by a conditional GET when older; a failed refresh keeps the stored copy |
| Elevation tiles | forever | none needed; terrain does not move at this resolution |
| Drive times | 180 days | expire on their own |

An area reports `data_as_of` (the date its map data is as of: an extract's own
date, or the cut time of tiles uploaded from a local file) and `refresh_suggested` once that is more than
180 days old. Nothing acts on the flag.

## Services

### Nominatim (geocoding)

- **Used for:** turning a free-text place ("Example Town") into coordinates
  when an area is imported by name. One result is requested.
- **Called:** once per distinct place text, ever, unless refreshed. A repeated
  import of the same place never calls it; spellings that differ only in case,
  surrounding or repeated whitespace, or Unicode composition count as the same
  place, and identical lookups running at once ask once.
- **Policy:** <https://operations.osmfoundation.org/policies/nominatim/>:
  at most one request per second in total, an identifying User-Agent, cached
  results, no bulk or automated geocoding. The policy also asks that
  LLM-driven use point to it, which the `route-finder` skill does.
- **miles enforces:** the public instance is refused until `MILES_CONTACT` is
  set (coordinates need no geocoding and keep working). One request at a time
  and a 1.1 s gap between them, one retry on a 503, no retry on 429 or 403. A
  429 keeps the whole process quiet for a minute and a 403 for an hour; during a
  long quiet period a lookup fails at once instead of waiting. Results are
  cached forever (`geocode_cache`), and so is "no match" (`geocode_misses`),
  until someone refreshes. A refresh of a place looked up less than a day ago
  returns the cached answer with a note, and one that fails leaves the cached
  answer in place. An unparseable response is an error and is not cached.
- **Reverse geocoding:** one lookup (`/reverse`, zoom 14) of a route's start
  point, made the first time its location is shown (the route page, or the
  `get_route` tool) when it has none stored. The answer becomes a short label
  (a named park or protected area, else the locality, then the state or
  region) saved on the route as automatic; an athlete-set location is never
  replaced. Answers, and "nothing there", are cached forever in
  `reverse_geocode_cache`, keyed by the point rounded to three decimals
  (about 100 m), so nearby routes share a lookup. Listing routes never calls
  it, nothing runs in the background, and nothing geocodes in bulk. It uses
  the same throttle as place lookups. Without `MILES_CONTACT`, or when
  Nominatim fails, the label of an imported area containing the start point
  stands in and nothing is stored, so the next view asks again.
- **Attribution:** `import_area` returns the place's display name and
  "© OpenStreetMap contributors, ODbL" with the geocode, for whoever shows the
  result.
- **Volume:** a handful of requests per new place, and one per route the first
  time it is viewed; none for a repeat.
- **Self-hosting:** `MILES_NOMINATIM_URL` (default
  `https://nominatim.openstreetmap.org/search`); another instance needs no
  contact setting.

### Geofabrik (regional extracts)

- **Used for:** `.osm.pbf` regional extracts, the only source of map data for
  area import. One download is cut locally into every area inside it. Which
  region covers an area is chosen from Geofabrik's `index-v1.json` (each
  region's boundary polygon): the smallest region containing the area's box,
  or, when the area straddles a border and several smaller regions cover it
  with less ground than the containing parent, those regions, each downloaded
  once and merged. A `region` argument (one path or several) is checked
  against the same index; with no index stored and none fetchable, it is
  accepted unchecked and a bad path fails as the download's 404.
- **Called:** `index-v1.json` once, then at most every 30 days with a
  conditional GET; a region's extract the first time an area needs it (plus
  its `.md5`), and on an explicit refresh.
- **Policy:** <https://download.geofabrik.de/>: extracts are rebuilt daily and
  are meant to be fetched, not mirrored; avoid repeated downloads of the same
  file.
- **miles enforces:** a registered extract is never downloaded again unless
  asked. A refresh is a conditional GET (`If-None-Match` and `If-Modified-Since`
  from the stored file's ETag and Last-Modified, or its download time when it
  has neither), so an extract Geofabrik has not rebuilt costs a 304 and keeps
  the file. A registered extract is not asked about again within 24 hours of
  its last success, refresh or not, and a region gets no GET (checksum or
  file) within an hour of its last one, whatever came of it, `--force-extract`
  included. One download at a time per process, and a per-region lock keeps two importers
  from fetching the same file; the one that finds it taken waits and then uses
  the file the holder registered. Two attempts per request, since a
  dropped connection restarts the file. An extract over 2 GB is refused unless
  `MILES_ALLOW_LARGE_EXTRACTS=1`; the error names the smaller regions that
  cover the area; the size comes from the HEAD that resolves the redirect, so
  a refusal sends no GET and does not start the one-hour cooldown.
  The `.md5` published beside the dated file that `-latest` redirects to is
  fetched first, and the body
  is requested from that dated URL only once the checksum is in hand; an
  unreachable `.md5` ends the attempt before any download. A file is registered only after its byte count matches
  Content-Length and its MD5 matches; the dated URL is immutable, so the
  `.md5` is not read a second time. The partial file is removed on any failure. A
  registered extract whose file has gone missing is downloaded again, and
  `miles-area-refresh LABEL --force-extract` replaces a bad file
  unconditionally. Tiles already cut from an extract's current file are not
  cut again. `radius_km` is capped at 60.
- **Volume:** the index (one large GeoJSON file) about once a month, one download of a
  region (tens to hundreds of MB, up to 2 GB) per region, then at most one small
  request a day per region while someone keeps refreshing.
- **Self-hosting:** `MILES_GEOFABRIK_URL` (default
  `https://download.geofabrik.de`). `miles-area-import --pbf FILE` cuts a file
  you obtained yourself and uploads the tiles, for anywhere without a usable
  extract.

### OSRM (drive times)

- **Used for:** drive time from a point to candidate trailheads, one batched
  `/table` call per search.
- **Called:** once per route search or trailhead search with a drive-time
  limit (at most 90 minutes), for the best-ranked candidates not already
  cached: candidates farther than a 120 km/h straight line could reach within
  the limit are dropped, the rest are ranked by the best score that time
  could give them, and only the top few times the number of starts wanted
  (never more than 99) are sent.
- **Policy:** the demo server's own wiki page,
  <https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server>: "the demo
  server usage is restricted to reasonable, non-commercial use-cases. Do not
  exceed 1 request per second. We provide no guarantees wrt. uptime, latency,
  or data updates." The page also links a usage and privacy policy, in German,
  from FOSSGIS, which runs the server. miles sends at most one request per
  second process-wide, with an identifying User-Agent; it caches answers and
  never caches a failure. Anything heavier belongs on your own server, via
  `MILES_OSRM_URL`.
- **miles enforces:** a 1.1 s gap between requests and up to two attempts on
  429/503/504, connection errors, and bodies cut short, each with a 3 s
  timeout. A search is waiting on the answer, so a dead server costs it about
  seven seconds before the estimates take over, and the single retry is spaced
  by the 1.1 s gap like any other request. Drive times are cached
  by origin (the 100 m grid cell it falls in) and destination: 180 days for a
  measured time and 7 days for an "unreachable" answer, which may be a gap in
  the road data. Expired rows are deleted as searches run. A failed request is
  never cached. After one, drive times are straight-line distance x 1.4 at a
  nominal 60 km/h, flagged `drive_time_estimated`, and OSRM is not tried again
  for 10 minutes (the pause is kept in the API process's memory, which owns
  the outbound calls, so a restart clears it).
- **Volume:** at most one request per search, none when the best-ranked
  candidates are cached for the origin's cell.
- **Self-hosting:** `MILES_OSRM_URL` (default
  `https://router.project-osrm.org`).

### AWS terrain tiles (elevation)

- **Used for:** the elevation source: Terrarium-encoded PNG tiles at zoom 13,
  draped onto tracks, routes, and the trail network.
- **Called:** once per tile, ever, when an area import, a terrain backfill, or
  a route redrape needs one that is not cached.
- **Policy:** <https://registry.opendata.aws/terrain-tiles/>: open data, free
  to use, with attribution to the sources listed at
  <https://github.com/tilezen/joerd/blob/master/docs/attribution.md>. Pages
  that show elevation-derived data credit "Terrain Tiles (Mapzen/Tilezen
  Joerd), via the AWS Open Data Registry". miles fetches each tile once and
  keeps it forever, at most ten requests per second, with an identifying
  User-Agent, and credits the source on the map.
- **miles enforces:** 0.1 s between first-time tile fetches, up to four
  attempts on 429/503/504 and connection errors. Tiles are validated before
  they are cached and then kept forever.
- **Volume:** one tile covers roughly 600 m at zoom 13; a city-sized area is
  tens of tiles, a decade of activities a few hundred, each fetched once.
- **Self-hosting:** `MILES_TERRARIUM_URL` (a template with `{z}`, `{x}`, `{y}`).

### Open-Meteo (weather)

- **Used for:** the weather around a run, from the archive endpoint for older
  runs and the forecast endpoint for the last few days.
- **Called:** by a sync, for activities without weather. Not counted against
  Strava.
- **Policy:** <https://open-meteo.com/en/terms>: the free API is for
  non-commercial use, under 10,000 calls a day, about 5,000 an hour and 600 a
  minute. A request's weight grows with its date span and variable count
  (roughly one unit per 14 days times one per 10 variables, treated here as
  an approximation). Data is CC BY 4.0 and needs the attribution "Weather data
  by Open-Meteo.com" linking <https://open-meteo.com/>.
- **miles enforces:** 1 s between requests, a 10 s timeout and up to three
  attempts. Runs at
  one ~11 km location are split into request windows that never span more
  than 14 days, so every request weighs one call, and one request covers the
  runs of a fortnight rather than a decade. The first failed request (a 429, any
  other HTTP error after retries, or a connection error) ends the weather pass
  and the rest waits for the next sync. When a successful response holds no
  hours for a run, the run gets a value-less marker row and is not asked about
  again for 30 days.
- **Volume:** one request per 14-day window of runs per ~11 km location
  group, so the count follows how many fortnights the runs fall into, not how
  many runs there are. Requests are at least 1 s apart (60 a minute, under
  the 600 a minute limit), so a first sync over a long history takes minutes
  of pacing, not hours. Later syncs request only the new runs.
- **Attribution:** the activity, build, and plan pages show the credit beside
  weather figures, and weather in API and MCP output carries an
  `attribution` / `weather_attribution` field.

### Strava

- **Used for:** the activity list, laps, and streams.
- **Called:** by `miles-sync` and the Sync button, and by an
  activity page that fetches its own streams. A sync runs in passes: the
  activity list always, then detail for what is new, then (for `miles-sync`
  when history is on, the default; the Sync button skips it unless asked with
  `POST /api/sync?history=true`) a trickle of older laps and streams. Every pass after the first
  stops when the day's remaining calls reach a reserve (12 by default,
  `MILES_STRAVA_RESERVE`), so a later sync the same day can still fetch new
  activities, and none takes the last calls of the 15-minute window.
- **Policy:** <https://developers.strava.com/docs/rate-limits/>: a 15-minute and
  a daily allowance (by default 100 and 1,000 for reads; an app can have
  higher limits, so miles takes the limits from the latest headers and assumes
  none). Every endpoint but uploads counts against the read limit, which is the
  only one miles uses. Windows reset at :00, :15, :30 and :45; the daily limit
  at midnight UTC. Requests over the short limit get a 429 and still count
  toward the daily one. The documentation says every response reports usage;
  observed, stream responses carry no rate-limit headers, so miles counts every
  request it sends between header readings. It waits out the window when it is
  nearly spent.
- **Credentials:** the token refresh sends the client secret and refresh token
  in the POST body, never the URL. stravalib puts the access token in each
  request's query string, and request-library errors repeat the URL, so every
  call goes through a boundary that replaces a transport failure with an
  error naming only the kind of request and the failure class, and every
  stored or returned error text is redacted (`miles/errors.py`). Requests time
  out after 30 seconds.
- **Allowance bookkeeping:** miles replaces stravalib's built-in rate limiter
  (which can sleep a request until the next UTC midnight) with one that has no
  rules; a response hook on the API session records the headers and tallies
  every response, whatever its status (a 429 counts). Streams responses (almost
  all of a history backfill) carry no rate-limit headers, and Strava's header
  usage appears not to include them, so a header reading alone would show a
  nearly untouched allowance after a restart. The rule is therefore: remaining =
  min(limit less the calls tallied in the current window or UTC day, the latest
  reading's figure less the calls tallied since it). A reading can lower the
  figure or supply the limits, never raise it above the tally. The token endpoint
  is not on that session and is not counted. A window or day that has ended
  restarts its tally at zero, so it reads as whole less the calls made in the new
  one (the daily limit is that of the last reading).
  The tally persists in the same `meta` row as the allowance
  (`strava_rate_remaining`), written once per response:
  `{"daily", "short", "at", "counts": {"day", "daily", "daily_limit", "window",
  "short", "short_limit"}}`, where `window` is the epoch second divided by 900.
  A new process reads it on first use and resumes the counts if their window or
  day is the current one (stale counts are dropped, the limits kept), so a
  restart mid-window continues the same window's spend. With no reading and
  nothing stored, nothing is known and
  nothing blocks; the activity list at the start of every sync supplies one. Every request passes one gate (`miles/strava_budget.py`)
  that checks the reading first. A 429 is waited out for one window; a second
  with nothing succeeding between, or one when the headers show the daily
  allowance spent, ends the pass ("Strava daily limit reached; resets 00:00
  UTC") and is recorded as the request's outcome. Activity pages that would
  fetch their own description, laps, or streams do not spend a call at the
  reserve or wait on a 429; the data shows as not fetched yet (streams answer
  503 with `Retry-After`).
- **Volume:** one list request per sync, one detail request per new workout
  (its laps and description come from the same response), one stream request per
  new run, plus up to 900 detail and 900 stream requests of history per sync
  while any remain. Token refreshes are separate and are not counted against
  the read allowance; they are recorded in the ledger as kind `token`.

### OpenTopoMap and OpenStreetMap tiles (basemaps, in the browser)

- **Used for:** the base map under route, activity, and terrain maps:
  OpenTopoMap by default, OpenStreetMap's standard tiles as a layer the user
  can switch to. These requests are made by the browser, not by the server, so
  miles' outbound request ledger does not count them.
- **Policy, OpenTopoMap:** <https://opentopomap.org>: free, CC-BY-SA map
  style over OpenStreetMap and SRTM data, for light use. The servers are
  volunteer-run and slow, and the map is capped at zoom 17.
- **Policy, OpenStreetMap:** <https://operations.osmfoundation.org/policies/tiles/>:
  light use only, a valid Referer or User-Agent (a browser's Leaflet requests
  send one), no bulk download or prefetching of tiles, and visible attribution.
  miles never prefetches tiles.
- **Configuration:** `MILES_TILE_URL` replaces OpenTopoMap as the default layer
  with any `{z}/{x}/{y}` tile template (a `{s}` subdomain placeholder is
  allowed), with `MILES_TILE_ATTRIBUTION` (HTML) and `MILES_TILE_MAX_ZOOM`.
  OpenStreetMap's standard tiles stay available as the alternative layer. The
  pages read these from `/api/config`. A custom layer with no
  `MILES_TILE_ATTRIBUTION` is credited to "© OpenStreetMap contributors" and
  the server logs a warning once.
- **Fallback:** if OpenTopoMap fails more tiles than it loads (six errors) or
  loads nothing within eight seconds, the map switches once to OpenStreetMap
  and says so; it never switches back by itself. The map's maximum zoom
  follows the default layer (17 for OpenTopoMap).

## Attribution

- Map pages credit "© OpenStreetMap contributors" (ODbL), plus SRTM and
  OpenTopoMap on the default layer.
- Pages and tool output showing weather credit "Weather data by Open-Meteo.com"
  (CC BY 4.0, <https://open-meteo.com/>).
- Pages showing elevation-derived data (the activity and route maps, and the
  terrain page, which has no map and prints the credit as a line of text)
  also credit the terrain tiles as above.
