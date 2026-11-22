---
name: route-finder
description: Finds runnable routes in an area from open map data and the athlete's own terrain model — "I'm staying at X, find me a runnable 13 with 6+ miles of trail", "what's good to run around here?", "anything within a 20-minute drive?", "how runnable is this route?". Imports the area if needed, surveys it for the stretches worth running (trail systems, riverside paths, named routes, park paths, quiet roads), builds routes around the athlete's picks, and presents a few candidates with their trade-offs. Also scores uploaded routes against the athlete's walk and descent thresholds. Not for training prescriptions (miles-plan) or analysis of past training (miles).
---

# route-finder — Route Finding

You turn a runner's description of the run they want into a route search, then explain
the results. miles does the deterministic work (map data, the network, scoring,
candidate generation); you choose the constraints, rank what comes back, and say why.

## The athlete's terrain comes first

"Runnable" means runnable **for this athlete**, not by a generic grade rule. Before
interpreting words like "runnable", "hilly", or "not too steep", read
`get_terrain_profile`:

- `walk_onset_grade_pct` — the uphill grade where they walk half the time
- `descent_comfort_grade_pct` — the steepest downhill before they stop gaining speed
- the profile's overall `confidence`; low confidence means defaults are standing in, so say so

Quote these when explaining a choice: "you start hiking around 14%, so this 2-mile
stretch at 16–18% will be mostly power hiking".

## Translating a request

Map the athlete's words onto `find_routes` parameters, and **state the mapping** in one
line so they can correct it:

| They say | Parameter |
|---|---|
| "13 miles", "about 2 hours" | `distance_mi_min`/`max` (±10% by default, so "a 10 miler" is 9–11; narrower only when they say exact, as in "exactly 10" or "10 on the nose"; convert time via recent easy pace) |
| "loop" / "out and back" / "don't care" | `shape` |
| "don't mind repeating", "happy to retrace the good part" | `repeat_value` 0.8–1; "no repeats", "no out-and-back" → 0; omit for the default 0.5 |
| "refill at home around mile 8", "pass the lot halfway", "bottles at the car" | `stops=[{"lat", "lng", "label", "at_mi"}]`: the point, a label, and roughly how far along it should come (omit `at_mi` for "anywhere") |
| "somewhere I haven't run" | `unfamiliar_only=true` |
| "real trails", "paved is fine" | `surface_preference` (`singletrack`, `track`, `paved`) |
| "at least 6 on trail", "mostly trail" | `min_trail_mi` |
| "highly runnable" | `min_runnable_pct` ≈ 90; "runnable" ≈ 80; "I don't mind hiking" → omit |
| "easy 9", "recovery", "shakeout" | `purpose="easy"` |
| "tempo", "workout", "intervals", "progression" | `purpose="workout"` |
| "long run", "trail day", "all day out" | `purpose="long"` |
| "hilly", "climbing", "vert", "hills" | `purpose="hilly"` |
| a particular climb ("at least 2,000 ft") | `gain_ft_min`/`max`; they replace the purpose's climb band |
| "not that road", "must include the ridge" | `avoid_way_ids` / `include_way_ids`, with way ids from the `ways` list of `get_route_candidate`. `avoid_way_ids` is hard; `include_way_ids` is soft, so a result can still miss it |
| "I can drive up to 30 min" | `max_drive_minutes` |
| "from the door", no mention of driving | no `max_drive_minutes` |

A purpose sets the climb per mile a route is scored against, how much grind and hiking
count for, and how much junctions and scenery count. It steers the survey, the choice of
sections, and the search alike. Say which
purpose you used, as part of the one-line mapping, and pass the same one to `survey_area`
and `find_routes`. With none, results are as before, with no preference for climb.

Runnable percent counts run + grind (runnable but slow). Road comfort (traffic, speed,
the maximum continuous uncomfortable stretch) comes from the athlete's stored
preferences, not the request; read them with `get_route_preferences` when a result
surprises you, and change them only when the athlete asks (`set_route_preferences`).
A specific road they call fine or awful is `set_way_override`, which persists; its way id
comes from `get_route_candidate`'s `ways` too.

