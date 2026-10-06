"""Review render: a plain HTML page per lake with its outline and access points, for checking the
match against a satellite view. Not the site's lake card; nothing here is styled or simplified.

The outline is the exact match-step polygon (no simplification), drawn in a local azimuthal
equidistant projection centred on the lake, north up, with a scale bar. Pins use the same transform.
Each pin links out to Google Maps satellite view at its coordinates (a link only; nothing is fetched).

Usage:
    python pipeline/review/render_outline.py L-000337 L-000290 [--region oakland]
Writes data/build/<region>/review/<lake_id>.html
"""

from __future__ import annotations

import argparse
import html
import math
import sys
from pathlib import Path

import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import BUILD_DIR, load_config  # noqa: E402

SIZE = 800      # viewBox width and height
PAD = 40
NONE_MAPPED = "none mapped"


def satellite_url(lat: float, lon: float, zoom: int = 18) -> str:
    return (f"https://www.google.com/maps/@?api=1&map_action=map&center={lat:.6f},{lon:.6f}"
            f"&zoom={zoom}&basemap=satellite")


def as_list(v) -> list:
    """List-valued GeoJSON properties come back as list, numpy array or None."""
    return [] if v is None else list(v)


def rings(geom):
    for poly in getattr(geom, "geoms", [geom]):
        yield poly.exterior
        yield from poly.interiors


def render_lake_html(lake: gpd.GeoSeries, spots: gpd.GeoDataFrame) -> str:
    """lake: one row of lakes.geojson (EPSG:4326). spots: that lake's rows of spots.geojson."""
    c = lake.geometry.centroid
    local = f"+proj=aeqd +lat_0={c.y} +lon_0={c.x} +units=m +datum=WGS84"
    g = gpd.GeoSeries([lake.geometry], crs=4326).to_crs(local).iloc[0]
    pins = spots.to_crs(local) if len(spots) else spots

    xmin, ymin, xmax, ymax = g.bounds
    scale = (SIZE - 2 * PAD) / max(xmax - xmin, ymax - ymin)   # px per metre
    ox = PAD + ((SIZE - 2 * PAD) - (xmax - xmin) * scale) / 2
    oy = PAD + ((SIZE - 2 * PAD) - (ymax - ymin) * scale) / 2

    def tx(x, y):  # projected metres -> SVG px, y axis flipped so north is up
        return ox + (x - xmin) * scale, SIZE - (oy + (y - ymin) * scale)

    path = " ".join(
        "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in (tx(x, y) for x, y in r.coords)) + " Z"
        for r in rings(g)
    )
    n_islands = sum(len(p.interiors) for p in getattr(g, "geoms", [g]))

    # Scale bar: a round length about a quarter of the drawing.
    target = (SIZE - 2 * PAD) / 4 / scale
    step = 10 ** math.floor(math.log10(target))
    bar_m = max(s for s in (step, 2 * step, 5 * step) if s <= target)
    bar_px = bar_m * scale
    bar_label = f"{bar_m / 1000:g} km" if bar_m >= 1000 else f"{bar_m:g} m"

    pin_svg, rows = [], []
    for i, (s, p) in enumerate(zip(spots.itertuples(), pins.geometry), start=1):
        px, py = tx(p.x, p.y)
        pin_svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="6" fill="#d00" stroke="#fff" stroke-width="2"/>'
                       f'<text x="{px + 9:.1f}" y="{py + 4:.1f}" font-size="14" font-weight="bold">{i}</text>')
        lat, lon = s.geometry.y, s.geometry.x
        rows.append(
            f"<tr><td>{i}</td><td>{s.spot_id}</td><td>{html.escape(s.name)}</td><td>{html.escape(s.waterbody)}</td>"
            f"<td>{s.legacyid}</td><td>{lat:.6f}, {lon:.6f}</td><td>{s.match_method}</td><td>{s.distance_m}</td>"
            f'<td><a href="{satellite_url(lat, lon)}">satellite</a></td></tr>'
        )

    def fact(v, missing="not recorded"):
        return html.escape(str(v)) if v is not None and v == v and v != [] else missing

    access = (
        "<table border=1 cellpadding=4><tr><th>#</th><th>spot_id</th><th>DNR name</th><th>DNR waterbody</th>"
        "<th>legacyid</th><th>lat, lon</th><th>match</th><th>distance to shore (m)</th><th>link</th></tr>"
        + "".join(rows) + "</table>"
        if rows else f"<p><b>Access points: {NONE_MAPPED}</b> (DNR boating access sites, Waterways Program "
                     "inventory). This is not a statement that the lake has no public access.</p>"
    )
    depth = f"{int(lake.max_depth_ft)} ft" if lake.max_depth_ft == lake.max_depth_ft and lake.max_depth_ft is not None else "not recorded"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(str(lake['name']))} ({lake.lake_id}) review</title></head>
