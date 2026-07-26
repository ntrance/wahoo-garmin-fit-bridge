# Deploy to Dokploy

## Create the application

1. Create a Dokploy project and application.
2. Select GitHub as the source and connect this repository.
3. Use `docker-compose.dokploy.yml`.
4. Add the required values from `.env.example` in Dokploy's Environment tab.
5. Store passwords and session secrets only as Dokploy environment secrets.

Recommended production values:

```env
WEB_AUTH_ENABLED=true
WEB_USERNAME=your-admin-username
WEB_PASSWORD=use-a-strong-password
SESSION_SECRET_KEY=use-a-separate-long-random-secret
SESSION_COOKIE_SECURE=true
GARMIN_PROFILE_NAME=wahoo
DRY_RUN=true
```

Generate `SESSION_SECRET_KEY` with:

```bash
openssl rand -hex 32
```

Do not put real credentials in build arguments or commit them to `.env`.

## Persistent volumes

The supplied compose file creates:

- `bridge_appdata` mounted at `/appdata`
- `bridge_data` mounted at `/data`

These volumes contain credentials, Garmin session tokens, deduplication history,
and processed activities. Confirm both mounts exist before enabling live uploads.
Deleting either volume can lose state and may allow old Dropbox files to be
processed again.

## First deployment

1. Deploy with `DRY_RUN=true`.
2. Open `/config`.
3. Configure and test Dropbox.
4. Upload a genuine Garmin FIT file and select the detected device.
5. Save the Garmin account and create its session.
6. Run a rescan and verify dates, distances, and duplicate handling.
7. Change `DRY_RUN=false` and redeploy.

## Updating

Back up both named volumes before a major update. A normal redeploy replaces the
application container but retains `/data` and `/appdata`.

After deployment, verify:

- `/health` returns `{"status":"ok"}`.
- Config shows Dropbox and Garmin as configured.
- Existing dashboard history is present.
- The bridge remains in dry-run mode until you intentionally enable uploads.
