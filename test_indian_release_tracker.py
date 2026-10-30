"""Tests for the Indian Regional Release Tracker tool.

    python -m unittest test_indian_release_tracker -v

No network: the fixtures mirror Wikipedia's year-list tables (rowspan month cells written as
stacked letters, a colspan "Opening" header, a box-office table that must be skipped), a
YouTube results page, and a CBFC search result.
"""
from __future__ import annotations

from datetime import date
import json
import unittest

from app.services.indian_release_tracker import (
    parse_listing_date, parse_listing_page, channel_matches, pick_official_channels, published_year, imdb_release,
    COVERAGE_COLUMNS, TITLE_COLUMNS, IndianReleaseTrackerService, Title, filter_window,
    merge_titles, parse_cbfc_results, parse_film_year_page, parse_tv_list_page,
    parse_youtube_results, pick_top3, read_title_list, resolve_window,
)

TODAY = date(2026, 10, 29)

FILM_PAGE = """
<table class="wikitable"><tr><th>Rank</th><th>Title</th><th>Worldwide gross</th></tr>
<tr><td>1</td><td>Peddi</td><td>₹500 crore</td></tr></table>
<table class="wikitable">
<tr><th colspan="2">Opening</th><th>Title</th><th>Director</th><th>Cast</th><th>Studio (production house)</th><th>Ref.</th></tr>
<tr><td rowspan="2">J<br>A<br>N</td><td rowspan="2">9</td><td>The RajaSaab</td><td>Maruthi</td><td>Prabhas<br>Nidhhi Agerwal<br>Malavika</td><td>People Media Factory</td><td><sup>[1]</sup></td></tr>
<tr><td>Silent Screams</td><td>Arvind Menon</td><td>Shruti Haasan</td><td>Sun NXT</td><td></td></tr>
<tr><td>N<br>O<br>V</td><td>20</td><td>Aakasamlo Oka Tara</td><td>Pavan Sadineni</td><td>Dulquer Salmaan</td><td>Geetha Arts</td><td></td></tr>
</table>
<table class="wikitable"><tr><th>Title</th><th>Director</th></tr><tr><td>Raaka</td><td>Atlee</td></tr></table>
"""

TV_PAGE = """
<table class="wikitable"><tr><th>Title</th><th>Genre</th><th>Premiere</th><th>Language</th></tr>
<tr><td>Kohrra Season 2</td><td>Crime drama</td><td>11 February 2026</td><td>Hindi</td></tr>
<tr><td>Old Show</td><td>Drama</td><td>3 March 2021</td><td>Tamil</td></tr></table>
"""


def yt_page(videos):
    contents = [{"videoRenderer": {
        "videoId": vid, "title": {"runs": [{"text": title}]},
        "ownerText": {"runs": [{"text": ch, "navigationEndpoint": {"browseEndpoint": {"browseId": "UC" + ch, "canonicalBaseUrl": "/@" + ch}}}]},
        "ownerBadges": [{"metadataBadgeRenderer": {"style": "BADGE_STYLE_TYPE_VERIFIED"}}] if verified else [],
    }} for vid, title, ch, verified in videos]
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {"contents": [{"itemSectionRenderer": {"contents": contents}}]}}}}}
    return f"<script>var ytInitialData = {json.dumps(data)};</script>"


