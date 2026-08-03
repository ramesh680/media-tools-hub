"""MovieInsider trailer feed scrape.

Fetches the newest ``/movie-trailers`` listing pages and returns them as flat
rows for Excel/CSV export. Nothing else - no YouTube Data API calls, no channel
resolution, no title matching. This module replaces the previous
``movie_trailer_channels`` service, which validated distributor channels; that
requirement was withdrawn.

Pagination is offset based, 24 cards per page::

    page 1 -> /movie-trailers
    page 2 -> /movie-trailers?page_offset=24
    page 3 -> /movie-trailers?page_offset=48

The default run is the first two pages (48 trailers).

Each card carries everything we export. The YouTube video id is embedded in the
thumbnail path, so no API call is needed to get it::

    https://s.movieinsider.com/images/clayface/ytimg/OGO4Mqvo3jI/hqdefault_m1784763184.jpg
                                                    |- video id -|

Only stdlib plus the ``HttpClient`` wrapper is used, so the parsers below are
unit-testable without network access.
"""
from __future__ import annotations

from html import unescape
from typing import Any, Callable
import re

from app.models import MOVIE_TRAILER_FEED_COLUMNS, utc_now_iso
from app.services.http_client import HttpClient


ProgressCallback = Callable[[int, str], None]

MOVIEINSIDER_BASE = "https://www.movieinsider.com"
TRAILERS_PATH = "/movie-trailers"

#: Cards per listing page, fixed by the site.
LISTING_PAGE_SIZE = 24
#: Pages fetched unless the caller overrides it: the first two.
DEFAULT_PAGE_COUNT = 2
#: Guard rail so a bad form value can never hammer the site.
MAX_PAGE_COUNT = 25

