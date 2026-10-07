"""Daily - TV Season and Episode Data Review: IMDb code + network fixes.

TMDB is mocked, so these run offline.
Run:  python -m pytest test_tv_season_matching.py -q
"""
from pathlib import Path

from app.services.http_client import HttpClient
from app.services.imdb import IMDbEnrichmentService
from app.services.metacritic import _find_networks


def _svc(tmp_path, search, details):
    svc = IMDbEnrichmentService(Path(tmp_path), 7, HttpClient("t", 5), tmdb_api_key="k")

    def fake_get(url, params):
        if url.endswith("/search/tv"):
            return {"results": search(params)}
        tmdb_id = int(url.rsplit("/", 1)[1])
        return details[tmdb_id]
    svc._tmdb_get_json = fake_get
    return svc


REAL = {  # long-running series returning for Season 5 in 2026
    "id": 1, "name": "The Bear", "first_air_date": "2022-06-23",
    "external_ids": {"imdb_id": "tt14452776"}, "networks": [{"name": "FX"}],
    "number_of_seasons": 5,
    "seasons": [{"season_number": n, "air_date": f"{2021 + n}-06-01", "episode_count": 10}
                for n in range(1, 6)],
}
DECOY = {  # a different, new 2026 show the year-only search returned
    "id": 2, "name": "The Bears", "first_air_date": "2026-06-25",
    "external_ids": {"imdb_id": "tt39999999"}, "networks": [{"name": "Netflix"}],
    "number_of_seasons": 1,
    "seasons": [{"season_number": 1, "air_date": "2026-06-25", "episode_count": 6}],
}


def _row(title="The Bear", date="2026-06-25", network="FX; Hulu"):
    return {"Title Name": title, "Release Date": date, "Availability / Network": network}


def test_returning_series_not_replaced_by_newer_same_name_show(tmp_path):
    # year-filtered search only sees the 2026 decoy; the old code took it
    search = lambda p: [{"id": 2}] if p.get("first_air_date_year") else [{"id": 1}, {"id": 2}]
    svc = _svc(tmp_path, search, {1: REAL, 2: DECOY})
    got = svc._tmdb_tv_season_lookup(_row(), preferred_season=5)
    assert got["ttcode"] == "tt14452776"
    assert got["latest_season_number"] == 5


def test_season_unknown_still_prefers_network_match(tmp_path):
    search = lambda p: [{"id": 2}] if p.get("first_air_date_year") else [{"id": 1}, {"id": 2}]
    svc = _svc(tmp_path, search, {1: REAL, 2: DECOY})
    got = svc._tmdb_tv_lookup(_row())
    assert got["ttcode"] == "tt14452776"


def test_weak_title_match_left_blank(tmp_path):
    other = dict(DECOY, id=3, name="Bear Grylls: Survival Island")
    svc = _svc(tmp_path, lambda p: [{"id": 3}], {3: other})
    assert svc._tmdb_tv_season_lookup(_row(), preferred_season=5) is None


def test_bad_ttcode_rejected(tmp_path):
    broken = dict(REAL, external_ids={"imdb_id": "nm0000001"})
    svc = _svc(tmp_path, lambda p: [{"id": 1}], {1: broken})
    assert svc._tmdb_tv_season_lookup(_row(), preferred_season=5)["ttcode"] == ""


def test_network_extraction():
    assert _find_networks("Apple TV+") == "Apple TV+"
    assert _find_networks("Discovery+") == "Discovery+"
    assert _find_networks("FOX") == "Fox"
    assert _find_networks("Reality, Food: Netflix") == "Netflix"
    assert _find_networks("Docuseries about the history of jazz: Netflix") == "Netflix"
    assert _find_networks("Drama: Starz (also on Hulu)") == "Starz; Hulu"
    assert _find_networks("Drama: HBO") == "HBO"
    assert _find_networks("Drama: HBO Max") == "HBO Max"
    assert _find_networks("Drama: AMC+") == "AMC+"
    assert _find_networks("Anime: Crunchyroll") == "Crunchyroll"
    assert _find_networks("Comedy: USA Network") == "USA Network"


CHICAGO_FIRE = {  # 15 seasons, 299 episodes overall; Season 15 has 5 so far
    "id": 3, "name": "Chicago Fire", "first_air_date": "2012-10-10",
    "external_ids": {"imdb_id": "tt2261391"}, "networks": [{"name": "NBC"}],
    "number_of_seasons": 15, "number_of_episodes": 299,
    "seasons": [{"season_number": 0, "air_date": "2013-01-01", "episode_count": 12}]
    + [{"season_number": n, "air_date": f"{2011 + n}-10-01", "episode_count": 22} for n in range(1, 15)]
    + [{"season_number": 15, "air_date": "2026-09-30", "episode_count": 5},
       {"season_number": 16, "air_date": None, "episode_count": 0}],
}


def test_imdb_enriched_uses_latest_season_episode_count(tmp_path):
    svc = _svc(tmp_path, lambda p: [{"id": 3}], {3: CHICAGO_FIRE})
    got = svc._tmdb_tv_lookup(_row("Chicago Fire", "2026-09-30", "NBC"))
    assert got["ttcode"] == "tt2261391"
    assert got["total_episodes"] == 5  # not 299
    row = svc._output_row(_row("Chicago Fire", "2026-09-30", "NBC"), got["ttcode"],
                          got["total_seasons"], got["total_episodes"], got["note"])
    assert row["Latest Season Episodes"] == 5
