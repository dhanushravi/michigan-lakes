# Michigan Lakes Portal

A free, ad-free, open-source guide to Michigan lakes and the nature spots around them. It helps people find where to go for sunsets, sunrises, quiet water and public access. The site is static: a Python pipeline builds files, the browser does the rest. Nothing runs on a server per visit.

Pilot region: Oakland County, Michigan

## Scope

**v1 (build this):** lake pages for one region (50-100 lakes), search and filters, sunset and sunrise direction with today's times, drive time from anchor cities, public access and boat launch info, a simple map with access pins.

**Not in v1:** OpenStreetMap features (benches, viewpoints, restrooms), trails, visitor tips and photos, the full-screen feature map, statewide coverage. Later releases: v2 statewide plus DNR trails; v3 visitor tips and photos; v4 detailed map.

## Principles

- **Static first.** No always-on database or API. Build files, serve files.
- **Compute at build time.** Sunset direction, view scores, outlines and drive times are precomputed. Sunset time and straight-line distance are computed in the browser.
- **Own stable IDs.** Every lake and place gets our own `lake_id` / `spot_id`, with a crosswalk table to each source's IDs. Never key anything on a source ID.
- **Overrides survive reruns.** Hand fixes live in `data/overrides/` (committed) and are applied after matching.
- **Provenance on every record:** `source`, `source_date`, `license`, `status`.
- **Quality gates fail the run.** A failed gate keeps the last published version live.
- **No ads, no tracking, no trackers.** Keep costs near zero.

## Data sources

| Source | Use | Release | Status |
| --- | --- | --- | --- |
| USGS 3DHP | Lake polygons (replaces retired NHD) | v1 | Verified to exist; confirm current download format |
| IFR MI Lake Deep Points (DNR Institute for Fisheries Research) | Max depth, as points (one per lake, ~2,176 lakes) | v1 | Verified; treat depth as optional. Not polygons: join by point-in-polygon |
| DNR Reference Hydro Poly | DNR lake polygons with Acres (digitized from 1978 USGS quads) | v1 | Verified; used to split 3DHP polygons that merge separate lakes |
| DNR boating access sites (Waterways Program inventory) | Access points and launches | v1 | Verified (updated Jan 2026). **Not a complete list of public access**: DNR-run sites plus local sites funded by Waterways grants only. **Terms pending**: mark `pending-confirmation` |
| Protomaps basemap (OSM-derived) | Map background as one PMTiles file | v1 | Verified; self-host glyph and sprite assets |
| DNR trails | Trails | v2 | Verified; includes motorized trails (filter out); results capped per query |
| OpenStreetMap features | Benches, viewpoints, toilets, beaches | v4 | Coverage not measured |
| DNR restroom layer | none | not used | Stale (catalog record retired, last modified 2019) |
| Google Maps photos | none | not used | API terms bar caching and storing; link out only |

Notes:
- Layer URLs, licenses and statuses are recorded in `pipeline/sources.yml`.
- The IFR "lake data" is two separate layers (Lake Deep Points and Reference Hydro Poly); there is no single IFR layer with area, depth and polygons.
- Because the access layer is the Waterways Program inventory, a lake with no access point shows "none mapped", never "no public access".
- DNR ArcGIS layers cap results per query. Page through results using the layer's `maxRecordCount` (check each layer's metadata).
- The DNR access layer's attribute fields have **not** been checked. Inspect the field list on first fetch before designing facility chips.
- While a source is `pending-confirmation`: use it freely for local development, but the public dataset export must skip it.

## Repo layout

```
pipeline/
  sources.yml        # URL, refresh cadence, license, status per source
  steps/             # fetch, clean, match, enrich, export
site/                # static front end: lake page, card, simple map
data/
  overrides/         # hand fixes (committed)
  crosswalk/         # our IDs <-> source IDs (committed; keeps IDs stable)
  raw/               # downloads (gitignored; never commit)
  build/             # step outputs and review renders (gitignored)
docs/
```

Stack: Python (GeoPandas, Shapely, DuckDB), Node for the site, Tippecanoe and PMTiles command-line tools for map files. Site framework is not decided (Astro is the current lean).

## Pipeline stages

fetch (paged, dated raw files) -> change check (stop early if unchanged) -> clean (one CRS, tidy names) -> match (join access points to lakes by spatial join within a buffer; assign own IDs; apply overrides) -> enrich (outline SVG, sunset and sunrise view, drive times) -> quality gates -> publish.

Quality gates: row counts within a set swing, geometries valid and inside Michigan, match rate above a threshold, no place loses its lake.

### Matching rules

