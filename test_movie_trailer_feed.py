"""Tests for the MovieInsider trailer feed scrape.

    python -m unittest test_movie_trailer_feed -v

No network, no API key. The HTML fixtures mirror the real markup, including the
awkward parts: truncated ``<h3>`` headings, HTML entities in titles, and the
trailing "Most Viewed" carousel that repeats cards from the listing above.
"""
from __future__ import annotations

import unittest

from app.models import MOVIE_TRAILER_FEED_COLUMNS
from app.services.movie_trailer_feed import (
    DEFAULT_PAGE_COUNT,
    LISTING_PAGE_SIZE,
    MAX_PAGE_COUNT,
    MovieInsiderBlocked,
    MovieTrailerFeedService,
    ParserBroken,
    page_url,
    parse_listing_page,
)


def card(
    title: str,
    slug: str,
    video_id: str,
    movie_id: str,
    video_page_id: str,
    label: str = "Official Trailer",
    heading: str | None = None,
) -> str:
    """One listing card in MovieInsider's markup shape."""
    return f"""
    <div class="trailers">
      <a href="https://www.movieinsider.com/m{movie_id}/{slug}/videos/{video_page_id}">
        <img src="https://s.movieinsider.com/images/{slug}/ytimg/{video_id}/hqdefault_m1785277691.jpg"
             alt="{title} trailer">
      </a>
      <h3 class="media-heading">
        <a href="https://www.movieinsider.com/m{movie_id}/{slug}/videos/{video_page_id}">{heading if heading is not None else title}</a>
      </h3>
      <p class="small text-muted">{label}</p>
    </div>
    """


def listing_page(*cards: str, most_viewed: str = "") -> str:
    return f"""<html><body>
    <h1>Movie Trailers &amp; Video Clips</h1>
    <h2>Newest Movie Trailers &amp; Clips</h2>
    {''.join(cards)}
    <h2>Most Viewed: Popular 2026 Trailers &amp; Clips</h2>
    {most_viewed}
    </body></html>"""


PAGE_ONE = listing_page(
    card("Below (series)", "below", "gH3fL9cMvuM", "25538", "22052"),
    card("Evel", "evel", "eiDxHQq4s0g", "25537", "22051"),
    card("Air Bud Returns", "air-bud-returns", "I46hFjWKGA4", "24504", "22050", label="Official Teaser"),
    card(
        "Children of Blood and Bone",
        "children-of-blood-and-bone",
        "HnWvg90lQt8",
        "17236",
        "22049",
        heading="Children of Blood and Bo...",
    ),
    # Repeated below the fold - must not produce a duplicate row.
    most_viewed=card("Evel", "evel", "eiDxHQq4s0g", "25537", "22051"),
)

PAGE_TWO = listing_page(
    card("Clayface", "clayface", "OGO4Mqvo3jI", "23586", "22033"),
    card("Camp Rock 3", "camp-rock-3", "02-RDIZ5Rdw", "24871", "22039"),
)


