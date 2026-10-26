#!/usr/bin/env python3
"""Manual Google Play Publisher Monitor for City Bank PLC."""

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

# Paths
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CURRENT_FILE = DATA_DIR / "current.json"
HISTORY_FILE = DATA_DIR / "history.json"

# Configuration
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


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path, default):
    """Read JSON without silently replacing corrupt existing data."""
    if not path.exists():
        return default.copy()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot safely read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")

    return data


def _write_json(path, data):
    """Write JSON atomically to an individual file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _request(url):
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response


class AppLinkParser(HTMLParser):
    """Extract package IDs from Google Play app links."""

    def __init__(self):
        super().__init__()
        self.package_ids = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key != "href" or not value:
                continue

            parsed = urlparse(value)
            if parsed.path.endswith("/store/apps/details"):
                package_id = parse_qs(parsed.query).get("id", [None])[0]
                if package_id and PACKAGE_RE.fullmatch(package_id):
                    self.package_ids.add(package_id)


def parse_version_from_html(html):
    """Return an explicitly exposed version, or None."""
    patterns = [
        r'"softwareVersion"\s*:\s*"([^"\\]{1,100})"',
        r'"softwareVersion"\s*:\s*([0-9]+(?:\.[0-9A-Za-z_-]+){1,5})',
        r'"version"\s*:\s*"([0-9]+(?:\.[0-9A-Za-z_-]+){1,5})"',
        r'itemprop=["\']softwareVersion["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']softwareVersion["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if not match:
            continue

        version = unquote(match.group(1)).strip()
        if (
            version
            and len(version) <= 100
            and version.casefold() not in {
                "varies with device",
                "unknown",
                "n/a",
            }
        ):
            return version

    return None


def parse_updated_date_from_html(html):
    """Extract an explicit update date, or None."""
    patterns = [
        r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"uploadDate"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"updated"\s*:\s*"(\d{4}-\d{2}-\d{2})',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)

    match = re.search(
        r"Updated on\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        html,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _parse_app(package_id, html):
    """Extract app metadata from a listing page."""
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
        if title_match
        else package_id
    )
    title = re.sub(r"\s*-\s*Apps on Google Play$", "", title, flags=re.I)

    return {
        "package_id": package_id,
        "name": title,
        "version": parse_version_from_html(html) or "",
        "last_updated": parse_updated_date_from_html(html) or "",
        "play_url": (
            "https://play.google.com/store/apps/details?id="
            + package_id
        ),
    }


def discover_package_ids():
    """Discover package IDs and identify the discovery source."""
    errors = []

    # Preferred method: publisher listing page.
    try:
        response = _request(PUBLISHER_URL)
        parser = AppLinkParser()
        parser.feed(response.text)

        if parser.package_ids:
            LOG.info("Found %d app links on publisher page",
                     len(parser.package_ids))
            return parser.package_ids, "publisher_html"

        errors.append("Publisher page contained no app links")
    except Exception as exc:
        errors.append(f"Publisher page: {exc}")
        LOG.warning("Publisher page failed: %s", exc)

    # Fallback: search candidates and verify publisher ownership.
    try:
        from google_play_scraper import app, search

        results = search(
            PUBLISHER_NAME,
            n=100,
            lang=LANGUAGE,
            country=COUNTRY,
        )

        verified = set()
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
            # Search does not guarantee complete publisher coverage.
            raise RuntimeError(
                f"Only {len(verified)} candidate apps were found through "
                "search. Completeness cannot be verified; inventory preserved."
            )

        errors.append("Search returned no verified apps")

    except RuntimeError:
        raise
    except Exception as exc:
        errors.append(f"Google Play search fallback: {exc}")

    raise RuntimeError(
        "App discovery failed; existing inventory was preserved. "
        + " | ".join(errors)
    )


def run_scan():
    """Scan apps and preserve existing data if collection fails."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Use module-level paths so tests can override them with monkeypatch.
    current_file = CURRENT_FILE
    history_file = HISTORY_FILE

    current = _read_json(current_file, {"apps": [], "last_scan": None})
    history = _read_json(history_file, {"scans": [], "changes": []})

    old_apps = current.get("apps", [])
    old_by_id = {
        item.get("package_id"): item
        for item in old_apps
        if item.get("package_id")
    }

    # Discovery errors happen before writing either inventory file.
    package_ids, source = discover_package_ids()
    if not package_ids:
        raise RuntimeError("No apps discovered; inventory was preserved.")

    timestamp = now_iso()
    fresh_apps = []
    changes = []

    # If any app request fails, stop before writing files.
    # This prevents a partial scan from overwriting the inventory.
    for package_id in sorted(package_ids):
        response = _request(
            "https://play.google.com/store/apps/details"
            f"?id={package_id}&hl={LANGUAGE}&gl={COUNTRY.upper()}"
        )
        details = _parse_app(package_id, response.text)
        previous = old_by_id.get(package_id)

        details["first_seen"] = (
            previous.get("first_seen", timestamp)
            if previous
            else timestamp
        )
        details["last_seen"] = timestamp

        old_version = (previous or {}).get("version", "").strip()
        new_version = details["version"].strip()

        if previous is None and old_by_id:
            changes.append({
                "type": "NEW_APP",
                "package_id": package_id,
                "name": details["name"],
                "detected_at": timestamp,
            })

        elif (
            previous
            and old_version
            and new_version
            and old_version != new_version
        ):
            changes.append({
                "type": "VERSION_UPDATED",
                "package_id": package_id,
                "name": details["name"],
                "previous_version": old_version,
                "current_version": new_version,
                "detected_at": timestamp,
            })

        # Keep known values when fresh metadata is missing.
        if previous:
            if not new_version:
                details["version"] = previous.get("version", "")
            if not details["last_updated"]:
                details["last_updated"] = previous.get("last_updated", "")

        fresh_apps.append(details)

    # Retain apps omitted from this scan; never infer that they were removed.
    discovered = {item["package_id"] for item in fresh_apps}
    for package_id, previous in old_by_id.items():
        if package_id not in discovered:
            fresh_apps.append(previous)

    snapshot = {
        "publisher": {
            "name": PUBLISHER_NAME,
            "url": PUBLISHER_URL,
        },
        "last_scan": timestamp,
        "scan_source": source,
        "apps": sorted(
            fresh_apps,
            key=lambda item: item.get("name", "").casefold(),
        ),
    }

    history.setdefault("scans", []).append({
        "timestamp": timestamp,
        "source": source,
        "discovered": len(package_ids),
        "status": "success",
    })
    history.setdefault("changes", []).extend(changes)

    # Write only after all app requests and parsing have succeeded.
    _write_json(current_file, snapshot)
    _write_json(history_file, history)

    LOG.info("Scan completed: %d apps, %d changes",
             len(package_ids), len(changes))
    return snapshot


def main():
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
