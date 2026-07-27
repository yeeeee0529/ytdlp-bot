# yt-dlp Bot — Operations Guide (English)

## Overview

Self-hosted Telegram and Discord media download bot. Users submit public HTTP(S) URLs; the service produces MP4/MP3 (or playlist ZIP) artifacts, reports progress, supports cancel/status, uploads directly under the platform limit, otherwise issues reusable signed range download links.

## Requirements

- Linux host with Docker and Docker Compose
- CPython 3.13 + [uv](https://github.com/astral-sh/uv) for development
- FFmpeg / ffprobe (image-bundled for deployment)
- Operator-chosen `capacity_bytes` within host disk limits

## Quick start (development)

```bash
uv sync --all-extras --frozen
uv run ruff format --check
uv run ruff check
uv run pyright
uv run pytest
```

## Configuration

Copy `config.example.toml` to a secure path (not world-readable). Resolve secrets via:

- `env:VAR_NAME`
- `file:/run/secrets/...`

Required highlights:

- `storage.capacity_bytes` — operator selected (example value is illustrative)
- Under capacity pressure, expired artifacts are removed first, then oldest non-expired by `ready_at`, skipping active HTTP stream / platform upload leases; only then is a new reservation denied
- `artifacts.public_base_url` — HTTPS, no query/fragment/trailing slash
- `artifacts.signing_secret_ref` — ≥ 32 bytes entropy
- Optional `media.cookie_file_ref` — absolute `file:` reference to an
  operator-managed Netscape-format cookie file
- At least one platform enabled with a token secret
- Static `access.administrators` cannot be changed via chat

### Optional authenticated media

To let yt-dlp use an operator-managed authenticated session, place a
Netscape-format cookie file under the existing read-only secrets mount:

```text
secrets/youtube_cookies.txt
```

Set restrictive host permissions, then enable it in `config.toml`:

```toml
[media]
cookie_file_ref = "file:/run/secrets/youtube_cookies.txt"
```

Only absolute `file:` references are accepted. Cookie contents are not loaded
into `EffectiveConfig`, sent through the worker protocol, exposed to chat
commands, or included in startup summaries. The cookie-file path is passed to
the worker, and yt-dlp opens the read-only file when processing a job. yt-dlp
cookie-jar write-back is disabled; update or rotate the mounted file through a
controlled deployment.

Use a dedicated account with the minimum required access. Every authorized bot
user can indirectly use the account's media entitlements, and high-volume use
can result in account restrictions. Rotate or remove the cookie file if the
session is exposed. Cookies may help with account-required content but do not
make deleted, inaccessible, DRM-protected, or otherwise unavailable media
downloadable.

## Deployment

```bash
docker compose config
docker compose build
# Provide secret files under ./secrets before up
docker compose up -d
```

Health: private `/healthz` (liveness) and `/readyz` (readiness). Public download origin serves only `/v1/artifacts/{id}/{name}`.

## Security notes

- Controlled egress is mandatory; URL validation alone is insufficient.
- Logs must never contain bot tokens, signing secrets, complete bearer URLs, or sensitive source URL components.
- Treat an authenticated cookie file as an account credential: never commit it,
  bake it into the image, or make it world-writable.
- Run the app as non-root with a read-only root filesystem expectation.

## Alerts and runbooks

| Signal | Meaning | Operator action |
| --- | --- | --- |
| `/readyz` not ready | Admission closed | Inspect logs for recovery/egress/storage; fix config or disk |
| Capacity denials rising | Near limit with nothing reclaimable (or all candidates leased) | Raise `capacity_bytes`, free disk, or wait for active downloads to finish |
| Cleanup last_error set | Deletion retry stuck | Check filesystem permissions and artifact leases |
| Worker spawn failures | Media pipeline unhealthy | Verify FFmpeg/yt-dlp in image; enable fixture mode only for CI |

Backup: stop writers, copy SQLite + WAL/SHM under `state/`, copy `data/artifacts/`. Restore onto empty volumes before starting.

Upgrade: pull image, `docker compose up -d`, confirm `/readyz`, run controlled live smoke.

## Live smoke (manual)

With real credentials (not part of routine CI): submit `/ytdl` and `/ytmp3` on both platforms, verify progress, cancel, status, direct upload below limit, signed link above limit, and restart reconciliation.

Release acceptance: deterministic gates live in `.github/workflows/ci.yml`. Local agent status: `doc/current_progress.md`. Historical AC checkbox ledger: `doc/archive/tasks/progress.md` (archived; not open work).
