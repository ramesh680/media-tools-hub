"""Offline tests for the Movie Trailer Channel Validator.

No network and no API key required. HTML fixtures are verbatim MovieInsider
markup captured from the live site on 2026-07-29, so a parser regression fails
here rather than in production.

    python -m unittest test_movie_trailer_channels -v
"""
from __future__ import annotations

import unittest

from app.services.movie_trailer_channels import (
    MATCH,
    MISMATCH,
    NEEDS_REVIEW,
    NOT_OFFICIAL_UPLOAD,
    NO_CHANNEL_FOUND,
    NO_CHANNEL_PROVIDED,
    TITLE_NOT_FOUND,
    MovieTrailerChannelService,
    TitleInput,
    classify_channel,
    match_key,
    parse_alpha_page,
    parse_listing_page,
    parse_channel_reference,
    title_score,
    _inputs_from_csv,
    _inputs_from_text,
)


LISTING_HTML = """
<div class="trailers"> <a class="trailer-thumb" href="https://www.movieinsider.com/m25538/below/videos/22052">
  <img src="https://s.movieinsider.com/images/below/ytimg/gH3fL9cMvuM/hqdefault_m1785277691.jpg"
       alt="Below (series) trailer" class="trailer-img" loading="lazy">
  <span class="trailer-overlay"></span> </a>
  <h3 class="media-heading"> <a href="https://www.movieinsider.com/m25538/below/videos/22052"> Below (series) </a> </h3>
  <p class="small text-muted">Official Trailer</p> </div>
<div class="trailers"> <a class="trailer-thumb" href="https://www.movieinsider.com/m23586/clayface/videos/22033">
  <img src="https://s.movieinsider.com/images/clayface/ytimg/OGO4Mqvo3jI/hqdefault_m1784763184.jpg"
       alt="Clayface trailer" class="trailer-img"></a>
  <h3 class="media-heading"><a href="https://www.movieinsider.com/m23586/clayface/videos/22033">Clayface</a></h3>
  <p class="small text-muted">Official Trailer</p> </div>
<div class="trailers"> <a class="trailer-thumb" href="https://www.movieinsider.com/m23586/clayface/videos/21816">
  <img src="https://s.movieinsider.com/images/clayface/ytimg/BBBBBBBBBBB/hqdefault_m1780000000.jpg"
       alt="Clayface trailer" class="trailer-img"></a>
  <h3 class="media-heading"><a href="https://www.movieinsider.com/m23586/clayface/videos/21816">Clayface</a></h3>
  <p class="small text-muted">Official Teaser</p> </div>
<div class="trailers"> <a class="trailer-thumb" href="https://www.movieinsider.com/m17236/children-of-blood-and-bone/videos/22049">
  <img src="https://s.movieinsider.com/images/children-of-blood-and-bone/ytimg/HnWvg90lQt8/hqdefault_m1785252877.jpg"
       alt="Children of Blood and Bone trailer" class="trailer-img"></a>
  <h3 class="media-heading"><a href="#">Children of Blood and Bo...</a></h3>
  <p class="small text-muted">Official Trailer</p> </div>
<div class="trailers"> <a class="trailer-thumb" href="https://www.movieinsider.com/m25364/virginia-woolf-s-night-day/videos/22041">
  <img src="https://s.movieinsider.com/images/virginia-woolf-s-night-day/ytimg/RT8_5rCTev4/hqdefault_m1784913041.jpg"
       alt="Virginia Woolf&amp;#39;s Night &amp;amp; Day trailer" class="trailer-img"></a>
  <h3 class="media-heading"><a href="#">Virginia Woolf's Night &amp;...</a></h3>
  <p class="small text-muted">Official Trailer #2</p> </div>
"""

ALPHA_HTML = """
<a href="https://www.movieinsider.com/m17236/children-of-blood-and-bone">Children of Blood and Bone</a>
<a href="https://www.movieinsider.com/m25337/colony"><span>Colony</span></a>
<a href="https://www.movieinsider.com/m23586/clayface">Clayface</a>
<a href="https://www.movieinsider.com/m16743/coyote-vs-acme">Coyote vs. ACME</a>
<p>Page 1 of 61</p>
"""


