#!/usr/bin/env python3
"""Generate GeoJSON and PMTiles footprints for Planet Open Data STAC catalogs."""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CATALOG_URL = "https://www.planet.com/data/stac/catalog.json"
USER_AGENT = "planet-open-data-footprints/1.0"
CATALOG_RELS = {"child", "collection"}
ITEM_RELS = {"item"}
FEATURE_COLLECTION_RELS = {"items", "next"}


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    title: str
    href: str
    slug: str


@dataclass
class CollectionContext:
    id: str | None
    title: str | None
    href: str


@dataclass
class ItemLink:
    href: str
    title: str | None
    collection: CollectionContext | None


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fetch_json(url: str, retries: int = 3, timeout: int = 60) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, application/geo+json;q=0.9, */*;q=0.1",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            return json.loads(payload)
        except (
            json.JSONDecodeError,
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            last_error = error
            if attempt == retries:
                break
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(
        f"Failed to fetch JSON after {retries} attempts: {url}"
    ) from last_error


def absolute_url(href: str, base_url: str) -> str:
    return urllib.parse.urljoin(base_url, href)


def slugify(value: str) -> str:
    slug = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            slug.append(char)
            previous_dash = False
        elif not previous_dash:
            slug.append("-")
            previous_dash = True
    return "".join(slug).strip("-") or "catalog"


def bbox_to_polygon(bbox: list[float] | tuple[float, ...]) -> dict[str, Any] | None:
    if len(bbox) >= 6:
        west, south, east, north = bbox[0], bbox[1], bbox[3], bbox[4]
    elif len(bbox) >= 4:
        west, south, east, north = bbox[0], bbox[1], bbox[2], bbox[3]
    else:
        return None

    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


def geometry_points(geometry: dict[str, Any] | None) -> list[tuple[float, float]]:
    if not geometry:
        return []

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "GeometryCollection":
        points: list[tuple[float, float]] = []
        for child in geometry.get("geometries", []):
            points.extend(geometry_points(child))
        return points

    points = []

    def collect(value: Any) -> None:
        if (
            isinstance(value, list)
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            points.append((float(value[0]), float(value[1])))
            return
        if isinstance(value, list):
            for item in value:
                collect(item)

    collect(coordinates)
    return points


def geometry_bounds(geometry: dict[str, Any] | None) -> list[float] | None:
    points = geometry_points(geometry)
    if not points:
        return None

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def merge_bounds(
    current: list[float] | None, bbox: list[float] | None
) -> list[float] | None:
    if bbox is None:
        return current
    if current is None:
        return list(bbox)
    return [
        min(current[0], bbox[0]),
        min(current[1], bbox[1]),
        max(current[2], bbox[2]),
        max(current[3], bbox[3]),
    ]


def item_bbox(
    item: dict[str, Any], geometry: dict[str, Any] | None
) -> list[float] | None:
    bbox = item.get("bbox")
    if isinstance(bbox, list) and len(bbox) >= 4:
        if len(bbox) >= 6:
            return [bbox[0], bbox[1], bbox[3], bbox[4]]
        return bbox[:4]
    return geometry_bounds(geometry)


def compact_properties(
    item: dict[str, Any],
    href: str,
    catalog: CatalogEntry,
    collection: CollectionContext | None,
    link_title: str | None,
) -> dict[str, Any]:
    item_properties = item.get("properties") or {}
    assets = item.get("assets") or {}
    return {
        "id": item.get("id"),
        "title": item_properties.get("title") or link_title or item.get("id"),
        "datetime": item_properties.get("datetime"),
        "start_datetime": item_properties.get("start_datetime"),
        "end_datetime": item_properties.get("end_datetime"),
        "collection": item.get("collection") or (collection.id if collection else None),
        "collection_title": collection.title if collection else None,
        "catalog_id": catalog.id,
        "catalog_title": catalog.title,
        "stac_href": href,
        "asset_count": len(assets),
        "asset_keys": sorted(assets.keys()),
    }


def item_to_feature(
    item: dict[str, Any],
    href: str,
    catalog: CatalogEntry,
    collection: CollectionContext | None,
    link_title: str | None = None,
) -> dict[str, Any] | None:
    geometry = item.get("geometry")
    if geometry is None:
        bbox = item.get("bbox")
        if isinstance(bbox, list):
            geometry = bbox_to_polygon(bbox)
    if geometry is None:
        return None

    feature = {
        "type": "Feature",
        "id": item.get("id") or href,
        "geometry": geometry,
        "properties": compact_properties(item, href, catalog, collection, link_title),
    }

    bbox = item_bbox(item, geometry)
    if bbox:
        feature["bbox"] = bbox

    return feature


def feature_href(item: dict[str, Any], base_url: str) -> str:
    for link in item.get("links", []):
        if link.get("rel") == "self" and link.get("href"):
            return absolute_url(link["href"], base_url)
    return str(item.get("id") or base_url)


def root_children(root: dict[str, Any], root_url: str) -> list[CatalogEntry]:
    children = []
    used_slugs: dict[str, int] = {}
    for link in root.get("links", []):
        if link.get("rel") not in CATALOG_RELS or not link.get("href"):
            continue

        href = absolute_url(link["href"], root_url)
        title = link.get("title") or Path(urllib.parse.urlparse(href).path).parent.name
        path = Path(urllib.parse.urlparse(href).path)
        catalog_id = slugify(path.parent.name or path.stem or title)
        slug = slugify(title or catalog_id)
        used_slugs[slug] = used_slugs.get(slug, 0) + 1
        if used_slugs[slug] > 1:
            slug = f"{slug}-{used_slugs[slug]}"
        children.append(CatalogEntry(id=catalog_id, title=title, href=href, slug=slug))

    if not children:
        title = root.get("title") or root.get("id") or "Planet STAC"
        children.append(
            CatalogEntry(
                id=str(root.get("id") or "planet"),
                title=str(title),
                href=root_url,
                slug=slugify(str(title)),
            )
        )

    return children


def link_context(document: dict[str, Any], url: str) -> CollectionContext | None:
    if document.get("type") != "Collection":
        return None
    return CollectionContext(
        id=document.get("id"),
        title=document.get("title") or document.get("id"),
        href=url,
    )


def discover_items(
    catalog: CatalogEntry,
    *,
    max_items: int | None = None,
) -> tuple[list[ItemLink], list[dict[str, Any]], list[CollectionContext]]:
    pending = [catalog.href]
    visited_catalogs: set[str] = set()
    seen_item_hrefs: set[str] = set()
    item_links: list[ItemLink] = []
    embedded_features: list[dict[str, Any]] = []
    collections: list[CollectionContext] = []

    while pending:
        url = pending.pop(0)
        if url in visited_catalogs:
            continue
        visited_catalogs.add(url)

        document = fetch_json(url)
        document_type = document.get("type")

        if document_type == "Feature":
            if url not in seen_item_hrefs:
                seen_item_hrefs.add(url)
                feature = item_to_feature(document, url, catalog, None)
                if feature:
                    embedded_features.append(feature)
            continue

        if document_type == "FeatureCollection":
            for item in document.get("features", []):
                if item.get("type") != "Feature":
                    continue
                item_url = feature_href(item, url)
                if item_url in seen_item_hrefs:
                    continue
                seen_item_hrefs.add(item_url)
                feature = item_to_feature(item, item_url, catalog, None)
                if feature:
                    embedded_features.append(feature)
            for link in document.get("links", []):
                if link.get("rel") == "next" and link.get("href"):
                    pending.append(absolute_url(link["href"], url))
            continue

        current_collection = link_context(document, url)
        if current_collection:
            collections.append(current_collection)

        for link in document.get("links", []):
            rel = link.get("rel")
            href = link.get("href")
            if not href:
                continue

            child_url = absolute_url(href, url)
            if rel in CATALOG_RELS:
                pending.append(child_url)
            elif rel in FEATURE_COLLECTION_RELS:
                pending.append(child_url)
            elif rel in ITEM_RELS:
                if child_url in seen_item_hrefs:
                    continue
                seen_item_hrefs.add(child_url)
                item_links.append(
                    ItemLink(
                        href=child_url,
                        title=link.get("title"),
                        collection=current_collection,
                    )
                )
                if max_items is not None and len(item_links) >= max_items:
                    return item_links, embedded_features, collections

    return item_links, embedded_features, collections


def fetch_item_feature(
    item_link: ItemLink, catalog: CatalogEntry
) -> dict[str, Any] | None:
    item = fetch_json(item_link.href)
    if item.get("type") == "FeatureCollection":
        features = item.get("features") or []
        if not features:
            return None
        item = features[0]
    if item.get("type") != "Feature":
        return None
    return item_to_feature(
        item, item_link.href, catalog, item_link.collection, item_link.title
    )


def feature_collection(features: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": name,
        "features": features,
    }


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(document, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(path)


def freestiler_available() -> bool:
    return importlib.util.find_spec("freestiler") is not None


def run_freestiler(
    input_path: Path, output_path: Path, layer: str, min_zoom: int, max_zoom: int
) -> None:
    from freestiler import freestile_file

    output_path.parent.mkdir(parents=True, exist_ok=True)
    freestile_file(
        str(input_path),
        str(output_path),
        layer_name=layer,
        tile_format="mvt",
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        overwrite=True,
        quiet=True,
        engine="duckdb",
    )


def prune_previous_outputs(output_dir: Path) -> None:
    manifest_path = output_dir / "catalogs.json"
    if not manifest_path.exists():
        return

    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return

    for catalog in previous.get("catalogs", []):
        slug = catalog.get("slug")
        if not slug:
            continue
        path = output_dir / slug
        if path.is_dir():
            shutil.rmtree(path)

    for filename in ("catalog-footprints.geojson", "catalog-footprints.pmtiles"):
        path = output_dir / filename
        if path.exists():
            path.unlink()


def generate(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    root = fetch_json(args.catalog_url)
    catalogs = root_children(root, args.catalog_url)
    if args.only_catalog:
        only = {slugify(value) for value in args.only_catalog}
        catalogs = [
            catalog
            for catalog in catalogs
            if catalog.slug in only
            or slugify(catalog.title) in only
            or slugify(catalog.id) in only
        ]

    if args.prune:
        prune_previous_outputs(output_dir)

    has_freestiler = freestiler_available()
    if not has_freestiler and not args.skip_pmtiles:
        message = "freestiler is not available; GeoJSON files will be written without PMTiles."
        if args.require_freestiler:
            raise RuntimeError(message)
        log(message)

    manifest_catalogs = []
    summary_features = []

    for catalog in catalogs:
        log(f"Discovering {catalog.title} ({catalog.href})")
        item_links, embedded_features, collections = discover_items(
            catalog,
            max_items=args.max_items,
        )
        log(f"Fetching {len(item_links)} linked items for {catalog.slug}")

        features = list(embedded_features)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers
        ) as executor:
            future_to_link = {
                executor.submit(fetch_item_feature, item_link, catalog): item_link
                for item_link in item_links
            }
            for future in concurrent.futures.as_completed(future_to_link):
                item_link = future_to_link[future]
                try:
                    feature = future.result()
                except Exception as error:  # noqa: BLE001
                    log(f"Warning: skipped {item_link.href}: {error}")
                    continue
                if feature:
                    features.append(feature)

        features.sort(key=lambda feature: str(feature.get("id") or ""))
        catalog_dir = output_dir / catalog.slug
        geojson_path = catalog_dir / "footprints.geojson"
        pmtiles_path = catalog_dir / "footprints.pmtiles"
        write_json(
            geojson_path, feature_collection(features, f"{catalog.slug}-footprints")
        )

        if has_freestiler and not args.skip_pmtiles:
            log(f"Writing {pmtiles_path}")
            run_freestiler(
                geojson_path, pmtiles_path, "footprints", args.min_zoom, args.max_zoom
            )

        bounds = None
        for feature in features:
            bounds = merge_bounds(
                bounds, feature.get("bbox") or geometry_bounds(feature.get("geometry"))
            )

        summary_geometry = bbox_to_polygon(bounds) if bounds else None
        if summary_geometry:
            summary_features.append(
                {
                    "type": "Feature",
                    "id": catalog.slug,
                    "geometry": summary_geometry,
                    "bbox": bounds,
                    "properties": {
                        "id": catalog.id,
                        "title": catalog.title,
                        "slug": catalog.slug,
                        "href": catalog.href,
                        "item_count": len(features),
                        "collection_count": len(collections),
                        "geojson": str(geojson_path.as_posix()),
                        "pmtiles": (
                            str(pmtiles_path.as_posix())
                            if has_freestiler and not args.skip_pmtiles
                            else None
                        ),
                    },
                }
            )

        manifest_catalogs.append(
            {
                "id": catalog.id,
                "title": catalog.title,
                "slug": catalog.slug,
                "href": catalog.href,
                "item_count": len(features),
                "collection_count": len(collections),
                "geojson": str(geojson_path.as_posix()),
                "pmtiles": (
                    str(pmtiles_path.as_posix())
                    if has_freestiler and not args.skip_pmtiles
                    else None
                ),
            }
        )
        log(f"Wrote {len(features)} features to {geojson_path}")

    summary_path = output_dir / "catalog-footprints.geojson"
    write_json(
        summary_path,
        feature_collection(summary_features, "planet-open-data-catalog-footprints"),
    )

    if has_freestiler and not args.skip_pmtiles:
        summary_pmtiles_path = output_dir / "catalog-footprints.pmtiles"
        log(f"Writing {summary_pmtiles_path}")
        run_freestiler(
            summary_path,
            summary_pmtiles_path,
            "catalog_footprints",
            args.min_zoom,
            args.summary_max_zoom,
        )

    write_json(
        output_dir / "catalogs.json",
        {
            "source_catalog": args.catalog_url,
            "catalog_count": len(manifest_catalogs),
            "summary_geojson": str(summary_path.as_posix()),
            "summary_pmtiles": (
                str((output_dir / "catalog-footprints.pmtiles").as_posix())
                if has_freestiler and not args.skip_pmtiles
                else None
            ),
            "catalogs": manifest_catalogs,
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate GeoJSON and PMTiles footprints from the Planet Open Data STAC catalog."
    )
    parser.add_argument("--catalog-url", default=DEFAULT_CATALOG_URL)
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--min-zoom", type=int, default=0)
    parser.add_argument("--max-zoom", type=int, default=8)
    parser.add_argument("--summary-max-zoom", type=int, default=1)
    parser.add_argument("--only-catalog", action="append", default=[])
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--skip-pmtiles", action="store_true")
    parser.add_argument("--require-freestiler", action="store_true")
    parser.add_argument(
        "--require-tippecanoe",
        dest="require_freestiler",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--no-prune", dest="prune", action="store_false")
    parser.set_defaults(prune=True)
    return parser.parse_args()


def main() -> int:
    try:
        generate(parse_args())
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001
        log(f"Error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
