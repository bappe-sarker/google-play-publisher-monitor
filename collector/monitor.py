
"""Google Play Publisher Monitor collector."""

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

# Corrected publisher URL: no trailing punctuation after PLC.
PUBLISHER_URL = (
    "https://play.google.com/store/apps/developer"
    "?id=City+Bank+PLC."
)

SEARCH_URL = (
    "https://play.google.com/store/search"
    "?q=City%20Bank%20PLC&c=apps&hl=en&gl=US"
)

REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------

def _request(url: str) -> requests.Response:
    """Fetch a URL and raise for unsuccessful HTTP responses."""
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
    """Extract package IDs from Google Play app detail links."""

    def __init__(self) -> None:
        super().__init__()
        self.package_ids: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "a":
            return

        href = dict(attrs).get("href")
        if not href:
            return

        parsed = urlparse(href)

        if parsed.path.rstrip("/") != "/store/apps/details":
            return

        package_id = parse_qs(parsed.query).get("id", [None])[0]

        if package_id and re.fullmatch(
            r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+",
            package_id,
        ):
            self.package_ids.add(package_id)


def parse_publisher_app_links(html: str) -> set[str]:
    """Return unique package IDs from Google Play app links."""
    parser = AppLinkParser()
    parser.feed(html)
    parser.close()
    return parser.package_ids


def discover_package_ids() -> tuple[set[str], str]:
    """Discover package IDs from the publisher page or search results.

    Search-based discovery is best-effort and may not find every app.
    """
    errors: list[str] = []

    sources = (
        (PUBLISHER_URL, "publisher_page"),
        (SEARCH_URL, "google_play_search"),
    )

    for url, source in sources:
        try:
            response = _request(url)
            package_ids = parse_publisher_app_links(response.text)

            if package_ids:
                return package_ids, source

            errors.append(f"{source}: no app links found")

        except requests.RequestException as exc:
            errors.append(f"{source}: {exc}")

    raise RuntimeError(
        "Unable to discover Google Play apps. "
        + " | ".join(errors)
        + ". Existing inventory was preserved."
    )


# ---------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------

def _extract_json_values(
    html: str,
    keys: tuple[str, ...],
) -> list[str]:
    """Extract quoted string values associated with candidate JSON keys."""
    values: list[str] = []

    for key in keys:
        pattern = (
            r'"'
            + re.escape(key)
            + r'"\s*:\s*"((?:\\.|[^"\\])*)"'
        )

        for match in re.finditer(pattern, html, re.IGNORECASE):
            raw_value = match.group(1)

            try:
                value = json.loads('"' + raw_value + '"')
            except (json.JSONDecodeError, TypeError):
                value = raw_value

            if isinstance(value, str) and value.strip():
                values.append(value.strip())

    return values


def parse_version_from_software_version_json(
    html: str,
) -> str | None:
    """Extract a version from a softwareVersion field."""
    for value in _extract_json_values(html, ("softwareVersion",)):
        if value.lower() not in {"null", "unknown", "n/a"}:
            return value

    return None


def parse_version_from_version_json(html: str) -> str | None:
    """Extract a version from a version field."""
    for value in _extract_json_values(html, ("version",)):
        if (
            value.lower() not in {"null", "unknown", "n/a"}
            and re.search(r"\d", value)
        ):
            return value

    return None


def parse_version_from_itemprop(html: str) -> str | None:
    """Extract softwareVersion from an HTML itemprop."""
    patterns = (
        r'<[^>]*itemprop=["\']softwareVersion["\'][^>]*'
        r'content=["\']([^"\']+)["\'][^>]*>',
        r'<[^>]*content=["\']([^"\']+)["\'][^>]*'
        r'itemprop=["\']softwareVersion["\'][^>]*>',
    )

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)

        if match:
            value = match.group(1).strip()

            if value and value.lower() not in {
                "null", "unknown", "n/a"
            }:
                return value

    return None


def parse_version_from_html(html: str) -> str | None:
    """Return the first detected version, or None."""
    return (
        parse_version_from_software_version_json(html)
        or parse_version_from_version_json(html)
        or parse_version_from_itemprop(html)
    )


# ---------------------------------------------------------------------
# Date and app-name parsing
# ---------------------------------------------------------------------

def parse_updated_date_from_html(html: str) -> str | None:
    """Extract updated date and normalize ISO timestamps to YYYY-MM-DD."""
    patterns = (
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"lastUpdated"\s*:\s*"([^"]+)"',
        r'"dateModified"\s*:\s*"([^"]+)"',
    )

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)

        if not match:
            continue

        value = match.group(1).strip()
        if not value:
            continue

        iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)

        if iso_match:
            return iso_match.group(1)

        return value

    visible_date = re.search(
        r"(?:Updated on|Updated)\s*"
        r"([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4})",
        html,
        re.IGNORECASE,
    )

    if visible_date:
        return visible_date.group(1).strip()

    return None


