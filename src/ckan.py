"""Tiny CKAN client for Toronto Open Data.

Fetches CSV resources to a local cache, only re-downloading when the
upstream resource's `last_modified` timestamp is newer than the cached file.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Iterator

import requests

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
PACKAGE_SHOW = f"{CKAN_BASE}/api/3/action/package_show"
DUMP_URL = f"{CKAN_BASE}/datastore/dump"

log = logging.getLogger(__name__)


class CKANClient:
    def __init__(self, cache_dir: Path, force_refresh: bool = False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.force_refresh = force_refresh

    def _resource_meta(self, package_id: str, resource_id: str) -> dict:
        r = requests.get(PACKAGE_SHOW, params={"id": package_id}, timeout=30)
        r.raise_for_status()
        pkg = r.json()["result"]
        for res in pkg["resources"]:
            if res["id"] == resource_id:
                return res
        raise KeyError(f"resource {resource_id} not in package {package_id}")

    def _is_fresh(self, csv_path: Path, last_modified: str | None) -> bool:
        if self.force_refresh or not csv_path.exists():
            return False
        if not last_modified:
            return True  # nothing to compare to; assume fresh
        try:
            remote = _dt.datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
        except ValueError:
            return True
        if remote.tzinfo is None:  # CKAN timestamps are UTC by convention
            remote = remote.replace(tzinfo=_dt.timezone.utc)
        local_mtime = _dt.datetime.fromtimestamp(csv_path.stat().st_mtime, tz=_dt.timezone.utc)
        return local_mtime >= remote

    def fetch_csv(self, package_id: str, resource_id: str) -> Path:
        """Download (if stale) and return the path to the cached CSV."""
        csv_path = self.cache_dir / f"{package_id}.csv"
        meta_path = self.cache_dir / f"{package_id}.meta.json"

        meta = self._resource_meta(package_id, resource_id)
        last_modified = meta.get("last_modified") or meta.get("metadata_modified")

        if self._is_fresh(csv_path, last_modified):
            log.info("cache hit: %s (last_modified=%s)", csv_path.name, last_modified)
            return csv_path

        url = f"{DUMP_URL}/{resource_id}"
        log.info("fetching %s -> %s", url, csv_path.name)
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp = csv_path.with_suffix(".csv.tmp")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
            os.replace(tmp, csv_path)

        meta_path.write_text(json.dumps({
            "package_id": package_id,
            "resource_id": resource_id,
            "last_modified": last_modified,
            "fetched_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
            "bytes": csv_path.stat().st_size,
        }, indent=2))
        return csv_path


def iter_csv(path: Path) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            yield row
