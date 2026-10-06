"""Checks on the match step: unit tests for ID assignment and polygon splitting, plus checks of the
Oakland build output against the pilot list. Build-output tests skip if match.py has not been run.

Run: .venv/bin/python -m pytest tests/
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
import yaml
from shapely.geometry import Point, box

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


match = _load("match", ROOT / "pipeline" / "steps" / "match.py")
render = _load("render_outline", ROOT / "pipeline" / "review" / "render_outline.py")

BUILD = ROOT / "data" / "build" / "oakland"
PILOT = yaml.safe_load((ROOT / "pipeline" / "pilot_lakes.yml").read_text())


# --------------------------------------------------------------------------- unit tests


def test_split_geometry_parts_add_up_to_original():
    lake = box(0, 0, 100, 50)
    # Dividers cover most of each half, leave a gap in the middle and stick out past the lake.
    left, right = box(-10, -10, 45, 60), box(55, -10, 110, 60)
    parts = match.split_geometry(lake, [left, right])
    assert sum(p.area for p in parts) == pytest.approx(lake.area)
    assert parts[0].intersection(parts[1]).area == pytest.approx(0)
    assert all(p.within(lake.buffer(1e-6)) for p in parts)


def _items(ids_and_points):
    return [{"key": k, "ids": {"id3dhp": k}, "geometry": box(x, y, x + 10, y + 10)} for k, (x, y) in ids_and_points]


def test_assign_ids_stable_and_reused_by_anchor(tmp_path):
    # Coordinates are WORK_CRS metres somewhere in Michigan.
    path = tmp_path / "crosswalk.csv"
    first = _items([("A", (500000, 300000)), ("B", (501000, 300000))])
    xw = match.Crosswalk(path, "2026-01-01")
    ids1 = match.assign_ids(xw, "lake", first, "usgs_3dhp_waterbody", ["id3dhp"], reuse_within_m=0)
    xw.save({"usgs_3dhp_waterbody"})
    assert ids1 == {"A": "L-000001", "B": "L-000002"}

    # Rerun with the same data: same IDs, crosswalk unchanged.
    before = path.read_text()
    xw = match.Crosswalk(path, "2026-02-01")
    assert match.assign_ids(xw, "lake", first, "usgs_3dhp_waterbody", ["id3dhp"], reuse_within_m=0) == ids1
    xw.save({"usgs_3dhp_waterbody"})
    assert path.read_text() == before

    # Source renumbers B as B2 at the same place, and adds C: B2 keeps B's ID, C gets a new one.
    second = _items([("A", (500000, 300000)), ("B2", (501000, 300000)), ("C", (502000, 300000))])
    xw = match.Crosswalk(path, "2026-03-01")
    ids3 = match.assign_ids(xw, "lake", second, "usgs_3dhp_waterbody", ["id3dhp"], reuse_within_m=0)
    assert ids3 == {"A": "L-000001", "B2": "L-000002", "C": "L-000003"}
    xw.save({"usgs_3dhp_waterbody"})
    df = pd.read_csv(path)
    assert df.set_index("source_id").loc["B", "status"] == "missing"


# --------------------------------------------------------------------------- build output

needs_build = pytest.mark.skipif(not (BUILD / "lakes.geojson").exists(), reason="run pipeline/steps/match.py first")


@pytest.fixture(scope="module")
def build():
    return {
        "lakes": gpd.read_file(BUILD / "lakes.geojson"),
        "spots": gpd.read_file(BUILD / "spots.geojson"),
        "report": json.loads((BUILD / "match_report.json").read_text()),
        "crosswalk": pd.read_csv(ROOT / "data" / "crosswalk" / "crosswalk.csv", dtype=str),
    }


@needs_build
def test_spatial_matches_within_distance(build):
    s = build["spots"]
    spatial = s[s.match_method == "spatial"]
    assert (spatial.distance_m <= match.MATCH_DISTANCE_M).all()
    assert s.lake_id.isna().sum() == len(build["report"]["summary"]["spots_unmatched"])


@needs_build
def test_davison_excluded_and_logged(build):
    assert "A-44-004" not in set(build["spots"].legacyid)
    issue = next(i for i in build["report"]["county_field_issues"] if i["legacyid"] == "A-44-004")
    assert not issue["in_region"]
    assert any("legacyid county number 44" in m for m in issue["issues"])


@needs_build
@pytest.mark.parametrize("pilot", PILOT["lakes"], ids=lambda p: p["name"])
def test_pilot_lake(build, pilot):
    row = build["lakes"][build["lakes"].lake_id == pilot["lake_id"]]
    assert len(row) == 1, "pilot lake_id missing from build"
    row = row.iloc[0]
    assert row["name"] == pilot["name"]
    assert row.id3dhp == pilot["id3dhp"]
    assert row.n_spots == pilot["expect_spots"]
    assert (build["spots"].lake_id == pilot["lake_id"]).sum() == pilot["expect_spots"]


@needs_build
def test_walled_lake_renders_none_mapped(build):
    lake = build["lakes"][build["lakes"]["name"] == "Walled Lake"].iloc[0]
    page = render.render_lake_html(lake, build["spots"][build["spots"].lake_id == lake.lake_id])
    assert "Access points: none mapped" in page
    assert "none here" not in page.lower()


@needs_build
def test_name_conflict_overrides(build):
    s = build["spots"].set_index("legacyid")
    for legacyid, lake_id in [("A-63-028", "L-000290"), ("A-63-013", "L-000130"), ("A-63-035", "L-000417")]:
        assert s.loc[legacyid, "lake_id"] == lake_id
        assert s.loc[legacyid, "match_method"] == "override"
    assert build["report"]["name_conflicts_unreviewed"] == []


@needs_build
def test_maceday_lotus_split(build):
    L = build["lakes"].set_index("name")
    mac, lot = L.loc["Maceday Lake"], L.loc["Lotus Lake"]
    assert mac.lake_id == "L-000016" and lot.lake_id != mac.lake_id
    assert mac.acres + lot.acres == pytest.approx(415.3, abs=1.0)   # parts add up to the 3DHP polygon
    assert mac.max_depth_ft == 117 and pd.isna(lot.max_depth_ft)     # 63-854 excluded as a duplicate
    assert "Maceday Lake" not in list(lot.aliases)
    assert (build["spots"].lake_id == lot.lake_id).sum() == 0


@needs_build
def test_crosswalk_integrity(build):
    xw = build["crosswalk"]
    assert not xw.duplicated(["source", "source_field", "source_id"]).any()
    same = xw[(xw.source == "usgs_3dhp_waterbody") & (xw.relation == "same") & (xw.status == "active")]
    assert not same.duplicated("our_id").any(), "a lake maps to two 3DHP polygons"
    for kind, df in (("lake", build["lakes"]), ("spot", build["spots"])):
        assert set(df[f"{kind}_id"]) <= set(xw[xw.entity == kind].our_id)
    assert set(build["spots"].legacyid) <= set(xw[xw.source_field == "legacyid"].source_id)
