"""Pure-stdlib aggregation pipeline for the Watch Your Speed dashboard.

Inputs are CSV files cached by ckan.py. Outputs are plain dicts/lists ready
to be serialised as JSON for the HTML dashboard.

The speeding metric is "% of vehicles measured more than 10 km/h over the
posted limit", approximated by including any 5 km/h speed bin whose lower
boundary is >= posted_limit + 10. Documented in the methodology footer of
the dashboard.
"""
from __future__ import annotations

import calendar
import html
import json
import logging
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from ckan import iter_csv

log = logging.getLogger(__name__)

# Toronto 25-ward map (2018 redraw). Ward 3 is Etobicoke-Lakeshore, which
# includes Mimico (the user's neighbourhood).
WARD_NAMES = {
    1: "Etobicoke North",
    2: "Etobicoke Centre",
    3: "Etobicoke-Lakeshore",
    4: "Parkdale-High Park",
    5: "York South-Weston",
    6: "York Centre",
    7: "Humber River-Black Creek",
    8: "Eglinton-Lawrence",
    9: "Davenport",
    10: "Spadina-Fort York",
    11: "University-Rosedale",
    12: "Toronto-St. Paul's",
    13: "Toronto Centre",
    14: "Toronto-Danforth",
    15: "Don Valley West",
    16: "Don Valley East",
    17: "Don Valley North",
    18: "Willowdale",
    19: "Beaches-East York",
    20: "Scarborough Southwest",
    21: "Scarborough Centre",
    22: "Scarborough-Agincourt",
    23: "Scarborough North",
    24: "Scarborough-Guildwood",
    25: "Scarborough-Rouge Park",
}

# Pre/post windows for the difference-in-differences analysis.
# Ontario banned ASE cameras November 2025; we compare 4-month windows
# straddling that date, with 2024 as a seasonal control.
PRE_2025 = ("2025-07", "2025-08", "2025-09", "2025-10")
POST_2025 = ("2025-12", "2026-01", "2026-02", "2026-03")
PRE_2024 = ("2024-07", "2024-08", "2024-09", "2024-10")
POST_2024 = ("2024-12", "2025-01", "2025-02", "2025-03")

BIN_LABELS = [f"spd_{i:02d}" for i in range(0, 100, 5)] + ["spd_100_and_above"]
BIN_LOWERS = list(range(0, 100, 5)) + [100]


# ---------- parsers ----------

_WARD_RE = re.compile(r"^\s*(\d+)")


def _parse_ward(raw: str | None) -> int | None:
    if not raw:
        return None
    m = _WARD_RE.match(str(raw))
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 25 else None


def _parse_int(raw: str | None) -> int | None:
    if raw in (None, "", "None"):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _parse_geometry(raw: str | None) -> tuple[float, float] | None:
    """Parse a CKAN geometry JSON string. Returns (lon, lat) or None."""
    if not raw:
        return None
    try:
        g = json.loads(raw)
    except (TypeError, ValueError):
        return None
    coords = g.get("coordinates") if isinstance(g, dict) else None
    if not coords or len(coords) < 2:
        return None
    try:
        return float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None


def _parse_month(raw: str | None) -> str | None:
    """Normalise a month value to 'YYYY-MM'."""
    if not raw:
        return None
    s = str(raw).strip()[:10]  # 'YYYY-MM-DD' -> 'YYYY-MM-DD'
    return s[:7] if len(s) >= 7 and s[4] == "-" else None


# ---------- location & ASE loaders ----------

def load_signs(locations_csv: Path) -> dict[str, dict]:
    """sign_id -> {ward, ward_name, limit, lat, lon, name, address, schedule, dir}."""
    signs: dict[str, dict] = {}
    dropped_no_limit = 0
    dropped_no_geo = 0
    dropped_no_ward = 0
    for row in iter_csv(locations_csv):
        sid = (row.get("sign_id") or "").strip()
        if not sid:
            continue
        limit = _parse_int(row.get("speed_limit"))
        if limit is None:
            dropped_no_limit += 1
            continue
        ward = _parse_ward(row.get("ward_no"))
        if ward is None:
            dropped_no_ward += 1
            continue
        lonlat = _parse_geometry(row.get("geometry"))
        if lonlat is None:
            dropped_no_geo += 1
            continue
        lon, lat = lonlat
        def clean(s: str | None) -> str:
            # Some upstream rows are HTML-escaped (occasionally double-escaped).
            t = (s or "").strip()
            for _ in range(2):
                u = html.unescape(t)
                if u == t:
                    break
                t = u
            return t

        signs[sid] = {
            "sign_id": sid,
            "ward": ward,
            "ward_name": WARD_NAMES.get(ward, f"Ward {ward}"),
            "limit": limit,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "name": clean(row.get("sign_name")),
            "address": clean(row.get("address")),
            "schedule": clean(row.get("schedule")),
            "dir": (row.get("dir") or "").strip(),
        }
    log.info(
        "loaded %d signs (dropped: %d no_limit, %d no_ward, %d no_geo)",
        len(signs), dropped_no_limit, dropped_no_ward, dropped_no_geo,
    )
    return signs


