"""Build the dashboard payloads and emit index.html + data.json."""
from __future__ import annotations

import csv
import datetime as _dt
import json
import logging
from collections import defaultdict
from pathlib import Path

from aggregate import (
    BIN_LABELS, BIN_LOWERS, DISTANCE_BIN_LABELS, TIERS_OVER, WARD_NAMES,
    aggregate_city, aggregate_sign, aggregate_sign_volume, aggregate_ward,
    aggregate_distribution, aggregate_yoy_city, compute_prepost,
    compute_yoy_segments, days_in_month, latest_per_sign,
    monthly_to_adt, sign_volume_to_adt, top_movers, top_yoy_increases,
)
from ckan import iter_csv

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).parent / "templates" / "index.html.tmpl"
STORY_TEMPLATE = Path(__file__).parent / "templates" / "story.html.tmpl"
MIMICO_TEMPLATE = Path(__file__).parent / "templates" / "mimico.html.tmpl"
WHENSPEED_TEMPLATE = Path(__file__).parent / "templates" / "whenspeed.html.tmpl"

# Signs the Mimico newsletter page focuses on. Hardcoded by sign_id.
MIMICO_SIGNS = ["112", "2841"]


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


def _prior_year_month(month: str) -> str:
    """'2026-04' -> '2025-04'."""
    y, m = int(month[:4]), int(month[5:7])
    return f"{y-1:04d}-{m:02d}"


def latest_bins_per_sign(monthly_csv: Path, signs_meta: dict[str, dict],
                          alias_map: dict[str, str] | None = None) -> dict[str, dict]:
    """Per-sign latest-month bin distribution + the prior-year same-month bins for YoY overlay.

    Streams the monthly CSV once, keeping a per-(sign, month) bin record only
    for months that turn out to be the latest or the latest-minus-one-year for
    that sign. Memory is bounded by ~2 × #signs entries by the end.
    """
    # Tracks: per sign -> latest month seen; per (sign, month) -> bin values.
    latest_month: dict[str, str] = {}
    bins_by_sm: dict[tuple[str, str], list[int]] = {}

    for row in iter_csv(monthly_csv):
        sid = (row.get("sign_id") or "").strip()
        if alias_map:
            sid = alias_map.get(sid, sid)
        if sid not in signs_meta:
            continue
        m = (row.get("month") or "")[:7]
        if not m:
            continue
        values = []
        for label in BIN_LABELS:
            v = row.get(label)
            try:
                values.append(int(float(v))) if v not in (None, "", "None") else values.append(0)
            except (TypeError, ValueError):
                values.append(0)
        existing = bins_by_sm.get((sid, m))
        if existing is None:
            bins_by_sm[(sid, m)] = values
        else:
            # Aliased sign_ids merging into the same canonical+month: sum the bins.
            bins_by_sm[(sid, m)] = [a + b for a, b in zip(existing, values)]
        prev = latest_month.get(sid)
        if prev is None or m > prev:
            latest_month[sid] = m

    human = [f"{lo}-{lo+4}" for lo in BIN_LOWERS[:-1]] + ["100+"]
    out: dict[str, dict] = {}
    for sid, latest in latest_month.items():
        prior = _prior_year_month(latest)
        values = bins_by_sm.get((sid, latest))
        prev_values = bins_by_sm.get((sid, prior))
        out[sid] = {
            "labels": human,
            "lowers": BIN_LOWERS,
            "values": values,
            "month": latest,
            "prior_values": prev_values,
            "prior_month": prior if prev_values is not None else None,
        }
    return out


def build_summary(signs: dict[str, dict], rows: list[dict], ase: list[dict],
                  city: dict[str, dict], wards: dict[int, dict[str, dict]],
                  signs_monthly: dict[str, dict[str, float]],
                  prepost: dict, all_months: list[str],
                  distribution: dict, mover_signs: list[dict],
                  hourly: dict | None = None,
                  yoy_city: dict | None = None,
                  yoy_top: dict[int, list] | None = None,
                  nearest_ase: dict[str, dict] | None = None,
                  spatial_did: dict | None = None,
                  bin_counts: dict[str, int] | None = None,
                  ase_camera_ranked: list | None = None,
                  rto: dict | None = None) -> dict:
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
        near = nearest_ase.get(sid) if nearest_ase else None
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
            "nearest_ase_m": (near or {}).get("distance_m"),
            "nearest_ase_loc": (near or {}).get("ase_location"),
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

    if ase_camera_ranked is not None:
        out["ase_camera_ranked"] = ase_camera_ranked

    if rto is not None:
        out["rto"] = rto

    if spatial_did is not None:
        # Emit bins in canonical order, including zero-sign bins as nulls.
        labels = DISTANCE_BIN_LABELS
        ordered = []
        for lbl in labels:
            entry = spatial_did.get(lbl, {})
            ordered.append({
                "label": lbl,
                "n_signs": (bin_counts or {}).get(lbl, 0),
                "pre_2025": entry.get("pre_2025"),
                "post_2025": entry.get("post_2025"),
                "delta_2025": entry.get("delta_2025"),
                "pre_2024": entry.get("pre_2024"),
                "post_2024": entry.get("post_2024"),
                "delta_2024": entry.get("delta_2024"),
                "did": entry.get("did"),
            })
        out["spatial_did"] = ordered

    # Hourly profile metadata (full per-sign profile goes in data.json, used by
    # the per-sign typical-day chart). The "unusual patterns" leaderboard was
    # removed; only the per-sign profile remains.
    if hourly:
        out["hourly_city"] = hourly.get("city")
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


