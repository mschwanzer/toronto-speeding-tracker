"""When-speeding-happens aggregator.

Streams the 2023 detailed hourly WYS archive and produces hour-of-day and
day-of-week breakdowns of speeding by *severity* (over the posted limit) —
needed to debunk the "speeding doesn't matter when kids aren't around"
framing with actual numbers.

Input:  data/raw/wys_stationary_detailed_2023.zip   (≈1.9 GB uncompressed)
Output: data/processed/whenspeed.json               (~1 MB)

Each input row is (sign, datetime hour, speed_bin, volume). We use the sign's
posted limit (passed in from the regular monthly pipeline) to convert speed_bin
into over_limit tiers (≥0, ≥10, ≥20, ≥30 km/h over).

City roll-up: hour-of-day × tier (weekday and weekend), DOW × tier.
Per-sign: same shape, scoped to that sign.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging
import zipfile
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
ARCHIVE = HERE / "data" / "raw" / "wys_stationary_detailed_2023.zip"
OUT_PATH = HERE / "data" / "processed" / "whenspeed.json"
YEAR_PREFIX = "wys_stationary_detailed_2023"

TIERS = (10, 20, 30)  # over the posted limit
SCHOOL_HOURS = set(range(8, 15))  # Mon-Fri 8:00-14:59


def _parse_bin_lower(bin_s: str) -> int | None:
    # Formats seen: "[15,20)", "[100,)", "[100+)"
    if not bin_s or bin_s[0] != "[":
        return None
    end = 1
    while end < len(bin_s) and bin_s[end].isdigit():
        end += 1
    if end == 1:
        return None
    try:
        return int(bin_s[1:end])
    except ValueError:
        return None


def process(signs: dict, zip_path: Path = ARCHIVE, out_path: Path = OUT_PATH) -> Path:
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)

    # Posted limit per sign_id (falls back to None for unknown signs)
    sign_limit: dict[str, int] = {sid: s["limit"] for sid, s in signs.items() if s.get("limit")}

    # Per-sign accumulators:
    #   hour × tier:   {sid: {tier: [24]}}  for weekday and weekend
    #   dow × tier:    {sid: {tier: [7]}}
    #   totals per tier
    def _zero_tiers_24(): return {t: [0] * 24 for t in TIERS}
    def _zero_tiers_7():  return {t: [0] * 7 for t in TIERS}

    sign_wk_hour_tier: dict[str, dict[int, list[int]]] = defaultdict(_zero_tiers_24)
    sign_we_hour_tier: dict[str, dict[int, list[int]]] = defaultdict(_zero_tiers_24)
    sign_dow_tier: dict[str, dict[int, list[int]]] = defaultdict(_zero_tiers_7)
    sign_wk_hour_all: dict[str, list[int]] = defaultdict(lambda: [0] * 24)
    sign_we_hour_all: dict[str, list[int]] = defaultdict(lambda: [0] * 24)
    sign_dow_all: dict[str, list[int]] = defaultdict(lambda: [0] * 7)
    # Splits for the headline metric: school-hours vs not, per tier
    sign_school_tier: dict[str, dict[int, int]] = defaultdict(lambda: {t: 0 for t in TIERS})
    sign_outside_tier: dict[str, dict[int, int]] = defaultdict(lambda: {t: 0 for t in TIERS})

    with zipfile.ZipFile(zip_path) as z:
        members = sorted(n for n in z.namelist()
                         if n.startswith(YEAR_PREFIX) and n.endswith(".csv"))
        log.info("processing %d monthly files from %s", len(members), zip_path.name)
        # Small cache of parsed (datetime → (hour, dow, is_school_hour))
        for member in members:
            log.info("  reading %s", member)
            with z.open(member) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                reader = csv.reader(text)
                header = next(reader)
                idx = {c: i for i, c in enumerate(header)}
                isign = idx["sign_id"]; idt = idx["datetime_bin"]
                ibin = idx["speed_bin"]; ivol = idx["volume"]
                rows = 0; kept = 0
                dt_cache: dict[str, tuple[int, int, bool]] = {}
                bin_cache: dict[str, int] = {}
                for parts in reader:
                    rows += 1
                    if len(parts) <= ivol:
                        continue
                    sid = parts[isign].strip()
                    if sid not in sign_limit:
                        continue
                    try:
                        vol = int(float(parts[ivol]))
                    except (ValueError, TypeError):
                        continue
                    if vol <= 0:
                        continue
                    bin_s = parts[ibin]
                    lo = bin_cache.get(bin_s)
                    if lo is None:
                        lo = _parse_bin_lower(bin_s)
                        if lo is None:
                            continue
                        bin_cache[bin_s] = lo
                    over = lo - sign_limit[sid]
                    if over < TIERS[0]:
                        # Track total weekday/weekend volume for share denominators
                        pass  # don't skip yet — we still need the date parse
                    dt_s = parts[idt].strip()
                    parsed = dt_cache.get(dt_s)
                    if parsed is None:
                        if len(dt_s) < 13 or dt_s[4] != "-" or dt_s[10] != "T":
                            continue
                        try:
                            hour = int(dt_s[11:13])
                            y = int(dt_s[0:4]); mo = int(dt_s[5:7]); d = int(dt_s[8:10])
                            dow = _dt.date(y, mo, d).weekday()
                        except ValueError:
                            continue
                        if hour < 0 or hour > 23:
                            continue
                        is_school = (dow < 5) and (hour in SCHOOL_HOURS)
                        parsed = (hour, dow, is_school)
                        dt_cache[dt_s] = parsed
                    hour, dow, is_school = parsed
                    is_weekend = dow >= 5
                    if is_weekend:
                        sign_we_hour_all[sid][hour] += vol
                    else:
                        sign_wk_hour_all[sid][hour] += vol
                    sign_dow_all[sid][dow] += vol
                    if over >= TIERS[0]:
                        for t in TIERS:
                            if over >= t:
                                if is_weekend:
                                    sign_we_hour_tier[sid][t][hour] += vol
                                else:
                                    sign_wk_hour_tier[sid][t][hour] += vol
                                sign_dow_tier[sid][t][dow] += vol
                                if is_school:
                                    sign_school_tier[sid][t] += vol
                                else:
                                    sign_outside_tier[sid][t] += vol
                    kept += 1
                log.info("    rows=%s kept=%s", rows, kept)

    # Roll up to city level
    city_wk_hour_tier = {t: [0] * 24 for t in TIERS}
    city_we_hour_tier = {t: [0] * 24 for t in TIERS}
    city_dow_tier = {t: [0] * 7 for t in TIERS}
    city_wk_hour_all = [0] * 24
    city_we_hour_all = [0] * 24
    city_dow_all = [0] * 7
    city_school_tier = {t: 0 for t in TIERS}
    city_outside_tier = {t: 0 for t in TIERS}

    out_signs: dict[str, dict] = {}
    all_ids = (set(sign_wk_hour_tier) | set(sign_we_hour_tier) | set(sign_dow_tier)
               | set(sign_wk_hour_all) | set(sign_we_hour_all))
    for sid in all_ids:
        total_vol = sum(sign_wk_hour_all.get(sid, [0] * 24)) + sum(sign_we_hour_all.get(sid, [0] * 24))
        if total_vol <= 0:
            continue
        wk = sign_wk_hour_tier.get(sid, _zero_tiers_24() if False else {t: [0] * 24 for t in TIERS})
        we = sign_we_hour_tier.get(sid, {t: [0] * 24 for t in TIERS})
        dw = sign_dow_tier.get(sid, {t: [0] * 7 for t in TIERS})
        for t in TIERS:
            for h in range(24):
                city_wk_hour_tier[t][h] += wk[t][h]
                city_we_hour_tier[t][h] += we[t][h]
            for d in range(7):
                city_dow_tier[t][d] += dw[t][d]
            city_school_tier[t] += sign_school_tier.get(sid, {}).get(t, 0)
            city_outside_tier[t] += sign_outside_tier.get(sid, {}).get(t, 0)
        for h in range(24):
            city_wk_hour_all[h] += sign_wk_hour_all.get(sid, [0] * 24)[h]
            city_we_hour_all[h] += sign_we_hour_all.get(sid, [0] * 24)[h]
        for d in range(7):
            city_dow_all[d] += sign_dow_all.get(sid, [0] * 7)[d]

        # Per-sign derived shares used for ranking samples
        tot_ge10 = sum(wk[10]) + sum(we[10])
        # Overnight = 22:00-05:59
        overnight_idx = list(range(22, 24)) + list(range(0, 6))
        overnight_ge10 = sum(wk[10][h] for h in overnight_idx) + sum(we[10][h] for h in overnight_idx)
        # Weekend speeders share
        wknd_ge10 = sum(we[10])
        # School vs outside
        school_ge10 = sign_school_tier.get(sid, {}).get(10, 0)
        outside_ge10 = sign_outside_tier.get(sid, {}).get(10, 0)
        # Worst hour (any day) for 10+
        hour_combined = [wk[10][h] + we[10][h] for h in range(24)]
        worst_hour = max(range(24), key=lambda h: hour_combined[h]) if any(hour_combined) else None

        out_signs[sid] = {
            "wk_hour_ge10": wk[10], "wk_hour_ge20": wk[20], "wk_hour_ge30": wk[30],
            "we_hour_ge10": we[10], "we_hour_ge20": we[20], "we_hour_ge30": we[30],
            "dow_ge10": dw[10], "dow_ge20": dw[20], "dow_ge30": dw[30],
            "wk_hour_all": sign_wk_hour_all.get(sid, [0] * 24),
            "we_hour_all": sign_we_hour_all.get(sid, [0] * 24),
            "total_volume": total_vol,
            "total_ge10": tot_ge10,
            "total_ge20": sum(wk[20]) + sum(we[20]),
            "total_ge30": sum(wk[30]) + sum(we[30]),
            "overnight_ge10": overnight_ge10,
            "weekend_ge10": wknd_ge10,
            "school_ge10": school_ge10,
            "outside_ge10": outside_ge10,
            "share_overnight_ge10": (overnight_ge10 / tot_ge10) if tot_ge10 > 0 else 0,
            "share_weekend_ge10": (wknd_ge10 / tot_ge10) if tot_ge10 > 0 else 0,
            "share_outside_school_ge10": (outside_ge10 / max(school_ge10 + outside_ge10, 1)),
            "worst_hour": worst_hour,
        }

    city = {
        "wk_hour_ge10": city_wk_hour_tier[10],
        "wk_hour_ge20": city_wk_hour_tier[20],
        "wk_hour_ge30": city_wk_hour_tier[30],
        "we_hour_ge10": city_we_hour_tier[10],
        "we_hour_ge20": city_we_hour_tier[20],
        "we_hour_ge30": city_we_hour_tier[30],
        "dow_ge10": city_dow_tier[10],
        "dow_ge20": city_dow_tier[20],
        "dow_ge30": city_dow_tier[30],
        "wk_hour_all": city_wk_hour_all,
        "we_hour_all": city_we_hour_all,
        "dow_all": city_dow_all,
        "school_tiers": {str(t): city_school_tier[t] for t in TIERS},
        "outside_tiers": {str(t): city_outside_tier[t] for t in TIERS},
        "n_signs": len(out_signs),
    }

    # Headline shares
    for t in TIERS:
        denom = city_school_tier[t] + city_outside_tier[t]
        city[f"share_outside_school_ge{t}"] = (city_outside_tier[t] / denom) if denom else 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"city": city, "signs": out_signs}, separators=(",", ":")))
    log.info("wrote %s (%d signs, %.1f KB)", out_path, len(out_signs), out_path.stat().st_size / 1024)
    log.info("city shares outside school hours: ge10=%.1f%% ge20=%.1f%% ge30=%.1f%%",
             city["share_outside_school_ge10"] * 100,
             city["share_outside_school_ge20"] * 100,
             city["share_outside_school_ge30"] * 100)
    return out_path


def load(path: Path = OUT_PATH) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


if __name__ == "__main__":
    import sys as _sys
    HERE_S = str(Path(__file__).parent)
    if HERE_S not in _sys.path:
        _sys.path.insert(0, HERE_S)
    from aggregate import load_signs  # noqa: E402
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signs_path = HERE / "data" / "raw" / "school-safety-zone-watch-your-speed-program-locations.csv"
    process(load_signs(signs_path))