class WindowTests(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(resolve_window("1", TODAY), (date(2024, 1, 1), date(2027, 10, 29), True))
        self.assertEqual(resolve_window("2", TODAY), (date(2026, 10, 1), date(2027, 1, 29), False))
        self.assertEqual(resolve_window("3", TODAY), (date(2026, 10, 1), date(2027, 4, 29), False))
        self.assertEqual(resolve_window("4", TODAY), (None, None, True))
        self.assertEqual(resolve_window("5", TODAY, "2027-03-31", "2027-01-01"), (date(2027, 1, 1), date(2027, 3, 31), False))

    def test_list_titles_always_kept(self):
        old = Title("Deep Jele Jai", sort_date="1959-05-01", from_list=True)
        out_of_window = Title("Old film", sort_date="2020-01-01")
        tba = Title("Announced")
        kept = filter_window([old, out_of_window, tba], date(2026, 10, 1), date(2027, 1, 29), False, False)
        self.assertEqual([t.title for t in kept], ["Deep Jele Jai"])
        self.assertEqual(len(filter_window([old, tba], date(2024, 1, 1), date(2027, 1, 1), True, False)), 2)


class WikipediaTests(unittest.TestCase):
    def test_year_page(self):
        rows = parse_film_year_page(FILM_PAGE, "Telugu", 2026, TODAY)
        by = {t.title: t for t in rows}
        self.assertNotIn("Peddi", by)                       # box-office table skipped
        self.assertEqual(by["The RajaSaab"].sort_date, "2026-01-09")
        self.assertEqual(by["Silent Screams"].sort_date, "2026-01-09")   # rowspan date carried down
        self.assertEqual(by["The RajaSaab"].status, "Released")
        self.assertEqual(by["Aakasamlo Oka Tara"].status, "Confirmed")
        self.assertEqual(by["The RajaSaab"].cast, "Prabhas, Nidhhi Agerwal, Malavika")
        self.assertEqual(by["Silent Screams"].network, "Sun NXT")
        self.assertEqual(by["Raaka"].status, "Date TBA")

    def test_tv_page(self):
        rows = parse_tv_list_page(TV_PAGE, "Prime Video", TODAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].category, rows[0].network, rows[0].sort_date), ("TV Show", "Prime Video", "2026-02-11"))


class YouTubeTests(unittest.TestCase):
    def test_deep_jele_jai_roles(self):
        trailer = parse_youtube_results(yt_page([
            ("a1", "Deep Jele Jai | Official Trailer | Uttam Kumar", "KLiKKjokhontokhon", True),
            ("a2", "Deep Jele Jai trailer reaction", "angelmoviescenes", False)]))
        movie = parse_youtube_results(yt_page([
            ("b1", "Deep Jele Jai | Bengali Full Movie | Suchitra Sen", "BengaliMoviesAngelDigital", True)]))
        songs = parse_youtube_results(yt_page([
            ("c1", "Ei Raat Tomar Amar | Deep Jele Jai | Video Song", "angelsongs", True)]))
        link, chans = pick_top3("Deep Jele Jai", [trailer, movie, songs])
        self.assertEqual(link, "https://www.youtube.com/watch?v=a1")
        self.assertEqual(chans, ["https://www.youtube.com/@KLiKKjokhontokhon",
                                 "https://www.youtube.com/@BengaliMoviesAngelDigital",
                                 "https://www.youtube.com/@angelsongs"])
        row = Title("Deep Jele Jai", youtube_channels=chans).row()
        self.assertEqual(row["Top 3 YouTube Channels"].split("\n")[0],
                         "https://www.youtube.com/@KLiKKjokhontokhon|Deep Jele Jai")

    def test_blocked_page(self):
        self.assertIsNone(parse_youtube_results("<html>consent.youtube.com</html>"))


class ListAndMergeTests(unittest.TestCase):
    def test_csv_and_paste(self):
        content = b"IMDb ID,Title,Content Type,Language,Release Date,Pay1/Library\ntt0321968,Deep Jele Jai,Movie,Bengali,1959,Library\n"
        rows = read_title_list(content, "list.csv", "Kantara: Chapter 1 | Kannada | 2025")
        self.assertEqual([r["title"] for r in rows], ["Deep Jele Jai", "Kantara: Chapter 1"])
        self.assertEqual(rows[0]["window"], "Library")

    def test_merge_fills_gaps(self):
        a = Title("Kara", language="Tamil", sort_date="2026-04-30", director="Vignesh Raja")
        b = Title("Kara", language="Tamil", sort_date="2026-04-30", imdb_id="tt1", network="Netflix", from_list=True)
        out = merge_titles([[a], [b]])
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].imdb_id, out[0].network, out[0].from_list), ("tt1", "Netflix", True))