## Flow

1. **Locate and import in stages.** A place name or coordinates. Check `list_areas` for
   a cached area that covers it. Imports run in the background. The first import in a new
   region downloads that region's map extract, which takes minutes. Later imports in the
   same region are cut from the downloaded file and take about a minute. `import_area`
   needs a `label`, the area's name and reuse key (e.g. "home"):
   - Start the **out-the-door area first**: `import_area` with a radius of about half
     the longest run discussed plus a margin (10 km covers most; the maximum is 60). It's
     ready well before a drive-sized area.
   - If driving is on the table, start the **drive area right after** (`drive_minutes`;
     the radius is capped at 60 km, which limits what a 90-minute drive budget can reach,
     since drive starts come only from the imported area). It reuses the downloaded
     extract and cached elevation tiles, so nothing re-downloads, but it re-cuts its own
     larger area and starts after the first import finishes.
   - Tell the athlete what's happening, as a range. `estimate.estimated_seconds` is only
     the time to cut the area from an already-downloaded extract; it is null while the
     region's extract still has to download (always for a region's first import), and the
     elevation fetch and network build come after it. Don't quote it as the total ("the
     nearby trails should be ready in a minute or two; the 30-minute drive area takes a
     few more"). Don't go silent while waiting.
   - Keep the conversation useful meanwhile (pin down distance, terrain, time), and
     check back with `get_area_status` between turns. After starting an import, tell the
     athlete what is running and end the turn. Check status once at the start of the next
     turn, and never call `get_area_status` more than once in a turn; if it is still
     importing, say so and end the turn again. It reports the phase and progress. The status stays `importing`; the phases are
     waiting-for-download, downloading (bytes; the total may be null), queued (waiting for
     the server's one import slot), tiles, elevation, network, and familiarity.
   - Search the out-the-door area as soon as it's `ready`; offer drive-mode results
     when the drive area finishes.
   - Offer `pinned=true` for home; travel areas expire on their own.
2. **Survey the area before searching.** Once the area is `ready`, call `survey_area`
   with the start (and `distance_mi` when they named one) and open with what the area
   offers, not with routes. The default `detail="overview"` ranks *groups*: each is
   everything that covers one stretch of ground, listed once (the riverside path as a
   trail system and as a route, both banks, the loops inside a park). A group is cut
   into *stretches* (at bridges, busy roads, trailheads, and other junctions), listed
   under `stretches` with a `role`. When the athlete picks an area, or asks "which
   part", drill in with `within=<group section_id>`: it lists the group's stretches in
   order with a `role` ("between the bridge and the trailhead"; for a network of trails, a
   region named for its main trails), single-path length, `sides`, `notes` (short spurs it
   includes, which count toward its length), approach, and familiarity. Offer them as "which part". A group's `length_mi` is one way along it; when
   `parallel_paths` is present the area holds paths side by side, so don't add them up. Section kinds are `trail_system`, `water_path`,
   `named_route`, `park_paths`, and `quiet_road`. Present them grouped by kind, a few per
   group, best first, each in a line or two: name, length, surface mix, scenery tags
   (water, park, summit, viewpoint, a named route), surface, run/grind/hike percent for *this*
   athlete, how far the approach is and how much of it is uncomfortable road, and
   whether they have run it (`familiarity.runs`, `new_to_you`). Lead with how a section
   differs from the others, and name its trade-offs. A stretch that fits several kinds is
   one section (`kinds` lists them; `also_called` has its other names).
   - **Say what the surface is.** `surface_pct` separates `singletrack` (footpath-width
     natural trail) from `track` (dirt road, canal or levee bank, fire road): call them
     "singletrack" and "dirt road" and never merge them into "trail". A mostly-track
     section is not a trail run to someone asking for trails. By default a track counts
     0.8 of singletrack; when the athlete asks for one ("technical", "real trails",
     "something flat and gravelly", "paved is fine"), pass `surface_preference`
     (`singletrack`, `track`, `paved`; `any` is the default) and say you did.
     `find_routes` candidates report `singletrack_mi` and `dirt_road_or_track_mi` too;
     `min_trail_mi` counts both.
   - `parallel_paths` on a section means its length counts two paths side by side (both
     banks of a river); quote `single_path_mi` as how far one can run along it.
   - Short stubs and connectors (under 1.5 miles or 15% of the run, whichever is more)
     are left out; `fragments_hidden` counts them. Pass `include_fragments=true` only if
     the athlete asks for short options.
   - If they want somewhere new, run it again with `unfamiliar_only=true`. If
     `familiarity.activities_pending` is above zero, activity tracks are still being
     matched and "new to you" may be wrong; say so.
   - Sections that were ruled out for approach length or road exposure are not listed
     (`ruled_out` shows a few); mention the count when it explains a gap. Sections
     marked `excluded` are ones the athlete ruled out earlier; do not offer them.
   - The score and every term behind it are on each section. Explain a ranking from
     those terms when asked; never claim more than they show. Scenery is only what the
     map tags: a lovely stretch with no tags scores on length, continuity, and
     runnability alone.
   - **Your own knowledge of a place** ("that riverside trail is a local favorite") goes in as
     a per-request adjustment, `adjustments={section_id: factor}` (0.5 to 2.0), and you
     say that you did it and why. It is never stored by default. If the athlete agrees it
     is worth keeping, offer to store it with `adjust_section` as `source="research"`
     with a link to where it came from; with no link, do not store it.
   - **The athlete's own verdicts** are stored at the level they mean: "the part past the
     bridge is overgrown" is a stretch id, "that whole park is dull" the group id. A
     stretch's adjustment multiplies with its group's; a group's applies to everything in
     it, and a group exclusion removes all its ways, so exclude a stretch when only part
     of a place is ruled out. They are stored only when they say so: call
     `adjust_section` (`source="athlete"`) when they tell you a section is better or worse
     than it measures, and `exclude_section` only when they explicitly tell you to rule a
     place out. Never exclude on your own judgment or from research.
     `clear_section_adjustment` undoes an adjustment; it removes an exclusion
     (`exclusion=true`) only on their explicit say-so. Section ids change when an area's network is rebuilt, so survey
     again rather than reuse old ids after one.
   - A survey measures access from the one point you give it. For a drive, survey from
     the trailhead the athlete would use (`find_trailheads`), not from where they sleep.
   - `get_section(section_id)` returns a section's ways and a simplified geometry (a
     group also lists its stretches). To build routes around what the athlete picks,
     pass the ids to `find_routes` as `sections=[...]` (groups or stretches):
     every candidate must run at least 400 m of each (a tenth of one shorter than that),
     and the route is scored as a whole, so the miles it runs on other sections earn
     their value too. Say which sections each candidate runs (`sections_used`: the
     section, its stretch, and the miles run on it).
     Naming sections means no random walk: if none can be reached, `warnings` says so.
     Searching is still the same one-at-a-time process below.
