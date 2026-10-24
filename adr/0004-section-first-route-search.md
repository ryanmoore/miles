# ADR 0004 · Section-first route search

Status: accepted · 2026-10-17 · amends the search decision in ADR 0003

## Context

ADR 0003's search generates candidates by randomized walks and shortest-path
turnarounds, then filters them against the request. Constraints such as a
trail-mileage floor only reject candidates; they never steer generation, so
in a street grid with trail some distance away a walk rarely strings enough
trail together, and the search spends its budget on routes it then discards.
Ranking is distance fit plus runnable percentage, so nothing rewards
qualities a runner looks for: a long continuous path along a river,
a connected trail system, a stretch of quiet road without intersections, a
park, a viewpoint.

A runner planning by hand works the other way round. They survey an area for
stretches that look good, judge how hard each is to reach, rule out any that
need a long or unpleasant approach, and only then build a route around the
best of them, preferring a loop when one is as good, and accepting an
out-and-back when its stretch is better.

## Decision

- **Sections are the unit of search.** At network build, an area is
  segmented into sections: maximal stretches worth running as a unit. A
  connected cluster of trails is a trail system; a path following water, a
  park's paths, a named walking, hiking or cycling route, and a road run with
  few intersections and comfortable traffic are each a section. Sections are
  stored per area with a stable id and rebuilt with the network
  (`NETWORK_DERIVE_VERSION`), so the athlete and an agent can refer to one
  across searches.
- **A section stores only what depends on the map.** Length, surface mix,
  continuity (intersections and road crossings per mile), scenery tags, and a
  grade histogram. Scenery tags come from the area's own extract at build time
  and need no further downloads: adjacency to water (rivers, streams, lakes),
  lying within a park or protected area, membership in a named route
  relation, and viewpoints and summits on or near it.
- **Anything that depends on the athlete or the start is computed per query.**
  Runnability is the stored grade histogram read through the current terrain
  model, so it follows terrain-model updates without a rebuild. Familiarity is
  how often the athlete's own activity tracks cover a section, from the raw
  streams already stored. Access cost is a preference-weighted shortest path
  from the start to each section's nearest entry, where uncomfortable road,
  crossings, and steep grades cost more than their length; a section whose
  approach exceeds the athlete's limits is ruled out. In drive mode, access is
  the drive time to the trailheads and parking that reach the best sections,
  timed for those sections only, through the existing cached OSRM path.
- **A survey comes before a search.** A survey tool ranks the sections around
  a start by quality and access and returns them with their traits, so a
  conversation can open with what an area offers before committing to a
  route.
- **Knowledge the map lacks enters as a bounded adjustment.** Each section's
  measured quality is multiplied by an adjustment (bounded, about ×0.5 to
  ×2) that carries what OpenStreetMap can't: the athlete's own verdict, an
  agent's knowledge of a place, or what research found. Adjustments the
  athlete makes or confirms persist with their source (`athlete` or
  `research`) and a note (with a link, for research); they are keyed to the
  section's underlying OSM way ids, as way overrides are, so they survive a
  rebuild that reshapes sections. An agent can also apply an adjustment to a
  single survey or search without storing it. Surveys and results show every
  adjustment with its note, so a ranking can always be explained.
  The bounds keep research and an agent's guesses from burying a section;
  ruling one out is the athlete's call alone. An athlete can **exclude** a
  section: it is never used in a route (as avoided ways are removed from the
  graph), persists with its note keyed the same way, can be undone, and still
  appears in a survey marked excluded.
- **Routes are built around chosen sections.** A candidate is a walk that runs
  stretches of the chosen sections and joins them with connectors (phase 2b).
  Explicit style requests (trail, flat, highly runnable) filter sections before
  the search rather than candidates after it. The randomized walk remains as the
  fallback for requests no section can serve.
- **Ranking trades section quality against access cost.**
  A candidate's score is the adjusted quality of the distance spent on
  sections, less the cost of the connectors between them. A stretch run again
  earns a share of its value (`repeat_value`, default 50%, adjustable per
  request from 0 to 100%), so a loop wins between otherwise equal routes and a
  better out-and-back still wins over a lesser loop. Scenery counts the same
  everywhere; unfamiliar sections are a filter the request can ask for, not a
  global weight.

## Phases

1. Sections and the survey: keep the scenery tags at build time, segment and
   store sections, add the survey tool with section adjustments (stored and
   per request), and open route conversations with it. An agent can already steer the existing search toward a section through
   `include_way_ids`.
2. Route search over the sections (first as per-shape builders, then one search over a section graph) and the new ranking.
3. Drive mode over sections and their trailheads.