- Region membership is by the region polygon only. The access layer's `county` field and the county number in `legacyid` are logged when they disagree, never used to include or exclude.
- An access point matches the nearest 3DHP `Lake` polygon within 25 m. Never match by name.
- Lakes get a `lake_id` if they are 10+ acres or have a matched access point. IDs live in `data/crosswalk/crosswalk.csv` (committed), mapped to 3DHP `id3dhp`, DNR `legacyid`/`globalid`, IFR `NewKey` and, for split lakes, DNR hydro `GlobalID`.
- Hand fixes are in `data/overrides/match.yml`, keyed on our IDs, with `check` source IDs that fail the run if they drift. Pilot test lakes are in `pipeline/pilot_lakes.yml`.

## Pilot findings: Oakland County (fetched 2026-10-06)

- DNR access: 39 sites inside the county (38 on lakes, 1 on the Huron River). All 38 lake sites are within 25 m of a 3DHP polygon (max 24.3 m); widening to 200 m adds nothing.
- 3DHP: 2,075 `Lake` polygons touch the county; 421 are 10+ acres; only 347 have a name.
- **35 lakes have at least one access point; 32 of the 421 lakes of 10+ acres do** (Shoe, Heart and Chamberlain are under 10 acres).
- Large lakes with no DNR site (the layer omits Metroparks and county parks): Kent (1,040 ac), Walled (653), Otter, Stony Creek, Lake Angelus, Pine, Upper Straits, Elizabeth. Walled Lake is the "none mapped" test lake.
- Davison Lake (`A-44-004`) is coded county 63 but sits 6 m outside the Oakland polygon and its `legacyid` says county 44 (Lapeer). Excluded by the clip and logged.
- Name conflicts (resolved by override; display name follows 3DHP): DNR Heron Lake = 3DHP Wildwood Lake; DNR Paint Lake = 3DHP Tan Lake; DNR Big Seven Lake = 3DHP Seven Lakes.
- 3DHP merges some separate lakes into one polygon. Maceday and Lotus are split using DNR Hydro Poly. Tan Lake is a chain of six DNR basins and is not split yet. 16 lakes contain more than one IFR deep point; each is a candidate merged polygon to review.
- IFR deep point 63-854 (Lotus Lake) duplicates 63-856 (Maceday Lake): same coordinates, same 117 ft. Excluded, so Lotus depth is not recorded.

### Sunset and sunrise view

For each access point: find the nearest shoreline segment, compute the direction it faces over the water, compare with the sun's azimuth range, and measure open water ahead along that bearing (ray cast through the lake polygon). Store a 1-5 score. Do not hardcode azimuths: compute them from latitude with a sun-position library. As a sanity check, at about 43 N sunset runs from roughly 237 degrees (December) to 303 degrees (June) and sunrise from roughly 57 to 123 degrees. This is an open-water estimate only; it does not see trees or hills (elevation data is a later addition).

### Outline SVG

Per lake: reproject to a flat local projection, simplify the polygon (looser tolerance for big lakes), fit into a fixed `viewBox` with padding, flip the y axis, write an SVG path string. Handle islands (polygon holes, use an even-odd fill rule) and multi-part lakes. Apply the same transform to access-point pins. Store the string as `outline` in the lake's JSON.

### Outputs

One small JSON file per lake, one compact search index for all lakes, and a PMTiles file for the map. Drive time comes from a local routing engine run for about 15 anchor cities at build time.

## First tasks (one lake, end to end)

1. Fetch DNR access sites and 3DHP lake polygons for the pilot county (raw files into `data/raw/`, paged).
2. Match each access point to its lake; assign `lake_id` and `spot_id`; write the crosswalk.
3. Compute the sunset score for one access point. Show the result with the lake outline so it can be checked against a satellite view and a site visit.
4. Export one lake's JSON and render it on a plain, unstyled page showing: name, outline, access points, sunset and sunrise score.

Success means the facts are correct, not that the page looks good. The designed lake card comes after the data is proven.

## Do not

- Add a server, database or API that runs per visit.
- Commit raw downloads or large generated files to git.
- Use Google Maps photos through their API; link out instead.
- Publish any `pending-confirmation` source in the public dataset.
- Invent data. Show a field only where the source has it; say "none mapped", never "none here".

## References

- Architecture plan doc and lake card design canvas live in Claude.ai (shared by the project owner). Ask for exported copies if needed.
- Lake page shape: header with outline, sunset and sunrise panel, size and depth stats, facility chips, simple map, one photo slot with a link to Google Maps, an empty tips slot (v3), and a data credit line.