def build_story_payload(summary: dict) -> dict:
    """Compress the dashboard summary into a small payload tailored to the
    story-page narrative. Pulls only the fields the story actually plots."""
    tiers_data = []
    for t in summary.get("yoy_tiers", []):
        tr = summary["yoy_city"]["tiers"].get(str(t)) or summary["yoy_city"]["tiers"].get(t)
        if tr is None:
            continue
        tiers_data.append({
            "tier": t,
            "last": tr["last"], "this": tr["this"],
            "delta": tr["delta"], "pct": tr["pct_change"],
        })
    wards = [{
        "ward": w["ward"], "name": w["name"], "n_signs": w["n_signs"], "did": w["did"],
    } for w in summary.get("ward_summary", []) if w.get("did") is not None]
    spatial = [{
        "label": b["label"], "n_signs": b["n_signs"],
        "delta_2025": b["delta_2025"], "did": b["did"],
    } for b in summary.get("spatial_did", []) if b.get("did") is not None]
    return {
        "generated_at": summary["generated_at"],
        "city_series": summary["city_series"],
        "tiers": tiers_data,
        "wards": wards,
        "spatial": spatial,
        "distribution": summary["distribution"],
        "rto": summary.get("rto"),
    }


def build_mimico_payload(summary: dict, full: dict,
                          sign_ids: list[str] = MIMICO_SIGNS) -> dict:
    """Tight per-sign payload for the Mimico newsletter page (two specific signs)."""
    signs_index = {str(s["sign_id"]): s for s in summary.get("signs", [])}
    yoy = (full or {}).get("yoy_signs", {})

    def make(sid: str) -> dict | None:
        s = signs_index.get(str(sid))
        if not s:
            return None
        y = yoy.get(str(sid)) or {}
        last = y.get("last") or {}
        this = y.get("this") or {}
        tiers_out = []
        for t in (10, 15, 20, 25, 30):
            last_c = int(last.get(str(t)) or last.get(t) or 0)
            this_c = int(this.get(str(t)) or this.get(t) or 0)
            delta = this_c - last_c
            pct = (delta / last_c * 100.0) if last_c else None
            tiers_out.append({
                "tier": t, "last": last_c, "this": this_c,
                "delta": delta, "pct": pct,
            })
        return {
            "sign_id": s["sign_id"],
            "name": s.get("name") or s.get("address") or f"Sign {sid}",
            "address": s.get("address") or "",
            "ward": s.get("ward"),
            "limit": s.get("limit"),
            "nearest_ase_m": s.get("nearest_ase_m"),
            "nearest_ase_loc": s.get("nearest_ase_loc"),
            "this_vol": int(y.get("this_vol") or 0),
            "last_vol": int(y.get("last_vol") or 0),
            "tiers": tiers_out,
        }

    return {
        "generated_at": summary["generated_at"],
        "signA": make(sign_ids[0]) if len(sign_ids) > 0 else None,
        "signB": make(sign_ids[1]) if len(sign_ids) > 1 else None,
    }