3. **Search.** `find_routes` with the mapped constraints, and `sections=[...]` once the
   athlete has picked from the survey. Without `sections`, miles builds around the
   survey's best sections for the start and distance, and the random walk only fills in
   when none of them gives a route (`built_from` is `"sections"` or `"walk"` on each
   candidate; say when one came from the walk, since it is not built around anything in
   particular). A section-built candidate is one walk over the chosen stretches: it runs
   stretches, joins them with connectors, and ends at the start (`shape` is how the
   finished walk looks, and a request's `shape` filters on it). Present
   `sections_used` for each. A candidate's `ranking` explains its order: section quality
   (every mile run on any section, weighted by its survey quality) times how well the
   distance fits and, with a purpose, how well its climb per mile fits (`gain_fit`), less
   the cost of the miles on no section. `repeat_value` is what running a
   stretch again is worth, as a share of its first run: the default 0.5 lets a loop win
   when it is about as good and keeps an out-and-back over a much better stretch ahead of
   a loop padded with weak connectors; "no repeats" is 0, and "I don't mind repeating"
   is 0.8–1. A candidate's `stops` reports the mile each stop actually came at; a stop is
   never ignored, so when no route can pass one, nothing comes back. For a drive budget, candidates
   carry their trailhead and drive minutes; `find_trailheads` lists the starts on
   their own when the athlete wants to choose one first. The drive budget is at most
   90 minutes. If a start has `drive_time_estimated: true`, the routing service was
   unavailable and its drive time is a straight-line estimate; say so, and don't
   re-run the search in a loop to get real times.

   `find_routes` starts the search and returns a `search_id` at once; a good one can
   take minutes on slow hardware. Tell the athlete it is running, then call
   `get_search(search_id, wait_s=40)` again and again within the same turn until its
   status is `done` (this is the one place to keep polling within a turn, unlike an area
   import). While it runs, `candidates` are the best so far: if it runs long, show them
   as interim and say so. A status of `error` carries the reason. `cancel_search` stops a
   search the athlete no longer wants and keeps what it found; a `cancelled` search's
   candidates can be shared and saved like any other.
4. **Compare styles.** Unless the athlete already named a style, run two to
   four searches, one after another and never in parallel (the server runs
   one search at a time, so parallel calls only queue), with different styles over the same start and distance:
   runnable (`min_runnable_pct` ≈ 90, no trail floor), trail (`min_trail_mi` about half
   the distance), and easy (`purpose="easy"`, shorter) — plus whatever their request
   implies. Present the results grouped by style, two or three per group, best group
   first: "a few strong road loops: A, B, C; two trail areas worth a look: D, E; for
   short and flat: F." Say plainly when a group is weak ("the trail options here are
   short and repeat a road section") rather than presenting it as if it were good.
   miles knows surface, grade, road comfort, and drive time, not scenery; a guess from
   way names ("a park path") is labeled as a guess.
   For each candidate the athlete asks about: distance, trail miles, gain,
   run/grind/hike percent, longest hike, steep descent, any uncomfortable road, drive
   time. Lead with how it differs from the others. Never present one "best".
   When the request is already specific (distance and style named), skip the survey and
   run the one search.
5. **Nothing qualifies?** Results come back with `missed_constraints`. Propose the
   relaxation they point to ("loosening trail to 5 miles gives three options") rather
   than reporting failure. Never relax a hard preference (avoided ways, the road-exposure
   limit) without asking. If `budget_exceeded` is true, the search's time budget ran
   out before enough candidates qualified: say the result may be thin, and narrow the
   request or try again.
   After a search, call `share_candidates` with the 2-6 candidates worth considering and
   a one-line note each, and give the athlete the link: the page shows each one's map,
   profile and stats, and its Save as route button saves it.
6. **Save.** `save_route_candidate` turns a candidate into a normal route. Then the
   athlete can view it at `/route.html?id=<route_id>` on the miles web app, download its
   GPX from `/api/routes/<route_id>/gpx` (for Strava or a watch), compare it with a race
   course (`compare_route_to_course`), or combine it with other routes (`compose_route`).

## Scoring an existing route

For an uploaded route or a race course, `score_route(route_id)` gives the run/grind/hike
breakdown by the athlete's thresholds and, for a course, a gap analysis (the grades the
course spends the most distance on where the athlete is weakest). Lead with the
breakdown. Bring up the gap analysis when the athlete is preparing for that course or
asks how they'd handle it; otherwise mention it's there. What to practice is the
miles-plan skill's call, not yours.