### Phase 1 as built

- Sections are cut from the graph in the network build's transaction
  (`sections.py`; its docstring has each kind's rule and constant). A way may be
  in sections of different kinds.
- Sections are two-level. A **group** is everything that covers one stretch of
  ground, so a riverside path that is a trail system, a water path, and a named
  route at once is listed once; a **stretch** is one cut of a group, the unit a
  runner picks between and routes are built along. Both are rows of
  `net_sections` (`level`, and `group_id` on a stretch), and a survey ranks groups
  (`detail="overview"`) or stretches (`detail="detail"`, optionally `within` one
  group). Drafts of different kinds that share at least half of the shorter one's
  length join one group, transitively. A group stores the union of its drafts' edges,
  every contributing kind, and a `parallel_m` measure of paths running beside each
  other within 200 m, whatever they are named (both banks), which a runner covers
  only one of; its single-path length is the sum of its stretches'. A group's
  approach is its best stretch's, and its familiarity is matched over its whole
  extent. The pieces of one route relation that meet within 200 m along the network
  are joined before merging. Sections under 1.5 miles (or 15% of the run distance,
  if more) are stored but left out of a survey unless asked for.
- Trail is split into `singletrack` (footpath-width natural-surface ways, or
  anything with a hiking or mountain-bike difficulty grade) and `track`
  (`highway=track`, `cycleway`, wide ways, ways with a `tracktype` or a vehicle
  access tag, bicycle-designated graded paths), decided from the way's tags at
  network build and stored on each edge. A survey weighs a track at 0.8 of
  singletrack by default and takes a `surface_preference` to tilt either way.
- The cut keeps waterways, water bodies, parks and protected areas (outer rings,
  thinned), summits and viewpoints, and walking or cycling route relations.
  A tile cut at an older version is re-cut from the registered extract on the
  next rebuild, and never re-downloaded.
- The survey's score is `quality x adjustment - access cost`, in
  quality-miles; `survey.py` holds the formula and weights. Adjustments and
  exclusions are keyed by way ids and re-attach to the rebuilt section of the
  same level and kind that shares at least half of their length in both
  directions. A stretch's adjustment multiplies with its group's (bounded); a
  group's exclusion removes every stretch's ways.
- Familiarity is matched from the raw streams in the background and stored per
  section; a rebuild clears it and every activity is matched again.
- Each group is cut into **stretches** at bridges and tunnels, busy roads,
  trailheads and parking, other junctions, and name changes, in that priority.
  Intervals under 1.5 km merge into a neighbor and those over 6 km split nearest
  the middle. A cut is named for the road that meets it, else the bridge,
  trailhead or parking, park, or differently named way there, else by its
  distance along the path; a role never reads "between X and X". Edges off the
  spine fall in the stretch beside them; a branch of 3 km or more becomes a unit
  of its own, a shorter one is a spur, noted on its stretch ("includes spur to
  ...") and counted in its length. A stretch spans both banks between the same
  cut points and counts parallel paths once. A **mesh of trails** (a trail
  system of mostly natural-surface trail where the longest path is under half
  its length and no banks run beside it) is divided into **regions** instead:
  its edges are cut at access points, roads and hubs into connected parts, small
  parts joined to a neighbor and large ones bisected until each holds 3 to 10 km
  of trail, so a 20-mile system becomes a handful of regions named for their
  main trails. A region's chains are a few trails through it. Judgments,
  familiarity, and the survey's detail view work per stretch.
  `sections.building_blocks` returns stretches as the blocks route assembly runs
  along (ordered edge chains per side, end and entry nodes, the stretch's own
  edge ids; where consecutive stretches meet end to end their primary chains run
  on as one), and `net_edges.structure` keeps the bridge and tunnel marks.
- Route search is unchanged apart from dropping excluded sections' ways from
  its graph; a section steers it only through `include_way_ids`.

### Phase 2 as built

- `assemble.py` plans the blocks a search runs over. With `sections=[ids]` (a group
  or stretch id; a group stands for its stretches) it uses exactly those and never
  falls back to the walk: an excluded section is refused, and a request none of whose
  sections can be reached returns no candidates and a warning. Without it the survey's
  best sections for each start (the first four, eight each) supply the blocks, and the
  walk runs only when no section-built candidate meets the request.
- Every candidate goes through the same pipeline as a walk: `score_path`, the
  road-exposure limit, soft constraints, diversification (a section-built
  candidate's score is scaled to the best in its pool), persistence, and the search
  lock and time budget. The survey behind the plan runs before the lock is taken.
- The graph a search runs on keeps every node of the chosen sections' edges, whatever
  their distance from the start, so a long edge is not lost to the search radius.
- Drive time to a start does not enter the ranking (phase 3).
- Phase 2's per-shape builders (an approach, one chain, a retrace or a tree home) and
  its loop bonus were replaced by phase 2b: they could only score a return as cost,
  and walked it by reversing the path from the start, which fails across a one-way
  edge.

### Phase 2b as built: one search over a section graph

- **One-way tags are read for a runner** (network build): a road ignores `oneway`
  unless `oneway:foot` is set, a path or track follows `oneway:foot` and then `oneway`,
  and `oneway:bicycle` never applies. Every path a search needs is computed in the
  direction of travel (an inbound tree for the way home), never by reversing one.
- **The graph.** Running edges are the pieces (about 250 m) of every edge of the
  chosen stretches, in each direction a runner may take; a piece is worth the
  stretch's adjusted quality per mile (`survey.density` times the section's factor)
  times its miles. Connectors are the preference-weighted shortest paths between hubs
  (stretch entry and end nodes, the start, the stops), charged half the survey's
  per-mile access charge on the weighted length of their miles on no stretch (what
  they earn on stretches is under "Phase 2c"), so a route that goes out and comes back
  pays the survey's charge once. The start and the stops are sinks
  that every hub and every junction of a stretch can connect to, by the cheapest path
  and by the cheapest that avoids the corridor the way out used, so a loop can come
  home by another road. A connector that is only stretches is dropped (the walk runs
  those), as is one with more continuous uncomfortable road than the preferences allow.
- **The search.** A bounded beam search walks the graph in layers of 150 m, keeping
  the best 200 walks (one per node and set of stops passed) by score so far plus an
  optimistic bound: the miles left, less the shortest way home, at the best density
  within reach. A walk is pruned when it can no longer get home inside the range, and
  complete when it is back at the start inside the range (5% slack, scored by the
  distance-fit term outside it) with every stop passed. To return walks that are not
  variations of one, a start is searched five times, each time with the stretches of the
  best walk so far worth 40% of their value. Finished walks are deduplicated by the
  original edges they run (the same run in either direction, or cut at different
  points, is one). Completions and finished walks are taken best of each set of
  stretches first, so turning-point variants of one stretch cannot crowd out the best
  walk over another set. Shape (out-and-back when half the walk repeats, lollipop when it ends
  retracing its start, otherwise loop) is read off the finished walk and only filters
  a request's `shape`.
- **Soft constraints steer the search.** A request for climbing or trail makes the
  search value a stretch more the more it helps (up to double for the climb per mile
  the request needs, plus half for being trail), in the search only: the reported
  score is unsteered. Without it the highest-scoring walks decide, and a request for
  a hilly route gets whatever hill the best walks happen to touch. A climb band's
  top steers the other way: a stretch climbing over it is valued less, to a quarter.
- **Repeats replace the loop bonus.** A piece run a second time earns `repeat_value`
  (default 0.5, a request may set 0 to 1) of its value and a third time or more its
  square, so oscillating on one stretch cannot fill a distance for free.
- **Stops.** `stops: [{lat, lng, label?, at_mi?}]` snap to the network like a start.
  A stop is passed when the walk is at its node, inside a mile either side of `at_mi`
  if given. Each candidate reports the mile it was passed at. A request with stops
  never falls back to the walk.
- **Connector trees are cached** in process per area, graph build and set of avoided
  ways, and per route preferences and terrain model (their edge costs), bounded to
  3 areas and 1000 trees each. Trees from the start and the stops depend on the
  request's own splice of those points into the graph and are computed per request.
- **Ranking, per mile:** `score = (quality - access_cost) / miles x fit x gain_fit x shape`,
  with `quality` the miles run on stretches weighted as above, `fit` 1 inside the range and falling by
  twice the miss as a share of the target outside it. A candidate reports
  `built_from`, `sections_used` (section, stretch and meters, a meter run twice counted
  twice, those with 400 m or more) and these terms (`value_per_mile`,
  `connector_cost_per_mile`, `repeats_effect`, `distance_fit`, `gain_fit`, `shape`, `score`).
  Total quality grew with length, so inside the range the longest walk always won and
  the search padded to the maximum; per mile it must be better, not longer. The beam's
  bound on partial walks stays in total value (what the miles left can still earn);
  finished walks are ordered per mile, as the final ranking is. `repeat_value` still
  discourages retracing, since a repeated mile lowers the average.
- **`shape`** (0.6 to 1.15, multiplicative) is the route's reach (farthest straight-line
  distance from the start as a share of its length, full credit at a quarter) mapped onto
  that range, times one minus the share of its length that runs within 50 m of a part of
  itself at least 400 m away along the route (the route is closed at the start, so the
  distance is taken either way round). Miles run twice are left out of that share, since
  `repeat_value` has charged them. It separates a figure eight, a wiggle or a filler spur
  from an open loop; reach was chosen over enclosed area because a loop's area is
  sensitive to a single detour while its reach is not.
- **The returned candidates differ.** One with more than `SIMILAR_SHARE` (60%) of the
  shorter route's ground within about 30 m of a higher-ranked returned one (a grid-cell
  set per route, whatever ways each runs on) is never returned, so a request for `n` can
  get fewer, with a warning. The search stops collecting once `n * POOL_FACTOR`
  candidates cover distinct ground (none similar to one already counted); variants of
  one loop stay in the pool for the best to win but do not count toward it.
