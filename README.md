# Google Play Publisher Monitor — City Bank PLC

A static, responsive GitHub Pages dashboard for monitoring public Android apps listed under [City Bank PLC on Google Play](https://play.google.com/store/apps/developer?id=City+Bank+PLC). A Python collector discovers package IDs, retrieves public listing metadata, detects new apps and observed version changes, and commits JSON snapshots to this repository.

## Important behavior and limitations

- **Manual scans only.** There is no schedule or cron trigger. `.github/workflows/manual-scan.yml` uses `workflow_dispatch` (and a reusable `workflow_call` entry point).
- **No browser token.** A GitHub Pages site is static and cannot safely hold a GitHub token. The **Scan Now** button opens the repository's Actions workflow page. Sign in and click **Run workflow** there; after the workflow finishes, use **Refresh dashboard** or reload the Pages site.
- **Version metadata is not always public.** Google Play may omit or obfuscate the version. The collector prefers `google-play-scraper` structured metadata, then checks explicit HTML/embedded metadata. If no value is present, it records `null`/preserves the last known value rather than guessing.
- **Conservative writes.** A discovery failure, app-level lookup error, empty result, or scan with no usable records fails without replacing the current JSON. Existing package IDs are retained if absent from a scan because publisher listings may be paginated or incomplete. This avoids destructive false removals, but a removed/unpublished app will remain marked as not seen until manually reconciled.
- **Version-change detection** compares explicit fresh versions with the previous known version. Missing metadata is not treated as a change. The history is an observation log, not proof of the exact publication timestamp.
- Google Play page markup and availability can change; review collector logs if discovery stops working.

## Project layout

```text
.
├── .github/workflows/manual-scan.yml
├── collector/monitor.py
├── data/current.json
├── data/history.json
├── tests/test_monitor.py
├── index.html
├── styles.css
├── app.js
├── config.js
├── requirements.txt
└── README.md
```

## Deploy to GitHub Pages

1. Create a GitHub repository (recommended name: `google-play-publisher-monitor`) and upload/push all files in this project to its default branch, usually `main`.
2. Edit `config.js`: set `githubOwner` to your GitHub username or organization. Keep `githubRepo` aligned with the repository name. **Never add a personal access token to `config.js` or any frontend file.**
3. In GitHub, open **Settings → Pages**. Under **Build and deployment**, select **Deploy from a branch**, choose `main` and `/ (root)`, then save. Wait for Pages to publish.
4. Open the Pages URL. The first visit will show an empty inventory until the first successful scan.
5. Open **Actions → Manual Google Play publisher scan → Run workflow**. The dashboard's **Scan Now** button opens this workflow page. Choose the branch and click **Run workflow**. The workflow installs dependencies, runs tests, executes the collector, and commits updated `data/current.json` and `data/history.json` when changed.
6. After a successful workflow, refresh the dashboard. Pages deployment/cache propagation may take a short time.

### Repository Actions permissions

In **Settings → Actions → General → Workflow permissions**, allow **Read and write permissions** so the workflow can commit JSON updates. Branch protection rules must also permit the workflow bot to push, or the commit step will fail. The workflow uses the minimum `contents: write` permission at workflow scope.

### Manual scan from the dashboard

GitHub does not allow an unauthenticated static website to start `workflow_dispatch` securely without credentials. This implementation deliberately avoids a token in JavaScript. Clicking **Scan Now** opens the GitHub Actions workflow page; the authenticated user must click **Run workflow**. The dashboard's **Refresh dashboard** button only reloads committed JSON and does not start a scan.

## Run locally

Python 3.11+ recommended (CI uses Python 3.12).

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
python collector/monitor.py
```

The collector uses `PLAY_PUBLISHER_NAME`, `PLAY_PUBLISHER_URL`, `PLAY_COUNTRY`, and `PLAY_LANGUAGE` environment variables if you need to change the publisher or locale. The default target is City Bank PLC, country `bd`, language `en`.

## Data format

- `data/current.json`: latest accepted inventory snapshot, app count, scan time, discovery method, app metadata, first/last seen.
- `data/history.json`: bounded scan history (last 500 scans) and detected changes (last 2,000 entries).
- An app with `metadata_status: "partial"` may not have a currently visible version. A retained record with `not_seen_in_latest_scan: true` was not in the latest discovered IDs and has intentionally not been deleted.

## Security notes

The workflow requires repository write permission to commit scan data. Keep the repository access-controlled if the inventory or monitoring history is considered internal. Public app listing data itself is sourced from public Google Play pages. The Pages site does not use credentials or secrets. Do not enable third-party untrusted pull-request workflows with write permissions without reviewing their security implications.