def parse_app_name_from_html(html: str) -> str | None:
    """Extract an app name from Open Graph metadata or the page title."""
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+'
        r'content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
        r'property=["\']og:title["\']',
    )

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)

        if match:
            return match.group(1).strip()

    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        html,
        re.IGNORECASE | re.DOTALL,
    )

    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        title = re.sub(
            r"\s*-\s*Apps on Google Play\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )

        if title:
            return title

    return None


# ---------------------------------------------------------------------
# Metadata fetching
# ---------------------------------------------------------------------

def fetch_app_metadata(package_id: str) -> dict[str, Any]:
    """Fetch metadata for a package without inventing missing values."""
    url = (
        "https://play.google.com/store/apps/details"
        f"?id={package_id}&hl=en&gl=US"
    )

    response = _request(url)
    html = response.text

    return {
        "package_id": package_id,
        "name": parse_app_name_from_html(html) or package_id,
        "version": parse_version_from_html(html),
        "last_updated": parse_updated_date_from_html(html),
        "play_store_url": url,
    }


# ---------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------

def _read_json(
    path: Path,
    default: dict[str, Any],
) -> dict[str, Any]:
    if not path.exists():
        return default.copy()

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")

    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON using a temporary file to avoid partial writes."""
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
    """Scan discovered apps and update current inventory and history.

    Existing JSON files are not modified if discovery or metadata
    fetching fails. Missing fresh versions do not create false changes.
    """
    package_ids, discovery_source = discover_package_ids()

    if not package_ids:
        raise RuntimeError(
            "No packages discovered; existing inventory was preserved."
        )

    # Fetch every app before reading or writing inventory files.
    fresh_apps: list[dict[str, Any]] = []

    for package_id in sorted(package_ids):
        metadata = fetch_app_metadata(package_id)

        if not isinstance(metadata, dict):
            raise RuntimeError(
                f"Invalid metadata for package {package_id}"
            )

        metadata.setdefault("package_id", package_id)
        fresh_apps.append(metadata)

    old_current = _read_json(
        CURRENT_FILE,
        {"apps": [], "last_scan": None},
    )
    old_history = _read_json(
        HISTORY_FILE,
        {"scans": [], "changes": []},
    )

    old_apps = old_current.get("apps", [])

    if not isinstance(old_apps, list):
        raise ValueError("current.json 'apps' must be a list.")

    old_by_package = {
        app.get("package_id"): app
        for app in old_apps
        if isinstance(app, dict) and app.get("package_id")
    }

    scans = old_history.get("scans", [])
    history_changes = old_history.get("changes", [])

    if not isinstance(scans, list) or not isinstance(history_changes, list):
        raise ValueError(
            "history.json must contain 'scans' and 'changes' lists."
        )

    scan_time = _utc_now()
    current_apps: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    for fresh in fresh_apps:
        package_id = fresh["package_id"]
        previous = old_by_package.get(package_id, {})

        old_version = previous.get("version")
        new_version = fresh.get("version")

        # Keep the last known version if Google Play omits the new one.
        if not new_version and old_version:
            fresh["version"] = old_version

        fresh["first_seen"] = previous.get("first_seen") or scan_time
        fresh["last_seen"] = scan_time

        if not fresh.get("last_updated") and previous.get("last_updated"):
            fresh["last_updated"] = previous["last_updated"]

        if (
            not fresh.get("name")
            or fresh.get("name") == package_id
        ) and previous.get("name"):
            fresh["name"] = previous["name"]

        # Compare only when both old and fresh versions are available.
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

    scans.append(
        {
            "timestamp": scan_time,
            "app_count": len(current_apps),
            "discovery_source": discovery_source,
        }
    )
    history_changes.extend(changes)

    new_current = {
        "last_scan": scan_time,
        "discovery_source": discovery_source,
        "app_count": len(current_apps),
        "apps": current_apps,
    }

    new_history = {
        "scans": scans,
        "changes": history_changes,
    }

    _write_json(CURRENT_FILE, new_current)
    _write_json(HISTORY_FILE, new_history)

    return {
        "last_scan": scan_time,
        "app_count": len(current_apps),
        "changes_detected": len(changes),
        "changes": changes,
        "discovery_source": discovery_source,
    }


def main() -> None:
    result = run_scan()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