def load_ase(ase_csv: Path) -> list[dict]:
    out: list[dict] = []
    for row in iter_csv(ase_csv):
        lonlat = _parse_geometry(row.get("geometry"))
        if lonlat is None:
            continue
        lon, lat = lonlat
        ward_raw = row.get("ward") or ""
        out.append({
            "id": row.get("_id") or row.get("Location_Code") or "",
            "location": (row.get("location") or "").strip(),
            "status": (row.get("Status") or "").strip(),
            "ward": _parse_ward(ward_raw),
            "ward_label": ward_raw.strip(),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        })
    log.info("loaded %d ASE points", len(out))
    return out


# ---------- monthly metric ----------

def _row_over_count(row: dict, limit: int) -> tuple[int, int]:
    """Return (over_count, volume) for one monthly row given the posted limit.

    over_count counts vehicles in any 5km/h speed bin whose lower boundary is
    at least 10 km/h above the limit (the "10+ km/h over" approximation).
    """
    cutoff = limit + 10
    over = 0
    volume = 0
    for label, lower in zip(BIN_LABELS, BIN_LOWERS):
        v = row.get(label)
        if v in (None, "", "None"):
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        volume += n
        if lower >= cutoff:
            over += n
    # Some rows include a 'volume' column we can use to sanity-check, but the
    # bin sum is what's consistent with the metric. Use bin sum.
    return over, volume


def load_monthly(monthly_csv: Path, signs: dict[str, dict]) -> list[dict]:
    """Yield enriched monthly rows: {sign_id, month, ward, limit, over, volume, pct}."""
    out: list[dict] = []
    skipped_unknown_sign = 0
    skipped_bad_month = 0
    skipped_zero_vol = 0
    for row in iter_csv(monthly_csv):
        sid = (row.get("sign_id") or "").strip()
        sign = signs.get(sid)
        if sign is None:
            skipped_unknown_sign += 1
            continue
        month = _parse_month(row.get("month"))
        if month is None:
            skipped_bad_month += 1
            continue
        over, volume = _row_over_count(row, sign["limit"])
        if volume <= 0:
            skipped_zero_vol += 1
            continue
        out.append({
            "sign_id": sid,
            "ward": sign["ward"],
            "limit": sign["limit"],
            "month": month,
            "over": over,
            "volume": volume,
            "pct": over / volume * 100.0,
        })
    log.info(
        "loaded %d monthly rows (skipped: %d unknown_sign, %d bad_month, %d zero_vol)",
        len(out), skipped_unknown_sign, skipped_bad_month, skipped_zero_vol,
    )
    return out


# ---------- aggregations ----------

def _weighted_monthly(rows: Iterable[dict], key_fn) -> dict:
    """For each (key, month), volume-weighted mean pct + unweighted mean."""
    acc: dict = defaultdict(lambda: {"over": 0, "volume": 0, "pct_sum": 0.0, "n": 0})
    for r in rows:
        k = (key_fn(r), r["month"])
        b = acc[k]
        b["over"] += r["over"]
        b["volume"] += r["volume"]
        b["pct_sum"] += r["pct"]
        b["n"] += 1
    out: dict = defaultdict(dict)  # key -> {month -> {weighted, unweighted, n, volume}}
    for (k, m), b in acc.items():
        out[k][m] = {
            "weighted": (b["over"] / b["volume"] * 100.0) if b["volume"] else None,
            "unweighted": (b["pct_sum"] / b["n"]) if b["n"] else None,
            "n_signs": b["n"],
            "volume": b["volume"],
        }
    return out


def aggregate_city(rows: list[dict]) -> dict[str, dict]:
    return _weighted_monthly(rows, key_fn=lambda r: "city")["city"]


