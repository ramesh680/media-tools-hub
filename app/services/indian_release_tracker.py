"""Indian Regional Release Tracker - web version of the `indian-regional-release-tracker` skill.

Builds one table of Indian movies and TV shows across regional languages for a chosen window,
or enriches only an uploaded title list, with the same columns as the skill's workbook:
IMDb ID, release date/status, genre, language, producer, Studio, Network, director, cast,
trailer link, Top YouTube channels (``<channel URL>|<Title>``: credited trailer channels + official studio channels), CBFC certificate fields,
origin, Pay 1/Library window and source.

Run modes (same as the skill's menu):
  1  2024-01-01 -> today + 1 year (announced "Date TBA" titles kept)
  2  this month + the next 2 (e.g. October -> 1 Oct to 31 Dec)
  3  this month + the next 5 (e.g. October -> 1 Oct to 31 Mar)
  4  only the uploaded / pasted title list (any year, any country)
  5  custom: own date range, languages, TV on/off, notes

Server-side sources (all public, no login):
  * Wikipedia "List of <Language> films of <Year>" tables (MediaWiki API, CC BY-SA)
  * Wikipedia OTT-originals lists for TV shows (Netflix India, Prime Video India, JioHotstar)
  * IMDb Advanced Title Search (IMDb's public GraphQL, the API behind imdb.com/search/title):
    every title with India as a country of origin released in the window - movies + TV movies,
    and TV series + mini-series - with director, cast, producers, production companies (Studio),
    streaming/TV distributors (Network), runtime and certificate. Best effort: IMDb may refuse
    datacentre traffic; the run then notes it and continues
  * TMDB (TMDB_API_KEY / TMDB_READ_ACCESS_TOKEN) and OMDb (OMDB_API_KEY) - the hub's existing keys -
    fill missing IMDb IDs, genres, directors, cast, production companies, TV networks and the
    India streaming provider, so rows carry what the BDR ingest template needs
  * Wikidata SPARQL (CC0) to resolve uploaded titles <-> IMDb IDs, studio, network
  * JustWatch India (apis.justwatch.com GraphQL): upcoming and newly added titles on every Indian
    OTT service JustWatch tracks - Netflix, Prime Video, Amazon Video, JioHotstar, ZEE5, Sony LIV,
    Sun NXT, aha, hoichoi, ManoramaMAX, MX Player, Chaupal, Hungama, ShemarooMe, BookMyShow Stream,
    EPIC ON - kept when India is a production country; gives the OTT platform and date
  * IMDb lists (e.g. ls548141776 "Most Anticipated Indian Movies") via the list page + IMDb GraphQL
  * Listing pages (best effort, generic table / "date -> title -> language" card parser): Sacnilk
    upcoming, BoxOfficeIndia24 OTT upcoming, OTTDrop, OTTweek, OTT Movie Releases, Binged, Filmibeat,
    91mobiles, District, What's on Netflix (India 2026), About Amazon India (Prime Video upcoming)
  * YouTube search results page (no key) for the trailer + Top YouTube channels; YouTube Data API v3
    (YOUTUBE_API_KEY or the per-run key) as a fallback when the results page is blocked
  * CBFC official website "Search Film" - best effort, read live on every run (no PDFs)

No network access happens at import time; every fetch goes through the shared HttpClient.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from typing import Any, Callable
import csv
import json
import re
import time
from urllib.parse import quote, quote_plus

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta

from app.models import utc_now_iso

ProgressCallback = Callable[[int, str], None]

# Render sets RENDER_GIT_COMMIT on every deploy: shown in the run summary and self-test so it is
# obvious which version of this file is live.
import os
BUILD_ID = (os.getenv("RENDER_GIT_COMMIT") or "local")[:7]

TRACKER_TYPE = "indian_release_tracker"

# Every Indian state / UT language with a film or TV industry, incl. regional languages and dialects.
# (state / region in the comment; IMDb and TMDB use these names)
INDIAN_LANGUAGES = [
    "Hindi", "Tamil", "Telugu", "Malayalam", "Kannada", "Marathi", "Bengali", "Gujarati", "Punjabi", "Odia",
    "Assamese", "Bhojpuri",                    # Bihar, Jharkhand, eastern UP
    "Tulu",                                    # coastal Karnataka
    "Urdu", "Konkani",                         # Goa / coastal Karnataka & Maharashtra
    "Rajasthani", "Marwari",                   # Rajasthan
    "Haryanvi",                                # Haryana
    "Garhwali", "Kumaoni",                     # Uttarakhand (Uttarakhandi)
    "Pahari",                                  # Himachal Pradesh
    "Kashmiri", "Dogri", "Ladakhi",            # Jammu & Kashmir, Ladakh
    "Chhattisgarhi",                           # Chhattisgarh
    "Maithili", "Magahi", "Angika",            # Bihar
    "Awadhi", "Braj",                          # Uttar Pradesh
    "Bundeli", "Malvi",                        # Madhya Pradesh
    "Santali", "Nagpuri", "Kurukh",            # Jharkhand
    "Sambalpuri",                              # western Odisha
    "Manipuri", "Mizo", "Khasi", "Garo", "Kokborok", "Bodo", "Nagamese",   # North-East
    "Nepali", "Lepcha",                        # Sikkim, Darjeeling
    "Sindhi", "Sanskrit", "Saurashtra", "Badaga", "Kodava", "Beary", "Banjara", "Lambadi",
]
# Names IMDb / TMDB / users write differently -> one of the names above
LANGUAGE_ALIASES = {"uttarakhandi": "Garhwali", "meitei": "Manipuri", "meiteilon": "Manipuri", "oriya": "Odia",
                    "bangla": "Bengali", "assamiya": "Assamese", "lushai": "Mizo", "tripuri": "Kokborok",
                    "nagpuri (sadri)": "Nagpuri", "sadri": "Nagpuri", "kumauni": "Kumaoni", "chhattisgarhi (cg)": "Chhattisgarhi"}
INDIAN_LOWER = {x.lower() for x in INDIAN_LANGUAGES} | set(LANGUAGE_ALIASES)

TITLE_COLUMNS = [
    "Title Name", "IMDb ID", "Category", "Release Date", "Release Status", "Genre", "Language",
    "Produce by", "Studio", "Network", "Director", "Cast", "Teaser / Trailer on YouTube",
    "Top YouTube Channels", "YouTube Channel Roles", "Type / Platform", "Certificate Type", "Certificate No.", "Duration",
    "CBFC Office", "Origin", "OTT Window (Pay 1 / Library)", "Source",
]
COVERAGE_COLUMNS = ["Title", "IMDb ID", "Language", "Year", "Origin", "Window", "Wikidata",
                    "Sources found", "Notes"]

STATUSES = ("Released", "Certified (CBFC)", "Confirmed", "Tentative", "Date TBA")
NETWORK_RE = re.compile(
    r"netflix|amazon|prime video|hotstar|jiocinema|disney|zee5|zee |sonyliv|sony |sun (tv|nxt)|\baha\b|"
    r"hoichoi|etv|star |colors|manorama|chaupal|planet marathi|mx player|apple tv|lionsgate play|"
    r"discovery|asianet|gemini|surya tv|udaya|waves ott", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
IMDB_GRAPHQL = "https://caching.graphql.imdb.com/"
YT_RESULTS = "https://www.youtube.com/results?search_query="
TMDB_API = "https://api.themoviedb.org/3"
OMDB_API = "https://www.omdbapi.com/"
YT_API_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_API_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
CBFC_HOME = "https://www.cbfcindia.gov.in/"
CBFC_SEARCH_CANDIDATES = [
    "https://www.cbfcindia.gov.in/cbfcAdmin/search-film.php",
    "https://www.cbfcindia.gov.in/cbfcAdmin/search-result.php",
]
JUSTWATCH_API = "https://apis.justwatch.com/graphql"
JW_PACKAGES = {"nfx": "Netflix", "prv": "Amazon Prime Video", "amz": "Amazon Video", "jhs": "JioHotstar",
               "zee": "ZEE5", "snl": "Sony LIV", "snx": "Sun NXT", "aha": "aha", "hoc": "hoichoi",
               "mrx": "ManoramaMAX", "mxp": "MX Player", "mni": "Amazon MX Player", "acl": "Chaupal",
               "hgp": "Hungama Play", "sme": "ShemarooMe", "bms": "BookMyShow Stream", "epc": "EPIC ON",
               "upa": "Ultra Play"}
JW_GENRES = {"act": "Action", "ani": "Animation", "cmy": "Comedy", "crm": "Crime", "doc": "Documentary",
             "drm": "Drama", "fml": "Family", "fnt": "Fantasy", "hst": "History", "hrr": "Horror",
             "msc": "Music", "rma": "Romance", "scf": "Sci-Fi", "spt": "Sport", "trl": "Thriller",
             "war": "War", "wsn": "Western", "rly": "Reality", "eur": "Made in Europe"}
JW_CONTENT = ("title fullPath originalReleaseDate productionCountries externalIds{imdbId} genres{shortName} "
              "credits(role:DIRECTOR){name} upcomingReleases(releaseTypes:[DIGITAL]){releaseDate package{clearName}}")
JW_QUERY = (
    "query($d:Date!,$pt:NewPageType!,$after:String,$packages:[String!]){newTitles(country:IN,date:$d,first:100,"
    "after:$after,filter:{packages:$packages},pageType:$pt,priceDrops:false){totalCount pageInfo{hasNextPage endCursor} "
    "edges{newOffer(platform:WEB){package{clearName}} node{__typename ... on MovieOrShow{objectType "
    'content(country:IN,language:"en"){' + JW_CONTENT + '}} ... on Season{content(country:IN,language:"en"){title} '
    'show{content(country:IN,language:"en"){' + JW_CONTENT + "}}}}}}}")
JW_NEW_DAYS = 45                      # "new on OTT" days scanned back from today
JW_MAX_PAGES = 10
IMDB_SUGGEST = "https://v2.sg.media-imdb.com/suggestion/{first}/{query}.json"
# The headers imdb.com itself sends with GraphQL calls; IMDb rejects bare server requests (HTTP 403).
IMDB_HEADERS = {"content-type": "application/json", "accept": "application/graphql+json, application/json",
                "origin": "https://www.imdb.com", "referer": "https://www.imdb.com/",
                "x-imdb-client-name": "imdb-web-next-localized", "x-imdb-user-country": "IN",
                "x-imdb-user-language": "en-IN",
                "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/130.0.0.0 Safari/537.36")}
TMDB_DISCOVER_MAX = 2500             # titles per run when TMDB replaces IMDb discovery
TMDB_DISCOVER_PAGES = 100            # 20 per page
IMDB_RESOLVE_MAX = 4000
IMDB_LISTS = [("ls548141776", "IMDb list – Most Anticipated Indian Movies")]
# Listing pages parsed with the generic table / card parser (default language, default platform).
# Only listing pages whose layout has been verified on real data create rows; unstructured
# news/aggregator pages are linked in the UI for manual cross-checks but never set titles or dates.
LISTING_SITES = [
    ("Sacnilk – upcoming movies", "https://www.sacnilk.com/upcoming-movies", "", ""),
]
CROSS_CHECK_SITES = [
    "https://www.boxofficeindia24.com/ott/upcoming", "https://www.ottdrop.com/", "https://ottweek.com/",
    "https://www.ottmoviereleases.com/", "https://www.binged.com/", "https://www.filmibeat.com/tamil/upcoming-movies.html",
    "https://www.91mobiles.com/entertainment/upcoming-tamil-movies", "https://www.district.in/movies/upcoming-movies",
    "https://in.bookmyshow.com/", "https://www.whats-on-netflix.com/coming-soon/new-indian-netflix-original-series-and-movies-coming-to-netflix-in-2026",
    "https://www.aboutamazon.in/news/entertainment/new-upcoming-shows-movies-prime-video-india",
]
LISTING_MAX_ROWS = 400
DATE_RX = re.compile(
    r"\b(?:(\d{1,2})(?:st|nd|rd|th)?[ \-/.]+([A-Za-z]{3,9})[ ,\-/.]+(\d{4})|"
    r"([A-Za-z]{3,9})[ .]+(\d{1,2})(?:st|nd|rd|th)?,?[ ]+(\d{4})|(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}))\b")
LISTING_STOP = re.compile(r"^(releasing|released|coming soon|upcoming|today|tomorrow|read more|watch|trailer|"
                          r"book|advertise|login|subscribe|home|menu|search|share|more|news|all |genre|language|"
                          r"release status|ott|box office|follow us|in this video)", re.I)
PLATFORM_RX = re.compile(r"netflix|prime video|amazon|jiohotstar|hotstar|zee5|sony ?liv|sun ?nxt|\baha\b|hoichoi|"
                         r"chaupal|planet marathi|manorama ?max|mx player|etv win|lionsgate play|discovery\+", re.I)


def parse_listing_date(text: str) -> date | None:
    m = DATE_RX.search(text or "")
    if not m:
        return None
    try:
        if m.group(1):
            mon = MONTHS.get(m.group(2)[:3].lower())
            return date(int(m.group(3)), mon, int(m.group(1))) if mon else None
        if m.group(4):
            mon = MONTHS.get(m.group(4)[:3].lower())
            return date(int(m.group(6)), mon, int(m.group(5))) if mon else None
        return date(int(m.group(9)), int(m.group(8)), int(m.group(7)))      # dd/mm/yyyy (Indian order)
    except (TypeError, ValueError):
        return None


def parse_listing_page(html: str, site: str, default_lang: str, default_platform: str, today: date,
                       page_url: str = "") -> list[Title]:
    """Generic: (1) tables with a title + date column, (2) card text 'date -> title -> language/platform'."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    found: dict[str, Title] = {}

    def add(title: str, d: date | None, lang: str = "", platform: str = "", category: str = "Movie") -> None:
        title = clean(re.sub(r"\s*\((?:19|20)\d\d\)\s*$", "", title))
        if not title or len(title) > 80 or len(title) < 2 or LISTING_STOP.match(title) or DATE_RX.search(title):
            return
        if PLATFORM_RX.fullmatch(title) or title.lower() in INDIAN_LOWER | {"english"}:
            return                                        # a platform or language cell, not a title
        if d is None or not (2023 <= d.year <= today.year + 3) or norm(title) in found:
            return
        if re.search(r"\bseason\b|\bs\d{1,2}\b|series", title, re.I):
            category = "TV Show"
        net = platform or default_platform
        t = Title(title=title, category=category, language=lang or default_lang or "Unspecified", network=net,
                  release_date=d.strftime("%d %b %Y"), sort_date=d.isoformat(),
                  status=status_for(d.isoformat(), today, "Tentative"),
                  type_platform=f"OTT ({net})" if net else "Film – Theatrical", source=site,
                  origin="International" if (lang or default_lang) == "English" else "Indian")
        t.add_url(page_url)
        found[norm(title)] = t

    for table in soup.select("table"):
        grid = table_grid(table)
        if len(grid) < 2:
            continue
        head = [c.lower() for c in grid[0]]
        c_t = next((i for i, h in enumerate(head) if re.search(r"movie|title|film|show|series|name", h)), -1)
        c_d = next((i for i, h in enumerate(head) if re.search(r"date|release", h)), -1)
        c_l = next((i for i, h in enumerate(head) if "language" in h), -1)
        c_p = next((i for i, h in enumerate(head) if re.search(r"platform|ott|network|streaming", h)), -1)
        if c_t < 0 or c_d < 0:
            continue
        for row in grid[1:]:
            g = lambda i: row[i] if 0 <= i < len(row) else ""  # noqa: E731
            add(g(c_t), parse_listing_date(g(c_d)), g(c_l).title() if g(c_l) else "",
                (PLATFORM_RX.search(g(c_p)) or [""])[0] if g(c_p) else "")

    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if len(found) >= LISTING_MAX_ROWS:
            break
        d = parse_listing_date(line)
        if not d or len(line) > 40:
            continue
        j = i + 1
        while j < len(lines) and j <= i + 3 and (LISTING_STOP.match(lines[j]) or parse_listing_date(lines[j])):
            j += 1
        if j >= len(lines):
            continue
        tail = " | ".join(lines[j + 1:j + 4])
        lang = next((l for l in INDIAN_LANGUAGES + ["English"] if re.search(rf"\b{l}\b", tail)), "")
        plat = PLATFORM_RX.search(tail)
        add(lines[j], d, lang, plat.group(0) if plat else "")
    return list(found.values())


