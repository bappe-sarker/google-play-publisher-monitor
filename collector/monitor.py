"""Google Play Publisher Monitor collector.

Scans a publisher's Google Play apps, extracts available metadata,
and records version changes without inventing missing version values.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CURRENT_FILE = DATA_DIR / "current.json"
HISTORY_FILE = DATA_DIR / "history.json"

PUBLISHER_URL = (
    "https://play.google.com/store/apps/developer"
    "?id=City%20Bank%20PLC"
)

REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------

def _request(url: str) -> requests.Response:
    """Request a page and raise an error for unsuccessful HTTP responses."""
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response


# ---------------------------------------------------------------------
# Google Play HTML parsing
# ---------------------------------------------------------------------

class AppLinkParser(HTMLParser):
    """Collect Google Play app links from publisher-page HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.package_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return

        attributes = dict(attrs)
        href = attributes.get("href")

        if not href:
            return

        parsed = urlparse(href)

        if parsed.path.rstrip("/") != "/store/apps/details":
            return

        package_id = parse_qs(parsed.query).get("id", [None])[0]

        if package_id and re.fullmatch(
            r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_id
        ):
            self.package_ids.add(package_id)


def parse_publisher_app_links(html: str) -> set[str]:
    """Return unique package IDs found in publisher-page HTML."""
    parser = AppLinkParser()
    parser.feed(html)
    parser.close()
    return parser.package_ids


def _extract_json_values(html: str, keys: tuple[str, ...]) -> list[str]:
    """Extract string values associated with candidate keys in embedded JSON."""
    found: list[str] = []

    for key in keys:
        pattern = (
            r'"'
            + re.escape(key)
            + r'"\s*:\s*"((?:\\.|[^"\\])*)"'
        )

        for match in re.finditer(pattern, html, re.IGNORECASE):
            raw = match.group(1)

            try:
                value = json.loads('"' + raw + '"')
            except (json.JSONDecodeError, TypeError):
                value = raw

            if isinstance(value, str) and value.strip():
                found.append(value.strip())

    return found


def parse_version_from_software_version_json(html: str) -> str | None:
    """Extract a version from softwareVersion fields in embedded JSON."""
    values = _extract_json_values(html, ("softwareVersion",))

    for value in values:
        value = value.strip()

        if value and value.lower() not in {"null", "unknown", "n/a"}:
            return value

    return None


def parse_version_from_version_json(html: str) -> str | None:
    """Extract a version from version fields in embedded JSON."""
    values = _extract_json_values(html, ("version",))

    for value in values:
        value = value.strip()

        if value and value.lower() not in {"null", "unknown", "n/a"}:
            # Avoid returning obvious non-version metadata.
            if re.search(r"\d", value):
                return value

    return None


def parse_version_from_itemprop(html: str) -> str | None:
    """Extract a version from an itemprop='softwareVersion' element."""
    patterns = (
        r'<[^>]*itemprop=["\']softwareVersion["\'][^>]*'
        r'content=["\']([^"\']+)["\'][^>]*>',
        r'<[^>]*content=["\']([^"\']+)["\'][^>]*'
        r'itemprop=["\']softwareVersion["\'][^>]*>',
        r'<meta[^>]*itemprop=["\']softwareVersion["\'][^>]*'
        r'content=["\']([^"\']+)["\'][^>]*>',
    )

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)

        if match:
            value = match.group(1).strip()
            if value and value.lower() not in {"null", "unknown", "n/a"}:
                return value

    return None


def parse_version_from_html(html: str) -> str | None:
    """Try supported HTML/JSON patterns and return a version or None."""
    return (
        parse_version_from_software_version_json(html)
        or parse_version_from_version_json(html)
        or parse_version_from_itemprop(html)
    )


def parse_updated_date_from_html(html: str) -> str | None:
    """Extract a Google Play updated date when one is present."""
    patterns = (
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"lastUpdated"\s*:\s*"([^"]+)"',
        r'"dateModified"\s*:\s*"([^"]+)"',
    )

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if value:
                return value

    # Common visible date formats, for example "Jan 5, 2026".
    match = re.search(
        r"(?:Updated on|Updated)\s*"
        r"([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4})",
        html,
        re.IGNORECASE,
    )

    if match:
        return match.group(1).strip()

    return None


def parse_app_name_from_html(html: str) -> str | None:
    """Extract the app title from the page title or embedded metadata."""
    match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        html,
        re.IGNORECASE,
    )

    if match:
        return match.group(1).strip()

    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        html,
        re.IGNORECASE | re.DOTALL,
    )

    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        title = re.sub(r"\s*-\s*Apps on Google Play\s*$", "", title)
        if title:
            return title

    return None


# ---------------------------------------------------------------------
# Publisher discovery
# ---------------------------------------------------------------------

