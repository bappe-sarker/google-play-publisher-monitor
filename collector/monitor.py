"""Collect public Google Play apps for a publisher and maintain a conservative inventory."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, parse_qs

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CURRENT_FILE = DATA_DIR / "current.json"
HISTORY_FILE = DATA_DIR / "history.json"
PUBLISHER_NAME = os.getenv("PLAY_PUBLISHER_NAME", "City Bank PLC")
PUBLISHER_URL = os.getenv(
    "PLAY_PUBLISHER_URL",
    "https://play.google.com/store/apps/developer?id=City+Bank+PLC",
)
COUNTRY = os.getenv("PLAY_COUNTRY", "bd")
LANGUAGE = os.getenv("PLAY_LANGUAGE", "en")
TIMEOUT = 25
LOG = logging.getLogger("play_monitor")


class AppLinkParser(HTMLParser):
    """Find app package IDs from Google Play publisher HTML."""
    def __init__(self) -> None:
        super().__init__()
        self.package_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key != "href" or not value:
                continue
            parsed = urlparse(value)
            if parsed.path.endswith("/store/apps/details"):
                package_id = parse_qs(parsed.query).get("id", [None])[0]
                if package_id and re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_id):
                    self.package_ids.add(package_id)


def parse_version_from_html(html: str) -> str | None:
    """Extract an explicit version from structured or rendered Play metadata only."""
    # Google Play has used JSON-LD and embedded JSON with a version field.
    patterns = [
        r'"softwareVersion"\s*:\s*"([^"\\]{1,100})"',
        r'"version"\s*:\s*"([^"\\]{1,100})"',
        r'itemprop=["\']softwareVersion["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']softwareVersion["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = unquote(match.group(1)).strip()
            if value and value.lower() not in {"varies with device", "unknown", "n/a"}:
                return value
    return None


def parse_updated_date_from_html(html: str) -> str | None:
    patterns = [
        r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"uploadDate"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"updated"\s*:\s*"(\d{4}-\d{2}-\d{2})',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _request(url: str) -> requests.Response:
    response = requests.get(
        url,
        timeout=TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CityBankPlayMonitor/1.0)"},
    )
    response.raise_for_status()
    return response


def discover_package_ids() -> tuple[set[str], str]:
    """Discover app IDs from publisher HTML, falling back to google-play-scraper."""
    errors: list[str] = []
    try:
        response = _request(PUBLISHER_URL)
        parser = AppLinkParser()
        parser.feed(response.text)
        if parser.package_ids:
            LOG.info("Publisher HTML exposed %d app IDs", len(parser.package_ids))
            return parser.package_ids, "publisher_html"
        errors.append("publisher HTML contained no app links")
    except Exception as exc:  # noqa: BLE001 - collector must log network/parser failures
        errors.append(f"publisher HTML: {exc}")

    try:
        from google_play_scraper import developer
        rows = developer(PUBLISHER_NAME, lang=LANGUAGE, country=COUNTRY)
        ids = {str(row["appId"]) for row in rows if row.get("appId")}
        if ids:
            LOG.info("google-play-scraper discovered %d app IDs", len(ids))
            return ids, "google_play_scraper"
        errors.append("google-play-scraper returned no apps")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"google-play-scraper: {exc}")
    raise RuntimeError("App discovery failed: " + " | ".join(errors))


def fetch_app_metadata(package_id: str) -> dict[str, Any]:
    """Prefer the structured library result; use HTML only to fill missing metadata."""
    result: dict[str, Any] = {
        "package_id": package_id,
        "name": None,
        "version": None,
        "last_updated": None,
        "play_store_url": f"https://play.google.com/store/apps/details?id={quote(package_id)}",
    }
    try:
        from google_play_scraper import app
        metadata = app(package_id, lang=LANGUAGE, country=COUNTRY)
        result["name"] = metadata.get("title")
        version = metadata.get("version")
        if isinstance(version, str) and version.strip() and version.strip().lower() != "varies with device":
            result["version"] = version.strip()
        updated = metadata.get("updated")
        if updated:
            if hasattr(updated, "strftime"):
                result["last_updated"] = updated.strftime("%Y-%m-%d")
            elif isinstance(updated, str):
                result["last_updated"] = updated[:10]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Structured metadata lookup failed for %s: %s", package_id, exc)

    if not result["version"] or not result["last_updated"] or not result["name"]:
        try:
            response = _request(result["play_store_url"] + "&hl=" + quote(LANGUAGE) + "&gl=" + quote(COUNTRY.upper()))
            html = response.text
            if not result["version"]:
                result["version"] = parse_version_from_html(html)
            if not result["last_updated"]:
                result["last_updated"] = parse_updated_date_from_html(html)
            if not result["name"]:
                title_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I)
                if title_match:
                    result["name"] = title_match.group(1).strip()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("HTML fallback failed for %s: %s", package_id, exc)
    return result


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        LOG.error("Could not read %s: %s", path, exc)
        raise


def run_scan() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    previous = _read_json(CURRENT_FILE, {"apps": [], "last_scan": None})
    old_apps = {item["package_id"]: item for item in previous.get("apps", []) if item.get("package_id")}
    scan_time = datetime.now(timezone.utc).isoformat(timespec="seconds")

    package_ids, discovery_method = discover_package_ids()
    if not package_ids:
        raise RuntimeError("Discovery returned an empty app list; existing inventory was not changed")

    collected: list[dict[str, Any]] = []
    errors: list[str] = []
    changes: list[dict[str, Any]] = []
    for package_id in sorted(package_ids):
        try:
            fresh = fetch_app_metadata(package_id)
            old = old_apps.get(package_id, {})
            # Metadata gaps are not evidence of a version change: retain prior known values.
            for field in ("name", "version", "last_updated"):
                if not fresh.get(field) and old.get(field):
                    fresh[field] = old[field]
            fresh["first_seen"] = old.get("first_seen", scan_time)
            fresh["last_seen"] = scan_time
            fresh["metadata_status"] = "complete" if fresh.get("version") and fresh.get("name") else "partial"
            if not old:
                changes.append({"type": "new_app", "package_id": package_id, "name": fresh.get("name"), "version": fresh.get("version"), "detected_at": scan_time})
            elif fresh.get("version") and old.get("version") and fresh["version"] != old["version"]:
                changes.append({"type": "version_changed", "package_id": package_id, "name": fresh.get("name") or old.get("name"), "previous_version": old["version"], "version": fresh["version"], "detected_at": scan_time})
            collected.append(fresh)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{package_id}: {exc}")
            LOG.exception("Failed to collect %s", package_id)
            if package_id in old_apps:
                retained = dict(old_apps[package_id])
                retained["last_seen"] = scan_time
                collected.append(retained)

    # Conservative completeness gate: if any discovered app failed or no usable detail was obtained,
    # preserve old records and do not publish a potentially destructive partial inventory.
    usable = sum(1 for app_item in collected if app_item.get("name") or app_item.get("version"))
    if errors or not collected or usable == 0:
        raise RuntimeError(
            f"Scan incomplete ({len(errors)} app errors, {usable} usable records); existing inventory was not replaced. "
            + "; ".join(errors[:8])
        )

    # Never delete prior package IDs just because a publisher page may have been truncated.
    present_ids = {item["package_id"] for item in collected}
    for package_id, old in old_apps.items():
        if package_id not in present_ids:
            retained = dict(old)
            retained["not_seen_in_latest_scan"] = True
            collected.append(retained)

    collected.sort(key=lambda item: (str(item.get("name") or "").lower(), item["package_id"]))
    snapshot = {
        "publisher_name": PUBLISHER_NAME,
        "publisher_url": PUBLISHER_URL,
        "country": COUNTRY,
        "last_scan": scan_time,
        "discovery_method": discovery_method,
        "app_count": len(collected),
        "apps": collected,
    }
    history = _read_json(HISTORY_FILE, {"scans": [], "changes": []})
    history.setdefault("scans", []).append({"scanned_at": scan_time, "discovered_count": len(package_ids), "inventory_count": len(collected), "discovery_method": discovery_method, "new_apps": sum(c["type"] == "new_app" for c in changes), "version_changes": sum(c["type"] == "version_changed" for c in changes)})
    history.setdefault("changes", []).extend(changes)
    # Keep the repo data bounded without losing recent history.
    history["scans"] = history["scans"][-500:]
    history["changes"] = history["changes"][-2000:]

    CURRENT_FILE.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOG.info("Scan completed: %d inventory records, %d changes", len(collected), len(changes))
    return {"snapshot": snapshot, "changes": changes}


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        result = run_scan()
        print(json.dumps({"app_count": result["snapshot"]["app_count"], "changes": len(result["changes"])}, indent=2))
    except Exception as exc:  # noqa: BLE001
        LOG.error("Scan failed safely: %s", exc)
        sys.exit(1)