class TestListingParser(unittest.TestCase):
    def test_all_cards_parsed(self):
        self.assertEqual(len(parse_listing_page(LISTING_HTML)), 5)

    def test_video_id_read_from_thumbnail(self):
        by_page = {r["video_page_id"]: r for r in parse_listing_page(LISTING_HTML)}
        self.assertEqual(by_page["22033"]["youtube_video_id"], "OGO4Mqvo3jI")
        self.assertEqual(by_page["22052"]["youtube_video_id"], "gH3fL9cMvuM")

    def test_movie_id_and_slug_parsed(self):
        record = next(r for r in parse_listing_page(LISTING_HTML) if r["video_page_id"] == "22033")
        self.assertEqual(record["movie_id"], "23586")
        self.assertEqual(record["slug"], "clayface")
        self.assertEqual(record["title"], "Clayface")

    def test_truncated_heading_never_wins(self):
        titles = [r["title"] for r in parse_listing_page(LISTING_HTML)]
        self.assertIn("Children of Blood and Bone", titles)
        self.assertFalse(any(t.endswith("...") for t in titles))

    def test_html_entities_in_alt_decoded(self):
        titles = [r["title"] for r in parse_listing_page(LISTING_HTML)]
        self.assertTrue(any("Night & Day" in t for t in titles), titles)

    def test_trailer_suffix_stripped(self):
        self.assertNotIn("Clayface trailer", [r["title"] for r in parse_listing_page(LISTING_HTML)])

    def test_regex_fallback_on_markup_change(self):
        mangled = LISTING_HTML.replace('class="trailers"', 'class="renamed"')
        records = parse_listing_page(mangled)
        self.assertGreaterEqual(len(records), 4)
        self.assertIn("OGO4Mqvo3jI", {r["youtube_video_id"] for r in records})

    def test_empty_page(self):
        self.assertEqual(parse_listing_page("<html><body>nothing</body></html>"), [])


class TestAlphaParser(unittest.TestCase):
    def test_movie_ids_and_titles(self):
        parsed = dict((title, movie_id) for movie_id, _slug, title in parse_alpha_page(ALPHA_HTML))
        self.assertEqual(parsed["Clayface"], "23586")
        self.assertEqual(parsed["Coyote vs. ACME"], "16743")

    def test_nested_markup_text_extracted(self):
        titles = [title for _mid, _slug, title in parse_alpha_page(ALPHA_HTML)]
        self.assertIn("Colony", titles)

    def test_duplicates_collapsed(self):
        doubled = ALPHA_HTML + ALPHA_HTML
        self.assertEqual(len(parse_alpha_page(doubled)), len(parse_alpha_page(ALPHA_HTML)))


class TestTitleMatching(unittest.TestCase):
    def test_year_and_punctuation_dropped(self):
        self.assertEqual(match_key("Dune: Part Two (2024)"), "dune 2")

    def test_leading_article_dropped(self):
        self.assertEqual(match_key("The Batman"), "batman")

    def test_apostrophes_do_not_split_words(self):
        self.assertEqual(
            match_key("Virginia Woolf's Night & Day"),
            match_key("Virginia Woolfs Night and Day"),
        )

    def test_roman_numerals_folded(self):
        self.assertEqual(match_key("Gladiator II"), match_key("Gladiator 2"))

    def test_identical_titles_score_100(self):
        self.assertEqual(title_score(match_key("Clayface"), match_key("Clayface")), 100)

    def test_punctuation_variant_scores_100(self):
        self.assertEqual(title_score(match_key("Coyote vs ACME"), match_key("Coyote vs. ACME")), 100)

    def test_sequel_numbers_are_not_conflated(self):
        """The bug this cap exists for: raw similarity rates these ~91."""
        score = title_score(match_key("Camp Rock 3"), match_key("Camp Rock 4"))
        self.assertLess(score, 90, "Camp Rock 3 must not auto-match Camp Rock 4")

    def test_unrelated_titles_score_low(self):
        self.assertLess(title_score(match_key("Clayface"), match_key("Coyote vs. ACME")), 40)


class TestChannelClassification(unittest.TestCase):
    def test_distributors(self):
        for name in ["Warner Bros. Pictures", "Netflix", "A24", "Universal Pictures",
                     "Sony Pictures Entertainment", "Searchlight Pictures", "Walt Disney Studios"]:
            self.assertEqual(classify_channel(name), "distributor", name)

    def test_regional_variant(self):
        self.assertEqual(classify_channel("Warner Bros. Pictures Australia"), "distributor")

    def test_aggregators(self):
        for name in ["JoBlo Movie Trailers", "Rotten Tomatoes Trailers", "Movieclips Trailers",
                     "KinoCheck International", "Movie Insider", "IGN"]:
            self.assertEqual(classify_channel(name), "aggregator", name)

    def test_unknown(self):
        self.assertEqual(classify_channel("Dave's Movie Reactions"), "unknown")