class CbfcTests(unittest.TestCase):
    def test_parse(self):
        html = ("<div>Film Name</div><div>KANTARA CHAPTER 1</div><div>Movie Language</div><div>Kannada</div>"
                "<div>Movie Category</div><div>UA 13+</div><div>Certificate No</div><div>DIL/2/123/2025</div>"
                "<div>Certified Length</div><div>168.00 mins</div>")
        recs = parse_cbfc_results(html)
        self.assertEqual(recs[0]["certno"], "DIL/2/123/2025")
        self.assertEqual(recs[0]["category"], "UA 13+")


IMDB_TV = {
    "id": "tt29625288", "titleText": {"text": "Akka"}, "titleType": {"id": "tvSeries"},
    "releaseDate": {"day": 30, "month": 10, "year": 2026}, "genres": {"genres": [{"text": "Crime"}, {"text": "Drama"}]},
    "spokenLanguages": {"spokenLanguages": [{"text": "Hindi"}]}, "countriesOfOrigin": {"countries": [{"id": "IN"}]},
    "directors": {"edges": [{"node": {"name": {"nameText": {"text": "Dharmaraj Shetty"}}}}]},
    "cast": {"edges": [{"node": {"name": {"nameText": {"text": "Keerthy Suresh"}}}}]},
    "producers": {"edges": [{"node": {"name": {"nameText": {"text": "Aditya Chopra"}}}}]},
    "prod": {"edges": [{"node": {"company": {"companyText": {"text": "YRF Entertainment"}}}}]},
    "dist": {"edges": [{"node": {"company": {"companyText": {"text": "Netflix"}}}},
                       {"node": {"company": {"companyText": {"text": "Some Theatre Chain"}}}}]},
    "runtime": None, "certificate": None,
}


class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200):
        self.payload, self.text, self.status_code = payload, text, status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def get(self, url, params=None, headers=None, timeout=None):
        if "wikipedia" in url:
            page = params["page"]
            if page == "List of Telugu films of 2026":
                return FakeResponse({"parse": {"text": {"*": FILM_PAGE}}})
            return FakeResponse({"error": {}})
        if "wikidata" in url:
            return FakeResponse({"results": {"bindings": [{
                "imdb": {"value": "tt0321968"}, "item": {"value": "http://www.wikidata.org/entity/Q5250225"},
                "co": {"value": "IN"}, "lang": {"value": "Bengali"}, "studio": {"value": "Angel Digital"}}]}})
        return FakeResponse({}, "")

    def post(self, *a, **k):
        variables = (k.get("json") or {}).get("variables") or {}
        if variables.get("types") == ["tvSeries", "tvMiniSeries"]:
            return FakeResponse({"data": {"advancedTitleSearch": {"total": 1, "pageInfo": {"hasNextPage": False}, "edges": [
                {"node": {"title": IMDB_TV}}]}}})
        if variables.get("types") == ["movie", "tvMovie"]:
            return FakeResponse({"data": {"advancedTitleSearch": {"total": 0, "pageInfo": {"hasNextPage": False}, "edges": []}}})
        return FakeResponse({"data": {}})


class FakeHttp:
    timeout_seconds = 5
    session = FakeSession()

    def get_text(self, url):
        if "youtube.com/results" in url:
            return yt_page([("x1", "Deep Jele Jai Official Trailer", "KLiKKjokhontokhon", True),
                            ("x2", "Deep Jele Jai Bengali Full Movie", "BengaliMoviesAngelDigital", True),
                            ("x3", "Deep Jele Jai Video Song", "angelsongs", True)])
        raise RuntimeError("blocked")