- **Open requests are anchored.** The survey's three best groups each get a beam round,
  after the first, in which their stretches are worth three times as much
  so the pool starts out spread over different parts of the map; later rounds discount
  the best walk as before. Requests naming sections are not anchored.
- **Fallback.** The random walk runs only for a request that names no sections and
  has no stops, when no section-built candidate meets the request; the section search
  then uses 60% of the time budget and leaves the rest to the walk.

### Phase 2c as built: credit for every mile, must-include, purpose

- **Every mile on a stored stretch earns that stretch's adjusted quality**, whether
  the walk runs it as a piece or a connector crosses it, and whether or not the
  request named it. Only miles on no stretch are connector cost. Every stretch of the
  area is a planned block; those chosen to build around (named, or the survey's best)
  supply running pieces and hubs, the rest only credit. A connector's crossing of a
  stretch is counted per piece where the stretch is built around and per edge elsewhere,
  so a repeat earns `repeat_value` by whichever way it was run, and a retrace by
  connector earns no more than one by running. The search and the final ranking share
  one value, so they agree. This replaced the rule that only the named sections earn:
  a loop home by a riverside path the request never named paid for those miles
  and earned nothing, and lost to a retraced out-and-back.
- **`sections=[ids]` means must-include.** A candidate qualifies only if it covers
  `MIN_INCLUDE_M` (400 m) of each named section, or `MIN_INCLUDE_SHARE` (a tenth) of it
  if that is less, counting any mile on it; the whole route is scored. A named section
  is still refused when excluded, and the walk still never fills in.
- **Purpose** (`easy`, `workout`, `long`, `hilly`; none keeps the earlier behavior)
  is a set of terms (`survey.PURPOSES` holds the values): a climb band in feet per mile,
  the value of grind and hiking in runnability, a scale on the continuity term, and one on
  scenery. They change the survey's section quality and so the search's block values;
  the survey also multiplies a section's score by a `gain_fit` of its own climb per
  mile. A route's score is multiplied by the same `gain_fit` of its climb per mile
  (`GAIN_FIT_SLOPE`, `GAIN_FIT_FLOOR`): 1 inside the band and, outside it, falling by
  the miss as a share of the band's nearest edge. A request's own gain range replaces
  the band; with a range and no purpose a route is not scored against it, as before. The
  search carries each walk's climb (half its smoothed vertical travel, as a walk is
  closed) so a finished walk is ordered by the fit, and the reported score uses the
  route's measured gain. The random walk ignores the purpose.
- **A candidate scoring under a quarter of the best** in the pool is offered only
  after everything above it (`DIVERSITY_FLOOR`): the diversity penalty otherwise
  lifted a route that its purpose had scored out, because it shared nothing.

### Background searches as built

- The MCP `find_routes` runs the search in a background thread (`search_jobs.py`),
  followed with `get_search` and stopped with `cancel_search`; the time budget is a
  safety cap.

## Consequences

- Network build takes longer and stores more per area (sections and their
  histograms, plus scenery tags), and every area needs one rebuild when the
  version bumps.
- A survey and a search are cheap at query time: the per-query work is one
  weighted shortest-path pass from the start and a histogram product per
  section.
- Scenery is only as good as OpenStreetMap tagging; a beautiful stretch with
  no nearby water, park, route relation, or viewpoint scores on length,
  continuity, and runnability alone.
- Familiarity needs the athlete's streams; it fills in as they are fetched.

## Open questions

- Segmentation rules: where one section ends and the next begins on long
  multi-use paths, and how a trail system that is one huge connected cluster
  is split into units a person would name.
- How far from water or a viewpoint a section may lie and still carry the tag.
- Whether familiarity should also discount sections run very often, or only
  be offered as a filter.
