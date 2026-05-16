"""Build the dashboard payloads and emit index.html + data.json."""
from __future__ import annotations

import csv
import datetime as _dt
import json
import logging
from collections import defaultdict
from pathlib import Path

from aggregate import (
    BIN_LABELS, BIN_LOWERS, TIERS_OVER, WARD_NAMES,
    aggregate_city, aggregate_sign, aggregate_sign_volume, aggregate_ward,
    aggregate_distribution, aggregate_yoy_city, compute_prepost,
    compute_yoy_segments, days_in_month, latest_per_sign,
    monthly_to_adt, sign_volume_to_adt, top_movers, top_yoy_increases,
)
from ckan import iter_csv

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).parent / "templates" / "index.html.tmpl"


def _series(monthly_map: dict[str, dict], months: list[str], key: str = "weighted") -> list[list]:
    """Return [[month-as-iso, value or None], ...] for an ECharts time axis."""
    return [[m + "-01", monthly_map.get(m, {}).get(key)] for m in months]


def _adt_series(monthly_map: dict[str, dict], months: list[str]) -> list[list]:
    """Return [[month-as-iso, avg-daily-traffic], ...]."""
    adt = monthly_to_adt(monthly_map)
    return [[m + "-01", round(adt[m])] if m in adt else [m + "-01", None] for m in months]


def _window_volume_mean(monthly_map: dict[str, dict], months: tuple) -> float | None:
    """Volume-weighted mean ADT across the window. Returns None if any month missing."""
    totals = []
    for m in months:
        b = monthly_map.get(m)
        if not b or not b.get("volume"):
            return None
        totals.append(b["volume"] / days_in_month(m))
    return sum(totals) / len(totals)


def _last_n_months(months: list[str], n: int) -> list[str]:
    return months[-n:]


def latest_bins_per_sign(monthly_csv: Path, signs_meta: dict[str, dict]) -> dict[str, dict]:
    """Return per-sign latest-month bin distribution for the detail histogram.

    We re-stream the CSV (cheap; <100MB) so we don't have to keep all bins in
    memory through the whole aggregation pipeline.
    """
    latest_month: dict[str, str] = {}
    bins: dict[str, dict] = {}
    for row in iter_csv(monthly_csv):
        sid = (row.get("sign_id") or "").strip()
        if sid not in signs_meta:
            continue
        m = (row.get("month") or "")[:7]
        if not m:
            continue
        prev = latest_month.get(sid)
        if prev is None or m > prev:
            latest_month[sid] = m
            values = []
            for label in BIN_LABELS:
                v = row.get(label)
                try:
                    values.append(int(float(v))) if v not in (None, "", "None") else values.append(0)
                except (TypeError, ValueError):
                    values.append(0)
            # human-readable labels
            human = [f"{lo}-{lo+4}" for lo in BIN_LOWERS[:-1]] + ["100+"]
            bins[sid] = {"labels": human, "lowers": BIN_LOWERS, "values": values, "month": m}
    return bins