def build_whenspeed_payload(summary: dict, whenspeed: dict | None) -> dict | None:
    """Story payload for whenspeed.html — debunks 'speeding only matters when
    students are around' by showing the temporal pattern from the 2023 archive
    (the most recent year for which detailed hour-of-day data is published).

    Picks three sample signs: highest overnight-share, highest weekend-share,
    highest outside-school-hours-share — all gated on a minimum 10+over count
    so we don't pick noisy low-traffic signs.
    """
    if not whenspeed:
        return None
    city = whenspeed.get("city") or {}
    signs_idx = {str(s["sign_id"]): s for s in summary.get("signs", [])}
    candidates = []
    for sid, w in (whenspeed.get("signs") or {}).items():
        if w.get("total_ge10", 0) < 5000:
            continue
        s = signs_idx.get(str(sid))
        if not s:
            continue
        candidates.append({
            "sign_id": sid,
            "name": s.get("name") or s.get("address") or f"Sign {sid}",
            "address": s.get("address") or "",
            "ward": s.get("ward"),
            "limit": s.get("limit"),
            "total_ge10": w["total_ge10"],
            "total_ge20": w.get("total_ge20", 0),
            "total_ge30": w.get("total_ge30", 0),
            "share_overnight_ge10": w["share_overnight_ge10"],
            "share_weekend_ge10": w["share_weekend_ge10"],
            "share_outside_school_ge10": w["share_outside_school_ge10"],
            "worst_hour": w["worst_hour"],
            "wk_hour_ge10": w["wk_hour_ge10"],
            "we_hour_ge10": w["we_hour_ge10"],
            "dow_ge10": w["dow_ge10"],
        })
    picks = {}
    if candidates:
        picks["overnight"] = max(candidates, key=lambda c: c["share_overnight_ge10"])
        picks["weekend"] = max(candidates, key=lambda c: c["share_weekend_ge10"])
        picks["outside_school"] = max(candidates, key=lambda c: c["share_outside_school_ge10"])

    # Headline post-ban shift, lifted from the city YoY tiers already in summary
    yoy_city = summary.get("yoy_city", {}).get("tiers", {}) or {}
    tier10 = yoy_city.get("10") or yoy_city.get(10) or {}
    tier20 = yoy_city.get("20") or yoy_city.get(20) or {}
    tier30 = yoy_city.get("30") or yoy_city.get(30) or {}

    return {
        "generated_at": summary["generated_at"],
        "city": {
            "wk_hour_ge10": city.get("wk_hour_ge10", []),
            "wk_hour_ge20": city.get("wk_hour_ge20", []),
            "wk_hour_ge30": city.get("wk_hour_ge30", []),
            "we_hour_ge10": city.get("we_hour_ge10", []),
            "we_hour_ge20": city.get("we_hour_ge20", []),
            "we_hour_ge30": city.get("we_hour_ge30", []),
            "dow_ge10": city.get("dow_ge10", []),
            "dow_ge20": city.get("dow_ge20", []),
            "dow_ge30": city.get("dow_ge30", []),
            "school_tiers": city.get("school_tiers", {}),
            "outside_tiers": city.get("outside_tiers", {}),
            "share_outside_school_ge10": city.get("share_outside_school_ge10", 0),
            "share_outside_school_ge20": city.get("share_outside_school_ge20", 0),
            "share_outside_school_ge30": city.get("share_outside_school_ge30", 0),
            "n_signs": city.get("n_signs", 0),
        },
        "post_ban_tiers": {
            "10": {"last": tier10.get("last"), "this": tier10.get("this"),
                    "delta": tier10.get("delta"), "pct": tier10.get("pct_change")},
            "20": {"last": tier20.get("last"), "this": tier20.get("this"),
                    "delta": tier20.get("delta"), "pct": tier20.get("pct_change")},
            "30": {"last": tier30.get("last"), "this": tier30.get("this"),
                    "delta": tier30.get("delta"), "pct": tier30.get("pct_change")},
        },
        "samples": picks,
    }


def render(summary: dict, full: dict, out_dir: Path,
            whenspeed: dict | None = None) -> tuple[Path, Path, Path, Path, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Main dashboard
    tpl = TEMPLATE.read_text(encoding="utf-8")
    html = tpl.replace("{{SUMMARY_JSON}}", json.dumps(summary, ensure_ascii=False))
    index_path = out_dir / "index.html"
    data_path = out_dir / "data.json"
    index_path.write_text(html, encoding="utf-8")
    data_path.write_text(json.dumps(full, ensure_ascii=False))
    # Story page
    story_tpl = STORY_TEMPLATE.read_text(encoding="utf-8")
    story_payload = build_story_payload(summary)
    story_html = story_tpl.replace("{{STORY_JSON}}", json.dumps(story_payload, ensure_ascii=False))
    story_path = out_dir / "story.html"
    story_path.write_text(story_html, encoding="utf-8")
    # Mimico newsletter page
    mimico_tpl = MIMICO_TEMPLATE.read_text(encoding="utf-8")
    mimico_payload = build_mimico_payload(summary, full)
    mimico_html = mimico_tpl.replace("{{MIMICO_JSON}}", json.dumps(mimico_payload, ensure_ascii=False))
    mimico_path = out_dir / "mimico.html"
    mimico_path.write_text(mimico_html, encoding="utf-8")
    # When-speeding-happens page
    whenspeed_path: Path | None = None
    if whenspeed and WHENSPEED_TEMPLATE.exists():
        ws_payload = build_whenspeed_payload(summary, whenspeed)
        if ws_payload:
            ws_tpl = WHENSPEED_TEMPLATE.read_text(encoding="utf-8")
            ws_html = ws_tpl.replace("{{WHENSPEED_JSON}}", json.dumps(ws_payload, ensure_ascii=False))
            whenspeed_path = out_dir / "whenspeed.html"
            whenspeed_path.write_text(ws_html, encoding="utf-8")
    return index_path, data_path, story_path, mimico_path, whenspeed_path