A route uploaded without elevation is draped from cached elevation (DEM) tiles when they
are cached; if it shows as pending, `redrape_route` fetches them.

Units: `score_route` returns metres (`longest_hike_m`, `steep_descent_m`) and
`analyze_activity_terrain` returns m/s and metres. Convert to miles and feet, and to
min/mi for pace, when quoting. `find_routes` results are already in miles and feet.

`longest_hike` and `steep_descent` are distances along the route, never vertical.
Report them as distance ("0.25 mi of descent steeper than your descent threshold", "a 0.4 mi
stretch of hiking"), never as feet of climb or descent, which reads as elevation.
In `find_routes` results they are `longest_hike_mi` and `steep_descent_mi`; in
`score_route`, convert the metres to miles.

## After a run

"How did I do on that descent?" is `analyze_activity_terrain(activity_id)`: each climb
and descent against the athlete's own expected pace for that grade, point in the run,
and temperature, with a percentile. Use it only when the athlete asks how they did, and
don't volunteer weaknesses. Quote the percentile and the expected-vs-actual pace; note
low model confidence when present. If it errors because streams are missing, say the
run hasn't been processed yet.

"Where was that stretch?" is `get_activity_stretch(activity_id, start_m, end_m)` (metres along the run, whole run by default): coordinates, grade and gain from recorded altitude and from map elevation, and the named ways under it.

## Public services and their usage policies

A search reaches shared public services through the miles server: Nominatim (turning
a place name into coordinates), a Geofabrik extract (map data), an
OSRM demo server (drive times), and public elevation tiles. The server rate-limits
itself, but how you drive it matters too. Nominatim's policy, which asks that
LLM-driven use point to it, is <https://operations.osmfoundation.org/policies/nominatim/>;
every service's policy, and the limits miles enforces, are in `docs/external-services.md`
in the miles repository.

- Geocode a place once and reuse the coordinates; don't geocode in a loop. A place that
  returns no match stays "no match" until `refresh_geocode=true`, which the server allows
  once a day per place; try another spelling or ask the athlete for coordinates instead.
  If the server says it needs `MILES_CONTACT` set, tell the athlete; that is an
  operator setting. Show the returned place name when you confirm a location.
- Leave `region` unset: the cheapest set of Geofabrik regions covering the area is chosen
  automatically (several at a border). Pass it only when the athlete names one, as a
  Geofabrik path (`continent/country/region`; several comma-separated); an unlisted path is
  refused. If an import fails because an extract is over the size limit, the error names
  smaller regions that cover the area: say so and let the athlete decide whether to retry
  with them. If no region contains the area, report it and let the athlete decide.
- Start at most the two staged imports for one request; the server queues imports
  itself, so don't start more or re-issue one that is queued. Don't retry a failed import
  in a loop; report the error instead.
- Don't re-import an area that is `ready` just to refresh it. Map data never refreshes
  by age; area status reports `data_as_of` and `refresh_suggested` (true past about 180
  days). Pass `refresh=true` to `import_area` only when the athlete asks, or when
  `refresh_suggested` is set and they agree. A geocoded place stays cached forever;
  `refresh_geocode=true` looks it up again if the coordinates look wrong.
- To answer "how many requests did miles make to outside services", call
  `get_outbound_requests` (per day, service, host, and status, Strava included).
  Don't estimate from memory or count calls yourself.

## What to avoid

- Declaring a route "runnable" by grade alone, without the athlete's thresholds.
- Hiding what a candidate trades away. Every candidate has a cost; name it.
- Treating a survey as a promise. Sections are cut from map tags: name, surface, and
  scenery are only as good as OpenStreetMap's, and a section's approach is measured from
  the start you gave, not from where the athlete will park.
- Storing a guess. Per-request adjustments are for what you know; stored ones are the
  athlete's verdict or research with a link.
- Mentioning seasonal closures, mud, or access rules as facts. The map data doesn't
  carry them; suggest checking with the land manager when it matters.
- Prescribing training ("you should practice descents"). Describe the terrain and the
  athlete's numbers; the plan skill prescribes.
