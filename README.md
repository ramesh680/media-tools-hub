# Media Data Tools Hub

A shared web home for the team's media data tools, ready to deploy to a free
container host (Render). It bundles:

- **Excel Validator** — upload an XLSX/CSV workbook and validate it against your rules/checkpoints.
- **YouTube Release Verifier** — verify movie/TV titles against official YouTube channels.
- **Upcoming Release Movies** — live upcoming theatrical release list (runs as a second service).

## What's intentionally NOT included on the hosted version

The Metacritic/IMDb calendar tools (TV premiere, movie/game release calendars,
TV seasons & episodes, IMDb bulk verifier, Billboard Artist 100) need a
multi-gigabyte local IMDb dataset and index. That can't run on a free tier, so
they are disabled here via `IMDB_ENABLED=false`. They still work in the full
local install. To enable them later, move to a paid plan with a persistent disk
and set `IMDB_ENABLED=true`.

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
