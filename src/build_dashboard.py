#!/usr/bin/env python3
"""Build the Toronto Watch Your Speed dashboard.

  python3 tools/watch_your_speed/build_dashboard.py [--force-refresh] [--skip-fetch]

Fetches three Toronto Open Data CKAN datasets (cached locally with
last-modified checks), aggregates them, and writes:

  output/reports/watch_your_speed/index.html
  output/reports/watch_your_speed/data.json
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a script from anywhere.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ckan import CKANClient                       # noqa: E402
from aggregate import (                            # noqa: E402
    aggregate_city, aggregate_distribution, aggregate_sign, aggregate_sign_volume,
    aggregate_ward, aggregate_yoy_city, all_months, compute_prepost,
    compute_yoy_segments, load_ase, load_monthly, load_signs,
    top_movers, top_yoy_increases,
)
from render import build_full, build_summary, latest_bins_per_sign, render  # noqa: E402
import hourly as _hourly                            # noqa: E402

# Layout-aware paths so this same file works in two places:
#  1. The author's dev tree (./tools/watch_your_speed/) — writes generated files
#     to ../../output/reports/watch_your_speed/ and ../../publish/toronto-speeding-tracker/
#  2. A standalone clone of the published repo (./src/) — writes index.html and
#     data.json directly to the parent directory (the repo root).
_IN_DEV_TREE = HERE.parent.name == "tools" and HERE.name == "watch_your_speed"
if _IN_DEV_TREE:
    REPO_ROOT = HERE.parent.parent
    CACHE_DIR = HERE / "data" / "raw"
    OUTPUT_DIR = REPO_ROOT / "output" / "reports" / "watch_your_speed"
    PUBLISH_DIR = REPO_ROOT / "publish" / "toronto-speeding-tracker"
else:
    REPO_ROOT = HERE.parent
    CACHE_DIR = REPO_ROOT / ".cache" / "raw"
    OUTPUT_DIR = REPO_ROOT
    PUBLISH_DIR = None  # nothing to stage when already standalone

DATASETS = {
    "monthly": ("safety-zone-watch-your-speed-program-monthly-summary",
                "c567e7f6-3686-439e-b740-dbd91725de2d"),
    "locations": ("school-safety-zone-watch-your-speed-program-locations",
                  "4e2221b9-da3a-4ef8-b8eb-17e95b7abaa0"),
    "ase": ("automated-speed-enforcement-locations",
            "e25e9460-a0e8-469c-b9fb-9a4837ac6c1c"),
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force-refresh", action="store_true", help="bypass the CSV cache")
    p.add_argument("--skip-fetch", action="store_true", help="use cached CSVs only; fail if missing")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("wys")

    if args.skip_fetch:
        for kind, (slug, _rid) in DATASETS.items():
            if not (CACHE_DIR / f"{slug}.csv").exists():
                log.error("--skip-fetch but cache missing for %s", slug)
                return 2
        paths = {kind: CACHE_DIR / f"{slug}.csv" for kind, (slug, _rid) in DATASETS.items()}
    else:
        client = CKANClient(CACHE_DIR, force_refresh=args.force_refresh)
        paths = {}
        for kind, (slug, rid) in DATASETS.items():
            paths[kind] = client.fetch_csv(slug, rid)

    log.info("loading signs")
    signs = load_signs(paths["locations"])
    log.info("loading ASE")
    ase = load_ase(paths["ase"])
    log.info("loading monthly rows + computing metric")
    rows = load_monthly(paths["monthly"], signs)
    if not rows:
        log.error("no monthly rows after loading — aborting")
        return 3
    months = all_months(rows)
    log.info("month coverage: %s .. %s (%d months)", months[0], months[-1], len(months))

    log.info("aggregating city / wards / signs")
    city = aggregate_city(rows)
    wards = aggregate_ward(rows)
    signs_monthly = aggregate_sign(rows)
    signs_volume = aggregate_sign_volume(rows)

    log.info("computing pre/post difference-in-differences")
    prepost = compute_prepost(city, wards, signs_monthly, signs)
    log.info("city headline: pre=%.2f post=%.2f Δ=%s DiD=%s",
             prepost["city"]["pre_2025"] or float("nan"),
             prepost["city"]["post_2025"] or float("nan"),
             prepost["city"]["delta_2025"],
             prepost["city"]["did"])

    log.info("computing pre/post speed distributions")
    distribution = aggregate_distribution(paths["monthly"], signs)

    log.info("ranking top movers")
    movers = top_movers(prepost["signs"], signs, n=20)

    log.info("extracting latest-month bin histograms")
    bins = latest_bins_per_sign(paths["monthly"], signs)

    log.info("loading hourly profiles (2023 baseline)")
    hourly_profiles = _hourly.load_profiles()
    if hourly_profiles:
        log.info("  %d signs in hourly profile", len(hourly_profiles.get("signs", {})))
    else:
        log.info("  no hourly cache — run hourly.py to generate it")

    log.info("computing YoY tiered speeder counts (Jan-Apr 2026 vs 2025)")
    yoy_per_sign = compute_yoy_segments(paths["monthly"], signs)
    yoy_city = aggregate_yoy_city(yoy_per_sign)
    yoy_top = {t: top_yoy_increases(yoy_per_sign, signs, tier=t, n=20) for t in (10, 20, 30)}
    log.info("  city 10+over: last=%s this=%s Δ=%s (%.1f%%)",
             f"{yoy_city['tiers'][10]['last']:,}",
             f"{yoy_city['tiers'][10]['this']:,}",
             f"{yoy_city['tiers'][10]['delta']:+,}",
             yoy_city['tiers'][10]['pct_change'] or 0)

    log.info("building payloads")
    summary = build_summary(signs, rows, ase, city, wards, signs_monthly,
                            prepost, months, distribution, movers, hourly_profiles,
                            yoy_city=yoy_city, yoy_top=yoy_top)
    full = build_full(signs_monthly, signs_volume, wards, months, bins,
                      hourly_profiles, yoy_per_sign=yoy_per_sign)

    log.info("rendering HTML to %s", OUTPUT_DIR)
    index_path, data_path = render(summary, full, OUTPUT_DIR)
    log.info("wrote %s (%.1f KB)", index_path, index_path.stat().st_size / 1024)
    log.info("wrote %s (%.1f KB)", data_path, data_path.stat().st_size / 1024)
    log.info("done — open %s in a browser", (PUBLISH_DIR or OUTPUT_DIR) / "index.html")

    # Stage the publish/ directory for GitHub Pages.
    if PUBLISH_DIR is not None:
        import shutil
        PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(index_path, PUBLISH_DIR / "index.html")
        shutil.copy2(data_path, PUBLISH_DIR / "data.json")

        # Stage the source code as well so anyone can clone + reproduce.
        src_dir = PUBLISH_DIR / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        for name in ("build_dashboard.py", "ckan.py", "aggregate.py",
                     "render.py", "hourly.py"):
            src_path = HERE / name
            if src_path.exists():
                shutil.copy2(src_path, src_dir / name)
        tpl_dir = src_dir / "templates"
        tpl_dir.mkdir(exist_ok=True)
        for tpl in (HERE / "templates").glob("*.tmpl"):
            shutil.copy2(tpl, tpl_dir / tpl.name)
        # README for the source dir
        src_readme = HERE / "src_README.md"
        if src_readme.exists():
            shutil.copy2(src_readme, src_dir / "README.md")
        # gitignore so cache files aren't committed if someone runs from a clone
        (src_dir / ".gitignore").write_text(".cache/\ndata/\n__pycache__/\n*.pyc\n")
        log.info("staged for publish: %s (with %d source files)",
                 PUBLISH_DIR, len(list(src_dir.glob("*.py"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
