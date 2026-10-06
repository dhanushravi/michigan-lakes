"""Fetch step: download each `fetch: true` source for the active region, page by page.

Writes, per source:
    data/raw/<source_id>/<region>/<YYYY-MM-DD>/features.geojson   all pages merged, EPSG:4326
    data/raw/<source_id>/<region>/<YYYY-MM-DD>/layer.json         layer metadata snapshot (fields etc.)
    data/raw/<source_id>/<region>/<YYYY-MM-DD>/manifest.json      url, query, counts, pages, sha256

The server-side filter is the region boundary's bounding box. Clipping to the exact
boundary happens in the clean step, so the raw file is exactly what the source returned.

Usage:
    python pipeline/steps/fetch.py [--region oakland] [--only dnr_boating_access ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCES_YML = ROOT / "pipeline" / "sources.yml"
RAW_DIR = ROOT / "data" / "raw"

USER_AGENT = "michigan-lakes-portal/0.1 (open-source, non-commercial)"
TIMEOUT = 120
RETRIES = 4

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def get_json(url: str, params: dict) -> dict:
    """GET an ArcGIS REST endpoint, retrying transient failures. ArcGIS reports errors in a 200 body."""
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"ArcGIS error from {url}: {body['error']}")
            return body
        except (requests.RequestException, ValueError, RuntimeError) as e:
            if attempt == RETRIES:
                raise
            wait = 2**attempt
            print(f"  retry {attempt}/{RETRIES - 1} in {wait}s: {e}", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def region_bbox(cfg: dict, region_key: str) -> tuple[list[float], dict]:
    """Return the region boundary's [xmin, ymin, xmax, ymax] in EPSG:4326, plus the boundary feature."""
    region = cfg["regions"][region_key]
    src = cfg["sources"][region["boundary_source"]]
    fc = get_json(
        src["url"] + "/query",
        {"where": region["boundary_where"], "outFields": "*", "outSR": 4326, "f": "geojson"},
    )
    if len(fc["features"]) != 1:
        raise RuntimeError(f"expected 1 boundary for {region_key}, got {len(fc['features'])}")
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for sub in c:
                walk(sub)

    walk(fc["features"][0]["geometry"]["coordinates"])
    return [min(xs), min(ys), max(xs), max(ys)], fc


def fetch_layer(url: str, bbox: list[float], page_size_cap: int | None) -> tuple[dict, dict, dict]:
    """Page through a layer inside bbox. Returns (feature_collection, layer_metadata, query_info)."""
    meta = get_json(url, {"f": "json"})
    max_rc = meta.get("maxRecordCount") or 1000
    page_size = min(max_rc, page_size_cap) if page_size_cap else max_rc
    if not (meta.get("advancedQueryCapabilities") or {}).get("supportsPagination", False):
        raise RuntimeError(f"{url} does not support pagination; add a fallback before fetching it")
    oid = meta.get("objectIdField") or next(
        f["name"] for f in meta["fields"] if f["type"] == "esriFieldTypeOID"
    )

    spatial = {
        "geometry": ",".join(f"{v:.8f}" for v in bbox),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "where": "1=1",
    }
    expected = get_json(url + "/query", {**spatial, "returnCountOnly": "true", "f": "json"})["count"]

    features, pages, offset = [], 0, 0
    while True:
        page = get_json(
            url + "/query",
            {
                **spatial,
                "outFields": "*",
                "outSR": 4326,
                "orderByFields": oid,  # stable order so offsets do not skip or repeat rows
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "f": "geojson",
            },
        )
        batch = page.get("features", [])
        features.extend(batch)
        pages += 1
        print(f"  page {pages}: +{len(batch)} (total {len(features)}/{expected})")
        more = page.get("exceededTransferLimit") or page.get("properties", {}).get("exceededTransferLimit")
        if not batch or (not more and len(batch) < page_size):
            break
        offset += len(batch)

    ids = [f.get("id") for f in features]
    if len(set(ids)) != len(ids):
        raise RuntimeError(f"{url}: duplicate object IDs across pages; paging is unstable")
    if len(features) != expected:
        raise RuntimeError(f"{url}: fetched {len(features)} features but the server counted {expected}")

    query = {"spatial_filter": spatial, "page_size": page_size, "pages": pages, "expected_count": expected}
    return {"type": "FeatureCollection", "features": features}, meta, query


def write_json(path: Path, obj) -> str:
    data = json.dumps(obj, separators=(",", ":")).encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region")
    ap.add_argument("--only", nargs="*", help="source ids to fetch (default: all with fetch: true)")
    args = ap.parse_args()

    cfg = yaml.safe_load(SOURCES_YML.read_text())
    region_key = args.region or cfg["active_region"]
    today = dt.date.today().isoformat()
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    print(f"region: {region_key}")
    bbox, boundary = region_bbox(cfg, region_key)
    print(f"bbox: {bbox}")
    bdir = RAW_DIR / cfg["regions"][region_key]["boundary_source"] / region_key / today
    bdir.mkdir(parents=True, exist_ok=True)
    write_json(bdir / "features.geojson", boundary)

    wanted = [
        sid for sid, s in cfg["sources"].items()
        if s.get("fetch") is True and (not args.only or sid in args.only)
    ]
    for sid in wanted:
        src = cfg["sources"][sid]
        print(f"\n{sid}: {src['url']}")
        fc, meta, query = fetch_layer(src["url"], bbox, src.get("max_record_count"))
        out = RAW_DIR / sid / region_key / today
        out.mkdir(parents=True, exist_ok=True)
        sha = write_json(out / "features.geojson", fc)
        write_json(out / "layer.json", meta)
        manifest = {
            "source_id": sid,
            "url": src["url"],
            "region": region_key,
            "region_bbox_4326": bbox,
            "fetched_at": fetched_at,
            "source_date": today,
            "license": src.get("license"),
            "status": src.get("status"),
            "feature_count": len(fc["features"]),
            "sha256": sha,
            **query,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"  wrote {out.relative_to(ROOT)} ({len(fc['features'])} features)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
