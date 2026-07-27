# FIT to Garmin Bridge

An automated, self-hosted Docker service that imports original FIT activities from
one or more sources, converts their device metadata to a selected Garmin identity,
and uploads them to Garmin Connect.

Supported sources are **Wahoo/ELEMNT via Dropbox** and **iGPSPORT Cloud**. Either
source or both can run at the same time. Once configured, new activities are
handled automatically so they can appear in Garmin Connect and, where Garmin
considers them eligible, contribute to statistics, challenges, and badges.

[![Support this project on Ko-fi](https://storage.ko-fi.com/cdn/kofi3.png?v=3)](https://ko-fi.com/ntr4nce)

> [!IMPORTANT]
> This is an unofficial community project. It is not affiliated with or supported
> by Wahoo, iGPSPORT, Garmin, or Dropbox. The Garmin upload and iGPSPORT Cloud
> integrations use unofficial APIs that may change and temporarily break this
> workflow.

## Features

- Independently polls Wahoo/Dropbox and iGPSPORT Cloud at source-specific intervals.
- Reuses iGPSPORT and Garmin sessions instead of signing in for every activity.
- Applies rate-limit handling, bounded retries, and exponential cloud backoff.
- Starts in dry-run mode so setup can be checked without uploading.
- Identifies a Garmin device from one genuine Garmin FIT file.
- Rewrites Wahoo FIT metadata with the Garmin FIT SDK.
- Reuses a persisted Garmin Connect session instead of signing in per activity.
- Prevents repeat uploads across sources using external IDs, SHA256, FIT start
  time, filename, file size, stored history, and Garmin conflict responses.
- Tracks uploaded, duplicate, failed, ignored, and dry-run activities in SQLite.
- Provides a password-protected dashboard with retry, reprocess, delete, logs,
  route preview, and activity charts.

## How It Works

```text
Wahoo/ELEMNT -> Dropbox ----\
                             \
                              FIT to Garmin Bridge
                             /
iGPSPORT device -> Cloud ---/
    |
    | deduplicate -> Garmin FIT SDK rewrite -> upload
    v
Garmin Connect
```

Every source feeds the same incoming FIT, deduplication, rewrite, and upload
pipeline. Normal polling never deletes remote activities. Dropbox deletion occurs
only through an explicit administrator action; iGPSPORT remote deletion is not
implemented.

## Requirements

- A Docker host with Docker Compose
- A Wahoo/ELEMNT device with Dropbox sharing, an iGPSPORT Cloud account, or both
- A Garmin Connect account
- One genuine FIT activity from the Garmin device identity you want to use

A genuine FIT file can contain device identifiers, timestamps, and location
history. Treat it as private data and never commit it to Git.

## Quick Start

```bash
git clone https://github.com/ntrance/wahoo-garmin-fit-bridge.git
cd wahoo-garmin-fit-bridge
cp .env.example .env
```

Set at least:

```dotenv
WEB_USERNAME=admin
WEB_PASSWORD=replace-with-a-strong-password
SESSION_SECRET_KEY=replace-with-a-separate-long-random-value
DRY_RUN=true
```

Generate a session secret with:

```bash
openssl rand -hex 32
```

Start the service:

```bash
docker compose up -d --build
```

Open `http://localhost:8088`, sign in, and keep dry-run mode enabled until every
enabled source, the Garmin device, and the Garmin session show as configured.

## Configure Dropbox

First enable Dropbox activity sharing in the Wahoo app:
[Wahoo Authorized Apps guide](https://support.wahoofitness.com/hc/en-us/articles/14467471126802-Authorized-Apps-Wahoo-app).

Then open **Config > Dropbox**:

1. Select **Start Dropbox Setup**.
2. Approve access in Dropbox.
3. Dropbox redirects to a `localhost:53682` address. The page may not load; copy
   the complete address from the browser bar.
4. Paste it into **Returned localhost URL** and finish setup.
5. Select **Test Dropbox**.

The rclone configuration is stored in the persistent `/appdata` volume.

## Configure iGPSPORT Cloud

Open **Config > Connection Settings**, enable **iGPSPORT Cloud**, and save. Then
open **Config > iGPSPORT Cloud**:

1. Enter the iGPSPORT account identifier and password.
2. Keep the default API base URL unless the account requires another region.
3. Keep **Only activities recorded after enabling iGPSPORT** for a safe first run.
4. Save the profile and select **Test iGPSPORT login**.
5. Select **Sync now**, or **Sync all enabled sources now**.

The password is written only to `/appdata/igpsport/profile.json`; it is never
stored in `runtime.env`, SQLite, logs, or rendered HTML. The access token is stored
separately in `/appdata/igpsport/session.json`. Both files use owner-only
permissions. Leaving the password field blank preserves the saved password.

iGPSPORT polls every 15 minutes by default, never automatically more often than
five minutes, and initially reads only the newest activity page. It stops at a
known ride ID, requests download URLs only for unknown rides, caps automatic
pagination, reuses valid tokens, and backs off from 15 minutes up to six hours
after failures. HTTP `429` responses also honour a valid `Retry-After` value.

Cloud access is unofficial. If it stops working, manually exporting an original
iGPSPORT FIT file and placing it in the Wahoo Dropbox folder is an alternative.
Do not assume iGPSPORT can automatically export activities to Dropbox.

### Historical iGPSPORT imports

Historical imports never run automatically. Under **Config > iGPSPORT Cloud**,
choose a start date, optional end date, maximum activity count, and dry-run or
upload mode. Review the separate confirmation page and enter `IMPORT`.

The action is bounded to 500 activities, retains global duplicate checks, and
refuses live upload while global dry-run mode is enabled. Start with a small
dry-run batch.

## Configure Garmin

Open **Config > Garmin Upload**:

1. Under **Identify Garmin Device**, upload one genuine FIT activity made by the
   Garmin device you want imported activities to resemble.
2. Select **Upload and Scan for Garmin Device**.
3. Select the detected device.
4. Enter the Garmin Connect email and password.
5. Save the Garmin upload profile.
6. Create the Garmin session.

The profile is stored at `/appdata/garmin/profile.json` and session tokens are
stored under `/appdata/garmin/tokens`. Both are private persistent data.

Garmin may request MFA even when the normal website or app does not. Complete the
flow shown by the bridge and stop retrying if Garmin reports rate limiting.

## Enable Uploads

While dry-run mode is enabled, select **Rescan** and confirm:

- Files from every enabled source are discovered.
- Activity dates and distances are correct.
- The expected Garmin device is selected.
- No unexpected duplicates appear.

Then set `DRY_RUN=false` and restart:

```bash
docker compose up -d --build
```

Dry-run records are not automatically uploaded later. Reprocess an older activity
only after confirming it is not already present in Garmin Connect.

## Duplicate Protection

The bridge records source metadata before processing and checks in this order:

- Source type and stable external activity ID
- FIT file SHA256
- Activity start time
- Filename and size fallback
- Garmin duplicate/conflict responses

Handled source IDs are skipped on later polls. When the same ride arrives through
Dropbox and iGPSPORT, both source references are retained where possible and only
the first physical activity can upload. No duplicate system can identify every
manually edited or re-exported activity, so use dry-run mode for archives.

## Persistent Data and Upgrades

Both paths must survive container replacement:

| Container path | Contents |
| --- | --- |
| `/data` | Incoming, processing, uploaded, duplicate, failed, and archived FIT files |
| `/appdata` | SQLite history, source state, Dropbox config, iGPSPORT profile/session, Garmin profile/tokens, detected devices, and logs |

Always mount persistent Docker volumes at both paths. Losing `/appdata` removes
deduplication history and saved authentication state. Back up both volumes before
upgrading or migrating.

Existing installations are migrated idempotently on startup. Existing Wahoo rows
are conservatively marked as Dropbox activities, source columns and source-state
tables are added without rewriting activity history, and Dropbox remains enabled
when the new enable variable is absent. `POLL_SECONDS` remains the Dropbox fallback
when `DROPBOX_POLL_SECONDS` is absent. Back up `/data` and `/appdata`, then verify
the Config page and a dry-run sync before enabling another source.

## Security

The admin interface includes signed HttpOnly session cookies, CSRF protection,
login rate limiting, security headers, bounded FIT uploads, private credential
files, and output redaction. The supplied container runs as an unprivileged user
with a read-only root filesystem, dropped Linux capabilities, and
`no-new-privileges`.

For stronger password storage:

```bash
docker compose run --rm bridge wahoo-bridge-hash-password
```

Configure `WEB_PASSWORD_HASH`, leave `WEB_PASSWORD` empty, terminate HTTPS at a
trusted reverse proxy, and do not expose port `8088` directly to the public
internet. Never commit:

- `.env`, `/data`, or `/appdata`
- SQLite databases or logs
- Dropbox, iGPSPORT, or Garmin credentials and tokens
- Genuine Garmin, Wahoo, or iGPSPORT FIT files
- Real Garmin unit IDs

Every push and pull request runs tests, Ruff, Bandit, `pip-audit`, Docker build,
Trivy image scanning, and Trivy deployment configuration scanning. Also enable
GitHub secret scanning, push protection, Dependabot alerts, and branch protection.
No software can be guaranteed impossible to compromise; keep the host and images
patched and rotate sessions if exposure is suspected.

See [SECURITY.md](SECURITY.md) for reporting instructions.

## Deployment

Use `docker-compose.yml` on a normal Docker host. For Dokploy, use
`docker-compose.dokploy.yml`, mount persistent volumes at `/data` and `/appdata`
before the first production deployment, and provide secrets through Dokploy
environment settings.

OrbStack can run the normal Compose file unchanged. Keep the bind-mounted `data`,
`appdata`, and `real_fit` directories when recreating the container. Dokploy must
retain its named `/data` and `/appdata` volumes across redeployments.

## Limitations

- Intended for personal, self-hosted use.
- Historic Wahoo activities are not automatically backfilled by Wahoo.
- iGPSPORT Cloud endpoints are unofficial and may change without notice.
- Automatic iGPSPORT polling intentionally favours low API load over immediacy.
- Garmin may change its private authentication or upload endpoints.
- Garmin decides how imported device information is displayed.
- A malformed FIT may require a fresh export from the original source.
- Garmin decides whether an imported activity qualifies for a badge or challenge.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check .
bandit -q -r app
pip-audit
```

Contributions must not include real FIT activities, credentials, tokens, unit IDs,
deployment addresses, or other personal data.

## Support

If this bridge saves you time, you can support development on
[Ko-fi](https://ko-fi.com/ntr4nce).

## License and Acknowledgements

This bridge is released under the [MIT License](LICENSE). It uses the
[Garmin FIT SDK](https://developer.garmin.com/fit/overview/),
[garminconnect](https://github.com/cyberjunky/python-garminconnect), and
[rclone](https://rclone.org/). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Wahoo, ELEMNT, Garmin, Garmin Connect, Dropbox, and their respective marks are the
property of their owners.
