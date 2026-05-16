# Source code

Everything needed to regenerate `index.html` and `data.json` from the City of Toronto's public datasets.

## Reproduce in three steps

```bash
# 1. only dependency
pip install requests

# 2. regenerate the dashboard
python3 src/build_dashboard.py

# 3. (optional) also regenerate the hourly time-of-day baseline
#    requires a one-time ~130 MB download of the City's 2023 hourly archive;
#    output is ./src/data/processed/hourly_profiles.json — re-runs are cached.
#    pi5@hq → ~3 minutes; modern laptop → ~30 seconds.
python3 src/hourly.py
python3 src/build_dashboard.py   # re-run to bake the hourly data in
```

That's it. `index.html` and `data.json` will be rewritten in the repo root with current data. No build step, no minifiers, no bundlers — the page is a single self-contained file with two CDN dependencies (ECharts + Leaflet).

## What each file does

| File | Purpose |
|---|---|
| `build_dashboard.py` | Entry point. Orchestrates fetch → aggregate → render. |
| `ckan.py` | Tiny CKAN client; downloads CSVs from Toronto Open Data with `last_modified` cache check. |
| `aggregate.py` | All the math: per-sign metric, volume-weighted ward/city rollups, difference-in-differences with the 2024 seasonal control, YoY tiered speeder counts, top-mover rankings. |
| `render.py` | Builds the inline summary JSON and the lazy-loaded `data.json` sidecar; writes the final HTML. |
| `hourly.py` | Streams the 2023 hourly archive (~2 GB uncompressed) into per-sign weekday/weekend hour-of-day profiles. Optional — the main dashboard works without it. |
| `templates/index.html.tmpl` | The single-file dashboard template. ECharts + Leaflet + Markercluster via CDN; no build step. |

## Methodology, in code

If you want to verify a specific number on the page, the cleanest path is to grep `aggregate.py`. Every metric on the dashboard is computed by a function whose name maps to a section of the page — `compute_pct_10over`, `compute_prepost`, `compute_yoy_segments`, `aggregate_distribution`, `top_yoy_increases`, etc. The math is plain Python stdlib (no pandas, no numpy) so it should read end-to-end in a single sitting.

## Data caching

`build_dashboard.py` caches CSV downloads under `.cache/raw/` and skips re-downloading if the upstream resource's `last_modified` timestamp is older than the local file. Force a refresh with `--force-refresh`; use cached data only with `--skip-fetch`.

## Toronto Open Data dependencies

The CKAN package IDs are hard-coded in `build_dashboard.py`. If the City rotates dataset IDs, edit the `DATASETS` dict at the top.

## Why no dependencies

`requests` is the only third-party package, and it's used only for the CKAN downloads. CSV parsing is stdlib `csv`; aggregations are stdlib `collections`; JSON output is stdlib `json`. The choice keeps the reproduction barrier as low as physically possible — `apt install python3-requests` on any Debian / Raspberry Pi OS works without a venv.

## Licence

MIT — see `../LICENSE`.
