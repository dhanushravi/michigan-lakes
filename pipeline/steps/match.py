"""Match step: join access points to lakes, assign our own IDs, apply overrides, write the crosswalk.

Rules (see CLAUDE.md):
  * Access points belong to the region only if they fall inside the region polygon. The source's
    `county` field and the county number inside `legacyid` are logged when they disagree, never used.
  * An access point matches the nearest 3DHP lake polygon within MATCH_DISTANCE_M. Names are never
    used for matching.
  * Hand fixes in data/overrides/match.yml are applied after spatial matching. They are keyed on our
    IDs and carry `check` source IDs; a check that no longer holds fails the run.
  * lake_id / spot_id come from data/crosswalk/crosswalk.csv (committed). A source ID seen before
    keeps its ID. A new source ID first tries to take over a missing record whose anchor point it
    covers, otherwise it gets a new ID.

Inputs:  latest data/raw/<source>/<region>/<date>/ for 3DHP, DNR access, IFR deep points, DNR hydro poly
Outputs: data/build/<region>/lakes.geojson, spots.geojson, match_report.json
         data/crosswalk/crosswalk.csv (updated in place)

Usage:
    python pipeline/steps/match.py [--region oakland]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import pandas as pd
import yaml
from shapely import make_valid
from shapely.geometry import Point
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import (  # noqa: E402
    BUILD_DIR,
    CROSSWALK_DIR,
    OVERRIDES_DIR,
    WORK_CRS,
    load_config,
    load_raw,
    region_polygon,
)

MATCH_DISTANCE_M = 25.0
MIN_LAKE_ACRES = 10.0          # lakes smaller than this get an ID only if an access point matches them
LAKE_FEATURE_TYPE = "Lake"     # 3DHP featuretypelabel for lakes and ponds
ACRES_PER_SQKM = 247.105381
EQUAL_AREA_CRS = 5070          # CONUS Albers, for areas we compute ourselves
LEGACYID_COUNTY = re.compile(r"^[A-Z]-(\d+)-")

CROSSWALK_PATH = CROSSWALK_DIR / "crosswalk.csv"
CROSSWALK_COLUMNS = [
    "entity", "our_id", "source", "source_field", "source_id", "relation",
    "anchor_lon", "anchor_lat", "first_seen", "status",
]
ID_PREFIX = {"lake": "L", "spot": "S"}


# --------------------------------------------------------------------------- crosswalk


class Crosswalk:
    """Our IDs <-> source IDs. Also the registry that keeps our IDs stable across runs."""

    def __init__(self, path: Path, run_date: str):
        self.path = path
        self.run_date = run_date
        if path.exists():
            self.df = pd.read_csv(path, dtype=str, keep_default_na=False)
        else:
            self.df = pd.DataFrame(columns=CROSSWALK_COLUMNS)
        self.seen: set[tuple[str, str, str]] = set()
        self.new_rows: list[dict] = []
        self.reused: list[dict] = []

    def _key(self, r) -> tuple[str, str, str]:
        return (r["source"], r["source_field"], r["source_id"])

    def lookup(self, source: str, field: str, source_id: str) -> str | None:
        rows = [r for r in self._rows() if self._key(r) == (source, field, source_id)]
        return rows[0]["our_id"] if rows else None

    def _rows(self) -> list[dict]:
        return self.df.to_dict("records") + self.new_rows

    def ids_in_use(self, entity: str) -> set[str]:
        return {r["our_id"] for r in self._rows() if r["entity"] == entity}

    def mint(self, entity: str) -> str:
        nums = [int(i[2:]) for i in self.ids_in_use(entity)]
        return f"{ID_PREFIX[entity]}-{(max(nums) + 1 if nums else 1):06d}"

    def link(self, entity: str, our_id: str, source: str, field: str, source_id: str,
             anchor: Point, relation: str = "same") -> None:
        """Record that our_id corresponds to this source record (idempotent)."""
        key = (source, field, source_id)
        self.seen.add(key)
        if any(self._key(r) == key for r in self._rows()):
            return
        lon, lat = anchor_lonlat(anchor)
        self.new_rows.append({
            "entity": entity, "our_id": our_id, "source": source, "source_field": field,
            "source_id": source_id, "relation": relation, "anchor_lon": lon, "anchor_lat": lat,
            "first_seen": self.run_date, "status": "active",
        })

    def missing_candidates(self, entity: str, source: str, field: str, present: set[str]) -> list[dict]:
        """Rows from earlier runs for this source whose source ID is gone from the current data."""
        return [
            r for r in self.df.to_dict("records")
            if r["entity"] == entity and r["source"] == source and r["source_field"] == field
            and r["relation"] == "same" and r["source_id"] not in present
        ]

    def save(self, fetched_sources: set[str]) -> None:
        df = pd.concat([self.df, pd.DataFrame(self.new_rows, columns=CROSSWALK_COLUMNS)], ignore_index=True)
        # Rows for sources fetched this run are active only if seen; other sources keep their status.
        fetched = df["source"].isin(fetched_sources)
        seen = df.apply(lambda r: self._key(r) in self.seen, axis=1)
        df.loc[fetched & seen, "status"] = "active"
        df.loc[fetched & ~seen, "status"] = "missing"
        df = df.sort_values(["entity", "our_id", "source", "source_field", "source_id"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        df[CROSSWALK_COLUMNS].to_csv(self.path, index=False)


def anchor_lonlat(geom) -> tuple[str, str]:
    p = gpd.GeoSeries([geom], crs=WORK_CRS).to_crs(4326).iloc[0]
    return f"{p.x:.6f}", f"{p.y:.6f}"


def anchor_point(row) -> Point:
    return gpd.GeoSeries([Point(float(row["anchor_lon"]), float(row["anchor_lat"]))], crs=4326).to_crs(WORK_CRS).iloc[0]


def assign_ids(xw: Crosswalk, entity: str, items: list[dict], source: str, fields: list[str],
               reuse_within_m: float) -> dict[str, str]:
    """Give every item an our_id. items: [{"key", "ids": {field: value}, "geometry"}].

    Lookup order: any known source ID (fields in order), then a missing record whose anchor lies
    within `reuse_within_m` of the geometry, then a new ID. Returns {item key: our_id}.
    """
    out: dict[str, str] = {}
    present = {f: {str(it["ids"][f]) for it in items if it["ids"].get(f)} for f in fields}
    pending = []
    for it in items:
        found = next(
            (xw.lookup(source, f, str(it["ids"][f])) for f in fields
             if it["ids"].get(f) and xw.lookup(source, f, str(it["ids"][f]))),
            None,
        )
        if found:
            out[it["key"]] = found
        else:
            pending.append(it)

    taken = set(out.values())
    candidates = [c for c in xw.missing_candidates(entity, source, fields[0], present[fields[0]])
                  if c["our_id"] not in taken]
    for it in sorted(pending, key=lambda i: str(i["ids"][fields[0]])):  # stable minting order
        reuse = next((c for c in candidates
                      if c["our_id"] not in taken and anchor_point(c).distance(it["geometry"]) <= reuse_within_m), None)
        if reuse:
            out[it["key"]] = reuse["our_id"]
            xw.reused.append({"our_id": reuse["our_id"], "old_source_id": reuse["source_id"],
                              "new_source_id": str(it["ids"][fields[0]])})
        else:
            out[it["key"]] = xw.mint(entity)
            # Register right away so the next mint sees this ID as used.
            xw.link(entity, out[it["key"]], source, fields[0], str(it["ids"][fields[0]]),
                    it["geometry"].representative_point())
        taken.add(out[it["key"]])

    for it in items:
        for f in fields:
            if it["ids"].get(f):
                xw.link(entity, out[it["key"]], source, f, str(it["ids"][f]), it["geometry"].representative_point())
    return out


# --------------------------------------------------------------------------- geometry


def polygonal(geom):
    """Keep only polygon parts (make_valid can return mixed collections)."""
    geom = make_valid(geom)
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    return unary_union([g for g in getattr(geom, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon")])


def split_geometry(geom, dividers: list) -> list:
    """Partition `geom` into len(dividers) pieces using other polygons as the guide.

    Each piece starts as geom ∩ divider (earlier dividers win overlaps). Leftover areas of geom that
    no divider covers go to the piece they share the most edge with, so the pieces always add up to
    the original geometry, with no slivers lost and no area made up.
    """
    pieces, used = [], None
    for d in dividers:
        p = polygonal(geom.intersection(d))
        if used is not None:
            p = polygonal(p.difference(used))
        pieces.append(p)
        used = p if used is None else unary_union([used, p])
    leftover = polygonal(geom.difference(used))
    for comp in getattr(leftover, "geoms", [leftover]):
        if comp.is_empty:
            continue
        touch = [comp.buffer(0.5).intersection(p).area for p in pieces]
        if max(touch) > 0:
            i = touch.index(max(touch))
        else:
            dist = [comp.distance(p) for p in pieces]
            i = dist.index(min(dist))
        pieces[i] = polygonal(unary_union([pieces[i], comp]))
    return pieces


def acres_equal_area(geom) -> float:
    return float(gpd.GeoSeries([geom], crs=WORK_CRS).to_crs(EQUAL_AREA_CRS).area.iloc[0] / 4046.8564224)


# --------------------------------------------------------------------------- main


def provenance(cfg: dict, source_id: str, manifest: dict) -> dict:
    s = cfg["sources"][source_id]
    return {"source": source_id, "source_date": manifest["source_date"],
            "license": s.get("license", "").strip(), "status": s["status"]}


def check(expected: dict, actual: dict, what: str) -> None:
    bad = {k: (v, actual.get(k)) for k, v in expected.items() if str(actual.get(k)) != str(v)}
    if bad:
        raise RuntimeError(f"override check failed for {what}: expected vs actual {bad}. "
                           "The source or crosswalk changed; review data/overrides/match.yml.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region")
    args = ap.parse_args()

    cfg = load_config()
    region = args.region or cfg["active_region"]
    mi_code = int(cfg["regions"][region]["mi_county_code"])
    poly = region_polygon(cfg, region)
    run_date = dt.date.today().isoformat()
    overrides = yaml.safe_load((OVERRIDES_DIR / "match.yml").read_text()) or {}
    report: dict = {"region": region, "run_date": run_date, "match_distance_m": MATCH_DISTANCE_M,
                    "min_lake_acres": MIN_LAKE_ACRES}

    wb, wb_m = load_raw("usgs_3dhp_waterbody", region)
    acc, acc_m = load_raw("dnr_boating_access", region)
    deep, deep_m = load_raw("ifr_lake_deep_points", region)
    hydro, hydro_m = load_raw("dnr_hydro_poly", region)
    report["inputs"] = {m["source_id"]: {"source_date": m["source_date"], "raw_count": m["feature_count"]}
                        for m in (wb_m, acc_m, deep_m, hydro_m)}

    # ---- access points: region membership by polygon only; log disagreeing source fields
    acc["in_region"] = acc.within(poly)
    acc["legacyid_county"] = acc["legacyid"].str.extract(LEGACYID_COUNTY)[0].astype("Int64")
    county_issues = []
    for _, r in acc.iterrows():
        issues = []
        if not r.in_region and r.county == mi_code:
            issues.append(f"county field is {mi_code} but the point is "
                          f"{r.geometry.distance(poly):.1f} m outside the region polygon; excluded")
        if r.in_region and r.county != mi_code:
            issues.append(f"inside the region polygon but county field is {r.county}; included")
        if pd.notna(r.legacyid_county) and r.legacyid_county != r.county:
            issues.append(f"legacyid county number {r.legacyid_county} != county field {r.county}")
        if issues:
            county_issues.append({"legacyid": r.legacyid, "name": r["name"], "in_region": bool(r.in_region),
                                  "issues": issues})
    report["county_field_issues"] = county_issues
    spots = acc[acc.in_region].copy().reset_index(drop=True)
    report["access_points"] = {"raw_in_bbox": len(acc), "in_region": len(spots),
                               "excluded_outside_region": int((~acc.in_region).sum())}

    # ---- lakes: 3DHP Lake features touching the region
    lakes3 = wb[(wb.featuretypelabel == LAKE_FEATURE_TYPE) & wb.intersects(poly)].copy().reset_index(drop=True)
    lakes3["acres"] = lakes3["areasqkm"] * ACRES_PER_SQKM

    # ---- spatial match: nearest lake polygon within MATCH_DISTANCE_M
    near = gpd.sjoin_nearest(spots[["legacyid", "geometry"]], lakes3[["id3dhp", "geometry"]],
                             max_distance=MATCH_DISTANCE_M, distance_col="distance_m", how="left")
    near = near.sort_values("distance_m").groupby(level=0).agg(
        id3dhp=("id3dhp", "first"), distance_m=("distance_m", "first"),
        candidates=("id3dhp", lambda s: list(s.dropna())))
    spots = spots.join(near)
    report["ambiguous_matches"] = [
        {"legacyid": r.legacyid, "candidates": r.candidates} for _, r in spots.iterrows() if len(r.candidates) > 1
    ]

    matched_3dhp = set(spots["id3dhp"].dropna())
    lakes3 = lakes3[(lakes3.acres >= MIN_LAKE_ACRES) | lakes3.id3dhp.isin(matched_3dhp)].reset_index(drop=True)

    # ---- our IDs
    xw = Crosswalk(CROSSWALK_PATH, run_date)
    lake_ids = assign_ids(
        xw, "lake",
        [{"key": r.id3dhp, "ids": {"id3dhp": r.id3dhp}, "geometry": r.geometry} for r in lakes3.itertuples()],
        "usgs_3dhp_waterbody", ["id3dhp"], reuse_within_m=0.0)
    spot_ids = assign_ids(
        xw, "spot",
        [{"key": r.legacyid, "ids": {"legacyid": r.legacyid, "globalid": r.globalid}, "geometry": r.geometry}
         for r in spots.itertuples()],
        "dnr_boating_access", ["legacyid", "globalid"], reuse_within_m=MATCH_DISTANCE_M)

    lakes = gpd.GeoDataFrame({
        "lake_id": lakes3.id3dhp.map(lake_ids),
        "id3dhp": lakes3.id3dhp,
        "gnis_name": lakes3.gnisidlabel,
        "acres": lakes3.acres.round(1),
        "area_source": "usgs_3dhp_waterbody.areasqkm",
        "geometry_source": "usgs_3dhp_waterbody",
        "override_name": None,
    }, geometry=lakes3.geometry.values, crs=WORK_CRS)
    spots["spot_id"] = spots.legacyid.map(spot_ids)
    spots["lake_id"] = spots.id3dhp.map(lake_ids)
    spots["match_method"] = spots.lake_id.notna().map({True: "spatial", False: None})

    # ---- overrides, applied after matching
    applied = []
    for o in overrides.get("split_lake") or []:
        row = lakes[lakes.lake_id == o["lake_id"]]
        if len(row) != 1:
            raise RuntimeError(f"split_lake: lake_id {o['lake_id']} not found")
        row = row.iloc[0]
        check(o.get("check", {}), {"id3dhp": row.id3dhp}, f"split_lake {o['lake_id']}")
        dividers = []
        for p in o["parts"]:
            h = hydro[hydro.GlobalID == p["dnr_hydro_globalid"]]
            if len(h) != 1:
                raise RuntimeError(f"split_lake: DNR hydro poly {p['dnr_hydro_globalid']} not found")
            dividers.append(h.geometry.iloc[0])
        pieces = split_geometry(row.geometry, dividers)
        new_rows = []
        for p, piece in zip(o["parts"], pieces):
            if p.get("keep_lake_id"):
                pid = row.lake_id
            else:
                pid = xw.lookup("dnr_hydro_poly", "GlobalID", p["dnr_hydro_globalid"]) or xw.mint("lake")
            xw.link("lake", pid, "dnr_hydro_poly", "GlobalID", p["dnr_hydro_globalid"], piece.representative_point())
            if not p.get("keep_lake_id"):
                xw.link("lake", pid, "usgs_3dhp_waterbody", "id3dhp:part", row.id3dhp,
                        piece.representative_point(), relation="part_of")
            new_rows.append({"lake_id": pid, "id3dhp": row.id3dhp, "gnis_name": row.gnis_name,
                             "acres": round(acres_equal_area(piece), 1),
                             "area_source": f"computed ({EQUAL_AREA_CRS}) from split",
                             "geometry_source": "usgs_3dhp_waterbody split by dnr_hydro_poly",
                             "override_name": p["name"], "geometry": piece})
        lakes = pd.concat([lakes[lakes.lake_id != row.lake_id],
                           gpd.GeoDataFrame(new_rows, crs=WORK_CRS)], ignore_index=True)
        # Spots that matched the merged polygon move to whichever part is nearest.
        parts = gpd.GeoDataFrame(new_rows, crs=WORK_CRS)
        for i in spots.index[spots.lake_id == row.lake_id]:
            d = parts.distance(spots.geometry[i])
            spots.loc[i, ["lake_id", "distance_m"]] = [parts.lake_id[d.idxmin()], float(d.min())]
        applied.append({"type": "split_lake", "lake_id": row.lake_id,
                        "parts": {r["lake_id"]: f"{r['override_name']} ({r['acres']} ac)" for r in new_rows}})

    for o in overrides.get("spot_lake") or []:
        idx = spots.index[spots.spot_id == o["spot_id"]]
        if len(idx) != 1:
            raise RuntimeError(f"spot_lake: spot_id {o['spot_id']} not found")
        i = idx[0]
        target = lakes[lakes.lake_id == o["lake_id"]]
        if len(target) != 1:
            raise RuntimeError(f"spot_lake: lake_id {o['lake_id']} not found")
        check(o.get("check", {}), {"legacyid": spots.legacyid[i], "id3dhp": target.id3dhp.iloc[0]},
              f"spot_lake {o['spot_id']}")
        before = spots.lake_id[i]
        spots.loc[i, ["lake_id", "match_method"]] = [o["lake_id"], "override"]
        spots.loc[i, "distance_m"] = float(target.distance(spots.geometry[i]).iloc[0])
        applied.append({"type": "spot_lake", "spot_id": o["spot_id"], "lake_id": o["lake_id"],
                        "spatial_result": before, "changed": before != o["lake_id"]})
    report["overrides_applied"] = applied

    # ---- depth from IFR deep points (point in final lake polygon), minus hand-excluded points
    excluded = {o["NewKey"]: o["reason"] for o in overrides.get("exclude_ifr_deep_point") or []}
    missing = set(excluded) - set(deep.NewKey)
    if missing:
        raise RuntimeError(f"exclude_ifr_deep_point: {sorted(missing)} not in the IFR data; review the override")
    deep = deep[~deep.NewKey.isin(excluded)]
    applied += [{"type": "exclude_ifr_deep_point", "NewKey": k} for k in excluded]
    dj = gpd.sjoin(deep, lakes[["lake_id", "geometry"]], predicate="within", how="left")
    for r in dj.dropna(subset=["lake_id"]).itertuples():
        xw.link("lake", r.lake_id, "ifr_lake_deep_points", "NewKey", r.NewKey, r.geometry)
    depth = dj.dropna(subset=["lake_id"]).groupby("lake_id").agg(
        max_depth_ft=("MaxDepth", "max"), ifr_names=("LakeName", lambda s: sorted(set(s.dropna()))),
        ifr_keys=("NewKey", lambda s: sorted(s)))
    lakes = lakes.merge(depth, left_on="lake_id", right_index=True, how="left")
    in_region_deep = deep[deep.within(poly)]
    report["ifr_deep_points"] = {"in_region": len(in_region_deep),
                                 "inside_a_lake": int(dj.lake_id.notna().sum()),
                                 "lakes_with_more_than_one": [k for k, v in depth.ifr_keys.items() if len(v) > 1]}

    # ---- names: override > 3DHP GNIS > DNR access waterbody (if one) > IFR (if one); never invented
    dnr_names = spots.dropna(subset=["lake_id"]).groupby("lake_id")["waterbody"].agg(lambda s: sorted(set(s)))

    def pick_name(r):
        if isinstance(r.override_name, str):
            return r.override_name, "override"
        if isinstance(r.gnis_name, str):
            return r.gnis_name, "usgs_3dhp_waterbody.gnisidlabel"
        d = dnr_names.get(r.lake_id, [])
        if len(d) == 1:
            return d[0], "dnr_boating_access.waterbody"
        f = r.ifr_names if isinstance(r.ifr_names, list) else []
        if len(f) == 1:
            return f[0], "ifr_lake_deep_points.LakeName"
        return None, None

    picked = lakes.apply(pick_name, axis=1, result_type="expand")
    lakes["name"], lakes["name_source"] = picked[0], picked[1]

    def aliases(r):
        names = set(dnr_names.get(r.lake_id, [])) | set(r.ifr_names if isinstance(r.ifr_names, list) else [])
        # A split part inherits the merged polygon's GNIS name, which names a different lake.
        if isinstance(r.gnis_name, str) and r.name_source == "override" and "split" not in r.geometry_source:
            names.add(r.gnis_name)
        return sorted(n for n in names if n and n != r["name"])

    lakes["aliases"] = lakes.apply(aliases, axis=1)

    # Spots whose DNR waterbody name differs from their lake's name and that no override has reviewed.
    reviewed = {o["spot_id"] for o in overrides.get("spot_lake") or []}
    lake_name = dict(zip(lakes.lake_id, lakes["name"]))
    report["name_conflicts_unreviewed"] = [
        {"spot_id": r.spot_id, "legacyid": r.legacyid, "dnr_waterbody": r.waterbody, "lake_id": r.lake_id,
         "lake_name": lake_name.get(r.lake_id)}
        for r in spots.dropna(subset=["lake_id"]).itertuples()
        if r.waterbody != lake_name.get(r.lake_id) and r.spot_id not in reviewed
    ]

    # ---- summary
    n_spots = spots.dropna(subset=["lake_id"]).groupby("lake_id").size()
    lakes["n_spots"] = lakes.lake_id.map(n_spots).fillna(0).astype(int)
    report["summary"] = {
        "lakes_with_id": len(lakes),
        "lakes_10ac_plus": int((lakes.acres >= MIN_LAKE_ACRES).sum()),
        "lakes_with_access": int((lakes.n_spots > 0).sum()),
        "lakes_10ac_plus_with_access": int(((lakes.acres >= MIN_LAKE_ACRES) & (lakes.n_spots > 0)).sum()),
        "spots_matched": int(spots.lake_id.notna().sum()),
        "spots_unmatched": spots.loc[spots.lake_id.isna(), ["spot_id", "legacyid", "name", "waterbody"]]
        .to_dict("records"),
        "max_match_distance_m": round(float(spots.loc[spots.match_method == "spatial", "distance_m"].max()), 1),
    }
    report["id_reuse_by_anchor"] = xw.reused

    # ---- write
    lake_prov = provenance(cfg, "usgs_3dhp_waterbody", wb_m)
    spot_prov = provenance(cfg, "dnr_boating_access", acc_m)
    out_lakes = lakes[["lake_id", "name", "name_source", "aliases", "acres", "area_source", "max_depth_ft",
                       "ifr_keys", "id3dhp", "geometry_source", "n_spots", "geometry"]].copy()
    out_lakes["max_depth_ft"] = out_lakes.max_depth_ft.astype("Int64")
    for k, v in lake_prov.items():
        out_lakes[k] = v
    out_spots = spots[["spot_id", "lake_id", "name", "waterbody", "waterbodytype", "legacyid", "globalid",
                       "match_method", "distance_m", "geometry"]].copy()
    out_spots["distance_m"] = out_spots.distance_m.astype(float).round(1)
    for k, v in spot_prov.items():
        out_spots[k] = v

    out_dir = BUILD_DIR / region
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, gdf in (("lakes", out_lakes), ("spots", out_spots)):
        gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=WORK_CRS).to_crs(4326)
        (out_dir / f"{name}.geojson").write_text(gdf.sort_values(f"{name[:-1]}_id").to_json(na="null", drop_id=True))
    (out_dir / "match_report.json").write_text(json.dumps(report, indent=2, default=str))
    xw.save({"usgs_3dhp_waterbody", "dnr_boating_access", "ifr_lake_deep_points", "dnr_hydro_poly"})

    s = report["summary"]
    print(f"region {region}: {s['lakes_with_id']} lakes with IDs, {s['spots_matched']}/{len(spots)} access points "
          f"matched (max {s['max_match_distance_m']} m), {s['lakes_with_access']} lakes with access "
          f"({s['lakes_10ac_plus_with_access']} of {s['lakes_10ac_plus']} at 10+ acres)")
    print(f"overrides applied: {len(applied)}; county field issues: {len(county_issues)}; "
          f"unreviewed name conflicts: {len(report['name_conflicts_unreviewed'])}")
    print(f"wrote {out_dir.relative_to(BUILD_DIR.parents[1])}/ and {CROSSWALK_PATH.relative_to(BUILD_DIR.parents[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