TV_LISTS = [
    ("List of Netflix India original programming", "Netflix"),
    ("List of Amazon India originals", "Prime Video"),
    ("List of Disney+ Hotstar original programming", "JioHotstar"),
]

# Free-tier friendly ceilings (a full mode-1 run otherwise means ~12k YouTube requests).
YOUTUBE_WORKERS = 4
YOUTUBE_MAX_TITLES = 600
YOUTUBE_API_MAX_TITLES = 25          # 3 searches x 100 quota units each -> ~7.5k units
CBFC_MAX_TITLES = 150
IMDB_SEARCH_PAGE = 250
IMDB_SEARCH_MAX_PAGES = 24            # 6,000 titles per kind is plenty for India in any window
TMDB_MAX_TITLES = 900
TMDB_WORKERS = 6
OMDB_MAX_TITLES = 150                 # free OMDb key: 1,000 requests a day

LANG_CODES = {"Hindi": "hi", "Tamil": "ta", "Telugu": "te", "Malayalam": "ml", "Kannada": "kn",
              "Marathi": "mr", "Bengali": "bn", "Gujarati": "gu", "Punjabi": "pa", "Odia": "or",
              "Bhojpuri": "bho", "Assamese": "as", "Tulu": "tcy", "Urdu": "ur", "English": "en",
              "Konkani": "kok", "Rajasthani": "raj", "Marwari": "mwr", "Haryanvi": "bgc", "Garhwali": "gbm",
              "Kumaoni": "kfy", "Kashmiri": "ks", "Dogri": "doi", "Chhattisgarhi": "hne", "Maithili": "mai",
              "Magahi": "mag", "Awadhi": "awa", "Braj": "bra", "Bundeli": "bns", "Santali": "sat", "Manipuri": "mni",
              "Mizo": "lus", "Khasi": "kha", "Garo": "grt", "Kokborok": "trp", "Bodo": "brx", "Nepali": "ne",
              "Sindhi": "sd", "Sanskrit": "sa", "Angika": "anp", "Nagpuri": "sck", "Kurukh": "kru", "Ladakhi": "lbj",
              "Lepcha": "lep", "Malvi": "mup", "Badaga": "bfq", "Kodava": "kfa", "Beary": "bcy",
              "Saurashtra": "saz", "Sambalpuri": "spv", "Pahari": "him", "Banjara": "lmn"}
CODE_LANGS = {v: k for k, v in LANG_CODES.items()}
IMDB_MOVIE_TYPES = ["movie", "tvMovie"]               # how IMDb title types are classified
IMDB_TV_TYPES = ["tvSeries", "tvMiniSeries"]
# The discovery rule, as an IMDb Advanced Search URL. START_DATE / END_DATE come from the run's window
# (options 1-3 compute them, option 5 "Custom" takes any From / To), so the timeframe is always adjustable.
# Title types can be changed with the env var INDIAN_TRACKER_IMDB_TITLE_TYPES (e.g. "feature,tv_series,tv_miniseries").
IMDB_SEARCH_URL_TEMPLATE = ("https://www.imdb.com/search/title/?title_type={TITLE_TYPES}"
                            "&release_date={START_DATE},{END_DATE}&countries=IN")
IMDB_RULE_TITLE_TYPES = os.getenv("INDIAN_TRACKER_IMDB_TITLE_TYPES", "feature,tv_series")
URL_TYPE_TO_GRAPHQL = {"feature": "movie", "tv_movie": "tvMovie", "tv_series": "tvSeries",
                       "tv_miniseries": "tvMiniSeries", "tv_special": "tvSpecial", "short": "short"}


def imdb_rule_url(start: date, end: date, title_types: str = "") -> str:
    """The IMDb Advanced Search page for a window, e.g. feature,tv_series released START..END in India."""
    return IMDB_SEARCH_URL_TEMPLATE.format(TITLE_TYPES=title_types or IMDB_RULE_TITLE_TYPES,
                                           START_DATE=start.isoformat(), END_DATE=end.isoformat())


def imdb_rule_kinds(title_types: str = "") -> list[tuple[str, list[str]]]:
    """('Movie', [...]), ('TV Show', [...]) GraphQL title types from the rule's title_type list."""
    ids = [URL_TYPE_TO_GRAPHQL[x.strip()] for x in (title_types or IMDB_RULE_TITLE_TYPES).split(",")
           if x.strip() in URL_TYPE_TO_GRAPHQL]
    movie = [x for x in ids if x not in IMDB_TV_TYPES]
    tv = [x for x in ids if x in IMDB_TV_TYPES]
    return [k for k in (("Movie", movie), ("TV Show", tv)) if k[1]]
IMDB_FIELDS = (
    "id titleText{text} titleType{id} releaseDate{day month year} genres{genres{text}} "
    "spokenLanguages{spokenLanguages{text}} countriesOfOrigin{countries{id}} "
    'inDate:releaseDates(first:5,filter:{countries:["IN"]}){edges{node{day month year}}} '
    'directors:credits(first:3,filter:{categories:["director"]}){edges{node{name{nameText{text}}}}} '
    'cast:credits(first:5,filter:{categories:["actor","actress"]}){edges{node{name{nameText{text}}}}} '
    'producers:credits(first:5,filter:{categories:["producer"]}){edges{node{name{nameText{text}}}}} '
    'prod:companyCredits(first:5,filter:{categories:["production"]}){edges{node{company{companyText{text}}}}} '
    'dist:companyCredits(first:15,filter:{categories:["distribution"]}){edges{node{company{companyText{text}}}}} '
    "runtime{seconds} certificate{rating}")
IMDB_SEARCH_QUERY = (
    "query($types:[String!]!,$start:Date,$end:Date,$after:String,$first:Int!){"
    "advancedTitleSearch(first:$first,after:$after,constraints:{"
    "titleTypeConstraint:{anyTitleTypeIds:$types},"
    "releaseDateConstraint:{releaseDateRange:{start:$start,end:$end}},"
    'originCountryConstraint:{allCountries:["IN"]}},'
    "sort:{sortBy:RELEASE_DATE,sortOrder:ASC}){total pageInfo{hasNextPage endCursor} "
    "edges{node{title{" + IMDB_FIELDS + "}}}}}")
IMDB_TITLES_QUERY = "query($ids:[ID!]!){titles(ids:$ids){" + IMDB_FIELDS + "}}"


# --------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------
def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def resolve_window(mode: str, today: date, custom_start: str = "", custom_end: str = "") -> tuple[date | None, date | None, bool]:
    """Return (from, to, keep_tba) for a run mode."""
    first = today.replace(day=1)
    if mode == "1":
        return date(2024, 1, 1), today + relativedelta(years=1), True
    if mode == "2":       # 3 calendar months incl. this one: Oct -> 1 Oct to 31 Dec
        return first, first + relativedelta(months=3) - timedelta(days=1), False
    if mode == "3":       # 6 calendar months incl. this one: Oct -> 1 Oct to 31 Mar
        return first, first + relativedelta(months=6) - timedelta(days=1), False
    if mode == "4":
        return None, None, True
    start = _parse_iso(custom_start) or date(2024, 1, 1)
    end = _parse_iso(custom_end) or today + relativedelta(years=1)
    if end < start:
        start, end = end, start
    return start, end, False


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def status_for(sort_date: str | None, today: date, future: str = "Confirmed") -> str:
    if not sort_date:
        return "Date TBA"
    return "Released" if sort_date <= today.isoformat() else future


@dataclass
class Title:
    title: str
    category: str = "Movie"
    release_date: str = "TBA (announced)"
    sort_date: str | None = None
    status: str = "Date TBA"
    genre: str = ""
    language: str = "Unspecified"
    produced_by: str = ""
    studio: str = ""
    network: str = ""
    director: str = ""
    cast: str = ""
    trailer_url: str = ""
    youtube_channels: list[str] = field(default_factory=list)
    youtube_roles: list[str] = field(default_factory=list)
    type_platform: str = "Film – Theatrical"
    certificate_type: str = ""
    certificate_no: str = ""
    duration: str = ""
    cbfc_office: str = ""
    origin: str = "Indian"
    window: str = ""
    imdb_id: str = ""
    source: str = ""
    from_list: bool = False
    source_urls: list[str] = field(default_factory=list)
    credits: dict[str, list[str]] = field(default_factory=dict)   # role -> names (for YouTube matching)
    validated: str = ""                                            # "IMDb" / "TMDB" when checked
    season_row: bool = False                                       # a new season of an older series

    def add_url(self, url: str) -> None:
        if url and url not in self.source_urls:
            self.source_urls.append(url)

    def row(self) -> dict[str, str]:
        trailer = self.trailer_url or YT_RESULTS + quote_plus(f"{self.title} official trailer")
        chans = "\n".join(f"{c}|{self.title}" for c in self.youtube_channels)
        net = self.network
        if not net:
            m = re.search(r"(?:OTT|TV)\s*\(([^)]+)\)", self.type_platform or "")
            net = m.group(1) if m else ""
        values = {
            "Title Name": self.title, "IMDb ID": self.imdb_id, "Category": self.category,
            "Release Date": self.release_date, "Release Status": self.status, "Genre": self.genre,
            "Language": self.language, "Produce by": self.produced_by,
            "Studio": self.studio or self.produced_by, "Network": net, "Director": self.director,
            "Cast": self.cast, "Teaser / Trailer on YouTube": trailer, "Top YouTube Channels": chans,
            "YouTube Channel Roles": "\n".join(ROLE_LABELS.get(r, r) for r in self.youtube_roles),
            "Type / Platform": self.type_platform, "Certificate Type": self.certificate_type,
            "Certificate No.": self.certificate_no, "Duration": self.duration,
            "CBFC Office": self.cbfc_office, "Origin": self.origin,
            "OTT Window (Pay 1 / Library)": self.window,
            "Source": "\n".join(self.source_urls) or self.source,
        }
        return {k: (v if v else "—") for k, v in values.items()}


ROLE_LABELS = {"studio_channel": "Studio (official channel)", "studio": "Studio", "producer": "Producer", "distributor": "Distributor", "network": "Network / OTT",
               "music": "Music company", "director": "Director", "actor": "Actor/Actress"}

MERGE_FIELDS = ("imdb_id", "genre", "produced_by", "studio", "network", "director", "cast",
                "trailer_url", "window", "certificate_type", "certificate_no", "duration", "cbfc_office")


def imdb_release(t: dict) -> tuple[date | None, str]:
    """Earliest India release date with a day, else IMDb's main release date."""
    best = None
    for e in ((t.get("inDate") or {}).get("edges") or []):
        n = e.get("node") or {}
        if n.get("year") and n.get("month") and n.get("day"):
            try:
                d = date(n["year"], n["month"], n["day"])
            except ValueError:
                continue
            best = d if best is None or d < best else best
    if best:
        return best, best.strftime("%d %b %Y")
    rd = t.get("releaseDate") or {}
    if not rd.get("year"):
        return None, "TBA (announced)"
    try:
        d = date(rd["year"], rd.get("month") or 1, rd.get("day") or 1)
    except ValueError:
        return None, "TBA (announced)"
    return d, d.strftime("%d %b %Y") if rd.get("day") else (d.strftime("%b %Y") if rd.get("month") else str(rd["year"]))