class ImdbAdvancedSearchTests(unittest.TestCase):
    def test_title_mapping(self):
        t = IndianReleaseTrackerService._imdb_title(IMDB_TV, "TV Show", {"hindi"}, TODAY)
        self.assertEqual((t.category, t.network, t.studio, t.produced_by), ("TV Show", "Netflix", "YRF Entertainment", "Aditya Chopra"))
        self.assertEqual((t.sort_date, t.status, t.type_platform), ("2026-10-30", "Tentative", "TV series – OTT (Netflix)"))
        self.assertIsNone(IndianReleaseTrackerService._imdb_title(IMDB_TV, "TV Show", {"tamil"}, TODAY))   # unticked language


class TmdbTests(unittest.TestCase):
    def test_apply_fills_only_gaps(self):
        svc = IndianReleaseTrackerService(FakeHttp(), tmdb_read_access_token="x")
        svc._tmdb = lambda path, params=None: {
            "external_ids": {"imdb_id": "tt999"}, "genres": [{"name": "Drama"}],
            "credits": {"crew": [{"job": "Director", "name": "A Director"}], "cast": [{"name": "Lead"}]},
            "production_companies": [{"name": "Studio X"}], "networks": [],
            "watch/providers": {"results": {"IN": {"flatrate": [{"provider_name": "Prime Video"}]}}},
            "original_language": "ta", "release_date": "2026-12-04"}
        t = Title("Kara", director="Keep Me")
        svc._apply_tmdb(t, "movie", 1)
        self.assertEqual((t.imdb_id, t.genre, t.director, t.studio, t.network, t.language, t.sort_date),
                         ("tt999", "Drama", "Keep Me", "Studio X", "Prime Video", "Tamil", "2026-12-04"))


class IngestExportTests(unittest.TestCase):
    def test_profiles_and_sections(self):
        from app.services.ingest_export import build_ingest_csv, ingest_category_for_tracker, profile_for
        from app.services.exporting import ExportService
        self.assertEqual(ingest_category_for_tracker("indian_release_tracker_movies"), "Movies")
        self.assertEqual(ingest_category_for_tracker("indian_release_tracker_tv"), "TV Shows")
        row = Title("Akka", category="TV Show", imdb_id="tt29625288", release_date="30 Oct 2026",
                    network="Netflix, JioHotstar", language="Hindi", genre="Crime / Drama").row()
        csv_text = build_ingest_csv([row], profile_for("indian-release-tv")).decode()
        self.assertIn("https://www.imdb.com/title/tt29625288", csv_text)
        self.assertIn("2026-10-30", csv_text)
        self.assertIn("Netflix", csv_text)
        self.assertNotIn("JioHotstar", csv_text)
        snap = IndianReleaseTrackerService(FakeHttp()).run(mode="2", languages=["Hindi", "Telugu"],
                                                           youtube_scope="none", check_cbfc=False, today=TODAY)
        snap = ExportService(60).register_snapshot_exports(snap)
        self.assertEqual([s["ingest_category"] for s in snap["sections"]], ["Movies", "TV Shows"])


SACNILK_PAGE = """
<html><body><nav>Home Upcoming Movies</nav><div>Release Status</div>
<div class="card"><span>RELEASING TODAY</span><span>Oct 30, 2026</span><h3>Nakhrewaalii</h3><span>Hindi</span><span>Comedy, Drama</span></div>
<div class="card"><span>COMING SOON</span><span>Nov 6, 2026</span><h3>Scene</h3><span>Tamil</span><span>Action</span></div>
<table><tr><th>Movie</th><th>Release Date</th><th>Platform</th></tr>
<tr><td>Kohrra Season 2</td><td>14/11/2026</td><td>Netflix</td></tr></table>
</body></html>
"""


class ListingAndJustWatchTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(parse_listing_date("Oct 30, 2026"), date(2026, 10, 30))
        self.assertEqual(parse_listing_date("6th November 2026"), date(2026, 11, 6))
        self.assertEqual(parse_listing_date("14/11/2026"), date(2026, 11, 14))

    def test_cards_and_tables(self):
        rows = {r.title: r for r in parse_listing_page(SACNILK_PAGE, "Sacnilk", "", "", TODAY)}
        self.assertEqual(set(rows), {"Nakhrewaalii", "Scene", "Kohrra Season 2"})
        self.assertEqual((rows["Scene"].language, rows["Scene"].sort_date), ("Tamil", "2026-11-06"))
        self.assertEqual((rows["Kohrra Season 2"].category, rows["Kohrra Season 2"].network), ("TV Show", "Netflix"))

    def test_justwatch_mapping(self):
        edge = {"newOffer": None, "node": {"__typename": "Season", "content": {"title": "Season 2"}, "show": {"content": {
            "title": "Kohrra", "productionCountries": ["IN"], "externalIds": {"imdbId": "tt123"},
            "genres": [{"shortName": "crm"}], "credits": [{"name": "Sudip Sharma"}],
            "upcomingReleases": [{"releaseDate": "2026-11-14", "package": {"clearName": "Netflix"}}]}}}}
        t = IndianReleaseTrackerService._jw_title(edge, TODAY, TODAY)
        self.assertEqual((t.title, t.category, t.network, t.sort_date, t.imdb_id, t.genre),
                         ("Kohrra – Season 2", "TV Show", "Netflix", "2026-11-14", "tt123", "Crime"))
        edge["node"]["show"]["content"]["productionCountries"] = ["US"]
        self.assertIsNone(IndianReleaseTrackerService._jw_title(edge, TODAY, TODAY))   # not Indian


# Real data captured 30 Oct 2026: IMDb GraphQL (credits) and YouTube results for "Mysaa 2026 official trailer"
# and "Akka 2026 Netflix official trailer" (channel name, handle, upload age, verified badge).
MYSAA_YT = [("Rta_M2zkpWk", "MYSAA Teaser (Hindi) | Rashmika | Rawindra Pulle | Jakes Bejoy", "T-Series", "/@tseries", "1mo ago", True),
            ("9kkl0O-CT3M", "MYSAA Trailer (2026) | Rashmika Mandanna | Concept Trailer", "T-TRAILER F.M.C", "/@T-Trailerfanmadeconcept", "1 min ago", False),
            ("SORZsU6Ytp4", "MYSAA Teaser (Telugu) | Rashmika | Rawindra Pulle", "T-Series Telugu", "/@TseriesTelugu", "1mo ago", True),
            ("iGyHorKWoos", "#MYSAA - Official Trailer | Rashmika Mandanna | Jakes Bejoy", "Bollywood Studios", "/@Bollywood_Studios", "1mo ago", False),
            ("tWeEGhoWoBQ", "MYSAA Pre Teaser | Rashmika | Rawindra Pulle | Jakes Bejoy | UnFormula", "UnFormula Films", "/@UnFormulaFilms", "1mo ago", False),
            ("1KIMI1Zhj0k", "Mysaa Trailer (2026) The Warrior Rises | Rashmika Mandanna", "S-Series Studio", "/@studiomovies-l6q", "1mo ago", False),
            ("DILtUVRVXQU", "Ranabaali Official Trailer (Hindi) | Vijay Deverakonda | Rashmika", "RKD Studios", "/@rkddigital", "3w ago", True)]
AKKA_YT = [("f6ga0g_-OVk", "Akka | Official Trailer | Keerthy Suresh, Radhika Apte | Netflix India", "Netflix India", "/@NetflixIndiaOfficial", "8d ago", True),
           ("NwJhml1_D54", "Akka | Official Teaser | Keerthy Suresh, Radhika Apte | Netflix India", "YRF", "/@yrf", "11d ago", True),
           ("5mSTPoUvefo", "Akka Official Trailer | Keerthy Suresh, Radhika Apte | Netflix India", "Movie Time Zone", "/@MovieTimeZone", "7d ago", False),
           ("aDlZ2MXxP5c", "Akka - Teaser Trailer | Netflix | Keerthy Suresh | Radhika Apte", "MTK Movies", "/@MTKMovies0", "2w ago", False)]