def aggregate_ward(rows: list[dict]) -> dict[int, dict[str, dict]]:
    return dict(_weighted_monthly(rows, key_fn=lambda r: r["ward"]))


# ---------- spatial: distance to nearest ASE camera ----------

# Bin edges in metres. Left-inclusive, right-exclusive.
# Only bins within ~500 m of a former ASE camera are reported: beyond that
# distance there's no plausible mechanistic association between a specific
# camera point and a sign's driver behaviour, so the "further" bins were
# methodological noise dressed up as a control group.
DISTANCE_BINS: list[tuple[int, int | None, str]] = [
    (0,   250, "0–250 m"),
    (250, 500, "250–500 m"),
]
DISTANCE_BIN_LABELS: list[str] = [lbl for _, _, lbl in DISTANCE_BINS]


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two (lat, lon) pairs."""
    R = 6_371_000.0  # earth radius, m
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def compute_nearest_ase(signs: dict[str, dict], ase: list[dict]) -> dict[str, dict]:
    """For each sign, the distance + identity of the nearest ASE camera."""
    if not ase:
        return {sid: {"distance_m": None, "ase_location": None} for sid in signs}
    out: dict[str, dict] = {}
    for sid, s in signs.items():
        best_d = float("inf")
        best_loc = None
        for a in ase:
            d = haversine(s["lat"], s["lon"], a["lat"], a["lon"])
            if d < best_d:
                best_d = d
                best_loc = a["location"]
        out[sid] = {"distance_m": round(best_d, 1), "ase_location": best_loc}
    return out


def assign_distance_bin(distance_m: float | None) -> str | None:
    if distance_m is None:
        return None
    for lo, hi, lbl in DISTANCE_BINS:
        if hi is None or distance_m < hi:
            if distance_m >= lo:
                return lbl
    return None


def aggregate_distance(rows: list[dict], sign_to_bin: dict[str, str]) -> dict[str, dict[str, dict]]:
    """Group monthly rows by ASE-distance bin, same shape as `aggregate_ward()`."""
    def key(r):
        return sign_to_bin.get(r["sign_id"])
    grouped = _weighted_monthly((r for r in rows if key(r) is not None), key_fn=key)
    return dict(grouped)


def aggregate_by_ase_camera(rows: list[dict], nearest_ase: dict[str, dict],
                             max_distance_m: int = 500) -> dict[str, dict[str, dict]]:
    """Group monthly rows by the WYS sign's nearest ASE camera (within max_distance_m).

    Signs further than max_distance_m from any camera are excluded; the assumption
    is the camera is no longer plausibly "associated" with those signs' behaviour.
    Returns a {ase_location -> monthly_map} dict, ready for compute_prepost_groups.
    """
    sign_to_ase: dict[str, str] = {}
    for sid, info in nearest_ase.items():
        d = info.get("distance_m")
        loc = info.get("ase_location")
        if d is not None and loc and d <= max_distance_m:
            sign_to_ase[sid] = loc

    def key(r):
        return sign_to_ase.get(r["sign_id"])

    grouped = _weighted_monthly((r for r in rows if key(r) is not None), key_fn=key)
    return dict(grouped)


def top_ase_camera_movers(ase_prepost: dict[str, dict], nearest_ase: dict[str, dict],
                           ase: list[dict], max_distance_m: int = 500,
                           min_signs: int = 1, n: int = 25) -> list[dict]:
    """Rank ASE cameras by DiD-adjusted speeding change at nearby WYS signs."""
    counts: dict[str, int] = {}
    for sid, info in nearest_ase.items():
        d = info.get("distance_m")
        loc = info.get("ase_location")
        if d is not None and loc and d <= max_distance_m:
            counts[loc] = counts.get(loc, 0) + 1

    ase_by_loc = {a["location"]: a for a in ase}
    items = []
    for loc, stats in ase_prepost.items():
        did = stats.get("did")
        delta25 = stats.get("delta_2025")
        if did is None and delta25 is None:
            continue
        c = counts.get(loc, 0)
        if c < min_signs:
            continue
        meta = ase_by_loc.get(loc, {})
        items.append({
            "location": loc,
            "n_signs": c,
            "status": meta.get("status"),
            "ward": meta.get("ward"),
            "lat": meta.get("lat"),
            "lon": meta.get("lon"),
            "did": did,
            "delta_2025": delta25,
            "pre_2025": stats.get("pre_2025"),
            "post_2025": stats.get("post_2025"),
        })
    items.sort(key=lambda x: (x["did"] if x["did"] is not None else -1e9), reverse=True)
    return items[:n]


def aggregate_sign(rows: list[dict]) -> dict[str, dict[str, float]]:
    """sign_id -> {month -> pct}. Per-sign monthly metric for detail charts."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        out[r["sign_id"]][r["month"]] = round(r["pct"], 2)
    return out


