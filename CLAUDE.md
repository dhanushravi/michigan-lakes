# Michigan Lakes Portal

A free, ad-free, open-source guide to Michigan lakes and the nature spots around them. It helps people find where to go for sunsets, sunrises, quiet water and public access. The site is static: a Python pipeline builds files, the browser does the rest. Nothing runs on a server per visit.

Pilot region: **[FILL IN BEFORE STARTING]** (pick somewhere the owner can visit, so results can be checked on the ground).

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
| DNR Institute for Fisheries Research (IFR) lake data | Area, depth, polygons for lakes 10+ acres | v1 | Verified; depth known for only ~2,176 lakes, so treat depth as optional |
| DNR Parks and Recreation boating access sites | Access points and launches | v1 | Verified to exist (updated Jan 2026). **Terms pending**: mark `pending-confirmation` |
| Protomaps basemap (OSM-derived) | Map background as one PMTiles file | v1 | Verified; self-host glyph and sprite assets |
| DNR trails | Trails | v2 | Verified; includes motorized trails (filter out); results capped per query |
| OpenStreetMap features | Benches, viewpoints, toilets, beaches | v4 | Coverage not measured |
| DNR restroom layer | none | not used | Stale (catalog record retired, last modified 2019) |
| Google Maps photos | none | not used | API terms bar caching and storing; link out only |

Notes:
- The exact layer URLs are not recorded here. Find them in the State of Michigan Open Data Portal (search the layer names above) and record them in `pipeline/sources.yml`.
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
  raw/               # downloads (gitignored; never commit)
docs/
```

Stack: Python (GeoPandas, Shapely, DuckDB), Node for the site, Tippecanoe and PMTiles command-line tools for map files. Site framework is not decided (Astro is the current lean).

## Pipeline stages

fetch (paged, dated raw files) -> change check (stop early if unchanged) -> clean (one CRS, tidy names) -> match (join access points to lakes by spatial join within a buffer; assign own IDs; apply overrides) -> enrich (outline SVG, sunset and sunrise view, drive times) -> quality gates -> publish.

Quality gates: row counts within a set swing, geometries valid and inside Michigan, match rate above a threshold, no place loses its lake.

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