IMDB_MYSAA = {"id": "tt37454145", "titleText": {"text": "Mysaa"}, "titleType": {"id": "movie"},
              "releaseDate": {"day": 27, "month": 11, "year": 2026},
              "inDate": {"edges": [{"node": {"day": None, "month": 10, "year": 2026}}, {"node": {"day": 27, "month": 11, "year": 2026}}]},
              "spokenLanguages": {"spokenLanguages": [{"text": "Telugu"}, {"text": "Hindi"}, {"text": "Tamil"}]},
              "countriesOfOrigin": {"countries": [{"id": "IN"}]}, "genres": {"genres": [{"text": "Action"}]},
              "directors": {"edges": [{"node": {"name": {"nameText": {"text": "Rawindra Pulle"}}}}]},
              "cast": {"edges": [{"node": {"name": {"nameText": {"text": n}}}} for n in ["Rashmika Mandanna", "Samuthirakani", "Easwari Rao"]]},
              "producers": {"edges": [{"node": {"name": {"nameText": {"text": n}}}} for n in ["Samardhi Bujji Babu", "Ajay Saipureddy"]]},
              "prod": {"edges": [{"node": {"company": {"companyText": {"text": n}}}} for n in ["Haashtag Media", "UnFormula Films"]]},
              "dist": {"edges": []}}


def yt(rows):
    return [{"id": i, "t": t, "name": n, "ch": "https://www.youtube.com" + h, "pub": p, "ver": v} for i, t, n, h, p, v in rows]


class OfficialChannelTests(unittest.TestCase):
    def test_matching(self):
        self.assertTrue(channel_matches("T-Series Telugu", "T-Series"))
        self.assertTrue(channel_matches("YRF", "Yash Raj Films"))
        self.assertTrue(channel_matches("Netflix India", "Netflix India"))
        self.assertTrue(channel_matches("UnFormula Films", "UnFormula Films"))
        self.assertFalse(channel_matches("S-Series Studio", "T-Series"))
        self.assertFalse(channel_matches("Bollywood Studios", "UnFormula Films"))
        self.assertEqual(published_year("1mo ago", TODAY), 2026)
        self.assertEqual(published_year("3 years ago", TODAY), 2023)

    def test_mysaa_only_credited_channels(self):
        t = IndianReleaseTrackerService._imdb_title(IMDB_MYSAA, "Movie", set(), TODAY)
        self.assertEqual((t.sort_date, t.language, t.studio, t.produced_by),
                         ("2026-11-27", "Telugu", "Haashtag Media, UnFormula Films", "Samardhi Bujji Babu, Ajay Saipureddy"))
        trailer, chans = pick_official_channels(t, [yt(MYSAA_YT)], TODAY)
        self.assertEqual(chans, [("https://www.youtube.com/@UnFormulaFilms", "studio"),
                                 ("https://www.youtube.com/@tseries", "music"),
                                 ("https://www.youtube.com/@TseriesTelugu", "music")])
        self.assertEqual(trailer, "https://www.youtube.com/watch?v=tWeEGhoWoBQ")

    def test_akka_series(self):
        t = IndianReleaseTrackerService._imdb_title(IMDB_TV, "Movie", set(), TODAY)
        self.assertEqual(t.category, "TV Show")                         # IMDb title type wins
        t.credits["studio"] = ["YRF Entertainment", "Yash Raj Films"]
        t.credits["distributor"] = ["Netflix India"]
        _, chans = pick_official_channels(t, [yt(AKKA_YT)], TODAY)
        self.assertEqual(chans, [("https://www.youtube.com/@yrf", "studio"),
                                 ("https://www.youtube.com/@NetflixIndiaOfficial", "distributor")])

    def test_old_upload_rejected_for_new_title(self):
        t = IndianReleaseTrackerService._imdb_title(IMDB_MYSAA, "Movie", set(), TODAY)
        old = [("x1", "MYSAA Official Trailer", "UnFormula Films", "/@UnFormulaFilms", "6 years ago", False)]
        self.assertEqual(pick_official_channels(t, [yt(old)], TODAY)[1], [])


