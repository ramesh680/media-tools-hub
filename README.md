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

## Indian Regional Release Tracker (`/indian-release-tracker`)

Web version of the `indian-regional-release-tracker` skill. Movies and TV shows across 13 Indian
languages with IMDb ID, release date/status, genre, language, producer, **Studio**, **Network**,
director, cast, trailer link, **Top 3 YouTube Channels** (`<channel URL>|<Title>` - one channel each
for the trailer, the full movie and the songs), CBFC certificate fields, origin and Pay 1/Library window.

Run modes: **1** 2024 -> today + 1 year (with Date TBA titles), **2** next 3 months, **3** next 6 months
(both from the 1st of the current month), **4** only an uploaded/pasted title list (any year/country,
plus a Source Coverage table), **5** custom dates + languages + notes. Titles from an uploaded list are
always kept, whatever their date.

Sources read live on every run: Wikipedia year lists and OTT-originals lists (MediaWiki API), the IMDb
India calendar (best effort - IMDb can refuse datacentre IPs; the run notes it and continues), Wikidata
(IMDb IDs, Studio, Network for listed titles), YouTube's results page (falls back to the YouTube Data API
when `YOUTUBE_API_KEY` or a pasted key is available; capped at 25 titles per run for quota), and CBFC's
official website (best effort - the site blocks many servers; the run says so when it does).

Free-tier limits: YouTube look-ups are capped at 600 titles per run (default scope: listed titles +
the last 90 days + upcoming); CBFC checks at 150 recent/upcoming films. Tests:
`python -m unittest test_indian_release_tracker -v` (offline fixtures, no network).

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
