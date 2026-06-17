# Media Data Tools Hub

A shared web home for the team's media data tools, ready to deploy to a free
container host (Render). It bundles:

- **Excel Validator** — upload an XLSX/CSV workbook and validate it against your rules/checkpoints.
- **YouTube Release Verifier** — verify movie/TV titles against official YouTube channels.
- **Upcoming Release Movies** — live upcoming theatrical release list (runs as a second service).

## TV Season & Episode enrichment on the hosted version (free)

The **"Daily - TV Season and Episode Data Review"** card works on the free Render
tier — no paid plan or disk required. When the local IMDb index is off
(`IMDB_ENABLED=false`, the hosted default), the tool enriches each Metacritic TV
title with its IMDb ttcode, total seasons, and total episodes by querying the
**TMDB API** instead of the multi-gigabyte local dataset.

To turn it on for the hosted site:

1. Get a free TMDB API key: themoviedb.org → create an account → **Settings →
   API** → request a developer key.
2. In the Render dashboard for `media-tools-hub` (Environment tab), set
   `TMDB_API_KEY` (or `TMDB_READ_ACCESS_TOKEN`). That's the only requirement —
   the card appears automatically once a TMDB key is present.

Caveats: TMDB matches by title, so an occasional obscure or ambiguous title may
come back blank or low-confidence; season/episode counts reflect TMDB's data
(usually accurate, can differ slightly from IMDb); and free Render instances
cold-start (~30–60s) after idle.

The heavier IMDb-index tools (full ttcode enrichment across all calendars, IMDb
bulk verifier) still need the multi-GB local dataset and run in the full local
install (`IMDB_ENABLED=true`). To run those on Render you'd need a paid plan with
a persistent disk; the TMDB path above avoids that for the season/episode tool.

## Deploy to Render (free)

1. Push this folder to a new GitHub repository.
2. In Render: **New + → Blueprint**, select the repo. Render reads `render.yaml`
   and creates two free web services: `media-tools-hub` and
   `upcoming-release-movies`.
3. On the `media-tools-hub` service, set these environment variables (Environment tab):
   - `YOUTUBE_API_KEY`
   - `TMDB_API_KEY`
   - `TMDB_READ_ACCESS_TOKEN`
   (Values are in your local `.env`. `IMDB_ENABLED` is already set to `false`.)
4. Wait for both services to go live. Copy the `upcoming-release-movies` URL,
   then set `UPCOMING_MOVIES_URL` on `media-tools-hub` to that full https URL and
   redeploy. A link card for it will appear on the hub home page.
5. Share the `media-tools-hub` URL with your team.

Note: free Render instances sleep after ~15 minutes idle and take ~30–60s to
wake on the next request. That's normal for the free tier.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in keys as needed
uvicorn app.main:app --port 8000           # hub  -> http://127.0.0.1:8000/
python upcoming_movies/upcoming_release_movies_app.py --port 8765   # movies app
```

## Configuration

All settings are read from environment variables (see `.env.example`). Secrets
should be set in the Render dashboard, never committed. `.gitignore` already
excludes `.env`, `data/`, logs, and any large `*.sqlite3` / `*.tsv.gz` files.