<body style="font-family:sans-serif;max-width:900px;margin:16px">
<h1>{fact(lake['name'], 'Unnamed lake')} <small>{lake.lake_id}</small></h1>
<p>Review render for checking against satellite view. North is up. Outline is the exact polygon
(not simplified); white areas inside are islands (even-odd fill).
<a href="{satellite_url(c.y, c.x, 15)}">Open lake centre in Google Maps satellite</a>.</p>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}" style="border:1px solid #999;background:#fff">
<path d="{path}" fill="#9cc7e8" fill-rule="evenodd" stroke="#1d5f91" stroke-width="1"/>
{''.join(pin_svg)}
<g transform="translate({PAD},{SIZE - 16})"><line x1="0" y1="0" x2="{bar_px:.1f}" y2="0" stroke="#000" stroke-width="3"/>
<text x="0" y="-6" font-size="13">{bar_label}</text></g>
<text x="{SIZE - 30}" y="30" font-size="16" text-anchor="middle">N</text>
<path d="M{SIZE - 30},36 l-7,18 l7,-5 l7,5 z" fill="#000"/>
</svg>
<h2>Access points</h2>
{access}
<h2>Lake facts</h2>
<table border=1 cellpadding=4>
<tr><td>name source</td><td>{fact(lake.name_source)}</td></tr>
<tr><td>other names in sources</td><td>{', '.join(as_list(lake.aliases)) or 'none'}</td></tr>
<tr><td>acres</td><td>{lake.acres} ({fact(lake.area_source)})</td></tr>
<tr><td>max depth</td><td>{depth} (IFR deep points: {', '.join(as_list(lake.ifr_keys)) or 'none'})</td></tr>
<tr><td>islands (polygon holes)</td><td>{n_islands}</td></tr>
<tr><td>geometry</td><td>{fact(lake.geometry_source)}, 3DHP id3dhp {lake.id3dhp}</td></tr>
<tr><td>provenance</td><td>{lake.source}, {lake.source_date}, status {lake.status}; access points: dnr_boating_access, status {spots.status.iloc[0] if len(spots) else 'n/a'}</td></tr>
</table>
</body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lake_ids", nargs="+")
    ap.add_argument("--region")
    args = ap.parse_args()
    region = args.region or load_config()["active_region"]
    build = BUILD_DIR / region
    lakes = gpd.read_file(build / "lakes.geojson")
    spots = gpd.read_file(build / "spots.geojson")
    out = build / "review"
    out.mkdir(parents=True, exist_ok=True)
    for lid in args.lake_ids:
        row = lakes[lakes.lake_id == lid]
        if len(row) != 1:
            raise SystemExit(f"lake_id {lid} not found in {build / 'lakes.geojson'}")
        path = out / f"{lid}.html"
        path.write_text(render_lake_html(row.iloc[0], spots[spots.lake_id == lid].sort_values("spot_id")))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
