#!/usr/bin/env python3
"""Manual Google Play publisher monitor for City Bank PLC."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

PUBLISHER_NAME = os.getenv("PLAY_PUBLISHER_NAME", "City Bank PLC")
PUBLISHER_URL = os.getenv(
    "PLAY_PUBLISHER_URL",
    "https://play.google.com/store/apps/developer?id=City+Bank+PLC",
)
COUNTRY = os.getenv("PLAY_COUNTRY", "bd")
LANGUAGE = os.getenv("PLAY_LANGUAGE", "en")
TIMEOUT = 25

LOG = logging.getLogger("play_monitor")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

PACKAGE_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: dict) -> dict:
    """Read a JSON object without silently replacing corrupt existing data."""
    if not path.exists():
        return default.copy()

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot safely read {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")

    return value


def _write_json(path: Path, value: dict) -> None:
    """Atomically replace a JSON file after writing its complete contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _request(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response


class AppLinkParser(HTMLParser):
    """Extract package IDs from Google Play app links."""

    def __init__(self):
        super().__init__()
        self.package_ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key != "href" or not value:
                continue

            parsed = urlparse(value)
            if parsed.path.endswith("/store/apps/details"):
                package_id = parse_qs(parsed.query).get("id", [None])[0]
                if package_id and PACKAGE_RE.fullmatch(package_id):
                    self.package_ids.add(package_id)


def parse_version_from_html(html: str) -> str | None:
    """Extract an explicitly labelled version; return None if unavailable."""
    patterns = (
        r'"softwareVersion"\s*:\s*"([^"\\]{1,100})"',
        r'"softwareVersion"\s*:\s*([0-9]+(?:\.[0-9A-Za-z_-]+){1,5})',
        r'"version"\s*:\s*"([0-9]+(?:\.[0-9A-Za-z_-]+){1,5})"',
        r'itemprop=["\']softwareVersion["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']softwareVersion["\']',
    )

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if not match:
            continue

        version = unquote(match.group(1)).strip()
        if (
            version
            and len(version) <= 100
            and version.casefold() not in {
                "varies with device", "unknown", "n/a"
            }
        ):
            return version

    return None


def parse_updated_date_from_html(html: str) -> str | None:
    """Extract an explicitly available ISO update date."""
    patterns = (
        r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"uploadDate"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"updated"\s*:\s*"(\d{4}-\d{2}-\d{2})',
    )

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)

    # Also support Google Play's visible "Updated on" label.
    match = re.search(
        r"Updated on\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        html,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _parse_app(package_id: str, html: str) -> dict:
    """Parse listing fields without fabricating missing metadata."""
    title_match = (
        re.search(
            r'<meta[^>]+itemprop=["\']name["\'][^>]+content=["\']([^"\']+)',
            html,
            re.IGNORECASE,
        )
        or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
            r'property=["\']og:title["\']',
            html,
            re.IGNORECASE,
        )
    )

    title = (
        unquote(title_match.group(1)).strip()
        if title_match else package_id
    )
    title = re.sub(r"\s*-\s*Apps on Google Play$", "", title, flags=re.I)

    developer_match = re.search(
        r'"author"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
        html,
        re.IGNORECASE,
    )

    return {
        "package_id": package_id,
        "name": title,
        "version": parse_version_from_html(html) or "",
        "last_updated": parse_updated_date_from_html(html) or "",
        "developer": developer_match.group(1) if developer_match else "",
        "play_url": (
            "https://play.google.com/store/apps/details?id="
            + package_id
        ),
    }