def aggregate_sign_volume(rows: list[dict]) -> dict[str, dict[str, int]]:
    """sign_id -> {month -> volume}. Used to derive average daily traffic."""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        out[r["sign_id"]][r["month"]] = r["volume"]
    return out


# ---------- average daily traffic ----------

def days_in_month(month_str: str) -> int:
    y, m = int(month_str[:4]), int(month_str[5:7])
    return calendar.monthrange(y, m)[1]


def monthly_to_adt(monthly_map: dict[str, dict]) -> dict[str, float]:
    """Convert a {month: {volume, ...}} map into {month: avg-daily-traffic}.

    For signs that operate only on a schedule (e.g. school hours) the volume
    is what was actually measured during operating days, so ADT is a relative
    indicator rather than a true road count — fine for trend comparison.
    """
    return {m: (b["volume"] / days_in_month(m)) for m, b in monthly_map.items() if b.get("volume")}


def sign_volume_to_adt(volume_map: dict[str, int]) -> dict[str, float]:
    return {m: (v / days_in_month(m)) for m, v in volume_map.items() if v}


# ---------- speed-distribution comparison (pre vs post ban) ----------

WINDOWS = {
    "pre_2025": PRE_2025,
    "post_2025": POST_2025,
    "pre_2024": PRE_2024,
    "post_2024": POST_2024,
}


def aggregate_distribution(monthly_csv: Path, signs: dict[str, dict]) -> dict:
    """Sum vehicle counts per speed bin across all signs, for each pre/post window.

    Returns a dict like:
        {"labels": ["0-4", "5-9", ...], "lowers": [0,5,...],
         "pre_2025": [counts...], "post_2025": [...], "pre_2024": [...], "post_2024": [...]}
    """
    from ckan import iter_csv  # local import to keep top of file lighter

    months_to_window: dict[str, str] = {}
    for win, months in WINDOWS.items():
        for m in months:
            months_to_window[m] = win

    sums = {win: [0] * len(BIN_LABELS) for win in WINDOWS}

    for row in iter_csv(monthly_csv):
        sid = (row.get("sign_id") or "").strip()
        if sid not in signs:
            continue
        month = _parse_month(row.get("month"))
        if not month:
            continue
        win = months_to_window.get(month)
        if not win:
            continue
        for i, label in enumerate(BIN_LABELS):
            v = row.get(label)
            if v in (None, "", "None"):
                continue
            try:
                sums[win][i] += int(float(v))
            except (TypeError, ValueError):
                pass

    human_labels = [f"{lo}-{lo+4}" for lo in BIN_LOWERS[:-1]] + ["100+"]
    out: dict = {"labels": human_labels, "lowers": BIN_LOWERS}
    for win, counts in sums.items():
        total = sum(counts)
        out[win] = counts
        out[win + "_pct"] = [(c / total * 100.0) if total else 0.0 for c in counts]
    return out


# ---------- year-over-year tiered speeder counts ----------
#
# Per the user's request (matching the Safe Parkside framing): count vehicles
# in km/h-over-limit tiers for the same Jan-Apr window in 2026 vs 2025.
# Tiers are cumulative ("10+ km/h over" includes "20+ km/h over"), and apply
# to each sign relative to its own posted speed_limit.

TIERS_OVER = (10, 15, 20, 25, 30)
YOY_CURRENT = ("2026-01", "2026-02", "2026-03", "2026-04")
YOY_PRIOR = ("2025-01", "2025-02", "2025-03", "2025-04")


