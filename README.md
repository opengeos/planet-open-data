# planet-stac-catalogs

Footprints of Planet Open Data STAC catalogs.

## Generate footprints

The generator reads the Planet STAC root catalog at
`https://www.planet.com/data/stac/catalog.json`, follows each top-level child
catalog recursively, and writes:

- `data/<catalog-slug>/footprints.geojson`: item footprints for that catalog
- `data/<catalog-slug>/footprints.pmtiles`: PMTiles for the same footprints
- `data/catalog-footprints.geojson`: one bounding footprint per top-level catalog
- `data/catalog-footprints.pmtiles`: PMTiles for the summary catalog footprints
- `data/catalogs.json`: manifest of generated catalogs and output paths

Run locally with GeoJSON only:

```bash
python scripts/generate_planet_footprints.py --skip-pmtiles
```

Run locally with PMTiles if `freestiler` is installed:

```bash
python -m pip install freestiler
python scripts/generate_planet_footprints.py --output-dir data
```

By default the script writes catalog item PMTiles from zoom `0-8` and the
summary catalog-footprint PMTiles from zoom `0-1`. Those conservative defaults
keep the footprint tiles compact; higher zooms duplicate polygon geometry across
many tiles and can make PMTiles larger than the source GeoJSON. Override these with
`--min-zoom`, `--max-zoom`, and `--summary-max-zoom`.

For a quick smoke test:

```bash
python scripts/generate_planet_footprints.py \
  --skip-pmtiles \
  --only-catalog "Planet Disaster Data (small sample)" \
  --max-items 5
```

## Daily updates

The GitHub Actions workflow in `.github/workflows/update-footprints.yml` runs
daily at 06:17 UTC and can also be triggered manually. It installs
`freestiler`, regenerates GeoJSON and PMTiles, and commits changes under
`data/` when the STAC footprints change.