class ValidationTests(unittest.TestCase):
    def test_justwatch_library_addition_keeps_original_year(self):
        edge = {"newOffer": {"package": {"clearName": "Netflix"}}, "node": {"__typename": "Movie", "objectType": "MOVIE",
                "content": {"title": "Old Film", "fullPath": "/in/movie/old-film", "originalReleaseDate": "2021-03-05",
                            "productionCountries": ["IN"], "externalIds": {"imdbId": "tt1"}}}}
        t = IndianReleaseTrackerService._jw_title(edge, date(2026, 10, 20), TODAY)
        self.assertEqual((t.sort_date, t.status), ("2021-03-05", "Released"))
        self.assertIn("on OTT 20 Oct 2026", t.type_platform)
        self.assertEqual(t.source_urls, ["https://www.justwatch.com/in/movie/old-film"])
        self.assertEqual(filter_window([t], date(2026, 10, 1), date(2027, 1, 29), False, False), [])

    def test_imdb_corrects_and_rejects(self):
        class H(FakeHttp):
            pass
        svc = IndianReleaseTrackerService(H())
        svc.http.session = type("S", (), {"post": lambda self, *a, **k: FakeResponse({"data": {"titles": [IMDB_MYSAA, IMDB_TV]}})})()
        same = Title("Mysaa", language="Telugu", sort_date="2026-10-01", imdb_id="tt37454145")
        clash = Title("Akka", language="Tamil", sort_date="2026-10-30", imdb_id="tt29625288")     # IMDb says Hindi
        svc._imdb_validate([same, clash], TODAY, lambda p, m: None)
        self.assertEqual((same.sort_date, same.validated, same.produced_by), ("2026-11-27", "IMDb", "Samardhi Bujji Babu, Ajay Saipureddy"))
        self.assertIn("https://www.imdb.com/title/tt37454145/", same.source_urls)
        self.assertEqual(clash.imdb_id, "")                                                        # wrong match removed

    def test_india_release_date_preferred(self):
        self.assertEqual(imdb_release(IMDB_MYSAA)[1], "27 Nov 2026")


# Real TMDB response for Mysaa (captured 30 Oct 2026, trimmed).
TMDB_MYSAA = {"id": 1504630, "title": "Mysaa", "original_language": "te", "release_date": "2026-11-27", "runtime": 157,
              "origin_country": ["IN"], "genres": [{"name": "Action"}, {"name": "Drama"}, {"name": "Thriller"}],
              "production_companies": [{"name": "UnFormula Films"}],
              "release_dates": {"results": [{"iso_3166_1": "IN", "release_dates": [{"release_date": "2026-11-27T00:00:00.000Z", "type": 3}]}]},
              "credits": {"cast": [{"name": n} for n in ["Rashmika Mandanna", "Easwari Rao", "Rahul Ravindran"]],
                          "crew": [{"job": "Director", "name": "Rawindra Pulle"}, {"job": "Producer", "name": "Anil Sayyapureddy"},
                                   {"job": "Producer", "name": "Ajay"}]},
              "external_ids": {"imdb_id": "tt37454145"}, "watch/providers": {"results": {}}}


