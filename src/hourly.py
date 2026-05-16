"""Stream the 2023 hourly WYS archive into per-sign time-of-day profiles.

Input:  data/raw/wys_stationary_detailed_2023.zip  (≈1.9 GB uncompressed)
Output: data/processed/hourly_profiles.json        (≈300 KB)

Each row of the source CSV is one (sign, datetime, speed_bin) tuple — we sum
across speed bins to recover the sign-hour volume, then aggregate to:

  * weekday hour-of-day average (24 values per sign)
  * weekend hour-of-day average (24 values per sign)
  * day-of-week average (7 values per sign, Mon=0)

A normalised "shape" vector is also stored so signs of different traffic
volumes can be compared on pattern alone (used by the outlier ranking).
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging
import math
import zipfile
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

ARCHIVE = Path(__file__).parent / "data" / "raw" / "wys_stationary_detailed_2023.zip"
OUT_PATH = Path(__file__).parent / "data" / "processed" / "hourly_profiles.json"
YEAR_PREFIX = "wys_stationary_detailed_2023"  # ignore the stray 2018 file in the archive


def process(zip_path: Path = ARCHIVE, out_path: Path = OUT_PATH) -> Path:
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)

    # accumulators
    sign_hour_wk = defaultdict(lambda: [0] * 24)
    sign_hour_we = defaultdict(lambda: [0] * 24)
    sign_dow = defaultdict(lambda: [0] * 7)
    # distinct operating dates per sign — used as proper divisor (signs only sample
    # during their schedule, so 261 weekdays is wrong for most school-zone signs)
    sign_wk_dates: dict[str, set] = defaultdict(set)
    sign_we_dates: dict[str, set] = defaultdict(set)
    sign_dow_dates: dict[str, list] = defaultdict(lambda: [set() for _ in range(7)])

    with zipfile.ZipFile(zip_path) as z:
        members = sorted(n for n in z.namelist() if n.startswith(YEAR_PREFIX) and n.endswith(".csv"))
        log.info("processing %d monthly files from %s", len(members), zip_path.name)
        for member in members:
            log.info("  reading %s", member)
            with z.open(member) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                reader = csv.reader(text)
                header = next(reader)
                idx = {c: i for i, c in enumerate(header)}
                isign = idx["sign_id"]
                idt = idx["datetime_bin"]
                ivol = idx["volume"]
                rows = 0
                kept = 0
                for parts in reader:
                    rows += 1
                    if len(parts) <= ivol:
                        continue
                    sid = parts[isign].strip()
                    dt_s = parts[idt].strip()
                    if not sid or not dt_s:
                        continue
                    try:
                        vol = int(float(parts[ivol]))
                    except (ValueError, TypeError):
                        continue
                    if vol <= 0:
                        continue
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
                    date_s = dt_s[:10]
                    if dow >= 5:
                        sign_hour_we[sid][hour] += vol
                        sign_we_dates[sid].add(date_s)
                    else:
                        sign_hour_wk[sid][hour] += vol
                        sign_wk_dates[sid].add(date_s)
                    sign_dow[sid][dow] += vol
                    sign_dow_dates[sid][dow].add(date_s)
                    kept += 1
                log.info("    rows=%s kept=%s", rows, kept)

    # produce normalised shapes + per-hour average volume per sign
    out = {}
    city_hour_wk = [0.0] * 24
    city_hour_we = [0.0] * 24
    city_dow = [0.0] * 7
    n_signs = 0

    city_per_hour_wk_acc: list[float] = [0.0] * 24
    city_per_hour_we_acc: list[float] = [0.0] * 24
    city_per_dow_acc: list[float] = [0.0] * 7
    city_shape_wk_acc: list[float] = [0.0] * 24

    for sid in sign_hour_wk.keys() | sign_hour_we.keys():
        wk = sign_hour_wk[sid]
        we = sign_hour_we[sid]
        dow_sum = sign_dow[sid]
        total = sum(wk) + sum(we)
        if total <= 0:
            continue
        n_wk_days = max(len(sign_wk_dates[sid]), 1)
        n_we_days = max(len(sign_we_dates[sid]), 1)
        wk_per_hour = [wk[h] / n_wk_days for h in range(24)]
        we_per_hour = [we[h] / n_we_days for h in range(24)]
        dow_per_day = [(dow_sum[d] / max(len(sign_dow_dates[sid][d]), 1)) for d in range(7)]
        sum_wk = sum(wk) or 1
        shape_wk = [v / sum_wk for v in wk]
        out[sid] = {
            "wk_per_hour": [round(v, 1) for v in wk_per_hour],
            "we_per_hour": [round(v, 1) for v in we_per_hour],
            "dow_per_day": [round(v, 1) for v in dow_per_day],
            "shape_wk": [round(v, 4) for v in shape_wk],
            "total_volume": int(total),
            "n_weekday_days": len(sign_wk_dates[sid]),
            "n_weekend_days": len(sign_we_dates[sid]),
        }
        for h in range(24):
            city_per_hour_wk_acc[h] += wk_per_hour[h]
            city_per_hour_we_acc[h] += we_per_hour[h]
            city_shape_wk_acc[h] += shape_wk[h]
        for d in range(7):
            city_per_dow_acc[d] += dow_per_day[d]
        n_signs += 1

    n = max(n_signs, 1)
    city_shape_wk = [v / n for v in city_shape_wk_acc]
    city = {
        "wk_per_hour_avg": [round(v / n, 1) for v in city_per_hour_wk_acc],
        "we_per_hour_avg": [round(v / n, 1) for v in city_per_hour_we_acc],
        "dow_per_day_avg": [round(v / n, 1) for v in city_per_dow_acc],
        "shape_wk": [round(v, 4) for v in city_shape_wk],
        "n_signs": n_signs,
    }

    # outlier score: L1 distance between each sign's weekday shape and city weekday shape
    for sid, payload in out.items():
        diff = sum(abs(a - b) for a, b in zip(payload["shape_wk"], city["shape_wk"]))
        payload["pattern_distance"] = round(diff, 4)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"city": city, "signs": out}, separators=(",", ":")))
    log.info("wrote %s (%d signs, %.1f KB)", out_path, len(out), out_path.stat().st_size / 1024)
    return out_path


def load_profiles(path: Path = OUT_PATH) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    process()
