# MovieInsider Trailer Feed

Scrapes the newest movie trailers off MovieInsider's listing and hands back an
Excel/CSV export. That is the whole tool.

- Card: **Trailers → MovieInsider Trailer Feed** on the hub home page
- Route: `POST /movie-trailer-feed/start`
- Service: `app/services/movie_trailer_feed.py`
- Columns: `MOVIE_TRAILER_FEED_COLUMNS` in `app/models.py`
- Tests: `python -m unittest test_movie_trailer_feed` (39 tests, no network, no API key)

**No new dependencies and no API keys.** Standard library plus the existing
`HttpClient`.

---

## Replaces the Movie Trailer Channel Validator

This tool supersedes the previous **Movie Trailer Channel Validator**, which
resolved each movie's official distributor YouTube channel and diffed it against
the team's existing mapping. That requirement was withdrawn on 2026-08-03, so the
old service, its tests, its docs and its YouTube Data API usage were deleted
rather than left dormant.

What went away with it: title inputs (paste/CSV/XLSX), fuzzy title matching, the
A–Z browse tier, the `search.list` fallback, channel-ID canonicalisation, and the
`Match` / `Mismatch` / `Needs review` statuses. Nothing in the new tool consumes
a YouTube API key.

---

## What it does

Two requests, 48 rows, no matching or guessing. Pagination is offset based with
24 cards per page:

| Page | URL |
|---|---|
| 1 | `https://www.movieinsider.com/movie-trailers` |
| 2 | `https://www.movieinsider.com/movie-trailers?page_offset=24` |
| 3 | `https://www.movieinsider.com/movie-trailers?page_offset=48` |

The default run is **the first two pages**. The card exposes a page count if a
wider sweep is ever needed; it is clamped to `MAX_PAGE_COUNT` (25) so a stray
form value cannot hammer the site.

## Input

None. Press the button.

## Output columns

| Column | Source |
|---|---|
| `#` | Sequential position in the feed, newest first |
| `Title` | Image `alt`, with the trailing " trailer" stripped |
| `Trailer` | The card's label — `Official Trailer`, `Official Teaser`, `Final Trailer`, `Official Trailer #2` … |
| `YouTube Video ID` | Parsed out of the thumbnail path |
| `YouTube URL` | `watch?v=<id>` built from the above |
| `Thumbnail URL` | Full MovieInsider CDN image URL |
| `MovieInsider Movie ID` | Numeric id from `/m<id>/<slug>` |
| `MovieInsider Slug` | Slug from the same link |
| `MovieInsider URL` | Canonical `/m<id>/<slug>/videos/<video_page_id>` link |
| `Source Page` | 1 or 2 |
| `Page URL` | The listing URL that row came from |

The YouTube video id is embedded in the thumbnail path, so it comes for free:

```
https://s.movieinsider.com/images/clayface/ytimg/OGO4Mqvo3jI/hqdefault_m1784763184.jpg
                                                └ video ID ┘
```

---

## Parsing notes worth keeping

**Titles come from the image `alt`, not the heading.** The `<h3>` is truncated
for long titles ("Children of Blood and Bo..."), so `alt` is authoritative. The
heading is only used as a fallback, and a trailing ellipsis is trimmed off it.

**Cards are found by thumbnail, not by wrapper class.** Anchoring on the
thumbnail URL — the one element every card has, and the one that carries both
slug and video id — then reading the neighbouring markup for the link, `alt` and
label. This survives CSS class renames, which the previous card-`<div>` regex did
not.

**The "Most Viewed" carousel is cut off.** It repeats cards from the listing
above, so everything after that heading is dropped before parsing. Only markers
appearing *after* the first thumbnail count, so a stray phrase in the nav cannot
blank the page. Video ids are deduplicated across pages as a second guard.

**Parser guard rail.** If the first listing page yields zero trailers the run
raises `ParserBroken` rather than reporting an empty feed. MovieInsider has no
public API, so markup changes must fail loudly.

**Cloudflare.** MovieInsider serves a "Just a moment…" interstitial to cold
clients. It is detected and raised as `MovieInsiderBlocked` with a readable
message. If Render's IP gets challenged persistently, add `cloudscraper` to
`requirements.txt` and swap it in behind `HttpClient`.

`robots.txt` permits `/movie-trailers` (only `/included/`, `/admin/`, `/m0//` and
`/images/icons/` are disallowed).