# Official YouTube channels must belong to someone credited on the title. Music companies and OTT
# networks are recognised from these curated names (Indian labels / platforms); nothing else counts.
MUSIC_LABELS = ["T-Series", "Saregama", "Sony Music", "Zee Music", "Tips", "Aditya Music", "Lahari", "Think Music",
                "Mango Music", "Muzik247", "Anand Audio", "Junglee Music", "Times Music", "Speed Records",
                "White Hill", "Saga Music", "Ultra", "Rajshri", "Shemaroo", "Venus", "Divo", "Panorama Music",
                "Everest", "Angel", "SVF", "Amara Muzik", "Tarang", "Sun Music", "Sony Music South", "Star Music",
                "Eros Now Music", "Yash Raj Films", "YRF", "Goldmines", "Pen Movies", "Krishna Digital",
                "Madhura Audio", "Mass Audios", "Satyam Audios", "PVR Music", "Raj Music", "Desi Melodies",
                "Speed Records", "Geet MP3", "Jass Records", "Humble Music", "Saavn", "Sun TV", "Manorama Music",
                "Millennium Audios", "Goodwill Entertainments", "Satyam Videos", "Bhavana Studios"]
OTT_NETWORKS = ["Netflix India", "Netflix", "Prime Video India", "Prime Video", "Amazon MX Player", "JioHotstar",
                "Disney+ Hotstar", "ZEE5", "Sony LIV", "SonyLIV", "Sun NXT", "aha", "hoichoi", "KLiKK", "Chaupal",
                "ManoramaMAX", "Planet Marathi", "ETV Win", "MX Player", "Lionsgate Play", "Discovery+",
                "ShemarooMe", "Hungama", "EPIC ON", "JioCinema", "Colors TV", "Star Plus", "Sony TV", "Zee TV",
                "Sun TV", "Star Maa", "Zee Telugu", "Zee Tamil", "Vijay Television", "Asianet", "Colors Kannada"]
_STRIP = ("officialchannel", "official", "entertainment", "entertainments", "productions", "production", "pictures",
          "films", "film", "studios", "studio", "media", "music", "musics", "movies", "movie", "creations", "company",
          "india", "pvtltd", "ltd", "pvt", "digital", "songs", "videos", "video", "audios", "audio", "records",
          "telugu", "tamil", "hindi", "malayalam", "kannada", "marathi", "bengali", "gujarati", "punjabi", "bhojpuri",
          "odia", "south", "vevo", "tv", "channel")
_GENERIC = {"", "bollywood", "tollywood", "kollywood", "mollywood", "sandalwood", "cinema", "trailer", "trailers",
            "new", "latest", "movieclips", "filmy", "world", "hd", "series", "sseries", "daily", "news"}


def channel_core(name: str) -> str:
    c = norm(name)
    changed = True
    while changed:
        changed = False
        for suf in _STRIP:
            if c.endswith(suf) and len(c) > len(suf):
                c, changed = c[: -len(suf)], True
            if c.startswith(suf) and len(c) > len(suf) + 2:
                c, changed = c[len(suf):], True
    return c


def channel_matches(channel: str, credit: str) -> bool:
    """Same entity? 'T-Series Telugu' ~ 'T-Series', 'YRF' ~ 'Yash Raj Films', 'Netflix India' ~ 'Netflix'."""
    c, k = channel_core(channel), channel_core(credit)
    if c in _GENERIC or k in _GENERIC or len(c) < 2 or len(k) < 2:
        return False
    if c == k:
        return True
    if len(c) >= 5 and len(k) >= 5 and (k.startswith(c) or c.startswith(k)):
        return True
    words = [w for w in re.findall(r"[A-Za-z0-9]+", credit) if w.lower() not in {"pvt", "ltd", "the", "and", "of"}]
    acronym = "".join(w[0] for w in words).lower()
    return len(acronym) >= 3 and c == acronym