def discover_package_ids() -> tuple[set[str], str]:
    """Discover publisher apps; fail closed if discovery cannot be trusted."""
    errors: list[str] = []

    try:
        response = _request(PUBLISHER_URL)
        parser = AppLinkParser()
        parser.feed(response.text)

        if parser.package_ids:
            LOG.info("Publisher page exposed %d package IDs",
                     len(parser.package_ids))
            return parser.package_ids, "publisher_html"

        errors.append("Publisher page contained no app links")
    except Exception as exc:
        errors.append(f"Publisher page: {exc}")
        LOG.warning("Publisher page discovery failed: %s", exc)

    # Search can find candidates, but cannot guarantee the complete inventory.
    try:
        from google_play_scraper import app, search

        results = search(
            PUBLISHER_NAME,
            n=100,
            lang=LANGUAGE,
            country=COUNTRY,
        )

        verified: set[str] = set()
        for item in results:
            package_id = item.get("appId")
            if not package_id or not PACKAGE_RE.fullmatch(package_id):
                continue

            try:
                metadata = app(
                    package_id,
                    lang=LANGUAGE,
                    country=COUNTRY,
                )
                developer = (metadata.get("developer") or "").strip()
                if developer.casefold() == PUBLISHER_NAME.strip().casefold():
                    verified.add(package_id)
            except Exception as exc:
                LOG.warning("Could not verify %s: %s", package_id, exc)

        if verified:
            # Do not silently claim a search result is the complete publisher list.
            raise RuntimeError(
                "Only partial search discovery was available "
                f"({len(verified)} verified apps); refusing to update inventory."
            )

        errors.append("Search returned no verified publisher apps")
    except RuntimeError:
        raise
    except Exception as exc:
        errors.append(f"Search fallback: {exc}")

    raise RuntimeError(
        "App discovery failed; existing inventory was preserved. "
        + " | ".join(errors)
    )


def run_scan() -> dict:
    """Run a scan and atomically save inventory/history only on success."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current_file = DATA_DIR / "current.json"
    history_file = DATA_DIR / "history.json"

    current = _read_json(
        current_file, {"apps": [], "last_scan": None}
    )
    history = _read_json(
        history_file, {"scans": [], "changes": []}
    )

    previous_apps = current.get("apps", [])
    previous_by_id = {
        item.get("package_id"): item
        for item in previous_apps
        if item.get("package_id")
    }

    # Any discovery failure occurs before either inventory file is written.
    package_ids, source = discover_package_ids()
    if not package_ids:
        raise RuntimeError("Discovery returned no apps; inventory preserved.")

    timestamp = now_iso()
    fresh_apps = []
    changes = []

    for package_id in sorted(package_ids):
        response = _request(
            "https://play.google.com/store/apps/details"
            f"?id={package_id}&hl={LANGUAGE}&gl={COUNTRY.upper()}"
        )
        details = _parse_app(package_id, response.text)
        old = previous_by_id.get(package_id)

        details["first_seen"] = (
            old.get("first_seen", timestamp) if old else timestamp
        )
        details["last_seen"] = timestamp

        old_version = (old or {}).get("version", "").strip()
        new_version = details["version"].strip()

        if old is None and previous_by_id:
            changes.append({
                "type": "NEW_APP",
                "package_id": package_id,
                "name": details["name"],
                "detected_at": timestamp,
            })
        elif old and old_version and new_version and old_version != new_version:
            changes.append({
                "type": "VERSION_UPDATED",
                "package_id": package_id,
                "name": details["name"],
                "previous_version": old_version,
                "current_version": new_version,
                "detected_at": timestamp,
            })

        # Do not erase a previously known version when fresh metadata is absent.
        if old and not new_version:
            details["version"] = old.get("version", "")
        if old and not details["last_updated"]:
            details["last_updated"] = old.get("last_updated", "")

        fresh_apps.append(details)

    # Keep previously tracked apps if they were not returned this time.
    discovered_ids = {item["package_id"] for item in fresh_apps}
    for package_id, old in previous_by_id.items():
        if package_id not in discovered_ids:
            fresh_apps.append(old)

    snapshot = {
        "publisher": {
            "name": PUBLISHER_NAME,
            "url": PUBLISHER_URL,
        },
        "last_scan": timestamp,
        "scan_source": source,
        "apps": sorted(fresh_apps, key=lambda item: item["name"].casefold()),
    }

    history.setdefault("scans", []).append({
        "timestamp": timestamp,
        "source": source,
        "discovered": len(package_ids),
        "status": "success",
    })
    history.setdefault("changes", []).extend(changes)

    # Both files are written only after discovery and every app fetch succeeds.
    _write_json(current_file, snapshot)
    _write_json(history_file, history)

    LOG.info(
        "Scan completed: %d discovered, %d changes",
        len(package_ids), len(changes),
    )
    return snapshot


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        run_scan()
        return 0
    except Exception as exc:
        LOG.error("Scan failed safely: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
