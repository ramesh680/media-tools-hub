# Movie Trailer Channel Validator

Finds the official **distributor** YouTube channel for each movie and validates
the team's existing channel mapping against it.

- Card: **Trailers · YouTube → Movie Trailer Channel Validator** on the hub home page
- Route: `POST /movie-trailer-channels/start`
- Service: `app/services/movie_trailer_channels.py`
- Columns: `MOVIE_TRAILER_CHANNEL_COLUMNS` in `app/models.py`
- Tests: `python -m unittest test_movie_trailer_channels` (49 tests, no network, no API key)

**No new dependencies.** Uses only the stdlib plus `openpyxl`, which is already in
`requirements.txt`.

---

## Why this exists next to the YouTube Release Verifier

The existing `YouTubeReleaseVerifierService` calls `search.list` once per title.
That costs **100 quota units per title**, so a 10,000 unit/day project quota tops
out near 99 titles/day — and it still has to guess which of the top ten search
results is the real official trailer.

MovieInsider publishes the trailer's YouTube video ID directly in its thumbnail URLs:

```
https://s.movieinsider.com/images/clayface/ytimg/OGO4Mqvo3jI/hqdefault_m1784763184.jpg
                                              └─ YouTube video ID ─┘
```

Feeding that into `videos.list` (1 unit per **50** videos) returns the uploader's
channel exactly, with no guessing.

| Approach | Cost per title | Titles per 10k quota |
|---|---|---|
| `search.list` (existing verifier) | ~101 units | ~99 |
| `videos.list` (this service) | ~0.02 units | ~500,000 |

The old verifier is not replaced — it becomes tier C below, so coverage never
gets worse.

---

## Lookup tiers

Each tier is bounded, so a run stays fast and needs no persistence. That matters:
Render's free tier has no persistent disk and sleeps after ~15 min idle.

**A. Trailer listing sweep.** The newest N pages of `/movie-trailers` (24 cards
each, default 25 pages = 600 trailers). Yields the video ID immediately. Covers
current and upcoming releases — the common case.

**B. A–Z browse.** For titles tier A missed, walks `/alpha/<letter>` (20 per page,
newest first) to find the numeric movie ID, then reads the trailer embed off
`/m<id>/<slug>`.

> This tier is necessary because **MovieInsider routes on the numeric ID and
> ignores the slug entirely** — `/m1/clayface` serves *Spy Kids 2*. A movie page
> cannot be reached from a title alone, and there is no on-site search API
> (site search is Google CSE).

**C. YouTube search fallback.** Delegates to the existing verifier for anything
MovieInsider has no trailer for. Off by unchecking the box on the card. Rows
resolved this way are labelled in **Resolved Via** and never auto-confirm unless
the old verifier itself returned `Confirmed`.

---

## Input

Paste titles one per line, or upload CSV/XLSX. Only the title is required.

```
Clayface | https://www.youtube.com/@wbpictures | 2026
Camp Rock 3
Coyote vs. ACME
```

Pipe order: `Title | channel | year | imdb_id | distributor`

Spreadsheet headers are alias-matched — `Movie`, `Movie Title`, `Film`,
`YouTube Channel URL`, `Release Year`, `IMDb ID`, `Distributor` all resolve.

**Channels are compared by channel ID, never by URL text.** The same channel is
reachable as `/channel/UC…`, `/@handle`, `/c/name` and `/user/name`; comparing
strings is the most common source of false mismatches in manual review, so every
reference is canonicalised first (handles cost 1 quota unit each, memoised per run).

---

## Statuses

| Status | Meaning |
|---|---|
| `Match` | Your channel is the distributor that uploaded the trailer |
| `Mismatch` | Your channel differs — the correct one is in the row |
| `Needs review` | Fuzzy title match, or your channel reference could not be canonicalised |
| `Not official upload` | Trailer was posted by an aggregator (JoBlo, Rotten Tomatoes Trailers, MovieClips), so the uploader is not the distributor |
| `Title not found` | No MovieInsider trailer within the scanned window |
| `No channel found` | Title matched, but the video is deleted, private or region-blocked |
| `No channel provided` | Your row had no channel — the output still tells you what it should be |

Rows are sorted worst-first so mismatches surface at the top of the export.

---

## Accuracy notes worth knowing

**Sequel numbers are protected.** A single digit decides which film it is, but
plain string similarity rates "Camp Rock 3" against "Camp Rock 4" at ~91 — above
any sane auto-accept threshold. Scores are capped at 74 when numeric tokens
disagree, which routes those pairs to review instead of silently matching the
wrong film. Covered by `test_sequel_confusion_does_not_produce_a_false_match`.

**Titles come from the image `alt`, not the heading.** The `<h3>` is truncated for
long titles ("Children of Blood and Bo..."), so `alt` is authoritative.

**Parser guard rail.** If the first listing page yields zero trailers the run
raises `ParserBroken` rather than reporting everything as not-found. MovieInsider
has no public API, so markup changes must fail loudly. A regex fallback handles
CSS class renames.

**Cloudflare.** MovieInsider serves a "Just a moment…" interstitial to cold
clients. It's detected and raised as `MovieInsiderBlocked` with a readable
message. If Render's IP gets challenged persistently, add `cloudscraper` to
requirements and swap it in behind `HttpClient`.

`robots.txt` permits `/movie-trailers` and `/alpha` (only `/included/`, `/admin/`,
`/m0//` and `/images/icons/` are disallowed).

---

## The one non-technical thing to flag

Individual films almost never have their own YouTube channel — trailers live on
distributor channels. Expect many titles to resolve to the same studio channel;
that is the correct answer given "official channel = the distributor's account",
not a bug. If the existing sheet was built assuming one channel per movie, most
"corrections" will be studio channels.
