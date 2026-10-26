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