def compute_yoy_segments(monthly_csv: Path, signs: dict[str, dict]) -> dict[str, dict]:
    """Per-sign vehicle counts in each "X+ over limit" tier, for current and prior windows."""
    from ckan import iter_csv

    cur = set(YOY_CURRENT)
    prior = set(YOY_PRIOR)
    out: dict[str, dict] = {sid: {
        "this": {t: 0 for t in TIERS_OVER},
        "last": {t: 0 for t in TIERS_OVER},
        "this_vol": 0, "last_vol": 0,
        "this_months": set(), "last_months": set(),
    } for sid in signs}

    for row in iter_csv(monthly_csv):
        sid = (row.get("sign_id") or "").strip()
        rec = out.get(sid)
        if rec is None:
            continue
        month = _parse_month(row.get("month"))
        if not month:
            continue
        if month in cur:
            tgt, vol_key, mset = rec["this"], "this_vol", rec["this_months"]
        elif month in prior:
            tgt, vol_key, mset = rec["last"], "last_vol", rec["last_months"]
        else:
            continue
        mset.add(month)
        limit = signs[sid]["limit"]
        for label, lower in zip(BIN_LABELS, BIN_LOWERS):
            v = row.get(label)
            if v in (None, "", "None"):
                continue
            try:
                n = int(float(v))
            except (TypeError, ValueError):
                continue
            if n <= 0:
                continue
            rec[vol_key] += n
            over = lower - limit
            for t in TIERS_OVER:
                if over >= t:
                    tgt[t] += n
    # convert month sets to counts for JSON friendliness
    for r in out.values():
        r["this_months"] = len(r["this_months"])
        r["last_months"] = len(r["last_months"])
    return out


def aggregate_yoy_city(per_sign: dict[str, dict]) -> dict:
    """Sum across signs that reported in both windows (apples-to-apples)."""
    apples = [r for r in per_sign.values() if r["this_vol"] > 0 and r["last_vol"] > 0]
    out = {
        "tiers": {},
        "this_vol": sum(r["this_vol"] for r in apples),
        "last_vol": sum(r["last_vol"] for r in apples),
        "n_signs": len(apples),
        "current_window": list(YOY_CURRENT),
        "prior_window": list(YOY_PRIOR),
    }
    for t in TIERS_OVER:
        this_c = sum(r["this"][t] for r in apples)
        last_c = sum(r["last"][t] for r in apples)
        out["tiers"][t] = {
            "this": this_c,
            "last": last_c,
            "delta": this_c - last_c,
            "pct_change": ((this_c - last_c) / last_c * 100.0) if last_c > 0 else None,
            "this_rate": (this_c / out["this_vol"] * 100.0) if out["this_vol"] else None,
            "last_rate": (last_c / out["last_vol"] * 100.0) if out["last_vol"] else None,
        }
    return out


def top_yoy_increases(per_sign: dict[str, dict], signs_meta: dict[str, dict],
                      tier: int = 20, n: int = 25, min_last: int = 200) -> list[dict]:
    """Top signs by absolute increase in <tier>+over count YoY."""
    items = []
    for sid, r in per_sign.items():
        if r["last_vol"] == 0 or r["this_vol"] == 0:
            continue
        last = r["last"][tier]
        this = r["this"][tier]
        if last < min_last:  # avoid noise
            continue
        s = signs_meta.get(sid)
        if not s:
            continue
        items.append({
            "sign_id": sid,
            "name": s["name"] or s["address"] or f"Sign {sid}",
            "ward": s["ward"],
            "limit": s["limit"],
            "last": last,
            "this": this,
            "delta": this - last,
            "pct": ((this - last) / last * 100.0) if last > 0 else None,
            "this_vol": r["this_vol"],
            "last_vol": r["last_vol"],
        })
    items.sort(key=lambda x: x["delta"], reverse=True)
    return items[:n]


def top_movers(prepost_signs: dict[str, dict], signs_meta: dict[str, dict],
               n: int = 25, min_vehicles_pre: int = 0) -> list[dict]:
    """Top signs by DiD-adjusted speeding change (largest positive first)."""
    items = []
    for sid, st in prepost_signs.items():
        did = st.get("did")
        if did is None:
            continue
        s = signs_meta.get(sid)
        if not s:
            continue
        items.append({
            "sign_id": sid,
            "name": s["name"] or s["address"] or f"Sign {sid}",
            "address": s["address"],
            "ward": s["ward"],
            "ward_name": s["ward_name"],
            "limit": s["limit"],
            "did": did,
            "delta_2025": st.get("delta_2025"),
            "pre_2025": st.get("pre_2025"),
            "post_2025": st.get("post_2025"),
        })
    items.sort(key=lambda x: x["did"], reverse=True)
    return items[:n]


# ---------- pre/post DiD ----------

