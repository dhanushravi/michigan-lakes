"""Shared helpers for pipeline steps: config, raw-file lookup, region boundary."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCES_YML = ROOT / "pipeline" / "sources.yml"
RAW_DIR = ROOT / "data" / "raw"
BUILD_DIR = ROOT / "data" / "build"
OVERRIDES_DIR = ROOT / "data" / "overrides"
CROSSWALK_DIR = ROOT / "data" / "crosswalk"

# Michigan GeoRef (metres). Used for all distance work inside the state.
WORK_CRS = 3078
SQM_PER_ACRE = 4046.8564224


def load_config() -> dict:
    return yaml.safe_load(SOURCES_YML.read_text())


def latest_raw_dir(source_id: str, region: str) -> Path:
    """Newest dated fetch directory for a source and region."""
    base = RAW_DIR / source_id / region
    dated = sorted(p for p in base.glob("*") if p.is_dir())
    if not dated:
        raise FileNotFoundError(f"no raw fetch for {source_id}/{region}; run pipeline/steps/fetch.py")
    return dated[-1]


def load_raw(source_id: str, region: str) -> tuple[gpd.GeoDataFrame, dict]:
    """Latest raw features in WORK_CRS, plus that fetch's manifest (empty for the boundary source)."""
    d = latest_raw_dir(source_id, region)
    gdf = gpd.read_file(d / "features.geojson").to_crs(WORK_CRS)
    manifest_path = d / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"source_date": d.name}
    return gdf, manifest


def region_polygon(cfg: dict, region: str):
    """The region boundary as a single shapely geometry in WORK_CRS."""
    boundary, _ = load_raw(cfg["regions"][region]["boundary_source"], region)
    if len(boundary) != 1:
        raise RuntimeError(f"expected one boundary polygon for {region}, got {len(boundary)}")
    return boundary.geometry.iloc[0]