class TestChannelReferenceParsing(unittest.TestCase):
    CID = "UCjmJDM5pRKbUlVIzDYYWb6g"

    def test_bare_id(self):
        self.assertEqual(parse_channel_reference(self.CID), ("id", self.CID))

    def test_channel_url(self):
        self.assertEqual(parse_channel_reference(f"https://www.youtube.com/channel/{self.CID}"),
                         ("id", self.CID))

    def test_channel_url_with_trailing_segment(self):
        self.assertEqual(parse_channel_reference(f"https://www.youtube.com/channel/{self.CID}/videos"),
                         ("id", self.CID))

    def test_query_string_stripped(self):
        self.assertEqual(parse_channel_reference(f"https://www.youtube.com/channel/{self.CID}?view=0"),
                         ("id", self.CID))

    def test_handle_url(self):
        self.assertEqual(parse_channel_reference("https://www.youtube.com/@wbpictures"),
                         ("handle", "wbpictures"))

    def test_bare_handle(self):
        self.assertEqual(parse_channel_reference("@wbpictures"), ("handle", "wbpictures"))

    def test_legacy_c_and_user(self):
        self.assertEqual(parse_channel_reference("youtube.com/c/warnerbros"), ("legacy", "warnerbros"))
        self.assertEqual(parse_channel_reference("https://www.youtube.com/user/warnerbros"),
                         ("legacy", "warnerbros"))

    def test_empty(self):
        self.assertEqual(parse_channel_reference("   ")[0], "empty")