def _window_mean(monthly_map: dict[str, dict], months: tuple[str, ...]) -> float | None:
    """Volume-weighted mean across the given months. Requires data in all months."""
    over = 0
    volume = 0
    for m in months:
        b = monthly_map.get(m)
        if not b or not b.get("volume"):
            return None
        over += int(round(b["weighted"] / 100.0 * b["volume"]))
        volume += b["volume"]
    return (over / volume * 100.0) if volume else None


def _window_mean_pcts(pcts: dict[str, float], months: tuple[str, ...]) -> float | None:
    vals = [pcts.get(m) for m in months]
    if any(v is None for v in vals):
        return None
    return sum(vals) / len(vals)  # equal-weight monthly mean for a single sign


def _prepost_for_series(series: dict[str, dict]) -> dict:
    """Compute pre/post + DiD numbers for one monthly map (city, ward, or bin)."""
    pre25 = _window_mean(series, PRE_2025)
    post25 = _window_mean(series, POST_2025)
    pre24 = _window_mean(series, PRE_2024)
    post24 = _window_mean(series, POST_2024)
    delta25 = (post25 - pre25) if (pre25 is not None and post25 is not None) else None
    delta24 = (post24 - pre24) if (pre24 is not None and post24 is not None) else None
    did = (delta25 - delta24) if (delta25 is not None and delta24 is not None) else None
    return {
        "pre_2025": pre25, "post_2025": post25, "delta_2025": delta25,
        "pre_2024": pre24, "post_2024": post24, "delta_2024": delta24,
        "did": did,
    }


def compute_prepost_groups(groups: dict) -> dict:
    """DiD numbers for an arbitrary {group_key: monthly_map} dict (used for distance bins)."""
    return {g: _prepost_for_series(series) for g, series in groups.items()}


def compute_prepost(city: dict[str, dict], wards: dict[int, dict[str, dict]],
                    signs_monthly: dict[str, dict[str, float]],
                    signs_meta: dict[str, dict]) -> dict:
    """Compute pre/post + DiD numbers for city, each ward, and each sign."""

    def for_series(series: dict[str, dict]) -> dict:
        pre25 = _window_mean(series, PRE_2025)
        post25 = _window_mean(series, POST_2025)
        pre24 = _window_mean(series, PRE_2024)
        post24 = _window_mean(series, POST_2024)
        delta25 = (post25 - pre25) if (pre25 is not None and post25 is not None) else None
        delta24 = (post24 - pre24) if (pre24 is not None and post24 is not None) else None
        did = (delta25 - delta24) if (delta25 is not None and delta24 is not None) else None
        return {
            "pre_2025": pre25, "post_2025": post25, "delta_2025": delta25,
            "pre_2024": pre24, "post_2024": post24, "delta_2024": delta24,
            "did": did,
        }

    city_stats = for_series(city)
    ward_stats = {w: for_series(series) for w, series in wards.items()}

    sign_stats: dict[str, dict] = {}
    for sid, pcts in signs_monthly.items():
        pre25 = _window_mean_pcts(pcts, PRE_2025)
        post25 = _window_mean_pcts(pcts, POST_2025)
        pre24 = _window_mean_pcts(pcts, PRE_2024)
        post24 = _window_mean_pcts(pcts, POST_2024)
        delta25 = (post25 - pre25) if (pre25 is not None and post25 is not None) else None
        delta24 = (post24 - pre24) if (pre24 is not None and post24 is not None) else None
        did = (delta25 - delta24) if (delta25 is not None and delta24 is not None) else None
        sign_stats[sid] = {
            "pre_2025": pre25, "post_2025": post25, "delta_2025": delta25,
            "pre_2024": pre24, "post_2024": post24, "delta_2024": delta24,
            "did": did,
        }

    return {"city": city_stats, "wards": ward_stats, "signs": sign_stats}


def latest_per_sign(signs_monthly: dict[str, dict[str, float]], n_months: int = 3) -> dict[str, float]:
    """Average of the most recent up-to-n months for each sign, for map coloring."""
    out: dict[str, float] = {}
    for sid, pcts in signs_monthly.items():
        if not pcts:
            continue
        recent = sorted(pcts.items())[-n_months:]
        out[sid] = sum(v for _, v in recent) / len(recent)
    return out


def all_months(rows: list[dict]) -> list[str]:
    months = sorted({r["month"] for r in rows})
    return months