def build_summary(signs: dict[str, dict], rows: list[dict], ase: list[dict],
                  city: dict[str, dict], wards: dict[int, dict[str, dict]],
                  signs_monthly: dict[str, dict[str, float]],
                  prepost: dict, all_months: list[str],
                  distribution: dict, mover_signs: list[dict],
                  hourly: dict | None = None,
                  yoy_city: dict | None = None,
                  yoy_top: dict[int, list] | None = None) -> dict:
    latest = latest_per_sign(signs_monthly, n_months=3)

    # ward summary (with last-24-month sparkline series)
    ward_summary = []
    counts_by_ward = defaultdict(int)
    for s in signs.values():
        if s["sign_id"] in latest:
            counts_by_ward[s["ward"]] += 1
    last24 = _last_n_months(all_months, 24)
    for ward in sorted(wards.keys()):
        series24 = _series(wards[ward], last24)
        # average latest 3 months for the ward latest-pct
        recent = [v for _, v in series24[-3:] if v is not None]
        latest_ward = sum(recent) / len(recent) if recent else None
        stats = prepost["wards"].get(ward, {})
        # ADT pre/post for the ward
        adt_pre = _window_volume_mean(wards[ward], ("2025-07","2025-08","2025-09","2025-10"))
        adt_post = _window_volume_mean(wards[ward], ("2025-12","2026-01","2026-02","2026-03"))
        ward_summary.append({
            "ward": ward,
            "name": WARD_NAMES.get(ward, f"Ward {ward}"),
            "n_signs": counts_by_ward.get(ward, 0),
            "latest": latest_ward,
            "delta_2025": stats.get("delta_2025"),
            "did": stats.get("did"),
            "adt_pre": adt_pre,
            "adt_post": adt_post,
            "adt_delta_pct": ((adt_post / adt_pre - 1) * 100.0) if (adt_pre and adt_post) else None,
            "series24": series24,
        })

    # per-sign quick stats for map + ward table
    sign_list = []
    for sid, s in signs.items():
        if sid not in latest:
            continue
        st = prepost["signs"].get(sid, {})
        sign_list.append({
            "sign_id": sid,
            "name": s["name"],
            "address": s["address"],
            "schedule": s["schedule"],
            "ward": s["ward"],
            "limit": s["limit"],
            "lat": s["lat"],
            "lon": s["lon"],
            "latest": latest[sid],
            "delta_2025": st.get("delta_2025"),
            "did": st.get("did"),
        })
    sign_list.sort(key=lambda x: (x["ward"], x["name"] or x["sign_id"]))

    city_series = _series(city, all_months)
    city_adt_series = _adt_series(city, all_months)

    # City-wide ADT pre/post for headline
    adt_pre = _window_volume_mean(city, ("2025-07","2025-08","2025-09","2025-10"))
    adt_post = _window_volume_mean(city, ("2025-12","2026-01","2026-02","2026-03"))
    adt_pre24 = _window_volume_mean(city, ("2024-07","2024-08","2024-09","2024-10"))
    adt_post24 = _window_volume_mean(city, ("2024-12","2025-01","2025-02","2025-03"))

    out = {
        "generated_at": _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "metric_label": "% > 10 km/h over posted limit",
        "headline": prepost["city"],
        "city_series": city_series,
        "city_adt_series": city_adt_series,
        "adt_headline": {
            "pre_2025": adt_pre, "post_2025": adt_post,
            "pre_2024": adt_pre24, "post_2024": adt_post24,
            "delta_pct_2025": ((adt_post / adt_pre - 1) * 100.0) if (adt_pre and adt_post) else None,
            "delta_pct_2024": ((adt_post24 / adt_pre24 - 1) * 100.0) if (adt_pre24 and adt_post24) else None,
        },
        "distribution": distribution,
        "top_movers": mover_signs,
        "ward_summary": ward_summary,
        "signs": sign_list,
        "ase": ase,
        "n_signs_total": len(signs),
        "n_signs_with_data": sum(1 for _ in latest),
        "n_ase": len(ase),
    }

    if yoy_city:
        out["yoy_city"] = yoy_city
    if yoy_top:
        out["yoy_top"] = yoy_top
    out["yoy_tiers"] = list(TIERS_OVER)

    # Hourly profile metadata (full per-sign profile goes in data.json)
    if hourly:
        out["hourly_city"] = hourly.get("city")
        # outlier list: signs whose weekday hourly shape diverges most from city avg
        unusual = []
        for sid, payload in hourly.get("signs", {}).items():
            s = signs.get(sid)
            if not s or payload.get("total_volume", 0) < 50000:  # min volume for stable shape
                continue
            unusual.append({
                "sign_id": sid,
                "name": s["name"] or s["address"] or f"Sign {sid}",
                "ward": s["ward"],
                "limit": s["limit"],
                "pattern_distance": payload["pattern_distance"],
                "total_volume": payload["total_volume"],
            })
        unusual.sort(key=lambda x: x["pattern_distance"], reverse=True)
        out["unusual_patterns"] = unusual[:15]
        out["hourly_year"] = 2023

    return out


def build_full(signs_monthly: dict[str, dict[str, float]],
               signs_volume: dict[str, dict[str, int]],
               wards: dict[int, dict[str, dict]],
               all_months: list[str],
               signs_bins: dict[str, dict],
               hourly: dict | None = None,
               yoy_per_sign: dict[str, dict] | None = None) -> dict:
    ward_monthly = {ward: _series(series, all_months) for ward, series in wards.items()}
    ward_adt = {ward: _adt_series(series, all_months) for ward, series in wards.items()}
    signs_adt = {sid: sign_volume_to_adt(vols) for sid, vols in signs_volume.items()}
    out = {
        "signs_monthly": signs_monthly,
        "signs_adt": signs_adt,
        "ward_monthly": ward_monthly,
        "ward_adt": ward_adt,
        "signs_bins": signs_bins,
    }
    if hourly is not None:
        out["hourly"] = hourly
    if yoy_per_sign is not None:
        # Trim: only include signs with non-zero volume in both windows to keep size down.
        slim = {}
        for sid, r in yoy_per_sign.items():
            if r["this_vol"] == 0 and r["last_vol"] == 0:
                continue
            slim[sid] = {
                "this": r["this"], "last": r["last"],
                "this_vol": r["this_vol"], "last_vol": r["last_vol"],
                "this_months": r["this_months"], "last_months": r["last_months"],
            }
        out["yoy_signs"] = slim
    return out


def render(summary: dict, full: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl = TEMPLATE.read_text(encoding="utf-8")
    # Inline summary using JSON's default ASCII-safe encoding (no quote conflicts).
    html = tpl.replace("{{SUMMARY_JSON}}", json.dumps(summary, ensure_ascii=False))
    index_path = out_dir / "index.html"
    data_path = out_dir / "data.json"
    index_path.write_text(html, encoding="utf-8")
    data_path.write_text(json.dumps(full, ensure_ascii=False))
    return index_path, data_path