THUMB_RE = re.compile(
    r"https?://[^\s\"']*?/images/(?P<slug>[^/\"']+)/ytimg/"
    r"(?P<video_id>[A-Za-z0-9_-]{11})/(?P<file>[^\s\"'>]+)",
    re.IGNORECASE,
)
DETAIL_RE = re.compile(
    r"/m(?P<movie_id>\d+)/(?P<slug>[^/\"'?#]+)/videos/(?P<video_page_id>\d+)",
    re.IGNORECASE,
)
ALT_RE = re.compile(r'alt="(?P<alt>[^"]*)"', re.IGNORECASE)
LABEL_RE = re.compile(r'<p class="small[^"]*">(?P<label>[^<]*)</p>', re.IGNORECASE)
HEADING_RE = re.compile(r'<h3 class="media-heading">(?P<heading>.*?)</h3>', re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
TRAILING_TRAILER_RE = re.compile(r"\s+trailer\s*$", re.IGNORECASE)

# The listing section ends before the "Most Viewed" carousel, which repeats
# cards already shown above. Cutting there keeps the export to the real feed.
SECTION_END_MARKERS = ("most viewed", "most popular")

CHALLENGE_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform")

#: How far either side of a thumbnail to look for that card's other fields.
_WINDOW_BEFORE = 400
_WINDOW_AFTER = 900


class MovieInsiderBlocked(RuntimeError):
    """MovieInsider served a Cloudflare interstitial instead of the listing."""


class ParserBroken(RuntimeError):
    """The first listing page parsed to zero trailers - markup likely changed."""


def page_url(page_number: int) -> str:
    """URL for 1-based ``page_number`` of the trailer listing."""
    offset = (max(page_number, 1) - 1) * LISTING_PAGE_SIZE
    return f"{MOVIEINSIDER_BASE}{TRAILERS_PATH}" + (f"?page_offset={offset}" if offset else "")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _listing_section(html: str) -> str:
    """Trim the trailing "Most Viewed" carousel off a listing page.

    Only markers that appear *after* the first thumbnail count, so a marker
    somewhere in the header or nav can never blank out the whole listing.
    """
    body = html or ""
    first_thumb = THUMB_RE.search(body)
    if not first_thumb:
        return body

    lowered = body.lower()
    cuts = [lowered.find(marker, first_thumb.end()) for marker in SECTION_END_MARKERS]
    cuts = [index for index in cuts if index > 0]
    return body[: min(cuts)] if cuts else body


def parse_listing_page(html: str, *, limit: int = LISTING_PAGE_SIZE) -> list[dict[str, Any]]:
    """Parse one ``/movie-trailers`` page into card records.

    Anchored on the thumbnail URL, because that is the one element every card
    has and it carries both the slug and the YouTube video id. The other fields
    are read from a window of markup around it.

    The image ``alt`` is the authoritative title: the ``<h3>`` is truncated with
    an ellipsis for long titles ("Children of Blood and Bo..."), whereas the alt
    holds the full title with a " trailer" suffix.
    """
    body = _listing_section(html)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    for thumb in THUMB_RE.finditer(body):
        video_id = thumb.group("video_id")
        if video_id in seen:
            continue

        start, end = thumb.start(), thumb.end()
        before = body[max(0, start - _WINDOW_BEFORE):start]
        after = body[end:end + _WINDOW_AFTER]

        alt = ALT_RE.search(after)
        title = _clean(TRAILING_TRAILER_RE.sub("", unescape(alt.group("alt")) if alt else ""))
        if not title or title.endswith("..."):
            heading = HEADING_RE.search(after)
            candidate = _clean(unescape(TAG_RE.sub(" ", heading.group("heading")))) if heading else ""
            if candidate.endswith("..."):
                candidate = candidate[:-3].strip()
            title = candidate or title
        if not title:
            title = thumb.group("slug").replace("-", " ").title()

        detail = DETAIL_RE.search(after) or DETAIL_RE.search(before)
        label = LABEL_RE.search(after)

        seen.add(video_id)
        records.append(
            {
                "title": title,
                "slug": detail.group("slug") if detail else thumb.group("slug"),
                "movie_id": detail.group("movie_id") if detail else "",
                "video_page_id": detail.group("video_page_id") if detail else "",
                "trailer_label": _clean(unescape(label.group("label"))) if label else "Trailer",
                "youtube_video_id": video_id,
                "thumbnail_url": thumb.group(0),
                "movieinsider_url": (
                    f"{MOVIEINSIDER_BASE}/m{detail.group('movie_id')}/{detail.group('slug')}"
                    f"/videos/{detail.group('video_page_id')}"
                    if detail
                    else ""
                ),
            }
        )
        if len(records) >= limit:
            break

    return records


class MovieTrailerFeedService:
    """Scrapes the newest MovieInsider trailer listing pages."""

    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client

    def fetch_feed(
        self,
        *,
        page_count: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        pages = DEFAULT_PAGE_COUNT if page_count is None else page_count
        pages = max(1, min(int(pages), MAX_PAGE_COUNT))

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        fetched_urls: list[str] = []

        for page_number in range(1, pages + 1):
            url = page_url(page_number)
            if progress:
                progress(
                    int(((page_number - 1) / pages) * 80) + 5,
                    f"Fetching trailer page {page_number} of {pages}",
                )

            html = self._get_html(url)
            fetched_urls.append(url)
            page_records = parse_listing_page(html)

            if not page_records:
                if page_number == 1:
                    raise ParserBroken(
                        "No trailers could be parsed from the first MovieInsider listing page. "
                        "The site markup has probably changed - the scrape fails loudly rather "
                        "than reporting an empty feed."
                    )
                break

            for record in page_records:
                if record["youtube_video_id"] in seen:
                    continue
                seen.add(record["youtube_video_id"])
                rows.append(
                    {
                        "#": len(rows) + 1,
                        "Title": record["title"],
                        "Trailer": record["trailer_label"],
                        "YouTube Video ID": record["youtube_video_id"],
                        "YouTube URL": f"https://www.youtube.com/watch?v={record['youtube_video_id']}",
                        "Thumbnail URL": record["thumbnail_url"],
                        "MovieInsider Movie ID": record["movie_id"],
                        "MovieInsider Slug": record["slug"],
                        "MovieInsider URL": record["movieinsider_url"],
                        "Source Page": page_number,
                        "Page URL": url,
                    }
                )

        if progress:
            progress(95, "Preparing exports")

        return {
            "tracker_type": "movie_trailer_feed",
            "title": "MovieInsider Trailer Feed",
            "created_at": utc_now_iso(),
            "source_url": fetched_urls[0] if fetched_urls else f"{MOVIEINSIDER_BASE}{TRAILERS_PATH}",
            "summary": (
                f"{len(rows)} trailers from the newest {len(fetched_urls)} "
                f"MovieInsider trailer {'page' if len(fetched_urls) == 1 else 'pages'} "
                f"({', '.join(fetched_urls)}). Download as Excel or CSV."
            ),
            "sections": [
                {
                    "key": "movie_trailer_feed",
                    "title": "Newest Movie Trailers",
                    "columns": MOVIE_TRAILER_FEED_COLUMNS,
                    "rows": rows,
                    "row_count": len(rows),
                    "supports_google": False,
                }
            ],
        }

    # -- transport ---------------------------------------------------------
    def _get_html(self, url: str) -> str:
        html = self.http_client.get_text(url)
        head = (html or "")[:4000].lower()
        if any(marker in head for marker in CHALLENGE_MARKERS):
            raise MovieInsiderBlocked(
                "MovieInsider returned a Cloudflare verification page instead of content. "
                "Retry in a minute; if it persists the host IP is being challenged."
            )
        return html