def discover_package_ids() -> tuple[set[str], str]:
    """Discover package IDs from the configured publisher page.

    Returns:
        (package IDs, discovery source)

    Raises:
        RuntimeError: if the publisher page cannot be fetched or no apps
        can be identified. A search result alone is not treated as a
        complete publisher inventory.
    """
    response = _request(PUBLISHER_URL)
    package_ids = parse_publisher_app_links(response.text)

    if not package_ids:
        raise RuntimeError(
            "No app links were found on the publisher page. "
            "The page may have changed, returned a consent page, or "
            "blocked automated requests. Existing inventory was preserved."
        )

    return package_ids, "publisher_page"


# ---------------------------------------------------------------------
# App metadata
# ---------------------------------------------------------------------

def fetch_app_metadata(package_id: str) -> dict[str, Any]:
    """Fetch metadata for one Google Play package.

    Missing versions remain None. They are never replaced with a guessed
    value or a fabricated default.
    """
    url = (
        "https://play.google.com/store/apps/details"
        f"?id={package_id}&hl=en&gl=US"
    )

    response = _request(url)
    html = response.text

    version = parse_version_from_html(html)
    last_updated = parse_updated_date_from_html(html)
    name = parse_app_name_from_html(html) or package_id

    return {
        "package_id": package_id,
        "name": name,
        "version": version,
        "last_updated": last_updated,
        "play_store_url": url,
    }


# ---------------------------------------------------------------------
# JSON storage helpers
# ---------------------------------------------------------------------

def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default.copy()

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")

    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically to avoid leaving a partial output file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")

    temporary_path.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Scan and change detection
# ---------------------------------------------------------------------

def run_scan() -> dict[str, Any]:
    """Scan the publisher and update inventory/history after a complete scan.

    If discovery or any metadata request fails, this function raises and
    does not overwrite the existing inventory or history.

    If an app has no version in fresh metadata, its previous known version
    is preserved and no version-change event is created.
    """
    package_ids, discovery_source = discover_package_ids()

    if not package_ids:
        raise RuntimeError(
            "Publisher discovery returned no packages; existing data preserved."
        )

    # Fetch all metadata before writing either output file.
    fresh_apps: list[dict[str, Any]] = []

    for package_id in sorted(package_ids):
        metadata = fetch_app_metadata(package_id)

        if not isinstance(metadata, dict):
            raise RuntimeError(
                f"Invalid metadata returned for package {package_id}"
            )

        if not metadata.get("package_id"):
            metadata["package_id"] = package_id

        fresh_apps.append(metadata)

    old_current = _read_json(CURRENT_FILE, {"apps": [], "last_scan": None})
    old_history = _read_json(HISTORY_FILE, {"scans": [], "changes": []})

    old_apps = old_current.get("apps", [])
    if not isinstance(old_apps, list):
        raise ValueError("The apps field in current.json must be a list.")

    old_by_package = {
        app.get("package_id"): app
        for app in old_apps
        if isinstance(app, dict) and app.get("package_id")
    }

    scan_time = _utc_now()
    current_apps: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    for fresh in fresh_apps:
        package_id = fresh["package_id"]
        previous = old_by_package.get(package_id, {})
        old_version = previous.get("version")
        new_version = fresh.get("version")

        # If Google Play does not expose the current version, preserve
        # the previous known version rather than recording a false change.
        if not new_version and old_version:
            fresh["version"] = old_version

        fresh["first_seen"] = (
            previous.get("first_seen") or scan_time
        )
        fresh["last_seen"] = scan_time

        if not fresh.get("last_updated") and previous.get("last_updated"):
            fresh["last_updated"] = previous["last_updated"]

        if not fresh.get("name") or fresh.get("name") == package_id:
            if previous.get("name"):
                fresh["name"] = previous["name"]

        # Only compare versions when both old and fresh values are known.
        if old_version and new_version and old_version != new_version:
            changes.append(
                {
                    "package_id": package_id,
                    "name": fresh.get("name", package_id),
                    "old_version": old_version,
                    "new_version": new_version,
                    "detected_at": scan_time,
                    "play_store_url": fresh.get("play_store_url"),
                }
            )

        current_apps.append(fresh)

    # Apps that disappear from discovery are not silently deleted from
    # history, but only currently discovered apps are listed in current.json.
    new_current = {
        "last_scan": scan_time,
        "discovery_source": discovery_source,
        "app_count": len(current_apps),
        "apps": current_apps,
    }

    scans = old_history.get("scans", [])
    history_changes = old_history.get("changes", [])

    if not isinstance(scans, list) or not isinstance(history_changes, list):
        raise ValueError("history.json must contain scans and changes lists.")

    scans.append(
        {
            "timestamp": scan_time,
            "app_count": len(current_apps),
            "discovery_source": discovery_source,
        }
    )
    history_changes.extend(changes)

    new_history = {
        "scans": scans,
        "changes": history_changes,
    }

    # Both files are prepared in memory before either is written.
    _write_json(CURRENT_FILE, new_current)
    _write_json(HISTORY_FILE, new_history)

    return {
        "last_scan": scan_time,
        "app_count": len(current_apps),
        "changes_detected": len(changes),
        "discovery_source": discovery_source,
    }


def main() -> None:
    result = run_scan()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
