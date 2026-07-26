# Wahoo to Garmin Bridge

An automated, self-hosted Docker service that imports Wahoo/ELEMNT FIT activities
from Dropbox, converts their device metadata to a selected Garmin device identity,
and uploads them to Garmin Connect.

Once configured, new Wahoo activities are handled automatically so they can appear
in Garmin Connect and, where Garmin considers them eligible, contribute to
statistics, challenges, and badges.

[![Support this project on Ko-fi](https://storage.ko-fi.com/cdn/kofi3.png?v=3)](https://ko-fi.com/ntr4nce)

> [!IMPORTANT]
> This is an unofficial community project. It is not affiliated with or supported
> by Wahoo, Garmin, or Dropbox. Garmin Connect does not provide a supported public
> API for this workflow, so upstream authentication or upload changes can
> temporarily break it.

## Features

- Polls the Wahoo Dropbox folder for new FIT activities.
- Starts in dry-run mode so setup can be checked without uploading.
- Identifies a Garmin device from one genuine Garmin FIT file.
- Rewrites Wahoo FIT metadata with the Garmin FIT SDK.
- Reuses a persisted Garmin Connect session instead of signing in per activity.
- Prevents repeat uploads using Dropbox metadata, SHA256, FIT start time, filename,
  file size, stored history, and Garmin conflict responses.
- Tracks uploaded, duplicate, failed, ignored, and dry-run activities in SQLite.
- Provides a password-protected dashboard with retry, reprocess, delete, logs,
  route preview, and activity charts.

## How It Works

```text
Wahoo/ELEMNT
    |
    | automatic activity sharing
    v
Dropbox Apps/WahooFitness
    |
    | non-destructive polling
    v
Wahoo to Garmin Bridge
    |
    | deduplicate -> Garmin FIT SDK rewrite -> upload
    v
Garmin Connect
```

Normal polling never deletes the source activity from Dropbox. Dropbox deletion
only occurs when an administrator explicitly uses the delete action.

## Requirements

- A Docker host with Docker Compose
- A Wahoo or ELEMNT device and the Wahoo app
- A Dropbox account
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

Open `http://localhost:8088`, sign in, and keep dry-run mode enabled until
Dropbox, the Garmin device, and the Garmin session all show as configured.

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

- Dropbox files are discovered.
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

The bridge records Dropbox metadata before download and checks:

- Dropbox filename and size
- FIT file SHA256
- Activity start time
- Filename and size fallback
- Garmin duplicate/conflict responses

Handled Dropbox files are skipped on later polls. No duplicate system can
reliably identify every manually edited or re-exported activity, so use dry-run
mode when importing an existing archive.

## Persistent Data and Upgrades

Both paths must survive container replacement:

| Container path | Contents |
| --- | --- |
| `/data` | Incoming, processing, uploaded, duplicate, failed, and archived FIT files |
| `/appdata` | SQLite history, settings, Dropbox config, Garmin profile and tokens, detected devices, and logs |

Always mount persistent Docker volumes at both paths. Losing `/appdata` removes
deduplication history and saved authentication state. Back up both volumes before
upgrading or migrating.

Existing installations are migrated automatically on first startup. The migration
copies the saved Garmin profile and session into `/appdata/garmin`; it does not
delete the previous files. Verify the Config page and one dry-run conversion before
removing any old application data.

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
- Dropbox or Garmin credentials and tokens
- Genuine Garmin or Wahoo FIT files
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

## Limitations

- Intended for personal, self-hosted use.
- Historic Wahoo activities are not automatically backfilled by Wahoo.
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
