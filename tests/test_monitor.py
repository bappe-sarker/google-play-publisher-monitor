import json
from pathlib import Path

import pytest

from collector.monitor import AppLinkParser, parse_version_from_html, parse_updated_date_from_html


def test_extract_version_from_software_version_json():
    html = '<script>{"softwareVersion":"4.12.0"}</script>'
    assert parse_version_from_html(html) == "4.12.0"


def test_extract_version_from_version_json():
    html = '<script>{"version":"2.0.1-beta"}</script>'
    assert parse_version_from_html(html) == "2.0.1-beta"


def test_extract_version_from_itemprop():
    html = '<meta itemprop="softwareVersion" content="7.3.5">'
    assert parse_version_from_html(html) == "7.3.5"


def test_no_version_means_none_not_invented():
    assert parse_version_from_html('<html><title>Example app</title></html>') is None
    assert parse_version_from_html('<script>{"version":"Varies with device"}</script>') is None


def test_extract_updated_date():
    assert parse_updated_date_from_html('<script>{"datePublished":"2026-10-14T00:00:00Z"}</script>') == "2026-10-14"


def test_publisher_link_parser():
    parser = AppLinkParser()
    parser.feed('<a href="/store/apps/details?id=com.example.app">App</a><a href="/store/apps/details?id=bad">bad</a>')
    assert parser.package_ids == {"com.example.app"}


def test_incomplete_scan_preserves_existing_files(tmp_path, monkeypatch):
    import collector.monitor as monitor

    current = tmp_path / "current.json"
    history = tmp_path / "history.json"
    current.write_text(json.dumps({"apps": [{"package_id": "com.example.old", "name": "Old", "version": "1.0"}], "last_scan": "old"}))
    history.write_text(json.dumps({"scans": [], "changes": []}))
    monkeypatch.setattr(monitor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(monitor, "CURRENT_FILE", current)
    monkeypatch.setattr(monitor, "HISTORY_FILE", history)
    monkeypatch.setattr(monitor, "discover_package_ids", lambda: (_ for _ in ()).throw(RuntimeError("network failure")))
    before_current = current.read_text()
    before_history = history.read_text()
    with pytest.raises(RuntimeError, match="network failure"):
        monitor.run_scan()
    assert current.read_text() == before_current
    assert history.read_text() == before_history


def test_missing_fresh_version_does_not_create_false_version_change(tmp_path, monkeypatch):
    import collector.monitor as monitor

    current = tmp_path / "current.json"
    history = tmp_path / "history.json"
    current.write_text(json.dumps({"apps": [{"package_id": "com.example.app", "name": "Example", "version": "3.2.1", "last_updated": "2026-01-01", "first_seen": "2026-01-01"}], "last_scan": "old"}))
    history.write_text(json.dumps({"scans": [], "changes": []}))
    monkeypatch.setattr(monitor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(monitor, "CURRENT_FILE", current)
    monkeypatch.setattr(monitor, "HISTORY_FILE", history)
    monkeypatch.setattr(monitor, "discover_package_ids", lambda: ({"com.example.app"}, "test"))
    monkeypatch.setattr(monitor, "fetch_app_metadata", lambda package_id: {"package_id": package_id, "name": "Example", "version": None, "last_updated": None, "play_store_url": "https://play.google.com/store/apps/details?id=com.example.app"})
    result = monitor.run_scan()
    assert result["changes"] == []
    saved = json.loads(current.read_text())
    assert saved["apps"][0]["version"] == "3.2.1"