class StubHttpClient:
    """Serves canned HTML per URL and records the request order."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get_text(self, url: str) -> str:
        self.requested.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected URL requested: {url}")
        return self.pages[url]


PAGES = {page_url(1): PAGE_ONE, page_url(2): PAGE_TWO}


def service(pages: dict[str, str] | None = None) -> MovieTrailerFeedService:
    return MovieTrailerFeedService(StubHttpClient(pages if pages is not None else dict(PAGES)))


class PageUrlTests(unittest.TestCase):
    def test_first_page_has_no_offset(self):
        self.assertEqual(page_url(1), "https://www.movieinsider.com/movie-trailers")

    def test_second_page_uses_offset_24(self):
        self.assertEqual(page_url(2), "https://www.movieinsider.com/movie-trailers?page_offset=24")

    def test_third_page_uses_offset_48(self):
        self.assertEqual(page_url(3), "https://www.movieinsider.com/movie-trailers?page_offset=48")

    def test_offset_steps_by_the_listing_page_size(self):
        self.assertEqual(LISTING_PAGE_SIZE, 24)

    def test_page_zero_is_treated_as_page_one(self):
        self.assertEqual(page_url(0), page_url(1))


class ParseListingPageTests(unittest.TestCase):
    def test_parses_every_card_on_the_page(self):
        self.assertEqual(len(parse_listing_page(PAGE_ONE)), 4)

    def test_reads_title_from_the_image_alt(self):
        self.assertEqual(parse_listing_page(PAGE_ONE)[0]["title"], "Below (series)")

    def test_strips_the_trailing_trailer_word_from_the_alt(self):
        self.assertNotIn("trailer", parse_listing_page(PAGE_ONE)[1]["title"].lower())

    def test_alt_beats_a_truncated_heading(self):
        titles = [record["title"] for record in parse_listing_page(PAGE_ONE)]
        self.assertIn("Children of Blood and Bone", titles)
        self.assertNotIn("Children of Blood and Bo...", titles)

    def test_extracts_the_youtube_video_id_from_the_thumbnail(self):
        self.assertEqual(parse_listing_page(PAGE_ONE)[0]["youtube_video_id"], "gH3fL9cMvuM")

    def test_keeps_the_full_thumbnail_url(self):
        thumb = parse_listing_page(PAGE_ONE)[0]["thumbnail_url"]
        self.assertTrue(thumb.startswith("https://s.movieinsider.com/images/below/ytimg/"))
        self.assertTrue(thumb.endswith(".jpg"))

    def test_extracts_movie_id_and_slug(self):
        record = parse_listing_page(PAGE_ONE)[0]
        self.assertEqual(record["movie_id"], "25538")
        self.assertEqual(record["slug"], "below")

    def test_builds_the_movieinsider_url(self):
        self.assertEqual(
            parse_listing_page(PAGE_ONE)[0]["movieinsider_url"],
            "https://www.movieinsider.com/m25538/below/videos/22052",
        )

    def test_captures_the_trailer_label(self):
        labels = [record["trailer_label"] for record in parse_listing_page(PAGE_ONE)]
        self.assertEqual(labels[0], "Official Trailer")
        self.assertEqual(labels[2], "Official Teaser")

    def test_ignores_the_most_viewed_carousel(self):
        video_ids = [record["youtube_video_id"] for record in parse_listing_page(PAGE_ONE)]
        self.assertEqual(len(video_ids), len(set(video_ids)))

    def test_never_returns_more_than_a_page_of_cards(self):
        many = listing_page(*[card(f"Movie {i}", f"movie-{i}", f"vid{i:08d}", str(i), str(i)) for i in range(40)])
        self.assertEqual(len(parse_listing_page(many)), LISTING_PAGE_SIZE)

    def test_decodes_html_entities_in_titles(self):
        page = listing_page(card("Fire &amp; Ice", "fire-ice", "aaaaaaaaaaa", "1", "2"))
        self.assertEqual(parse_listing_page(page)[0]["title"], "Fire & Ice")

    def test_falls_back_to_the_slug_when_no_title_markup_exists(self):
        bare = '<img src="https://s.movieinsider.com/images/zip-wire/ytimg/M9tHmu0wNHM/hqdefault_m1.jpg">'
        self.assertEqual(parse_listing_page(bare)[0]["title"], "Zip Wire")

    def test_empty_html_yields_nothing(self):
        self.assertEqual(parse_listing_page(""), [])

    def test_page_without_thumbnails_yields_nothing(self):
        self.assertEqual(parse_listing_page("<html><body><p>nothing here</p></body></html>"), [])


class FetchFeedTests(unittest.TestCase):
    def test_defaults_to_the_first_two_pages(self):
        self.assertEqual(DEFAULT_PAGE_COUNT, 2)
        svc = service()
        svc.fetch_feed()
        self.assertEqual(svc.http_client.requested, [page_url(1), page_url(2)])

    def test_returns_one_row_per_trailer(self):
        rows = service().fetch_feed()["sections"][0]["rows"]
        self.assertEqual(len(rows), 6)

    def test_rows_use_the_declared_columns(self):
        rows = service().fetch_feed()["sections"][0]["rows"]
        self.assertEqual(list(rows[0].keys()), MOVIE_TRAILER_FEED_COLUMNS)

    def test_section_declares_the_export_columns(self):
        section = service().fetch_feed()["sections"][0]
        self.assertEqual(section["columns"], MOVIE_TRAILER_FEED_COLUMNS)
        self.assertEqual(section["row_count"], len(section["rows"]))

    def test_numbers_rows_sequentially_across_pages(self):
        rows = service().fetch_feed()["sections"][0]["rows"]
        self.assertEqual([row["#"] for row in rows], [1, 2, 3, 4, 5, 6])

    def test_records_which_page_each_row_came_from(self):
        rows = service().fetch_feed()["sections"][0]["rows"]
        self.assertEqual([row["Source Page"] for row in rows], [1, 1, 1, 1, 2, 2])
        self.assertEqual(rows[4]["Page URL"], page_url(2))

    def test_builds_a_watchable_youtube_url(self):
        rows = service().fetch_feed()["sections"][0]["rows"]
        self.assertEqual(rows[0]["YouTube URL"], "https://www.youtube.com/watch?v=gH3fL9cMvuM")

    def test_preserves_listing_order(self):
        rows = service().fetch_feed()["sections"][0]["rows"]
        self.assertEqual(rows[0]["Title"], "Below (series)")
        self.assertEqual(rows[4]["Title"], "Clayface")

    def test_one_page_run_only_makes_one_request(self):
        svc = service({page_url(1): PAGE_ONE})
        result = svc.fetch_feed(page_count=1)
        self.assertEqual(svc.http_client.requested, [page_url(1)])
        self.assertEqual(len(result["sections"][0]["rows"]), 4)

    def test_page_count_is_clamped_to_the_maximum(self):
        svc = service({page_url(n): (PAGE_ONE if n == 1 else "") for n in range(1, MAX_PAGE_COUNT + 1)})
        svc.fetch_feed(page_count=999)
        self.assertLessEqual(len(svc.http_client.requested), MAX_PAGE_COUNT)

    def test_zero_or_negative_page_count_still_fetches_one_page(self):
        svc = service({page_url(1): PAGE_ONE})
        svc.fetch_feed(page_count=0)
        self.assertEqual(svc.http_client.requested, [page_url(1)])

    def test_stops_early_when_a_later_page_is_empty(self):
        svc = service({page_url(1): PAGE_ONE, page_url(2): "<html></html>"})
        result = svc.fetch_feed()
        self.assertEqual(len(result["sections"][0]["rows"]), 4)

    def test_deduplicates_a_trailer_repeated_across_pages(self):
        svc = service({page_url(1): PAGE_ONE, page_url(2): PAGE_ONE})
        rows = svc.fetch_feed()["sections"][0]["rows"]
        self.assertEqual(len(rows), 4)

    def test_tracker_type_and_title_are_set(self):
        result = service().fetch_feed()
        self.assertEqual(result["tracker_type"], "movie_trailer_feed")
        self.assertEqual(result["title"], "MovieInsider Trailer Feed")

    def test_summary_names_both_fetched_pages(self):
        summary = service().fetch_feed()["summary"]
        self.assertIn(page_url(1), summary)
        self.assertIn(page_url(2), summary)
        self.assertIn("6 trailers", summary)

    def test_progress_is_reported(self):
        seen: list[tuple[int, str]] = []
        service().fetch_feed(progress=lambda pct, msg: seen.append((pct, msg)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], 95)
        self.assertTrue(all(0 <= pct <= 100 for pct, _ in seen))

    def test_export_is_not_google_sheets_capable(self):
        self.assertFalse(service().fetch_feed()["sections"][0]["supports_google"])


class FailureTests(unittest.TestCase):
    def test_empty_first_page_raises_rather_than_reporting_zero_trailers(self):
        svc = service({page_url(1): "<html><body>no cards</body></html>"})
        with self.assertRaises(ParserBroken):
            svc.fetch_feed(page_count=1)

    def test_cloudflare_interstitial_is_reported_clearly(self):
        svc = service({page_url(1): "<html><title>Just a moment...</title></html>"})
        with self.assertRaises(MovieInsiderBlocked):
            svc.fetch_feed(page_count=1)


if __name__ == "__main__":
    unittest.main()