class TestInputParsing(unittest.TestCase):
    def test_pipe_separated_text(self):
        rows = _inputs_from_text("Clayface | @wbpictures | 2026\nCamp Rock 3\n\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].title, "Clayface")
        self.assertEqual(rows[0].channel, "@wbpictures")
        self.assertEqual(rows[0].year, "2026")

    def test_csv_with_aliased_headers(self):
        rows = _inputs_from_csv(
            b"Movie Title,Year,YouTube Channel URL\nClayface,2026,https://www.youtube.com/@wbpictures\n"
        )
        self.assertEqual(rows[0].title, "Clayface")
        self.assertEqual(rows[0].year, "2026")
        self.assertTrue(rows[0].channel.endswith("@wbpictures"))

    def test_semicolon_csv(self):
        rows = _inputs_from_csv(b"title;channel_id\nClayface;UCjmJDM5pRKbUlVIzDYYWb6g\n")
        self.assertEqual(rows[0].title, "Clayface")


class TestEndToEnd(unittest.TestCase):
    """Drives the real service with HTTP and the YouTube API stubbed out."""

    WB = "UCjmJDM5pRKbUlVIzDYYWb6g"
    NFX = "UCq0OueAsdxH6b8nyAspwViw"
    AGG = "UCaggregator0000000000AA"

    def setUp(self):
        service = MovieTrailerChannelService.__new__(MovieTrailerChannelService)
        service.http_client = None
        service.api_key = "test-key"
        service.fallback_service = None
        service.page_budget = 1
        service.alpha_page_budget = 0
        service.quota_units = 0

        service._sweep_trailer_listing = lambda budget, progress: parse_listing_page(LISTING_HTML)
        service._lookup_via_alpha = lambda item: None

        assignments = {
            "OGO4Mqvo3jI": (self.WB, "Warner Bros. Pictures"),
            "BBBBBBBBBBB": (self.WB, "Warner Bros. Pictures"),
            "gH3fL9cMvuM": (self.NFX, "Netflix"),
            "HnWvg90lQt8": (self.AGG, "JoBlo Movie Trailers"),
            "RT8_5rCTev4": ("", ""),  # deleted / private video
        }
        service._fetch_videos = lambda ids, key: {
            vid: {
                "channel_id": assignments.get(vid, ("", ""))[0],
                "channel_title": assignments.get(vid, ("", ""))[1],
                "video_title": f"{vid} trailer",
                "published_at": "2026-06-01",
            }
            for vid in ids
        }
        service._fetch_channels = lambda ids, key: {
            self.WB: {"title": "Warner Bros. Pictures", "handle": "@wbpictures",
                      "subscriber_count": "12400000", "country": "US"},
            self.NFX: {"title": "Netflix", "handle": "@Netflix",
                       "subscriber_count": "29800000", "country": "US"},
            self.AGG: {"title": "JoBlo Movie Trailers", "handle": "@joblo",
                       "subscriber_count": "2100000", "country": "CA"},
        }
        service._resolve_handle = lambda handle, key, cache: (
            self.WB if handle.lower() == "wbpictures" else ""
        )
        self.service = service

    def _statuses(self, text):
        snapshot = self.service.validate_bulk(bulk_text=text, use_fallback=False)
        return {r["Input Title"]: r["Status"] for r in snapshot["sections"][0]["rows"]}

    def test_snapshot_shape_matches_hub_contract(self):
        snapshot = self.service.validate_bulk(bulk_text="Clayface", use_fallback=False)
        for key in ("tracker_type", "title", "created_at", "source_url", "summary", "sections"):
            self.assertIn(key, snapshot)
        section = snapshot["sections"][0]
        for key in ("key", "title", "columns", "rows", "row_count", "supports_google"):
            self.assertIn(key, section)
        self.assertEqual(section["row_count"], len(section["rows"]))
        self.assertEqual(set(section["rows"][0]), set(section["columns"]))

    def test_correct_channel_matches(self):
        self.assertEqual(self._statuses(f"Clayface | {self.WB}")["Clayface"], MATCH)

    def test_handle_resolves_to_same_channel(self):
        """The false-mismatch guard: a handle and an ID for one channel must agree."""
        self.assertEqual(self._statuses("Clayface | @wbpictures")["Clayface"], MATCH)

    def test_channel_url_form_matches(self):
        row = f"Clayface | https://www.youtube.com/channel/{self.WB}/videos"
        self.assertEqual(self._statuses(row)["Clayface"], MATCH)

    def test_wrong_channel_is_mismatch(self):
        snapshot = self.service.validate_bulk(bulk_text=f"Clayface | {self.NFX}", use_fallback=False)
        row = snapshot["sections"][0]["rows"][0]
        self.assertEqual(row["Status"], MISMATCH)
        self.assertEqual(row["Official Channel ID"], self.WB)
        self.assertEqual(row["Official Channel (Distributor)"], "Warner Bros. Pictures")

    def test_missing_channel_still_reports_the_answer(self):
        snapshot = self.service.validate_bulk(bulk_text="Clayface", use_fallback=False)
        row = snapshot["sections"][0]["rows"][0]
        self.assertEqual(row["Status"], NO_CHANNEL_PROVIDED)
        self.assertEqual(row["Official Channel ID"], self.WB)

    def test_aggregator_upload_flagged(self):
        row = f"Children of Blood and Bone | {self.AGG}"
        self.assertEqual(self._statuses(row)["Children of Blood and Bone"], NOT_OFFICIAL_UPLOAD)

    def test_unavailable_video_reported(self):
        statuses = self._statuses(f"Virginia Woolf's Night & Day | {self.WB}")
        self.assertEqual(list(statuses.values())[0], NO_CHANNEL_FOUND)

    def test_unknown_title_not_found(self):
        self.assertEqual(self._statuses("A Film Nobody Ever Made")["A Film Nobody Ever Made"],
                         TITLE_NOT_FOUND)

    def test_official_trailer_preferred_over_teaser(self):
        snapshot = self.service.validate_bulk(bulk_text="Clayface", use_fallback=False)
        row = snapshot["sections"][0]["rows"][0]
        self.assertEqual(row["Trailer"], "Official Trailer")
        self.assertIn("OGO4Mqvo3jI", row["Trailer URL"])

    def test_sequel_confusion_does_not_produce_a_false_match(self):
        statuses = self._statuses(f"Camp Rock 4 | {self.WB}")
        self.assertEqual(statuses["Camp Rock 4"], TITLE_NOT_FOUND)

    def test_quota_accounting_is_cheap(self):
        self.service.validate_bulk(bulk_text="Clayface\nBelow (series)", use_fallback=False)
        self.assertLessEqual(self.service.quota_units, 2)

    def test_missing_api_key_rejected(self):
        self.service.api_key = ""
        with self.assertRaises(ValueError):
            self.service.validate_bulk(bulk_text="Clayface", use_fallback=False)

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            self.service.validate_bulk(bulk_text="   ", use_fallback=False)

    def test_summary_mentions_quota_saving(self):
        snapshot = self.service.validate_bulk(bulk_text="Clayface\nBelow (series)", use_fallback=False)
        self.assertIn("quota", snapshot["summary"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