class TmdbFallbackTests(unittest.TestCase):
    def test_tmdb_title_mapping(self):
        t = IndianReleaseTrackerService._tmdb_title(TMDB_MYSAA, "movie", TODAY)
        self.assertEqual((t.title, t.category, t.language, t.sort_date, t.studio, t.produced_by, t.imdb_id, t.validated),
                         ("Mysaa", "Movie", "Telugu", "2026-11-27", "UnFormula Films", "Anil Sayyapureddy, Ajay", "tt37454145", "TMDB"))
        self.assertEqual(t.source_urls, ["https://www.themoviedb.org/movie/1504630", "https://www.imdb.com/title/tt37454145/"])

    def test_imdb_403_switches_to_tmdb(self):
        class Blocked(FakeSession):
            def post(self, url, *a, **k):
                if "imdb" in url:
                    return FakeResponse({}, "Forbidden", status_code=403)
                return FakeResponse({"data": {}})

        class H(FakeHttp):
            session = Blocked()

        svc = IndianReleaseTrackerService(H(), tmdb_api_key="k")
        junk = {"id": 9, "title": "Fan Edit", "original_language": "te", "release_date": "2026-11-20", "credits": {}}

        def fake_tmdb(path, params=None):
            if path == "/discover/movie":
                return {"total_pages": 1, "results": [{"id": 1504630, "original_language": "te"}, {"id": 9, "original_language": "te"}]}
            if path == "/movie/1504630":
                return TMDB_MYSAA
            if path == "/movie/9":
                return junk
            return {}
        svc._tmdb = fake_tmdb
        snap = svc.run(mode="2", languages=["Telugu"], include_tv=False, youtube_scope="none", check_cbfc=False, today=TODAY)
        rows = snap["sections"][0]["rows"]
        self.assertIn("Mysaa", [r["Title Name"] for r in rows])
        self.assertNotIn("Fan Edit", [r["Title Name"] for r in rows])            # no IMDb ID / company -> dropped
        mysaa = next(r for r in rows if r["Title Name"] == "Mysaa")
        self.assertEqual((mysaa["Release Date"], mysaa["Language"], mysaa["Studio"]), ("27 Nov 2026", "Telugu", "UnFormula Films"))
        self.assertIn("HTTP 403", snap["summary"])


class DiagnosticsTests(unittest.TestCase):
    def test_reports_every_source_without_raising(self):
        checks = IndianReleaseTrackerService(FakeHttp()).diagnose()
        self.assertEqual(len(checks), 13)                                # 12 data sources + Sacnilk
        by = {c["source"]: c["status"] for c in checks}
        self.assertEqual(by["TMDB API"], "SKIPPED")                   # no key in tests
        self.assertEqual(by["YouTube results page"], "OK")
        self.assertEqual(by["CBFC website"], "BLOCKED / FAILED")


class EndToEndTests(unittest.TestCase):
    def test_mode2_and_mode4(self):
        svc = IndianReleaseTrackerService(FakeHttp())
        snap = svc.run(mode="2", languages=["Telugu"], include_tv=False, youtube_scope="none", today=TODAY)
        self.assertEqual([s["key"] for s in snap["sections"]], ["movies", "tv_shows"])
        titles = [r["Title Name"] for r in snap["sections"][0]["rows"]]
        self.assertEqual(titles, ["Aakasamlo Oka Tara"])           # only 01 Oct 2026 -> 29 Jan 2027
        self.assertEqual(snap["sections"][0]["columns"], TITLE_COLUMNS)
        self.assertIn("CBFC", snap["summary"])                      # CBFC blocked -> noted, not fatal

        snap = IndianReleaseTrackerService(FakeHttp()).run(mode="2", languages=["Hindi"], youtube_scope="none",
                                                           check_cbfc=False, today=TODAY)
        tv = snap["sections"][1]["rows"]
        self.assertEqual([(r["Title Name"], r["Network"], r["IMDb ID"]) for r in tv], [("Akka", "Netflix", "tt29625288")])

        snap = IndianReleaseTrackerService(FakeHttp()).run(
            mode="4", bulk_text="Deep Jele Jai | Bengali | 1959 | tt0321968", check_cbfc=False, today=TODAY)
        row = snap["sections"][0]["rows"][0]
        self.assertEqual((row["IMDb ID"], row["Studio"], row["Origin"]), ("tt0321968", "Angel Digital", "Indian"))
        self.assertTrue(row["Top 3 YouTube Channels"].startswith("https://www.youtube.com/@KLiKKjokhontokhon|Deep Jele Jai"))
        self.assertEqual(snap["sections"][1]["columns"], COVERAGE_COLUMNS)

    def test_mode4_needs_list(self):
        with self.assertRaises(ValueError):
            IndianReleaseTrackerService(FakeHttp()).run(mode="4", today=TODAY)


if __name__ == "__main__":
    unittest.main()