def published_year(text: str, today: date) -> int | None:
    """'1mo ago' / '3 years ago' / '2 weeks ago' -> approximate upload year."""
    m = re.search(r"(\d+)\s*(y|year|mo|month|w|week|d|day|h|hour|min|minute|s|second)", (text or "").lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = n * {"y": 365, "year": 365, "mo": 30, "month": 30, "w": 7, "week": 7}.get(unit, 0)
    return (today - timedelta(days=days)).year


YT_ROLE_ORDER = ("studio", "producer", "distributor", "network", "music", "director", "actor")


def pick_official_channels(t: "Title", results: list[list[dict[str, Any]]], today: date) -> tuple[str, list[tuple[str, str]]]:
    """Official trailers only: the video's title must contain the film's title, be a trailer/teaser/glimpse,
    be uploaded around the release year, and come from a channel of someone credited on the title
    (studio, producer, distributor, network, music company, director, actor). Returns (trailer, [(url, role)])."""
    base = re.sub(r"\s*\(.*?\)\s*", " ", t.title)
    base = re.sub(r"\s*[-–]?\s*season\s*\d+.*$", "", base, flags=re.I).strip()
    key = norm(base)
    year = int(t.sort_date[:4]) if t.sort_date else None
    credits = {r: [n for n in t.credits.get(r, []) if n] for r in YT_ROLE_ORDER}
    credits["network"] = list(dict.fromkeys(credits.get("network", []) + [n for n in re.split(r",\s*", t.network or "") if n]))
    credits["studio"] = list(dict.fromkeys(credits.get("studio", []) + [n for n in re.split(r",\s*", t.studio or "") if n]))
    chosen: dict[str, tuple[int, str, str]] = {}
    trailer = ""
    for videos in results:
        for v in videos:
            if not key or key not in norm(v["t"]) or not re.search(r"trailer|teaser|glimpse|promo|first look", v["t"], re.I):
                continue
            if year:
                py = published_year(v.get("pub", ""), today)
                if py and not (year - 2 <= py <= year + 1) and year >= today.year - 3:
                    continue                          # uploaded in a different era -> a different title
            role = next((r for r in YT_ROLE_ORDER for n in credits.get(r, []) if channel_matches(v["name"], n)), "")
            if not role and v.get("ver"):
                if any(channel_matches(v["name"], n) for n in MUSIC_LABELS):
                    role = "music"
                elif any(channel_matches(v["name"], n) for n in OTT_NETWORKS):
                    role = "network"
            if not role:
                continue
            rank = YT_ROLE_ORDER.index(role)
            if v["ch"] not in chosen or rank < chosen[v["ch"]][0]:
                chosen[v["ch"]] = (rank, role, v["id"])
    ordered = sorted(chosen.items(), key=lambda kv: kv[1][0])[:3]
    if ordered:
        trailer = "https://www.youtube.com/watch?v=" + ordered[0][1][2]
    return trailer, [(url, role) for url, (_, role, _) in ordered]


def parse_youtube_channel_results(html: str) -> list[dict[str, Any]] | None:
    """Channels from a YouTube channel search (sp=EgIQAg%3D%3D); None when the page is blocked."""
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return None
    found: list = []

    def walk(o):
        if isinstance(o, dict):
            if "channelRenderer" in o:
                found.append(o["channelRenderer"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(json.loads(m.group(1)))
    out = []
    for c in found:
        ep = ((c.get("navigationEndpoint") or {}).get("browseEndpoint") or {})
        if not ep:
            continue
        out.append({"name": (c.get("title") or {}).get("simpleText", ""),
                    "ch": "https://www.youtube.com" + (ep.get("canonicalBaseUrl") or "/channel/" + ep.get("browseId", "")),
                    "ver": "VERIFIED" in json.dumps(c.get("ownerBadges") or [])})
    return out


def pick_studio_channel(studio: str, channels: list[dict[str, Any]]) -> str:
    """The studio's official channel: a verified channel of the same name, else the top result if its name
    matches exactly (look-alike copies further down, e.g. a second unverified 'Hombale Films', are ignored)."""
    matching = [c for c in channels[:5] if channel_matches(c["name"], studio)]
    verified = next((c for c in matching if c["ver"]), None)
    if verified:
        return verified["ch"]
    if channels and channels[0] in matching and channel_core(channels[0]["name"]) == channel_core(studio):
        return channels[0]["ch"]
    return ""


def merge_titles(groups: list[list[Title]]) -> list[Title]:
    """Dedupe across sources; earlier groups win, later ones fill empty fields."""
    kept: list[Title] = []
    by_id: dict[str, Title] = {}
    by_key: dict[tuple[str, str], list[Title]] = {}
    for group in groups:
        for t in group:
            lang = re.split(r"[ (,/]", t.language.strip())[0].lower()
            hit = by_id.get(t.imdb_id) if t.imdb_id else None
            if hit is not None and norm(hit.title) != norm(t.title) and t.category == "TV Show":
                hit = None                        # different seasons of one series stay separate
            if hit is None:
                key = (norm(t.title), (t.sort_date or "")[:4])
                for cand in by_key.get(key, []):
                    clang = re.split(r"[ (,/]", cand.language.strip())[0].lower()
                    if clang == lang or "unspecified" in (clang, lang) or lang in cand.language.lower():
                        hit = cand
                        break
            if hit is not None:
                for fld in MERGE_FIELDS:
                    if not getattr(hit, fld) and getattr(t, fld):
                        setattr(hit, fld, getattr(t, fld))
                if not hit.youtube_channels and t.youtube_channels:
                    hit.youtube_channels = t.youtube_channels
                for u in t.source_urls:
                    hit.add_url(u)
                for role, names in t.credits.items():
                    hit.credits.setdefault(role, names)
                if hit.language in ("", "Unspecified") and t.language not in ("", "Unspecified"):
                    hit.language = t.language
                if t.category == "TV Show" and hit.category != "TV Show" and not hit.validated:
                    hit.category, hit.season_row = "TV Show", hit.season_row or t.season_row
                if t.from_list:
                    hit.from_list = True
                    if t.window:
                        hit.window = t.window
                if t.source and t.source not in hit.source:
                    hit.source = f"{hit.source}; {t.source}" if hit.source else t.source
                if t.imdb_id:
                    by_id.setdefault(t.imdb_id, hit)
                continue
            kept.append(t)
            if t.imdb_id:
                by_id[t.imdb_id] = t
            by_key.setdefault((norm(t.title), (t.sort_date or "")[:4]), []).append(t)
    return kept


def filter_window(titles: list[Title], start: date | None, end: date | None, keep_tba: bool,
                  only_list: bool) -> list[Title]:
    if only_list:
        return [t for t in titles if t.from_list]
    lo = start.isoformat() if start else "0000-01-01"
    hi = end.isoformat() if end else "9999-12-31"
    return [t for t in titles if t.from_list or (lo <= t.sort_date <= hi if t.sort_date else keep_tba)]


# --------------------------------------------------------------------------------------------
# Wikipedia tables
# --------------------------------------------------------------------------------------------
def _cell_text(cell) -> str:
    for tag in cell.find_all(["style", "sup"]):
        tag.decompose()
    for br in cell.find_all("br"):
        br.replace_with(", ")
    for li in cell.find_all("li"):
        li.append(", ")
    text = re.sub(r"\s+", " ", cell.get_text(" "))
    text = re.sub(r"(\s*,\s*)+", ", ", text)
    return text.strip(" ,")


def table_grid(table) -> list[list[str]]:
    """Expand rowspan/colspan so every row has a value in every column."""
    grid: list[list[str | None]] = []
    for ri, tr in enumerate(table.find_all("tr")):
        while len(grid) <= ri:
            grid.append([])
        ci = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while ci < len(grid[ri]) and grid[ri][ci] is not None:
                ci += 1
            rs = int(cell.get("rowspan", 1) or 1) if str(cell.get("rowspan", "1")).isdigit() else 1
            cs = int(cell.get("colspan", 1) or 1) if str(cell.get("colspan", "1")).isdigit() else 1
            value = _cell_text(cell)
            for a in range(rs):
                while len(grid) <= ri + a:
                    grid.append([])
                row = grid[ri + a]
                for b in range(cs):
                    while len(row) <= ci + b:
                        row.append(None)
                    row[ci + b] = value
            ci += cs
    return [[c or "" for c in row] for row in grid]


def parse_film_year_page(html: str, language: str, year: int, today: date, page_url: str = "") -> list[Title]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Title] = []
    seen: set[str] = set()
    for table in soup.select("table.wikitable"):
        grid = table_grid(table)
        hi = next((i for i, r in enumerate(grid) if any(re.fullmatch(r"(title|film|name)", c.strip(), re.I) for c in r)), -1)
        if hi < 0:
            continue
        head = [c.lower() for c in grid[hi]]
        if any(re.search(r"rank|gross|box office|collection|award|category", h) for h in head):
            continue
        col = lambda pat: next((i for i, h in enumerate(head) if re.search(pat, h)), -1)  # noqa: E731
        c_t = next(i for i, h in enumerate(head) if re.fullmatch(r"(title|film|name)", h.strip()))
        c_d, c_c = col(r"director"), col(r"cast|starring")
        c_p, c_g, c_r = col(r"production|studio|producer|banner"), col(r"genre"), col(r"release date|^date")
        for row in grid[hi + 1:]:
            if len(row) <= c_t or not row[c_t] or re.fullmatch(r"(title|film)", row[c_t], re.I):
                continue
            title = row[c_t]
            if norm(title) in seen:
                continue
            seen.add(norm(title))
            mon = day = None
            cells = row[:c_t] + ([row[c_r]] if 0 <= c_r < len(row) and c_r > c_t else [])
            for raw in cells:
                v = re.sub(r"[^a-z0-9]", "", raw.lower())
                if v[:3] in MONTHS and v.isalpha():
                    mon = MONTHS[v[:3]]
                elif v.isdigit() and len(v) <= 2:
                    day = int(v)
                else:
                    m = re.match(r"^(\d{1,2})([a-z]{3,9})|^([a-z]{3,9})(\d{1,2})", v)
                    if m and (m.group(2) or m.group(3))[:3] in MONTHS:
                        mon = MONTHS[(m.group(2) or m.group(3))[:3]]
                        day = int(m.group(1) or m.group(4))
            get = lambda i: row[i] if 0 <= i < len(row) else ""  # noqa: E731
            prod = get(c_p)
            t = Title(title=title, language=language, director=get(c_d)[:80],
                      cast=", ".join(get(c_c).split(", ")[:3]), studio=prod, genre=get(c_g),
                      source=f"Wikipedia – List of {language} films of {year}")
            t.add_url(page_url or f"https://en.wikipedia.org/wiki/List_of_{language}_films_of_{year}")
            net = NETWORK_RE.search(prod or "")
            if net:
                t.network = net.group(0).strip()
                t.studio = re.sub(r",?\s*(Netflix|Amazon Prime Video|Prime Video|JioHotstar|ZEE5|SonyLIV|Sun NXT|ETV Win)\b", "", prod).strip(" ,/")
            t.credits = {"director": [x for x in t.director.split(", ") if x],
                         "actor": [x for x in t.cast.split(", ") if x],
                         "studio": [x.strip() for x in re.split(r"[,/]", t.studio) if x.strip()]}
            try:
                if mon and day:
                    d = date(year, mon, day)
                    t.release_date, t.sort_date = d.strftime("%d %b %Y"), d.isoformat()
                elif mon:
                    d = date(year, mon, 1)
                    t.release_date, t.sort_date = d.strftime("%b %Y"), d.isoformat()
            except ValueError:
                pass
            t.status = status_for(t.sort_date, today)
            out.append(t)
    return out


def parse_tv_list_page(html: str, platform: str, today: date, page_url: str = "") -> list[Title]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Title] = []
    for table in soup.select("table.wikitable"):
        grid = table_grid(table)
        if not grid:
            continue
        hi = next((i for i, r in enumerate(grid) if any(re.fullmatch(r"(title|series|show)", c.strip(), re.I) for c in r)), -1)
        if hi < 0:
            continue
        head = [c.lower() for c in grid[hi]]
        c_t = next(i for i, h in enumerate(head) if re.fullmatch(r"(title|series|show)", h.strip()))
        c_d = next((i for i, h in enumerate(head) if re.search(r"premiere|release|date", h)), -1)
        c_l = next((i for i, h in enumerate(head) if "language" in h), -1)
        c_g = next((i for i, h in enumerate(head) if "genre" in h), -1)
        last_lang = ""
        for row in grid[hi + 1:]:
            if len(row) <= c_t or not row[c_t]:
                continue
            raw_date = row[c_d] if 0 <= c_d < len(row) else ""
            d = None
            if raw_date:
                try:
                    d = dateparser.parse(re.sub(r"\(.*?\)|\[.*?\]", "", raw_date), default=datetime(1900, 1, 1)).date()
                except (ValueError, OverflowError):
                    d = None
            if d is None or d.year < 2024:
                continue
            lang = (row[c_l] if 0 <= c_l < len(row) else "") or last_lang
            last_lang = lang
            t = Title(title=row[c_t], category="TV Show", language=lang or "Hindi",
                      genre=row[c_g] if 0 <= c_g < len(row) else "", network=platform,
                      type_platform=f"Web series – OTT ({platform})",
                      release_date=d.strftime("%d %b %Y"), sort_date=d.isoformat(),
                      source=f"Wikipedia – {platform} originals list")
            t.add_url(page_url)
            t.status = status_for(t.sort_date, today)
            out.append(t)
    return out


# --------------------------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------------------------
def _walk_videos(obj, out: list) -> None:
    if isinstance(obj, dict):
        if "videoRenderer" in obj:
            out.append(obj["videoRenderer"])
        for v in obj.values():
            _walk_videos(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_videos(v, out)


def parse_youtube_results(html: str) -> list[dict[str, Any]] | None:
    """Videos from a YouTube results page; None when the page has no ytInitialData (blocked)."""
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return None
    vids: list = []
    _walk_videos(json.loads(m.group(1)), vids)
    out = []
    for v in vids:
        run = ((v.get("ownerText") or {}).get("runs") or [{}])[0] or ((v.get("longBylineText") or {}).get("runs") or [{}])[0]
        ep = ((run.get("navigationEndpoint") or {}).get("browseEndpoint") or {})
        if not ep or not v.get("videoId"):
            continue
        out.append({
            "id": v["videoId"],
            "t": "".join(r.get("text", "") for r in (v.get("title") or {}).get("runs", [])),
            "ch": "https://www.youtube.com" + (ep.get("canonicalBaseUrl") or "/channel/" + ep.get("browseId", "")),
            "ver": "VERIFIED" in json.dumps(v.get("ownerBadges") or []),
            "name": run.get("text", ""),
            "pub": (v.get("publishedTimeText") or {}).get("simpleText", ""),
        })
    return out


YT_ROLES = (("trailer", re.compile(r"trailer|teaser|glimpse", re.I), True),
            ("full movie", re.compile(r"full\s*movie|full\s*hd|movie|episode", re.I), False),
            ("songs", re.compile(r"song|lyric|jukebox|video", re.I), False))


def pick_top3(title: str, role_results: list[list[dict[str, Any]]]) -> tuple[str, list[str]]:
    """One channel per role (trailer, full movie, songs), verified first, then fill from the rest."""
    base = re.sub(r"\s*\(.*?\)\s*", " ", title)
    base = re.sub(r"\s*[-–]?\s*season\s*\d+.*$", "", base, flags=re.I).strip()
    key = norm(base)
    picked: list[str] = []
    pool: list[dict[str, Any]] = []
    trailer = ""
    for (_, pattern, is_trailer), results in zip(YT_ROLES, role_results):
        matches = [x for x in results if key and key in norm(x["t"])]
        pool.extend(matches)
        cands = [x for x in matches if pattern.search(x["t"]) and x["ch"] not in picked]
        choice = next((x for x in cands if x["ver"]), cands[0] if cands else None)
        if choice:
            picked.append(choice["ch"])
            if is_trailer:
                trailer = "https://www.youtube.com/watch?v=" + choice["id"]
    for x in [x for x in pool if x["ver"]] + pool:
        if len(picked) < 3 and x["ch"] not in picked:
            picked.append(x["ch"])
    return trailer, picked[:3]


# --------------------------------------------------------------------------------------------
# CBFC website
# --------------------------------------------------------------------------------------------
CBFC_LABELS = {"film name": "film", "movie language": "language", "movie category": "category",
               "certified by regional office": "office", "certificate no": "certno",
               "certificate date": "certdate", "certified length": "length", "name of producer": "producer"}


def parse_cbfc_results(html: str) -> list[dict[str, str]]:
    lines = [l.strip() for l in BeautifulSoup(html, "html.parser").get_text("\n").split("\n") if l.strip()]
    recs: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for i, line in enumerate(lines):
        key = CBFC_LABELS.get(re.sub(r"[:.]", "", line.lower()).strip())
        if not key:
            continue
        if key == "film":
            cur = {}
            recs.append(cur)
        if cur is not None:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            cur[key] = "" if CBFC_LABELS.get(re.sub(r"[:.]", "", nxt.lower()).strip()) else nxt
    return [r for r in recs if r.get("film") and r["film"] != "-"]


# --------------------------------------------------------------------------------------------
# uploaded lists
# --------------------------------------------------------------------------------------------
def read_title_list(file_content: bytes | None, filename: str, bulk_text: str) -> list[dict[str, Any]]:
    rows: list[list[Any]] = []
    if file_content and filename:
        if filename.lower().endswith(".csv"):
            rows = list(csv.reader(StringIO(file_content.decode("utf-8-sig", errors="replace"))))
        else:
            from openpyxl import load_workbook
            ws = load_workbook(BytesIO(file_content), data_only=True, read_only=True).active
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
    out: list[dict[str, Any]] = []
    if rows:
        head = [str(h or "").strip().lower() for h in rows[0]]
        col = lambda *keys: next((i for i, h in enumerate(head) if any(k in h for k in keys)), None)  # noqa: E731
        c = dict(imdb=col("imdb"), title=col("title"), lang=col("language", "lang"), date=col("date", "year"),
                 ctype=col("content type", "type", "category"), window=col("pay1", "pay 1", "library", "window"))
        if c["title"] is None:
            c["title"] = 0
        for r in rows[1:]:
            g = lambda k: r[c[k]] if c[k] is not None and c[k] < len(r) else None  # noqa: E731
            if g("title"):
                out.append({k: g(k) for k in c})
    for line in (bulk_text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0]:
            out.append({"title": parts[0], "lang": parts[1] if len(parts) > 1 else "",
                        "date": parts[2] if len(parts) > 2 else "", "imdb": parts[3] if len(parts) > 3 else "",
                        "ctype": "", "window": ""})
    return out


def to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 < value < 80000:       # Excel serial
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = clean(value)
    if re.fullmatch(r"\d{4}", text):
        return date(int(text), 12, 31)
    if text:
        try:
            return dateparser.parse(text, dayfirst=True).date()
        except (ValueError, OverflowError):
            return None
    return None


# --------------------------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------------------------
class IndianReleaseTrackerService:
    def __init__(self, http_client, youtube_api_key: str = "", tmdb_api_key: str = "",
                 tmdb_read_access_token: str = "", omdb_api_key: str = "", imdb_service=None) -> None:
        self.http = http_client
        self.youtube_api_key = youtube_api_key
        self.tmdb_api_key = tmdb_api_key
        self.tmdb_token = tmdb_read_access_token
        self.omdb_api_key = omdb_api_key
        self.imdb_service = imdb_service
        self.imdb_blocked = False
        self._tmdb_token_bad = False
        self.notes: list[str] = []

    # -- HTTP --------------------------------------------------------------------------------
    def _get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict | None = None) -> dict:
        r = self.http.session.get(url, params=params, headers=headers, timeout=self.http.timeout_seconds)
        r.raise_for_status()
        return r.json()

    def _wiki_page_html(self, page: str) -> str | None:
        try:
            data = self._get_json(WIKI_API, {"action": "parse", "prop": "text", "redirects": 1,
                                            "format": "json", "page": page})
        except Exception:
            return None
        return (data.get("parse") or {}).get("text", {}).get("*")

    # -- run ---------------------------------------------------------------------------------
    def run(self, *, mode: str, custom_start: str = "", custom_end: str = "", languages: list[str] | None = None,
            include_tv: bool = True, youtube_scope: str = "recent", check_cbfc: bool = True,
            file_content: bytes | None = None, filename: str = "", bulk_text: str = "",
            api_key_override: str = "", notes: str = "", progress: ProgressCallback | None = None,
            today: date | None = None) -> dict[str, Any]:
        progress = progress or (lambda p, m: None)
        today = today or date.today()
        self.notes = []
        self.imdb_blocked = False
        languages = [l for l in (languages or INDIAN_LANGUAGES) if l in INDIAN_LANGUAGES] or INDIAN_LANGUAGES
        start, end, keep_tba = resolve_window(mode, today, custom_start, custom_end)
        only_list = mode == "4"
        listed = read_title_list(file_content, filename, bulk_text)
        if only_list and not listed:
            raise ValueError("Option 4 needs a title list: upload a CSV/Excel file or paste titles.")

        groups: list[list[Title]] = []
        coverage: list[dict[str, str]] = []
        if not only_list:
            years = list(range(start.year, end.year + 1))
            imdb_rows = self._imdb_advanced_search(start, end, include_tv, languages, today, progress)
            groups.append(imdb_rows)
            if self.imdb_blocked or not imdb_rows:
                groups.append(self._tmdb_discover(start, end, include_tv, languages, today, progress))
            groups.append(self._wikipedia_films(languages, years, today, progress))
            groups.append(self._imdb_lists(today, progress))
            groups.append(self._justwatch(start, end, today, progress))
            groups.append(self._listing_sites(languages, today, progress))
            if include_tv:
                groups.append(self._wikipedia_tv(today, progress))
        if listed:
            list_titles, coverage = self._resolve_list(listed, today, progress)
            groups.append(list_titles)

        progress(50, "Merging sources")
        merged = merge_titles(groups)
        # loose window first (validation can move a date by up to a year), then validate, then strict window
        if not only_list and start and end:
            lo, hi = (start - timedelta(days=400)).isoformat(), (end + timedelta(days=400)).isoformat()
            merged = [t for t in merged if t.from_list or not t.sort_date or lo <= t.sort_date <= hi]
        self._resolve_imdb_ids(merged, progress)
        self._imdb_validate(merged, today, progress)
        self._tmdb_enrich(merged, today, progress)
        self._omdb_enrich(merged, progress)
        merged = merge_titles([merged])                       # same IMDb ID found under two names
        titles = filter_window(merged, start, end, keep_tba, only_list)
        titles.sort(key=lambda t: (t.sort_date or "9999-12-31", t.title.lower()))

        self._youtube(titles, youtube_scope, today, api_key_override or self.youtube_api_key, progress)
        if check_cbfc:
            self._cbfc(titles, today, progress)

        progress(97, "Building result tables")
        rows = [t.row() for t in titles]
        movie_rows = [r for r in rows if r["Category"] == "Movie"]
        tv_rows = [r for r in rows if r["Category"] != "Movie"]
        window_text = ("your title list only" if only_list else
                       f"{start:%d %b %Y} → {end:%d %b %Y}" + (" + Date TBA titles" if keep_tba else ""))
        movies = sum(t.category == "Movie" for t in titles)
        with_yt = sum(bool(t.youtube_channels) for t in titles)
        with_net = sum(bool(r["Network"] != "—") for r in rows)
        by_status = ", ".join(f"{s}: {sum(t.status == s for t in titles)}" for s in STATUSES if any(t.status == s for t in titles))
        summary = (f"Mode {mode} – {window_text}. {len(titles)} titles ({movies} movies, {len(titles) - movies} TV shows); "
                   f"{sum(t.validated == 'IMDb' for t in titles)} validated on IMDb, {sum(t.validated == 'TMDB' for t in titles)} on TMDB; "
                   f"{sum(bool(t.imdb_id) for t in titles)} with an IMDb ID, {with_yt} with Top YouTube channels, "
                   f"{with_net} with a Network. {by_status}.")
        tv_sources: dict[str, int] = {}
        for t in titles:
            if t.category == "TV Show":
                src = ("TMDB" if any("themoviedb" in u for u in t.source_urls) else "IMDb" if t.validated == "IMDb"
                       else "JustWatch" if any("justwatch" in u for u in t.source_urls) else "Wikipedia")
                tv_sources[src] = tv_sources.get(src, 0) + 1
        summary += (" TV shows by source: " + (", ".join(f"{k} {v}" for k, v in tv_sources.items()) or "none") + ".")
        summary += f" Build {BUILD_ID}."
        summary += (f" The first table has all {len(rows)} titles; Movies ({len(movie_rows)}) and TV Shows ({len(tv_rows)}) "
                    "follow as separate tables with their own BDR ingest templates.")
        if notes:
            summary += f" Custom notes: {clean(notes)}."
        if self.notes:
            summary += " Notes: " + " ".join(self.notes)
        # Two tables so each gets its own BDR ingest template (Movies / TV Shows) - the ingest
        # export maps a section's tracker_type to one title category.
        # 1) everything in one table (Category = Movie / TV Show), 2-3) one table per category, each with its
        #    own BDR ingest template (the ingest export maps a table's tracker_type to one title category).
        sections = [{"key": "all_titles", "title": f"All titles – {len(movie_rows)} movies + {len(tv_rows)} TV shows",
                     "columns": TITLE_COLUMNS, "rows": rows, "row_count": len(rows), "supports_google": True}]
        sections += [{"key": "movies", "title": "Indian Regional Movies", "columns": TITLE_COLUMNS,
                     "rows": movie_rows, "row_count": len(movie_rows), "supports_google": True,
                     "tracker_type": TRACKER_TYPE + "_movies"}]
        if tv_rows or not only_list:
            sections.append({"key": "tv_shows", "title": "Indian Regional TV Shows", "columns": TITLE_COLUMNS,
                             "rows": tv_rows, "row_count": len(tv_rows), "supports_google": True,
                             "tracker_type": TRACKER_TYPE + "_tv"})
        if coverage:
            sections.append({"key": "source_coverage", "title": "Source Coverage (uploaded list)",
                             "columns": COVERAGE_COLUMNS, "rows": coverage, "row_count": len(coverage),
                             "supports_google": True})
        return {"tracker_type": TRACKER_TYPE, "title": "Indian Regional Release Tracker",
                "created_at": utc_now_iso(), "source_url": "https://en.wikipedia.org/wiki/List_of_Indian_films",
                "summary": summary, "sections": sections}

    # -- self-test (GET /indian-release-tracker/diagnostics) ---------------------------------
    def diagnose(self) -> list[dict[str, str]]:
        """Probe every source once FROM THIS SERVER, so a deployment shows what it can reach."""
        today = date.today()
        checks: list[dict[str, str]] = []

        def probe(name: str, fn) -> None:
            t0 = time.time()
            try:
                ok, detail = fn()
            except Exception as exc:  # noqa: BLE001 - report, never raise
                ok, detail = False, f"{exc.__class__.__name__}: {str(exc)[:160]}"
            checks.append({"source": name, "status": "OK" if ok else ("SKIPPED" if ok is None else "BLOCKED / FAILED"),
                           "seconds": f"{time.time() - t0:.1f}", "detail": detail})

        def imdb():
            r = self.http.session.post(IMDB_GRAPHQL, timeout=self.http.timeout_seconds,
                                       headers=IMDB_HEADERS,
                                       json={"query": IMDB_SEARCH_QUERY, "variables": {
                                           "types": IMDB_MOVIE_TYPES + IMDB_TV_TYPES, "start": today.isoformat(),
                                           "end": (today + timedelta(days=30)).isoformat(), "after": None, "first": 3}})
            data = (r.json().get("data") or {}).get("advancedTitleSearch") if r.status_code == 200 else None
            if not data:
                return False, f"HTTP {r.status_code}, no advancedTitleSearch data"
            first = [e["node"]["title"]["titleText"]["text"] for e in data.get("edges") or []]
            return True, f"{data.get('total')} India titles in the next 30 days, e.g. {', '.join(first)}"

        def imdb_suggest():
            data = json.loads(self.http.get_text(IMDB_SUGGEST.format(first="m", query="mysaa")))
            hit = next((x for x in data.get("d") or [] if str(x.get("id", "")).startswith("tt")), {})
            return bool(hit), f"'Mysaa' -> {hit.get('id', 'none')} ({hit.get('y', '')}) – used to match IMDb IDs"

        def tmdb_discover():
            if not (self.tmdb_api_key or self.tmdb_token):
                return None, "TMDB key not set"
            m = self._tmdb("/discover/movie", {"with_origin_country": "IN", "primary_release_date.gte": today.isoformat(),
                                               "primary_release_date.lte": (today + timedelta(days=90)).isoformat()})
            tv = self._tmdb("/discover/tv", {"with_origin_country": "IN", "first_air_date.gte": today.isoformat(),
                                             "first_air_date.lte": (today + timedelta(days=90)).isoformat()})
            return True, (f"{m.get('total_results')} India movies and {tv.get('total_results')} India TV shows in the next "
                          "90 days – used when IMDb refuses this server")

        def tmdb():
            if not (self.tmdb_api_key or self.tmdb_token):
                return None, "TMDB_API_KEY / TMDB_READ_ACCESS_TOKEN not set"
            found = self._tmdb("/find/tt0321968", {"external_source": "imdb_id"})
            hit = (found.get("movie_results") or [{}])[0]
            return bool(hit), f"tt0321968 -> {hit.get('title', 'no match')} ({hit.get('release_date', '')})"

        def omdb():
            if not self.omdb_api_key:
                return None, "OMDB_API_KEY not set"
            d = self._get_json(OMDB_API, {"i": "tt0321968", "apikey": self.omdb_api_key})
            return d.get("Response") == "True", f"{d.get('Title') or d.get('Error')} – {d.get('Director', '')}"

        def youtube_page():
            vids = parse_youtube_results(self.http.get_text(YT_RESULTS + quote_plus("Deep Jele Jai Bengali trailer")))
            if vids is None:
                return False, "results page served without ytInitialData (consent/bot page)"
            return bool(vids), f"{len(vids)} videos; first channel {vids[0]['ch'] if vids else '-'}"

        def youtube_api():
            if not self.youtube_api_key:
                return None, "YOUTUBE_API_KEY not set (only used if the results page is blocked)"
            res = self._youtube_api_search("Deep Jele Jai Bengali trailer", self.youtube_api_key)
            return bool(res), f"{len(res)} videos; first channel {res[0]['ch'] if res else '-'}"

        def wikipedia():
            html = self._wiki_page_html(f"List of Hindi films of {today.year}")
            rows = parse_film_year_page(html, "Hindi", today.year, today) if html else []
            return bool(rows), f"List of Hindi films of {today.year}: {len(rows)} films parsed"

        def wikidata():
            d = self._get_json(WIKIDATA_SPARQL, {"format": "json", "query":
                               'SELECT ?item WHERE { ?item wdt:P345 "tt0321968" }'},
                               headers={"Accept": "application/sparql-results+json"})
            b = d["results"]["bindings"]
            return bool(b), f"tt0321968 -> {b[0]['item']['value'].rsplit('/', 1)[-1] if b else 'no item'}"

        def cbfc():
            form = self._cbfc_form()
            return (form is not None), (f"Search Film form found: {form[1].upper()} {form[0]} (field '{form[3]}')"
                                        if form else "no Search Film form served to this server")

        def justwatch():
            page = self._jw({"d": today.isoformat(), "pt": "UPCOMING", "after": None, "packages": list(JW_PACKAGES)})
            indian = [t.title for t in (self._jw_title(e, today, today) for e in page.get("edges") or []) if t]
            return True, f"{page.get('totalCount')} upcoming OTT titles in India; Indian-made on page 1: {', '.join(indian[:4]) or 'none'}"

        def imdb_list():
            html = self.http.get_text(f"https://www.imdb.com/list/{IMDB_LISTS[0][0]}/")
            ids = list(dict.fromkeys(re.findall(r'"listItem":\{"id":"(tt\d+)"', html)))
            return bool(ids), f"{IMDB_LISTS[0][0]}: {len(ids)} titles"

        listing_probes = []
        for name, url, lang, platform in LISTING_SITES:
            def site(url=url, name=name, lang=lang, platform=platform):
                rows = parse_listing_page(self.http.get_text(url), name, lang, platform, today)
                return bool(rows), f"{len(rows)} titles parsed, e.g. {', '.join(r.title for r in rows[:3]) or '-'}"
            listing_probes.append((f"Listing page: {name}", site))

        checks.append({"source": "Build (git commit live on this server)", "status": "OK", "seconds": "0.0",
                       "detail": BUILD_ID})
        for name, fn in (("IMDb Advanced Search (GraphQL)", imdb), ("IMDb suggestion API (IDs)", imdb_suggest),
                         ("TMDB India discovery (IMDb fallback)", tmdb_discover),
                         ("IMDb list", imdb_list), ("JustWatch India", justwatch),
                         ("TMDB API", tmdb), ("OMDb API", omdb),
                         ("YouTube results page", youtube_page), ("YouTube Data API", youtube_api),
                         ("Wikipedia API", wikipedia), ("Wikidata SPARQL", wikidata), ("CBFC website", cbfc),
                         *listing_probes):
            probe(name, fn)
        return checks

    # -- sources -----------------------------------------------------------------------------
    def _wikipedia_films(self, languages, years, today, progress) -> list[Title]:
        out: list[Title] = []
        jobs = [(l, y) for l in languages for y in years]
        missing = []
        for i, (lang, year) in enumerate(jobs):
            progress(10 + int(30 * i / max(len(jobs), 1)), f"Wikipedia: {lang} films of {year}")
            page = f"List of {lang} films of {year}"
            html = self._wiki_page_html(page)
            if not html:
                page = f"List of Indian {lang} films of {year}"
                html = self._wiki_page_html(page)
            if not html:
                missing.append(f"{lang} {year}")
                continue
            out.extend(parse_film_year_page(html, lang, year, today,
                                            "https://en.wikipedia.org/wiki/" + quote(page.replace(" ", "_"))))
        if missing:
            self.notes.append(f"No Wikipedia year page yet for: {', '.join(missing[:12])}{'…' if len(missing) > 12 else ''}.")
        return out

    def _wikipedia_tv(self, today, progress) -> list[Title]:
        out: list[Title] = []
        for page, platform in TV_LISTS:
            progress(42, f"Wikipedia: {platform} originals")
            html = self._wiki_page_html(page)
            if html:
                out.extend(parse_tv_list_page(html, platform, today,
                                              "https://en.wikipedia.org/wiki/" + quote(page.replace(" ", "_"))))
        return out

    def _imdb_advanced_search(self, start, end, include_tv, languages, today, progress) -> list[Title]:
        """IMDb Advanced Title Search: India-origin titles released in the window (movies + TV)."""
        kinds = [k for k in imdb_rule_kinds() if include_tv or k[0] == "Movie"]
        wanted = {l.lower() for l in languages}
        out: list[Title] = []
        rule_url = imdb_rule_url(start, end)
        self.notes.append(f"IMDb rule: {rule_url}")
        for category, types in kinds:
            after, pages, total = None, 0, 0
            while pages < IMDB_SEARCH_MAX_PAGES:
                progress(5 + pages, f"IMDb advanced search: {category}s {start:%d %b %Y} → {end:%d %b %Y}")
                try:
                    r = self.http.session.post(
                        IMDB_GRAPHQL, timeout=self.http.timeout_seconds,
                        headers=IMDB_HEADERS,
                        json={"query": IMDB_SEARCH_QUERY, "variables": {
                            "types": types, "start": start.isoformat(), "end": end.isoformat(),
                            "after": after, "first": IMDB_SEARCH_PAGE}})
                    if r.status_code in (401, 403, 429):
                        self.imdb_blocked = True
                        self.notes.append(f"IMDb refused this server (HTTP {r.status_code}); TMDB India discovery used instead.")
                        return out
                    data = (r.json().get("data") or {}).get("advancedTitleSearch")
                except Exception as exc:
                    self.notes.append(f"IMDb advanced search not reachable from the server ({exc.__class__.__name__}); "
                                      "Wikipedia and TMDB cover these titles instead.")
                    return out
                if not data:
                    self.notes.append("IMDb advanced search returned no data (IMDb may be blocking the server).")
                    return out
                total = data.get("total") or 0
                for edge in data.get("edges") or []:
                    t = self._imdb_title(edge["node"]["title"], category, wanted, today)
                    if t:
                        t.add_url(rule_url)
                        out.append(t)
                pages += 1
                info = data.get("pageInfo") or {}
                if not info.get("hasNextPage"):
                    break
                after = info.get("endCursor")
            if pages >= IMDB_SEARCH_MAX_PAGES:
                self.notes.append(f"IMDb advanced search for {category}s stopped at {pages * IMDB_SEARCH_PAGE} of {total} titles.")
        return out

    @staticmethod
    def _imdb_title(t: dict, category: str, wanted: set[str], today: date) -> Title | None:
        """Map an IMDb GraphQL title. India release date wins over the worldwide one."""
        names = lambda key, path: [e["node"][path[0]][path[1]]["text"]  # noqa: E731
                                   for e in ((t.get(key) or {}).get("edges") or [])]
        langs = [LANGUAGE_ALIASES.get(x["text"].lower(), x["text"])
                 for x in ((t.get("spokenLanguages") or {}).get("spokenLanguages") or [])]
        indian = [l for l in langs if l.lower() in INDIAN_LOWER]
        if indian and wanted and not any(l.lower() in wanted for l in indian):
            return None                                   # a language the user unticked
        d, release = imdb_release(t)
        dist_all = names("dist", ("company", "companyText"))
        dist = [n for n in dist_all if NETWORK_RE.search(n)]
        prod = names("prod", ("company", "companyText"))
        type_id = (t.get("titleType") or {}).get("id", "")
        if type_id in IMDB_TV_TYPES:
            category = "TV Show"
        elif type_id in IMDB_MOVIE_TYPES:
            category = "Movie"
        runtime = ((t.get("runtime") or {}).get("seconds") or 0) // 60
        if category == "Movie":
            platform = "TV Movie" if type_id == "tvMovie" else "Film – Theatrical"
        else:
            platform = f"{'Mini-series' if type_id == 'tvMiniSeries' else 'TV series'} – " + (f"OTT ({dist[0]})" if dist else "TV / OTT")
        countries = [x["id"] for x in ((t.get("countriesOfOrigin") or {}).get("countries") or [])]
        row = Title(
            title=(t.get("titleText") or {}).get("text", ""), imdb_id=t["id"], category=category,
            genre=" / ".join(g["text"] for g in ((t.get("genres") or {}).get("genres") or [])),
            language=langs[0] if langs else "Unspecified",
            director=", ".join(names("directors", ("name", "nameText"))),
            cast=", ".join(names("cast", ("name", "nameText"))[:3]),
            produced_by=", ".join(names("producers", ("name", "nameText"))), studio=", ".join(prod),
            network=", ".join(dict.fromkeys(dist[:3])),
            release_date=release, sort_date=d.isoformat() if d else None,
            status=status_for(d.isoformat() if d else None, today, "Tentative"),
            duration=f"{runtime} min" if runtime else "",
            certificate_type=((t.get("certificate") or {}).get("rating") or ""),
            type_platform=platform, validated="IMDb",
            origin="Indian" if ("IN" in countries and (indian or not langs)) else "International",
            source="IMDb")
        row.credits = {"actor": names("cast", ("name", "nameText")), "director": names("directors", ("name", "nameText")),
                       "producer": names("producers", ("name", "nameText")), "studio": prod, "distributor": dist_all}
        row.add_url(f"https://www.imdb.com/title/{t['id']}/")
        return row

    # -- validation: IMDb IDs (hub logic) + IMDb as the authority -----------------------------
    def _imdb_suggest(self, t: Title) -> str:
        """The hub's ttcode logic: the local IMDb index when enabled, else IMDb's suggestion API,
        matched on exact title + title type + year (+-1)."""
        year = int(t.sort_date[:4]) if t.sort_date else None
        types = ["tvSeries", "tvMiniSeries"] if t.category == "TV Show" else ["movie", "tvMovie"]
        base = re.sub(r"\s*[-–]\s*season\s*\d+.*$", "", re.sub(r"\s*\(.*?\)\s*", " ", t.title), flags=re.I).strip()
        if self.imdb_service is not None and getattr(self.imdb_service, "enabled", False):
            try:
                hit = self.imdb_service.lookup_title(base, category="TV Shows" if t.category == "TV Show" else "Movies",
                                                     release_date=date(year, 1, 1) if year else None, preferred_types=types)
                if hit and norm(hit.get("title", "")) == norm(base) and (not year or not str(hit.get("start_year", "")).isdigit()
                                                                          or abs(int(hit["start_year"]) - year) <= 1):
                    return hit["imdb_id"]
            except Exception:
                pass
        q = re.sub(r"\s+", "_", base.lower())
        first = next((ch for ch in q if ch.isalnum()), "x")
        try:
            data = json.loads(self.http.get_text(IMDB_SUGGEST.format(first=first, query=quote(q))))
        except Exception:
            return ""
        best, best_score = "", -1
        for item in data.get("d") or []:
            iid = str(item.get("id", ""))
            if not iid.startswith("tt") or norm(item.get("l", "")) != norm(base):
                continue
            score = 10 if item.get("qid") in types else 0
            y = item.get("y")
            if year and isinstance(y, int):
                if abs(y - year) > 1 and not t.season_row:
                    continue
                score += 20 - abs(y - year) * 5
            elif year:
                score -= 5
            if score > best_score:
                best, best_score = iid, score
        return best

    def _resolve_imdb_ids(self, titles: list[Title], progress) -> None:
        targets = [t for t in titles if not t.imdb_id][:IMDB_RESOLVE_MAX]
        if not targets:
            return
        progress(52, f"IMDb: matching {len(targets)} titles to IMDb IDs")
        found = {"n": 0}

        def one(t: Title) -> None:
            iid = self._imdb_suggest(t)
            if iid:
                t.imdb_id = iid
                found["n"] += 1

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(one, targets))
        self.notes.append(f"IMDb IDs matched for {found['n']} of {len(targets)} titles that had none (title + type + year).")

    def _imdb_validate(self, titles: list[Title], today: date, progress) -> None:
        """IMDb is the authority for category, release date, language, producers and studios. A row whose
        IMDb record disagrees (other language, release years apart) loses the IMDb ID instead of being merged."""
        ids = list(dict.fromkeys(t.imdb_id for t in titles if t.imdb_id))
        if not ids:
            return
        if self.imdb_blocked:
            self.notes.append("IMDb validation skipped (IMDb refuses this server) – TMDB validates instead.")
            return
        progress(55, f"IMDb: validating {len(ids)} titles")
        info: dict[str, dict] = {}
        failed = False
        for i in range(0, len(ids), 50):
            try:
                r = self.http.session.post(IMDB_GRAPHQL, timeout=self.http.timeout_seconds,
                                           headers=IMDB_HEADERS,
                                           json={"query": IMDB_TITLES_QUERY, "variables": {"ids": ids[i:i + 50]}})
                if r.status_code in (401, 403, 429):
                    self.imdb_blocked = failed = True
                    break
                for t in (r.json().get("data") or {}).get("titles") or []:
                    if t:
                        info[t["id"]] = t
            except Exception:
                failed = True
        if failed and not info:
            self.notes.append("IMDb validation unavailable from the server – rows rely on their own sources.")
            return
        corrected = rejected = 0
        for t in titles:
            raw = info.get(t.imdb_id)
            if not raw:
                continue
            ref = self._imdb_title(raw, t.category, set(), today)
            if ref is None:
                continue
            our_lang = t.language.split(" (")[0].strip().lower()
            ref_langs = [x["text"].lower() for x in ((raw.get("spokenLanguages") or {}).get("spokenLanguages") or [])]
            year_gap = (abs(int(t.sort_date[:4]) - int(ref.sort_date[:4]))
                        if t.sort_date and ref.sort_date and not t.season_row else 0)
            lang_clash = our_lang in INDIAN_LOWER and ref_langs and our_lang not in ref_langs
            if (year_gap > 1 or lang_clash) and not t.from_list:
                t.imdb_id = ""                       # different title with the same name
                rejected += 1
                continue
            t.category = ref.category if not t.season_row else "TV Show"
            if ref.sort_date and not t.season_row and t.sort_date != ref.sort_date:
                if t.sort_date and t.sort_date[:4] != ref.sort_date[:4]:
                    corrected += 1
                t.release_date, t.sort_date, t.status = ref.release_date, ref.sort_date, ref.status
            if t.language in ("", "Unspecified") or (our_lang not in INDIAN_LOWER and ref.language != "Unspecified"):
                t.language = ref.language
            for fld in ("genre", "director", "cast", "produced_by", "studio", "network", "duration"):
                if getattr(ref, fld) and (not getattr(t, fld) or fld in ("produced_by", "studio")):
                    setattr(t, fld, getattr(ref, fld))
            for role, names in ref.credits.items():
                t.credits[role] = list(dict.fromkeys(names + t.credits.get(role, [])))
            t.origin = ref.origin if t.origin == "Indian" else t.origin
            if t.category == "TV Show" and "Theatrical" in t.type_platform:
                t.type_platform = ref.type_platform
            t.validated = "IMDb"
            t.add_url(f"https://www.imdb.com/title/{t.imdb_id}/")
        self.notes.append(f"IMDb validation: {sum(t.validated == 'IMDb' for t in titles)} rows confirmed, "
                          f"{corrected} release dates corrected, {rejected} wrong IMDb matches removed.")

    # -- TMDB / OMDb (the hub's API keys) ----------------------------------------------------
    def _tmdb(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        headers = {"Accept": "application/json"}
        if self.tmdb_token and not self._tmdb_token_bad:
            r = self.http.session.get(TMDB_API + path, params=params,
                                      headers={**headers, "Authorization": f"Bearer {self.tmdb_token}"},
                                      timeout=self.http.timeout_seconds)
            if r.status_code != 401 or not self.tmdb_api_key:
                r.raise_for_status()
                return r.json()
            self._tmdb_token_bad = True              # bad/expired token -> use the API key from now on
            self.notes.append("TMDB read-access token rejected (401); used TMDB_API_KEY instead.")
        if self.tmdb_api_key:
            params["api_key"] = self.tmdb_api_key
        return self._get_json(TMDB_API + path, params, headers)

    @staticmethod
    def _tmdb_title(d: dict, kind: str, today: date) -> Title:
        """Map TMDB details (credits, release_dates, external_ids, watch/providers appended)."""
        crew = (d.get("credits") or {}).get("crew") or []
        india = None
        for r in (d.get("release_dates") or {}).get("results") or []:
            if r.get("iso_3166_1") == "IN":
                ds = [x["release_date"][:10] for x in r.get("release_dates") or [] if x.get("type") in (2, 3, 4) and x.get("release_date")]
                india = min(ds) if ds else None
        raw = india or d.get("release_date") or d.get("first_air_date") or ""
        dd = date.fromisoformat(raw) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else None
        nets = [n["name"] for n in d.get("networks") or []]
        flat = [p["provider_name"] for p in ((((d.get("watch/providers") or {}).get("results") or {}).get("IN") or {}).get("flatrate") or [])]
        tv = kind == "tv"
        companies = [c["name"] for c in d.get("production_companies") or []]
        producers = list(dict.fromkeys(c["name"] for c in crew if c.get("job") in ("Producer", "Executive Producer")))
        directors = [c["name"] for c in crew if c.get("job") == "Director"] or [c.get("name", "") for c in d.get("created_by") or []]
        cast = [c["name"] for c in ((d.get("credits") or {}).get("cast") or [])[:5]]
        network = ", ".join(dict.fromkeys((nets or flat)[:3]))
        iid = ((d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id") or "")
        row = Title(
            title=d.get("title") or d.get("name") or "", category="TV Show" if tv else "Movie",
            imdb_id=iid if iid.startswith("tt") else "",
            genre=" / ".join(g["name"] for g in d.get("genres") or []),
            language=CODE_LANGS.get(d.get("original_language", ""), "Unspecified"),
            director=", ".join(directors[:3]), cast=", ".join(cast[:3]), produced_by=", ".join(producers[:5]),
            studio=", ".join(companies[:5]), network=network,
            release_date=dd.strftime("%d %b %Y") if dd else "TBA (announced)", sort_date=dd.isoformat() if dd else None,
            status=status_for(dd.isoformat() if dd else None, today, "Confirmed"),
            duration=f"{d['runtime']} min" if d.get("runtime") else "",
            type_platform=(f"TV series – {'OTT' if flat or not nets else 'TV'} ({network})" if network else "TV series")
            if tv else (f"Film – Theatrical" + (f" / OTT ({network})" if network else "")),
            origin="Indian" if "IN" in (d.get("origin_country") or [c.get("iso_3166_1") for c in d.get("production_countries") or []]) else "International",
            validated="TMDB", source="TMDB")
        row.credits = {"actor": cast, "director": directors, "producer": producers, "studio": companies,
                       "network": nets + flat}
        row.add_url(f"https://www.themoviedb.org/{kind}/{d.get('id')}")
        if row.imdb_id:
            row.add_url(f"https://www.imdb.com/title/{row.imdb_id}/")
        return row

    def _tmdb_discover(self, start, end, include_tv, languages, today, progress) -> list[Title]:
        """IMDb-equivalent discovery on TMDB: India-origin movies (+ TV) released in the window."""
        if not (self.tmdb_api_key or self.tmdb_token) or not (start and end):
            return []
        wanted = {LANG_CODES[l] for l in languages if l in LANG_CODES}
        found: list[tuple[str, int]] = []
        rule = [k for k, _ in imdb_rule_kinds()]
        kinds = ([("movie", "primary_release_date")] if "Movie" in rule else []) + \
                ([("tv", "first_air_date")] if include_tv and "TV Show" in rule else [])
        try:
            for kind, field in kinds:
                chunk_start = start
                while chunk_start <= end and len(found) < TMDB_DISCOVER_MAX:
                    chunk_end = min(end, chunk_start + timedelta(days=180))
                    page, pages = 1, 1
                    while page <= pages and page <= TMDB_DISCOVER_PAGES and len(found) < TMDB_DISCOVER_MAX:
                        progress(10 + min(page, 30), f"TMDB India: {kind} {chunk_start:%b %Y}–{chunk_end:%b %Y}, page {page}")
                        data = self._tmdb(f"/discover/{kind}", {
                            "with_origin_country": "IN", f"{field}.gte": chunk_start.isoformat(),
                            f"{field}.lte": chunk_end.isoformat(), "sort_by": f"{field}.asc", "page": page})
                        pages = min(int(data.get("total_pages") or 1), 500)
                        for r in data.get("results") or []:
                            if not wanted or r.get("original_language") in wanted or r.get("original_language") not in CODE_LANGS:
                                found.append((kind, r["id"]))
                        page += 1
                    chunk_start = chunk_end + timedelta(days=1)
        except Exception as exc:
            self.notes.append(f"TMDB India discovery incomplete ({exc.__class__.__name__}).")
        rows: list[Title] = []

        def one(item):
            kind, tid = item
            try:
                d = self._tmdb(f"/{kind}/{tid}", {"append_to_response": "credits,release_dates,external_ids,watch/providers"})
            except Exception:
                return
            t = self._tmdb_title(d, kind, today)
            # TMDB is user-edited: keep a title only if it has an IMDb ID, a production company or a TV network
            if t.imdb_id or t.studio or (kind == "tv" and d.get("networks")):
                rows.append(t)

        progress(40, f"TMDB India: details for {len(found)} titles")
        with ThreadPoolExecutor(max_workers=TMDB_WORKERS) as pool:
            list(pool.map(one, list(dict.fromkeys(found))))
        self.notes.append(f"TMDB India discovery: {len(rows)} titles kept of {len(found)} found "
                          "(kept only with an IMDb ID, a production company or a TV network).")
        return rows

    def _tmdb_enrich(self, titles: list[Title], today: date, progress) -> None:
        if not (self.tmdb_api_key or self.tmdb_token):
            self.notes.append("TMDB key not configured – skipped TMDB enrichment.")
            return
        needs = [t for t in titles if t.validated != "IMDb" or not (t.genre and t.director and t.studio and t.network)]
        needs.sort(key=lambda t: (not t.from_list, t.sort_date is None, -(int(t.sort_date.replace("-", "")) if t.sort_date else 0)))
        targets = needs[:TMDB_MAX_TITLES]
        if len(needs) > TMDB_MAX_TITLES:
            self.notes.append(f"TMDB enrichment capped at {TMDB_MAX_TITLES} of {len(needs)} titles (listed, upcoming and recent first).")
        if not targets:
            return
        state = {"done": 0, "hit": 0}

        def one(t: Title) -> None:
            try:
                kind, tid = self._tmdb_locate(t)
                if tid:
                    self._apply_tmdb(t, kind, tid)
                    state["hit"] += 1
            except Exception:
                pass
            state["done"] += 1
            if state["done"] % 25 == 0:
                progress(56 + int(4 * state["done"] / len(targets)), f"TMDB: {state['done']}/{len(targets)} titles")

        progress(56, f"TMDB: enriching {len(targets)} titles")
        with ThreadPoolExecutor(max_workers=TMDB_WORKERS) as pool:
            list(pool.map(one, targets))
        self.notes.append(f"TMDB matched {state['hit']} of {len(targets)} titles that had gaps.")

    def _tmdb_locate(self, t: Title) -> tuple[str, int | None]:
        want = "tv" if t.category != "Movie" else "movie"
        if t.imdb_id:
            found = self._tmdb(f"/find/{t.imdb_id}", {"external_source": "imdb_id"})
            for kind, key in (("movie", "movie_results"), ("tv", "tv_results")):
                if found.get(key):
                    return kind, found[key][0]["id"]
            return want, None
        lang = LANG_CODES.get(t.language.split(" (")[0].split()[0] if t.language else "", "")
        params = {"query": re.sub(r"\s*\(.*?\)\s*", " ", t.title).strip()}
        if t.sort_date:
            params["year" if want == "movie" else "first_air_date_year"] = t.sort_date[:4]
        results = self._tmdb(f"/search/{want}", params).get("results") or []
        key = norm(params["query"])
        for r in results[:8]:
            names = [r.get("title") or r.get("name") or "", r.get("original_title") or r.get("original_name") or ""]
            if any(norm(n) == key for n in names) and (not lang or r.get("original_language") == lang):
                return want, r["id"]
        return want, None

    def _apply_tmdb(self, t: Title, kind: str, tid: int) -> None:
        d = self._tmdb(f"/{kind}/{tid}", {"append_to_response": "credits,release_dates,external_ids,watch/providers"})
        ref = self._tmdb_title(d, kind, date.today())
        our_lang = t.language.split(" (")[0].strip().lower()
        if (not t.validated and our_lang in INDIAN_LOWER and ref.language != "Unspecified"
                and ref.language.lower() != our_lang and not t.from_list):
            return                                            # TMDB found a same-name title in another language
        raw = ref.sort_date or ""
        has_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw))
        if (not t.validated and has_date and not t.season_row and t.sort_date
                and abs(int(t.sort_date[:4]) - int(raw[:4])) > 1):
            return                                            # TMDB found a different title of the same name
        t.add_url(f"https://www.themoviedb.org/{kind}/{tid}")
        if not t.validated:
            t.category = "TV Show" if kind == "tv" else "Movie"
            if not t.produced_by:
                t.produced_by = ref.produced_by
            if ref.studio:
                t.studio = ref.studio
            if has_date and not t.season_row:
                t.sort_date, t.release_date = raw, date.fromisoformat(raw).strftime("%d %b %Y")
                t.status = status_for(raw, date.today(), "Confirmed")
            t.validated = "TMDB"
        ext = d.get("external_ids") or {}
        if not t.imdb_id and (ext.get("imdb_id") or d.get("imdb_id") or "").startswith("tt"):
            t.imdb_id = ext.get("imdb_id") or d.get("imdb_id")
        if not t.genre:
            t.genre = " / ".join(g["name"] for g in d.get("genres") or [])
        credits = d.get("credits") or {}
        if not t.director:
            crew = credits.get("crew") or []
            t.director = ", ".join(dict.fromkeys(c["name"] for c in crew if c.get("job") in ("Director", "Series Director")))[:120] \
                or ", ".join(c.get("name", "") for c in (d.get("created_by") or []))
        if not t.cast:
            t.cast = ", ".join(c["name"] for c in (credits.get("cast") or [])[:3])
        if not t.studio:
            t.studio = ", ".join(c["name"] for c in (d.get("production_companies") or [])[:3])
        t.credits.setdefault("studio", [c["name"] for c in (d.get("production_companies") or [])[:5]])
        t.credits.setdefault("actor", [c["name"] for c in (credits.get("cast") or [])[:5]])
        t.credits.setdefault("director", [c["name"] for c in (credits.get("crew") or []) if c.get("job") == "Director"][:3])
        if not t.produced_by:
            t.produced_by = ", ".join(dict.fromkeys(c["name"] for c in (credits.get("crew") or [])
                                                    if c.get("job") in ("Producer", "Executive Producer")))[:200]
        if not t.network:
            nets = [n["name"] for n in d.get("networks") or []]
            providers = ((d.get("watch/providers") or {}).get("results") or {}).get("IN") or {}
            flat = [p["provider_name"] for p in providers.get("flatrate") or []]
            t.network = ", ".join(dict.fromkeys((nets or flat)[:3]))
        if t.language in ("", "Unspecified") and d.get("original_language") in CODE_LANGS | {"en": "English"}:
            t.language = CODE_LANGS[d["original_language"]]
        if not t.sort_date:
            raw = d.get("release_date") or d.get("first_air_date") or ""
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                dd = date.fromisoformat(raw)
                t.sort_date, t.release_date = raw, dd.strftime("%d %b %Y")
        t.source += "; TMDB"

    def _omdb_enrich(self, titles: list[Title], progress) -> None:
        if not self.omdb_api_key:
            return
        targets = [t for t in titles if t.imdb_id and not (t.genre and t.director and t.cast)][:OMDB_MAX_TITLES]
        if not targets:
            return
        progress(59, f"OMDb: filling gaps for {len(targets)} titles")
        filled = 0
        for t in targets:
            try:
                d = self._get_json(OMDB_API, {"i": t.imdb_id, "apikey": self.omdb_api_key})
            except Exception:
                continue
            if d.get("Response") != "True":
                continue
            ok = lambda v: v and v != "N/A"  # noqa: E731
            if not t.genre and ok(d.get("Genre")):
                t.genre = d["Genre"].replace(", ", " / ")
            if not t.director and ok(d.get("Director")):
                t.director = d["Director"]
            if not t.cast and ok(d.get("Actors")):
                t.cast = d["Actors"]
            filled += 1
            t.source += "; OMDb"
        if filled:
            self.notes.append(f"OMDb filled gaps for {filled} titles.")

    def _imdb_details(self, ids: list[str], today: date, category_hint: str = "") -> list[Title]:
        out: list[Title] = []
        for i in range(0, len(ids), 50):
            r = self.http.session.post(IMDB_GRAPHQL, timeout=self.http.timeout_seconds,
                                       headers=IMDB_HEADERS,
                                       json={"query": IMDB_TITLES_QUERY, "variables": {"ids": ids[i:i + 50]}})
            for t in (r.json().get("data") or {}).get("titles") or []:
                if not t:
                    continue
                cat = "TV Show" if (t.get("titleType") or {}).get("id") in IMDB_TV_TYPES else "Movie"
                row = self._imdb_title(t, category_hint or cat, set(), today)
                if row:
                    out.append(row)
        return out

    def _imdb_lists(self, today, progress) -> list[Title]:
        out: list[Title] = []
        for list_id, label in IMDB_LISTS:
            progress(9, f"{label}")
            try:
                html = self.http.get_text(f"https://www.imdb.com/list/{list_id}/")
                ids = list(dict.fromkeys(re.findall(r'"listItem":\{"id":"(tt\d+)"', html)))
                rows = self._imdb_details(ids, today)
                for r in rows:
                    r.source = f"{label} ({list_id})"
                out.extend(rows)
            except Exception as exc:
                self.notes.append(f"{label} not reachable ({exc.__class__.__name__}).")
        return out

    def _jw(self, variables: dict) -> dict:
        r = self.http.session.post(JUSTWATCH_API, timeout=self.http.timeout_seconds,
                                   headers={"content-type": "application/json"},
                                   json={"query": JW_QUERY, "variables": variables})
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:200])
        return data["data"]["newTitles"]

    @staticmethod
    def _jw_title(edge: dict, page_date: date, today: date) -> Title | None:
        """JustWatch title. Release date = the title's ORIGINAL release date; the date it was added to
        an OTT platform is only recorded in Type / Platform (old films added to Netflix are not new)."""
        node = edge.get("node") or {}
        is_season = node.get("__typename") == "Season"
        c = ((node.get("show") or {}).get("content") if is_season else node.get("content")) or {}
        if "IN" not in (c.get("productionCountries") or []):
            return None
        offer = ((edge.get("newOffer") or {}).get("package") or {}).get("clearName", "")
        ups = c.get("upcomingReleases") or []
        ott = None
        if ups and ups[0].get("releaseDate"):
            ott = date.fromisoformat(ups[0]["releaseDate"][:10])
            offer = offer or ((ups[0].get("package") or {}).get("clearName") or "")
        elif edge.get("newOffer"):
            ott = page_date
        original = date.fromisoformat(c["originalReleaseDate"][:10]) if c.get("originalReleaseDate") else None
        season = clean(((node.get("content") or {}).get("title") or "")) if is_season else ""
        new_season = bool(is_season and season and season.lower() != "season 1")
        # a new season is dated by its OTT premiere; everything else by its original release
        d = ott if new_season else (original or (ott if not is_season and ups else None))
        title = c.get("title", "") + (f" – {season}" if new_season else "")
        tv = is_season or node.get("objectType") == "SHOW"
        row = Title(
            title=title, category="TV Show" if tv else "Movie", season_row=new_season,
            imdb_id=((c.get("externalIds") or {}).get("imdbId") or ""),
            genre=" / ".join(JW_GENRES.get(g["shortName"], g["shortName"]) for g in c.get("genres") or []),
            director=", ".join(x["name"] for x in (c.get("credits") or [])[:3]), network=offer,
            release_date=d.strftime("%d %b %Y") if d else "TBA (announced)", sort_date=d.isoformat() if d else None,
            status=status_for(d.isoformat() if d else None, today, "Confirmed"),
            type_platform=(f"{'Web series' if tv else 'Film'} – OTT ({offer}"
                           + (f", on OTT {ott:%d %b %Y}" if ott else "") + ")") if offer else "OTT",
            source="JustWatch India")
        if c.get("fullPath"):
            row.add_url("https://www.justwatch.com" + c["fullPath"])
        if offer:
            row.credits = {"network": [offer]}
        return row

    def _justwatch(self, start, end, today, progress) -> list[Title]:
        packages = list(JW_PACKAGES)
        out: dict[str, Title] = {}

        def keep(t: Title | None) -> None:
            if t and (t.imdb_id or norm(t.title)) not in out:
                out[t.imdb_id or norm(t.title)] = t

        try:
            progress(44, "JustWatch India: upcoming on OTT")
            after = None
            for _ in range(JW_MAX_PAGES):
                page = self._jw({"d": today.isoformat(), "pt": "UPCOMING", "after": after, "packages": packages})
                for e in page.get("edges") or []:
                    keep(self._jw_title(e, today, today))
                if not (page.get("pageInfo") or {}).get("hasNextPage"):
                    break
                after = page["pageInfo"]["endCursor"]
            first = max(start, today - timedelta(days=JW_NEW_DAYS)) if start else today - timedelta(days=JW_NEW_DAYS)
            day = min(end, today) if end else today
            while day >= first:
                progress(45, f"JustWatch India: new on OTT {day:%d %b}")
                after = None
                for _ in range(3):
                    page = self._jw({"d": day.isoformat(), "pt": "NEW", "after": after, "packages": packages})
                    for e in page.get("edges") or []:
                        keep(self._jw_title(e, day, today))
                    if not (page.get("pageInfo") or {}).get("hasNextPage"):
                        break
                    after = page["pageInfo"]["endCursor"]
                day -= timedelta(days=1)
        except Exception as exc:
            self.notes.append(f"JustWatch India not fully reachable ({exc.__class__.__name__}).")
        return list(out.values())

    def _listing_sites(self, languages, today, progress) -> list[Title]:
        wanted = {l.lower() for l in languages}
        out: list[Title] = []
        report = []
        for name, url, lang, platform in LISTING_SITES:
            progress(46, f"Listing pages: {name}")
            try:
                rows = parse_listing_page(self.http.get_text(url), name, lang, platform, today, url)
            except Exception as exc:
                report.append(f"{name}: blocked ({exc.__class__.__name__})")
                continue
            rows = [r for r in rows if r.language == "Unspecified" or r.language.lower() in wanted
                    or r.language.lower() not in INDIAN_LOWER]
            report.append(f"{name}: {len(rows)}")
            out.extend(rows)
        self.notes.append("Listing pages – " + "; ".join(report) + ".")
        return out

    def _resolve_list(self, listed, today, progress) -> tuple[list[Title], list[dict[str, str]]]:
        progress(45, f"Resolving {len(listed)} listed titles on Wikidata")
        ids = sorted({clean(r.get("imdb")) for r in listed if re.fullmatch(r"tt\d+", clean(r.get("imdb")))})
        info: dict[str, dict] = {}
        for i in range(0, len(ids), 150):
            values = " ".join(f'"{x}"' for x in ids[i:i + 150])
            query = ("SELECT ?imdb ?item (GROUP_CONCAT(DISTINCT ?cc;separator='/') AS ?co) "
                     "(GROUP_CONCAT(DISTINCT ?ll;separator='/') AS ?lang) (GROUP_CONCAT(DISTINCT ?dl;separator=', ') AS ?dir) "
                     "(GROUP_CONCAT(DISTINCT ?sl;separator=', ') AS ?studio) (GROUP_CONCAT(DISTINCT ?nl;separator=', ') AS ?net) "
                     f"WHERE {{ VALUES ?imdb {{{values}}} ?item wdt:P345 ?imdb. "
                     "OPTIONAL{?item wdt:P495/wdt:P297 ?cc.} OPTIONAL{?item wdt:P364 ?l. ?l rdfs:label ?ll FILTER(lang(?ll)='en')} "
                     "OPTIONAL{?item wdt:P57 ?d. ?d rdfs:label ?dl FILTER(lang(?dl)='en')} "
                     "OPTIONAL{?item wdt:P272 ?s. ?s rdfs:label ?sl FILTER(lang(?sl)='en')} "
                     "OPTIONAL{?item wdt:P449 ?n. ?n rdfs:label ?nl FILTER(lang(?nl)='en')} } GROUP BY ?imdb ?item")
            try:
                data = self._get_json(WIKIDATA_SPARQL, {"format": "json", "query": query},
                                      headers={"Accept": "application/sparql-results+json"})
                for b in data["results"]["bindings"]:
                    info.setdefault(b["imdb"]["value"], {k: v["value"] for k, v in b.items()})
            except Exception as exc:
                self.notes.append(f"Wikidata lookup failed ({exc.__class__.__name__}).")
                break
        titles, coverage = [], []
        for r in listed:
            title = clean(r.get("title"))
            iid = clean(r.get("imdb")) if re.fullmatch(r"tt\d+", clean(r.get("imdb"))) else ""
            d = to_date(r.get("date"))
            lang = clean(r.get("lang")).title() or "Unspecified"
            ctype = clean(r.get("ctype")).lower()
            cat = "Movie" if (not ctype or "movie" in ctype or "film" in ctype) else "TV Show"
            b = info.get(iid, {})
            countries = (b.get("co") or "").split("/")
            orig_langs = [x.replace(" language", "") for x in (b.get("lang") or "").split("/") if x]
            origin = "Indian" if ("IN" in countries or lang.lower().split()[0] in INDIAN_LOWER) else "International"
            note = []
            language = lang
            if orig_langs and lang.lower().split()[0] not in " ".join(orig_langs).lower():
                language = f"{lang} (original: {', '.join(orig_langs)})"
                note.append(f"{lang} looks like a dub; original {', '.join(orig_langs)}")
            qid = (b.get("item") or "").rsplit("/", 1)[-1]
            urls = ([f"https://www.wikidata.org/wiki/{qid}"] if qid else []) + ([f"https://www.imdb.com/title/{iid}/"] if iid else [])
            srcs = ["Wikidata " + qid] if qid else []
            if iid:
                srcs.append("IMDb")
            srcs.append("Uploaded title list")
            if not qid:
                note.append("Not on Wikidata – check Sacnilk / JustWatch India by title")
            sd = d.isoformat() if d else None
            titles.append(Title(
                title=title, imdb_id=iid, category=cat, language=language, director=b.get("dir", ""),
                studio=b.get("studio", ""), network=b.get("net", ""), origin=origin,
                window=clean(r.get("window")), release_date=d.strftime("%d %b %Y") if d else "TBA",
                sort_date=sd, status=status_for(sd, today), from_list=True, source="; ".join(srcs),
                type_platform="Film – OTT" if cat == "Movie" else "TV series – OTT", source_urls=urls))
            coverage.append({"Title": title, "IMDb ID": iid or "—", "Language": lang, "Year": str(d.year) if d else "",
                             "Origin": origin, "Window": clean(r.get("window")) or "—", "Wikidata": qid or "—",
                             "Sources found": "; ".join(srcs), "Notes": "; ".join(note)})
        return titles, coverage

    def _youtube(self, titles, scope, today, api_key, progress) -> None:
        if scope == "none":
            return
        if scope == "recent":
            cutoff = (today - timedelta(days=90)).isoformat()
            targets = [t for t in titles if t.from_list or not t.sort_date or t.sort_date >= cutoff]
        else:
            targets = list(titles)
        if len(targets) > YOUTUBE_MAX_TITLES:
            self.notes.append(f"YouTube lookup capped at {YOUTUBE_MAX_TITLES} of {len(targets)} titles (upcoming/list titles first).")
            targets = sorted(targets, key=lambda t: (not t.from_list, -(int(t.sort_date.replace('-', '')) if t.sort_date else 99999999)))[:YOUTUBE_MAX_TITLES]
        if not targets:
            return
        state = {"done": 0, "blocked": False, "api_used": 0, "found": 0}

        def search_page(q: str) -> list[dict[str, Any]] | None:
            try:
                return parse_youtube_results(self.http.get_text(YT_RESULTS + quote_plus(q)))
            except Exception:
                return None

        studio_cache: dict[str, str] = {}

        def studio_channel(studio: str) -> str:
            key = norm(studio)
            if key in studio_cache:
                return studio_cache[key]
            url = ""
            if not state["blocked"] and channel_core(studio) not in _GENERIC:
                try:
                    chans = parse_youtube_channel_results(
                        self.http.get_text(YT_RESULTS + quote_plus(studio) + "&sp=EgIQAg%3D%3D"))
                    url = pick_studio_channel(studio, chans or [])
                except Exception:
                    url = ""
            studio_cache[key] = url
            return url

        def one(t: Title) -> None:
            lang = t.language.split(" (")[0].split()[0] if t.language and t.language != "Unspecified" else ""
            base = re.sub(r"\s*\(.*?\)\s*", " ", t.title).strip()
            year = t.sort_date[:4] if t.sort_date else ""
            net = (t.network or "").split(",")[0].strip()
            queries = [f"{base} {year} official trailer", f"{base} {lang} trailer {year}",
                       f"{base} {net if t.category == 'TV Show' and net else 'teaser'} {year}"]
            queries = [re.sub(r"\s+", " ", q).strip() for q in queries]
            results = [None if state["blocked"] else search_page(q) for q in queries]
            if any(r is None for r in results):
                state["blocked"] = True
                if api_key and state["api_used"] < YOUTUBE_API_MAX_TITLES:
                    state["api_used"] += 1
                    results = [self._youtube_api_search(q, api_key) for q in queries]
                else:
                    return
            trailer, chans = pick_official_channels(t, [r or [] for r in results], today)
            studios = list(dict.fromkeys([n.strip() for n in re.split(r",\s*", t.studio or "") if n.strip()]
                                         + t.credits.get("studio", [])))[:4]
            for studio in studios:
                url = studio_channel(studio)
                if url and url not in [u for u, _ in chans]:
                    chans.append((url, "studio_channel"))
            if chans:
                t.youtube_channels = [u for u, _ in chans]
                t.youtube_roles = [r for _, r in chans]
                state["found"] += 1
            if trailer:
                t.trailer_url = trailer
            state["done"] += 1
            if state["done"] % 10 == 0:
                progress(70 + int(18 * state["done"] / len(targets)), f"YouTube: {state['done']}/{len(targets)} titles")

        progress(70, f"YouTube: official channels for {len(targets)} titles")
        with ThreadPoolExecutor(max_workers=YOUTUBE_WORKERS) as pool:
            list(pool.map(one, targets))
        self.notes.append(f"YouTube: official channels found for {state['found']} of {len(targets)} titles "
                          f"(credited trailer channels + {sum(1 for v in studio_cache.values() if v)} studio channels); "
                          "titles without a credited channel are left blank rather than guessed.")
        if state["blocked"]:
            self.notes.append("YouTube's results page was blocked for the server"
                              + (f"; used the YouTube Data API for {state['api_used']} titles (quota-limited)." if state["api_used"]
                                 else "; add YOUTUBE_API_KEY (or paste a key) for an API fallback."))

    def _youtube_api_search(self, q: str, api_key: str) -> list[dict[str, Any]]:
        try:
            data = self._get_json(YT_API_SEARCH, {"part": "snippet", "q": q, "type": "video", "maxResults": 15, "key": api_key})
        except Exception:
            return []
        items = data.get("items", [])
        ids = list({i["snippet"]["channelId"] for i in items})
        handles: dict[str, str] = {}
        if ids:
            try:
                ch = self._get_json(YT_API_CHANNELS, {"part": "snippet", "id": ",".join(ids[:50]), "key": api_key})
                handles = {c["id"]: c["snippet"].get("customUrl", "") for c in ch.get("items", [])}
            except Exception:
                pass
        out = []
        for i in items:
            cid = i["snippet"]["channelId"]
            h = handles.get(cid, "")
            pub = i["snippet"].get("publishedAt", "")[:4]
            out.append({"id": i["id"].get("videoId", ""), "t": i["snippet"].get("title", ""),
                        "ch": f"https://www.youtube.com/{h}" if h.startswith("@") else f"https://www.youtube.com/channel/{cid}",
                        "ver": False, "name": i["snippet"].get("channelTitle", ""),
                        "pub": f"{date.today().year - int(pub)} years ago" if pub.isdigit() else ""})
        return out

    def _cbfc(self, titles, today, progress) -> None:
        targets = [t for t in titles if t.category == "Movie" and t.origin == "Indian"
                   and (t.from_list or not t.sort_date or t.sort_date >= (today - timedelta(days=120)).isoformat())]
        if not targets:
            return
        progress(88, "CBFC website: locating Search Film form")
        form = self._cbfc_form()
        if form is None:
            self.notes.append("CBFC's website did not serve its Search Film form to the server (it blocks bots); "
                              "certificates were not checked this run – use the skill's browser step for CBFC.")
            return
        action, method, fields, box = form
        found = 0
        for i, t in enumerate(targets[:CBFC_MAX_TITLES]):
            data = dict(fields)
            data[box] = t.title
            try:
                if method == "post":
                    r = self.http.session.post(action, data=data, timeout=self.http.timeout_seconds)
                else:
                    r = self.http.session.get(action, params=data, timeout=self.http.timeout_seconds)
                recs = parse_cbfc_results(r.text)
            except Exception:
                continue
            rec = next((x for x in recs if norm(x.get("film")) == norm(t.title)), None)
            if rec:
                found += 1
                t.certificate_type = t.certificate_type or rec.get("category", "")
                t.certificate_no = t.certificate_no or rec.get("certno", "")
                t.duration = t.duration or rec.get("length", "")
                t.cbfc_office = t.cbfc_office or rec.get("office", "")
                if t.status in ("Confirmed", "Tentative", "Date TBA"):
                    t.status = "Certified (CBFC)"
                t.source += f"; CBFC website, checked {today:%d %b %Y}"
                t.add_url(action)
            if i % 10 == 0:
                progress(88 + int(8 * i / len(targets)), f"CBFC website: {i}/{len(targets)} titles")
            time.sleep(0.3)
        self.notes.append(f"CBFC website: {found} of {min(len(targets), CBFC_MAX_TITLES)} recent/upcoming films matched a certificate.")

    def _cbfc_form(self):
        for url in CBFC_SEARCH_CANDIDATES + [CBFC_HOME]:
            try:
                html = self.http.get_text(url)
            except Exception:
                continue
            soup = BeautifulSoup(html, "html.parser")
            for form in soup.find_all("form"):
                box = form.find("input", attrs={"type": lambda v: v in (None, "text", "search")})
                if not box or not box.get("name"):
                    continue
                fields = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
                for s in form.find_all("select"):
                    if s.get("name"):
                        opt = s.find("option", selected=True) or s.find("option")
                        fields[s["name"]] = opt.get("value", "") if opt else ""
                action = form.get("action") or url
                if not action.startswith("http"):
                    action = re.sub(r"[^/]*$", "", url) + action.lstrip("/") if not action.startswith("/") else CBFC_HOME.rstrip("/") + action
                return action, (form.get("method") or "get").lower(), fields, box["name"]
        return None
