"""Google Play Publisher Monitor collector.

Discovers apps from the official publisher page AND Google Play search,
verifies search candidates against the developer name, collects metadata,
detects newly published apps/version changes, and maintains JSON history.

Requires:
    pip install requests google-play-scraper
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from google_play_scraper import app as play_store_app
    from google_play_scraper import search as play_store_search
except ImportError:  # HTML fallback remains available for detail-page retrieval.
    play_store_app = None
    play_store_search = None


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CURRENT_FILE = DATA_DIR / "current.json"
HISTORY_FILE = DATA_DIR / "history.json"

# Keep the trailing period: it is part of the publisher identifier supplied.
PUBLISHER_URL = (
    "https://play.google.com/store/apps/developer"
    "?id=City+Bank+PLC."
)

# Search is an additional discovery source. Search results are NOT trusted
# until the app's developer field has been checked against EXPECTED_DEVELOPER.
SEARCH_QUERIES = (
    "City Bank PLC",
    "City Bank",
    "The City Bank PLC",
)
PLAY_LANGUAGE = "en"
# Search both Bangladesh and US storefronts to reduce regional blind spots.
PLAY_COUNTRIES = ("bd", "us")
PRIMARY_PLAY_COUNTRY = PLAY_COUNTRIES[0]
EXPECTED_DEVELOPER = "City Bank PLC."

REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
SEARCH_RESULT_LIMIT = 100
MISSING_SCANS_BEFORE_REMOVAL = 3
REQUEST_DELAY_SECONDS = 0.4

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


SESSION = _build_session()


def _request(url: str) -> requests.Response:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response


def _app_url(package_id: str) -> str:
    query = urlencode({"id": package_id, "hl": PLAY_LANGUAGE, "gl": PRIMARY_PLAY_COUNTRY})
    return f"https://play.google.com/store/apps/details?{query}"


# ---------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------


def _normalize_developer(value: Any) -> str:
    """Normalize developer labels for conservative identity comparison."""
    if value is None:
        return ""
    value = str(value).casefold().strip()
    value = re.sub(r"[®™©]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


EXPECTED_DEVELOPER_NORMALIZED = _normalize_developer(EXPECTED_DEVELOPER)


def _is_expected_developer(value: Any) -> bool:
    """Only accept search candidates whose developer matches the publisher."""
    return _normalize_developer(value) == EXPECTED_DEVELOPER_NORMALIZED


def _valid_package_id(package_id: str | None) -> bool:
    return bool(
        package_id
        and re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_id)
    )


# ---------------------------------------------------------------------
# Google Play app-link parser
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
        if _valid_package_id(package_id):
            self.package_ids.add(package_id)


def parse_app_links(html: str) -> set[str]:
    parser = AppLinkParser()
    parser.feed(html)
    parser.close()
    return parser.package_ids


def _publisher_page_packages() -> tuple[set[str], str | None]:
    try:
        response = _request(PUBLISHER_URL)
        package_ids = parse_app_links(response.text)
        if package_ids:
            return package_ids, None
        return set(), "Publisher page returned no app links (possibly dynamic or incomplete HTML)."
    except requests.RequestException as exc:
        return set(), f"Publisher page request failed: {exc}"


def _search_packages_from_library() -> tuple[set[str], list[str]]:
    """Search Google Play and retain only exact developer-name matches."""
    found: set[str] = set()
    errors: list[str] = []

    if play_store_search is None:
        return found, ["google-play-scraper search is unavailable"]

    for country in PLAY_COUNTRIES:
        for query in SEARCH_QUERIES:
            try:
                results = play_store_search(
                    query,
                    lang=PLAY_LANGUAGE,
                    country=country,
                    n=SEARCH_RESULT_LIMIT,
                )
                for item in results:
                    package_id = item.get("appId")
                    developer = item.get("developer")
                    if _valid_package_id(package_id) and _is_expected_developer(developer):
                        found.add(package_id)
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:  # Library/network errors should not hide publisher results.
                errors.append(f"Search '{query}' ({country}) failed: {exc}")

    return found, errors


def _search_packages_from_html() -> tuple[set[str], list[str]]:
    """HTML search fallback; candidates are verified later using app metadata."""
    found: set[str] = set()
    errors: list[str] = []

    for query in SEARCH_QUERIES:
        params = {
            "q": query,
            "c": "apps",
            "hl": PLAY_LANGUAGE,
            "gl": PRIMARY_PLAY_COUNTRY.upper(),
        }
        url = "https://play.google.com/store/search?" + urlencode(params)
        try:
            response = _request(url)
            found.update(parse_app_links(response.text))
            time.sleep(REQUEST_DELAY_SECONDS)
        except requests.RequestException as exc:
            errors.append(f"HTML search '{query}' failed: {exc}")

    return found, errors


def discover_package_ids() -> tuple[set[str], dict[str, Any]]:
    """Merge publisher and search discovery; do not stop at first source.

    Publisher-page results are treated as publisher-listed apps. Search results
    are candidates and are verified against developer metadata in run_scan().
    """
    publisher_ids, publisher_error = _publisher_page_packages()
    search_ids, search_errors = _search_packages_from_library()

    # If the library's search API is unavailable or yields no candidates, use
    # Google Play's HTML search pages as a candidate source.
    html_search_ids: set[str] = set()
    if not search_ids:
        html_search_ids, html_errors = _search_packages_from_html()
        search_errors.extend(html_errors)

    candidates = publisher_ids | search_ids | html_search_ids
    details = {
        "publisher_count": len(publisher_ids),
        "search_verified_count": len(search_ids),
        "html_search_candidate_count": len(html_search_ids),
        "publisher_error": publisher_error,
        "search_errors": search_errors,
    }

    if not candidates:
        errors = [publisher_error] if publisher_error else []
        errors.extend(search_errors)
        raise RuntimeError(
            "Unable to discover Google Play apps. "
            + " | ".join(errors or ["No app package IDs were found"])
            + ". Existing inventory was preserved."
        )

    return candidates, details


# ---------------------------------------------------------------------
# HTML metadata parsers (fallback)
# ---------------------------------------------------------------------


def _extract_json_values(html: str, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        pattern = (
            r'"' + re.escape(key)
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


def parse_version_from_html(html: str) -> str | None:
    """Extract a version only from app-specific metadata patterns."""
    for value in _extract_json_values(html, ("softwareVersion",)):
        if value.lower() not in {"null", "unknown", "n/a"}:
            return value

    patterns = (
        r'<[^>]*itemprop=["\']softwareVersion["\'][^>]*content=["\']([^"\']+)["\'][^>]*>',
        r'<[^>]*content=["\']([^"\']+)["\'][^>]*itemprop=["\']softwareVersion["\'][^>]*>',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if value and value.lower() not in {"null", "unknown", "n/a"}:
                return value
    # Deliberately do not use a generic JSON "version" key: it can describe
    # the Google Play page/client rather than the application itself.
    return None


def parse_updated_date_from_html(html: str) -> str | None:
    """Extract an update date, avoiding datePublished (original release date)."""
    patterns = (
        r'"lastUpdated"\s*:\s*"([^"]+)"',
        r'"dateModified"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
            return iso_match.group(1) if iso_match else value

    visible_date = re.search(
        r"(?:Updated on|Updated)\s*([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4})",
        html,
        re.IGNORECASE,
    )
    return visible_date.group(1).strip() if visible_date else None


def parse_app_name_from_html(html: str) -> str | None:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        title = re.sub(r"\s*-\s*Apps on Google Play\s*$", "", title, flags=re.IGNORECASE)
        return title or None
    return None


def parse_developer_from_html(html: str) -> str | None:
    patterns = (
        r'"author"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
        r'<meta[^>]+itemprop=["\']author["\'][^>]+content=["\']([^"\']+)',
        r'<a[^>]+href=["\'][^"\']*/store/apps/dev\?id=[^"\']+["\'][^>]*>(.*?)</a>',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            value = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if value:
                return value
    return None


# ---------------------------------------------------------------------
# Metadata normalization
# ---------------------------------------------------------------------


def _normalize_version(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result or result.lower() in {"none", "null", "unknown", "n/a", "unavailable"}:
        return None
    return result


def _normalize_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:  # milliseconds -> seconds
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return None

    text_value = str(value).strip()
    if not text_value:
        return None
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", text_value)
    if iso_match:
        return iso_match.group(1)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text_value, fmt).date().isoformat()
        except ValueError:
            continue
    return text_value


# ---------------------------------------------------------------------
# App metadata fetching and candidate verification
# ---------------------------------------------------------------------


def fetch_app_metadata(package_id: str, publisher_listed: bool = False) -> dict[str, Any]:
    """Fetch app details and verify developer for search-only candidates."""
    url = _app_url(package_id)
    scraper_error: Exception | None = None
    data: dict[str, Any] = {}

    if play_store_app is not None:
        for country in PLAY_COUNTRIES:
            try:
                data = play_store_app(
                    package_id,
                    lang=PLAY_LANGUAGE,
                    country=country,
                )
                if data:
                    break
            except Exception as exc:
                scraper_error = exc

    if data:
        developer = data.get("developer") or data.get("developerId")
        # Publisher-page membership is the strongest discovery signal. For
        # search-only results, require exact normalized developer-name match.
        if not publisher_listed and not _is_expected_developer(developer):
            raise ValueError(
                f"Rejected search candidate {package_id}: developer "
                f"{developer!r} does not match {EXPECTED_DEVELOPER!r}"
            )

        return {
            "package_id": package_id,
            "name": data.get("title") or package_id,
            "version": _normalize_version(data.get("version")),
            "last_updated": _normalize_date(data.get("updated")),
            "developer": developer,
            "play_store_url": url,
            "discovery_source": "publisher_page" if publisher_listed else "google_play_search_verified",
        }

    # HTML fallback. Search-only candidates must still pass developer check.
    response = _request(url)
    html = response.text
    developer = parse_developer_from_html(html)
    if not publisher_listed and not _is_expected_developer(developer):
        raise ValueError(
            f"Rejected search candidate {package_id}: could not verify developer "
            f"({developer!r}) against {EXPECTED_DEVELOPER!r}"
        )

    return {
        "package_id": package_id,
        "name": parse_app_name_from_html(html) or package_id,
        "version": _normalize_version(parse_version_from_html(html)),
        "last_updated": _normalize_date(parse_updated_date_from_html(html)),
        "developer": developer,
        "play_store_url": url,
        "discovery_source": "publisher_page" if publisher_listed else "google_play_search_verified",
        "metadata_warning": str(scraper_error) if scraper_error else None,
    }


# ---------------------------------------------------------------------
# JSON persistence
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
    """Run a complete scan; preserve prior inventory if discovery/fetch fails."""
    candidate_ids, discovery = discover_package_ids()

    # Re-fetch the publisher page IDs so search-only candidates can be
    # distinguished and verified. A publisher page error does not invalidate
    # search discovery, but it is recorded in scan metadata.
    publisher_ids, publisher_error = _publisher_page_packages()

    fresh_apps: list[dict[str, Any]] = []
    rejected_candidates: list[dict[str, str]] = []
    metadata_errors: list[str] = []

    for package_id in sorted(candidate_ids):
        try:
            metadata = fetch_app_metadata(
                package_id,
                publisher_listed=(package_id in publisher_ids),
            )
            metadata.setdefault("package_id", package_id)
            fresh_apps.append(metadata)
        except ValueError as exc:
            rejected_candidates.append({"package_id": package_id, "reason": str(exc)})
        except Exception as exc:
            metadata_errors.append(f"{package_id}: {exc}")
        time.sleep(REQUEST_DELAY_SECONDS)

    # Do not replace inventory with a partial scan if any discovered app could
    # not be fetched. This avoids false removals and misleading app counts.
    if metadata_errors:
        raise RuntimeError(
            "Metadata collection was incomplete; existing inventory was preserved. "
            + " | ".join(metadata_errors)
        )
    if not fresh_apps:
        raise RuntimeError("No verified apps collected; existing inventory was preserved.")

    old_current = _read_json(CURRENT_FILE, {"apps": [], "last_scan": None})
    old_history = _read_json(HISTORY_FILE, {"scans": [], "changes": []})
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
        raise ValueError("history.json must contain 'scans' and 'changes' lists.")

    scan_time = _utc_now()
    fresh_by_package = {app["package_id"]: app for app in fresh_apps}
    current_apps: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    for package_id, fresh in fresh_by_package.items():
        previous = old_by_package.get(package_id, {})
        old_version = _normalize_version(previous.get("version"))
        new_version = _normalize_version(fresh.get("version"))
        fresh["version"] = new_version or old_version
        fresh["last_updated"] = _normalize_date(fresh.get("last_updated")) or previous.get("last_updated")

        if (not fresh.get("name") or fresh.get("name") == package_id) and previous.get("name"):
            fresh["name"] = previous["name"]

        fresh["first_seen"] = previous.get("first_seen") or scan_time
        fresh["last_seen"] = scan_time
        fresh["status"] = "active"
        fresh["missing_scans"] = 0

        if not previous:
            changes.append({
                "change_type": "new_app",
                "package_id": package_id,
                "name": fresh.get("name", package_id),
                "new_version": fresh.get("version"),
                "detected_at": scan_time,
                "play_store_url": fresh.get("play_store_url"),
            })
        elif old_version and new_version and old_version != new_version:
            changes.append({
                "change_type": "version_change",
                "package_id": package_id,
                "name": fresh.get("name", package_id),
                "old_version": old_version,
                "new_version": new_version,
                "detected_at": scan_time,
                "play_store_url": fresh.get("play_store_url"),
            })

        current_apps.append(fresh)

    # Retain previously known apps that were not returned in this scan.
    # A missing result is not proof that the app has been unpublished.
    for package_id, previous in old_by_package.items():
        if package_id in fresh_by_package:
            continue
        retained = dict(previous)
        missing_scans = int(retained.get("missing_scans", 0)) + 1
        retained["missing_scans"] = missing_scans
        retained["status"] = (
            "removed_candidate"
            if missing_scans >= MISSING_SCANS_BEFORE_REMOVAL
            else "missing"
        )
        retained["last_missing_at"] = scan_time
        current_apps.append(retained)
        if missing_scans == 1:
            changes.append({
                "change_type": "app_not_seen",
                "package_id": package_id,
                "name": retained.get("name", package_id),
                "detected_at": scan_time,
                "note": "Not returned by this scan; not confirmed unpublished.",
            })
        elif missing_scans == MISSING_SCANS_BEFORE_REMOVAL:
            changes.append({
                "change_type": "possibly_unpublished",
                "package_id": package_id,
                "name": retained.get("name", package_id),
                "detected_at": scan_time,
                "note": "Missing across multiple scans; verify manually before removal.",
            })

    scans.append({
        "timestamp": scan_time,
        "app_count": len(fresh_apps),
        "inventory_count": len(current_apps),
        "publisher_count": discovery.get("publisher_count", 0),
        "search_verified_count": discovery.get("search_verified_count", 0),
        "html_search_candidate_count": discovery.get("html_search_candidate_count", 0),
        "rejected_candidate_count": len(rejected_candidates),
        "discovery": discovery,
    })
    history_changes.extend(changes)

    new_current = {
        "last_scan": scan_time,
        "discovery": discovery,
        "app_count": len(current_apps),
        "active_app_count": len(fresh_apps),
        "apps": sorted(current_apps, key=lambda item: item.get("name", "").casefold()),
    }
    new_history = {"scans": scans, "changes": history_changes}

    # Write both files only after the entire scan has completed successfully.
    _write_json(CURRENT_FILE, new_current)
    _write_json(HISTORY_FILE, new_history)

    return {
        "last_scan": scan_time,
        "active_app_count": len(fresh_apps),
        "inventory_count": len(current_apps),
        "new_apps_detected": sum(1 for item in changes if item.get("change_type") == "new_app"),
        "version_changes_detected": sum(1 for item in changes if item.get("change_type") == "version_change"),
        "changes_detected": len(changes),
        "changes": changes,
        "rejected_candidates": rejected_candidates,
        "discovery": discovery,
        "publisher_error": publisher_error,
    }


def main() -> None:
    result = run_scan()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
