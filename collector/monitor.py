import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse
class AppLinkParser(HTMLParser):
    """Extract valid app package IDs from Google Play HTML."""

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

                if package_id and re.fullmatch(
                    r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+",
                    package_id,
                ):
                    self.package_ids.add(package_id)


def parse_version_from_html(html: str) -> str | None:
    """Return an explicitly published version, or None if unavailable."""

    patterns = [
        r'"softwareVersion"\s*:\s*"([^"\\]{1,100})"',
        r'"version"\s*:\s*"([^"\\]{1,100})"',
        r'itemprop=["\']softwareVersion["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']softwareVersion["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            version = unquote(match.group(1)).strip()

            if version and version.casefold() not in {
                "varies with device",
                "unknown",
                "n/a",
            }:
                return version

    return None


def parse_updated_date_from_html(html: str) -> str | None:
    """Extract a date from structured metadata when explicitly available."""

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
def discover_package_ids() -> tuple[set[str], str]:
    """Discover apps conservatively using publisher HTML and search fallback."""
    errors: list[str] = []

    # First, try the configured publisher page.
    try:
        response = _request(PUBLISHER_URL)
        parser = AppLinkParser()
        parser.feed(response.text)

        if parser.package_ids:
            LOG.info(
                "Publisher HTML exposed %d app IDs",
                len(parser.package_ids),
            )
            return parser.package_ids, "publisher_html"

        errors.append("Publisher HTML contained no app links")
    except Exception as exc:
        errors.append(f"Publisher HTML: {exc}")
        LOG.warning("Publisher page discovery failed: %s", exc)

    # Fallback: search Google Play and verify each candidate's publisher.
    try:
        from google_play_scraper import app, search

        results = search(
            PUBLISHER_NAME,
            n=100,
            lang=LANGUAGE,
            country=COUNTRY,
        )

        verified_ids: set[str] = set()

        for item in results:
            package_id = item.get("appId")
            if not package_id:
                continue

            try:
                metadata = app(
                    package_id,
                    lang=LANGUAGE,
                    country=COUNTRY,
                )

                actual_publisher = (
                    metadata.get("developer") or ""
                ).strip().casefold()

                expected_publisher = PUBLISHER_NAME.strip().casefold()

                if actual_publisher == expected_publisher:
                    verified_ids.add(package_id)

            except Exception as exc:
                LOG.warning(
                    "Could not verify candidate %s: %s",
                    package_id,
                    exc,
                )

        if verified_ids:
            LOG.warning(
                "Found %d publisher-verified candidates through search. "
                "Search results may be incomplete.",
                len(verified_ids),
            )
            return verified_ids, "verified_search"

        errors.append("Search returned no publisher-verified apps")

    except Exception as exc:
        errors.append(f"Google Play search fallback: {exc}")
        LOG.exception("Search fallback failed")

    raise RuntimeError(
        "App discovery failed; existing inventory was not changed. "
        + " | ".join(errors)
    )
