from __future__ import annotations

from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable
import csv
import gzip
import os
import re
import sqlite3
import time

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from app.models import IMDB_COLUMNS, TV_SEASON_EPISODE_COLUMNS, utc_now_iso
from app.services.http_client import HttpClient
from app.services.metacritic import TV_PREMIERE_URL, MetacriticParser, metacritic_url_for_row


ProgressCallback = Callable[[int, str], None]

IMDB_DATASETS = {
    "basics": "https://datasets.imdbws.com/title.basics.tsv.gz",
    "episode": "https://datasets.imdbws.com/title.episode.tsv.gz",
    "names": "https://datasets.imdbws.com/name.basics.tsv.gz",
}

IMDB_INDEX_VERSION = "2"
TITLE_TYPES_FOR_LOOKUP = {"movie", "tvMovie", "tvSeries", "tvMiniSeries", "videoGame"}
TT_CODE_COLUMN = "ttcode"
TITLE_COLUMN_CANDIDATES = ("Title Name", "Input Title", "title", "Title")
SERIES_EPISODE_COUNT_OVERRIDES = {
    ("my two cents", 1, 2026): 6,
    ("due spicci", 1, 2026): 6,
}


class IMDbUnavailableError(RuntimeError):
    """Raised when IMDb dataset features are disabled for this deployment.

    The IMDb tools need a multi-GB local dataset/index that cannot be hosted on
    a free tier. Setting the env var IMDB_ENABLED=false turns these features off
    so the lightweight tools (Excel Validator, YouTube Release Verifier, etc.)
    keep working without ever trying to download or build the large index.
    """


def _imdb_features_enabled() -> bool:
    return os.getenv("IMDB_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


class IMDbEnrichmentService:
    def __init__(
        self,
        cache_dir: Path,
        max_age_days: int,
        http_client: HttpClient,
        tmdb_api_key: str = "",
        tmdb_read_access_token: str = "",
    ) -> None:
        self.cache_dir = cache_dir
        self.max_age_days = max_age_days
        self.http_client = http_client
        self.enabled = _imdb_features_enabled()
        self.tmdb_api_key = tmdb_api_key
        self.tmdb_read_access_token = tmdb_read_access_token
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "imdb_series_index.sqlite3"
        self._metacritic_season_cache: dict[str, tuple[date | None, date | None, int | None]] = {}
        self._imdb_episode_count_cache: dict[tuple[str, int], int] = {}
        self._tmdb_tv_cache: dict[tuple[str, str], dict[str, str | int] | None] = {}

    @property
    def tmdb_enabled(self) -> bool:
        return bool(self.tmdb_api_key or self.tmdb_read_access_token)

    def fetch_snapshot(
        self,
        metacritic: MetacriticParser,
        today: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict:
        today = today or date.today()
        start_date = start_date or today - timedelta(days=2)
        end_date = end_date or today
        if end_date < start_date:
            raise ValueError("End date must be the same as or later than the start date.")
        if progress:
            progress(6, "Fetching Metacritic TV data for the IMDb scan window")
        html = metacritic.http_client.get_text(TV_PREMIERE_URL)
        tv_rows = metacritic.parse_tv_calendar(html, today=today)
        window_rows = [
            row
            for row in tv_rows
            if start_date <= date.fromisoformat(row["Release Date"]) <= end_date
        ]
        filtered_rows = [row for row in window_rows if self._is_imdb_eligible(row)]
        if progress:
            progress(20, "Preparing local IMDb datasets")
        self.ensure_index(progress=progress)
        if progress:
            progress(76, "Matching eligible TV titles to IMDb series records")

        enriched: list[dict[str, str | int]] = []
        matched = 0
        manual_review = 0
        with sqlite3.connect(self.index_path) as connection:
            connection.row_factory = sqlite3.Row
            for row in filtered_rows:
                match = self._match_series(connection, row)
                if match["ttcode"]:
                    matched += 1
                else:
                    manual_review += 1
                enriched.append(match)

        summary = (
            f"Scanned Metacritic rows from {start_date.isoformat()} through {end_date.isoformat()}. "
            f"Filtered rows: {len(filtered_rows)}. Matched to IMDb: {matched}. "
            f"Manual review needed: {manual_review}. Release date comes from Metacritic because "
            "IMDb title.basics mainly provides start year."
        )
        return {
            "tracker_type": "imdb",
            "title": "IMDb-Enriched TV Series Snapshot",
            "created_at": utc_now_iso(),
            "source_url": TV_PREMIERE_URL,
            "summary": summary,
            "sections": [
                {
                    "key": "imdb",
                    "title": "IMDb-Enriched TV Series Snapshot",
                    "columns": IMDB_COLUMNS,
                    "rows": enriched,
                    "row_count": len(enriched),
                    "supports_google": False,
                }
            ],
        }

    def fetch_season_episode_snapshot(
        self,
        metacritic: MetacriticParser,
        today: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict:
        today = today or date.today()
        start_date = start_date or today - timedelta(days=7)
        end_date = end_date or today
        if end_date < start_date:
            raise ValueError("End date must be the same as or later than the start date.")
        if progress:
            progress(6, "Fetching Metacritic TV rows for the season and episode window")
        html = metacritic.http_client.get_text(TV_PREMIERE_URL)
        tv_rows = metacritic.parse_tv_calendar(html, today=today)
        window_rows = [
            row
            for row in tv_rows
            if start_date <= date.fromisoformat(row["Release Date"]) <= end_date
        ]
        filtered_rows = [row for row in window_rows if self._is_imdb_eligible(row)]

        if progress:
            progress(20, "Preparing local IMDb title and episode index")
        self.ensure_index(progress=progress)

        if progress:
            progress(76, "Matching Metacritic TV rows to IMDb seasons and episode counts")
        output_rows: list[dict[str, str | int]] = []
        matched = 0
        with sqlite3.connect(self.index_path) as connection:
            connection.row_factory = sqlite3.Row
            total = max(len(filtered_rows), 1)
            for index, row in enumerate(filtered_rows, start=1):
                if progress:
                    progress(76 + int((index / total) * 14), f"Reviewing season data for {row['Title Name']}")
                output_row = self._season_episode_row(connection, row)
                if output_row.get("imdb_id"):
                    matched += 1
                output_rows.append(output_row)

        summary = (
            f"Scanned Metacritic TV rows from {start_date.isoformat()} through {end_date.isoformat()}. "
            f"Filtered rows: {len(filtered_rows)}. Matched to IMDb: {matched}. "
            "Release date and season start date use the season premiere when the Metacritic row links to a "
            "season or episode page; otherwise they use the calendar row date. latest_season_episode_count "
            "counts only the matched latest season, not all episodes across the full series. The end date uses "
            "a later dated episode/finale from Metacritic details when available; otherwise it defaults to "
            "30 days after the start date."
        )
        return {
            "tracker_type": "tv_seasons",
            "title": "TV Seasons and Episodes Snapshot",
            "created_at": utc_now_iso(),
            "source_url": TV_PREMIERE_URL,
            "summary": summary,
            "sections": [
                {
                    "key": "tv_seasons",
                    "title": "TV Seasons and Episodes",
                    "columns": TV_SEASON_EPISODE_COLUMNS,
                    "rows": output_rows,
                    "row_count": len(output_rows),
                    "supports_google": False,
                }
            ],
        }

    def fetch_snapshot_via_tmdb(
        self,
        metacritic: MetacriticParser,
        today: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict:
        """IMDb-Enriched TV Series snapshot built from the TMDB API.

        This is the free-tier path: it produces the same columns as
        ``fetch_snapshot`` (ttcode, Total Seasons, Total Episodes) but sources
        them live from TMDB instead of the multi-GB local IMDb index, so it runs
        on a hosted free instance with only a TMDB API key configured.
        """
        if not self.tmdb_enabled:
            raise IMDbUnavailableError(
                "TMDB enrichment needs a TMDB API key. Set TMDB_API_KEY (or "
                "TMDB_READ_ACCESS_TOKEN) to enable the IMDb-Enriched Series tool "
                "on this deployment, or run the full local install."
            )
        today = today or date.today()
        start_date = start_date or today - timedelta(days=2)
        end_date = end_date or today
        if end_date < start_date:
            raise ValueError("End date must be the same as or later than the start date.")
        if progress:
            progress(8, "Fetching Metacritic TV data for the enrichment window")
        html = metacritic.http_client.get_text(TV_PREMIERE_URL)
        tv_rows = metacritic.parse_tv_calendar(html, today=today)
        window_rows = [
            row
            for row in tv_rows
            if start_date <= date.fromisoformat(row["Release Date"]) <= end_date
        ]
        filtered_rows = [row for row in window_rows if self._is_imdb_eligible(row)]

        enriched: list[dict[str, str | int]] = []
        matched = 0
        total = max(len(filtered_rows), 1)
        for index, row in enumerate(filtered_rows, start=1):
            if progress:
                progress(20 + int((index / total) * 70), f"Looking up {row.get('Title Name', '')} on TMDB")
            result = self._tmdb_tv_lookup(row)
            if result and result.get("ttcode"):
                matched += 1
            enriched.append(
                self._output_row(
                    row,
                    result.get("ttcode", "") if result else "",
                    result.get("total_seasons", "") if result else "",
                    result.get("total_episodes", "") if result else "",
                    result.get("note", "No confident TMDB match.") if result else "No confident TMDB match.",
                )
            )

        summary = (
            f"Scanned Metacritic TV rows from {start_date.isoformat()} through {end_date.isoformat()}. "
            f"Filtered rows: {len(filtered_rows)}. Matched on TMDB: {matched}. "
            "Total Seasons, Total Episodes, and the IMDb ttcode come from the TMDB API "
            "(free-tier source; no local IMDb index). Release date comes from Metacritic. "
            "A few obscure or ambiguous titles may be blank when TMDB has no confident match."
        )
        return {
            "tracker_type": "imdb",
            "title": "IMDb-Enriched TV Series Snapshot",
            "created_at": utc_now_iso(),
            "source_url": TV_PREMIERE_URL,
            "summary": summary,
            "sections": [
                {
                    "key": "imdb",
                    "title": "IMDb-Enriched TV Series Snapshot",
                    "columns": IMDB_COLUMNS,
                    "rows": enriched,
                    "row_count": len(enriched),
                    "supports_google": False,
                }
            ],
        }

    def _tmdb_tv_lookup(self, metacritic_row: dict[str, str]) -> dict[str, str | int] | None:
        title = (metacritic_row.get("Title Name", "") or "").strip()
        if not title:
            return None
        release_date_raw = metacritic_row.get("Release Date", "") or ""
        release_year = release_date_raw[:4]
        cache_key = (normalize_title(title), release_year)
        if cache_key in self._tmdb_tv_cache:
            return self._tmdb_tv_cache[cache_key]

        result: dict[str, str | int] | None = None
        try:
            search_params: dict[str, Any] = {
                "query": title,
                "include_adult": "false",
                "language": "en-US",
            }
            if release_year.isdigit():
                search_params["first_air_date_year"] = release_year
            if self.tmdb_api_key:
                search_params["api_key"] = self.tmdb_api_key
            results = self._tmdb_get_json(
                "https://api.themoviedb.org/3/search/tv", search_params
            ).get("results", [])[:6]

            normalized_target = normalize_title(title)
            best_score = -1.0
            best: dict[str, str | int] | None = None
            for candidate in results:
                tmdb_id = candidate.get("id")
                if not tmdb_id:
                    continue
                detail_params: dict[str, Any] = {
                    "append_to_response": "external_ids",
                    "language": "en-US",
                }
                if self.tmdb_api_key:
                    detail_params["api_key"] = self.tmdb_api_key
                details = self._tmdb_get_json(
                    f"https://api.themoviedb.org/3/tv/{tmdb_id}", detail_params
                )
                candidate_name = details.get("name") or details.get("original_name") or ""
                score = SequenceMatcher(None, normalized_target, normalize_title(candidate_name)).ratio() * 100
                first_air = (details.get("first_air_date") or "")[:4]
                if release_year.isdigit() and first_air.isdigit():
                    if first_air == release_year:
                        score += 25
                    elif abs(int(first_air) - int(release_year)) <= 1:
                        score += 10
                if score <= best_score:
                    continue
                external_ids = details.get("external_ids") or {}
                best_score = score
                best = {
                    "ttcode": external_ids.get("imdb_id") or "",
                    "total_seasons": details.get("number_of_seasons") or "",
                    "total_episodes": details.get("number_of_episodes") or "",
                    "note": (
                        f"Matched via TMDB (\"{candidate_name}\")."
                        if score >= 60
                        else f"Low-confidence TMDB match (\"{candidate_name}\")."
                    ),
                }
            # Require a reasonable title similarity before accepting the match.
            if best is not None and best_score >= 45:
                result = best
        except Exception:
            result = None

        self._tmdb_tv_cache[cache_key] = result
        return result

    def _tmdb_get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.tmdb_read_access_token:
            headers["Authorization"] = f"Bearer {self.tmdb_read_access_token}"
        response = self.http_client.session.get(
            url,
            params=params,
            headers=headers,
            timeout=self.http_client.timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response.json()

    def ensure_index(self, progress: ProgressCallback | None = None) -> None:
        if not self.enabled:
            raise IMDbUnavailableError(
                "IMDb dataset tools are not available on this deployment. "
                "They require a large local dataset and run only on the full local install."
            )
        basics_path = self._ensure_dataset("basics", progress=progress, progress_base=24)
        episode_path = self._ensure_dataset("episode", progress=progress, progress_base=34)
        names_path = self._ensure_dataset("names", progress=progress, progress_base=38)
        if self._index_is_current([basics_path, episode_path, names_path]):
            if progress:
                progress(62, "Using the existing local IMDb SQLite index")
            return
        self._build_index(basics_path, episode_path, names_path, progress=progress)

    def lookup_title(
        self,
        title: str,
        category: str = "",
        release_date: date | None = None,
        preferred_types: Iterable[str] | None = None,
    ) -> dict[str, str] | None:
        if not self.enabled:
            return None
        self.ensure_index()
        with sqlite3.connect(self.index_path) as connection:
            connection.row_factory = sqlite3.Row
            return self._lookup_title_with_connection(
                connection,
                title,
                category=category,
                release_date=release_date,
                preferred_types=preferred_types,
            )

    def lookup_name(self, name: str, profession_hint: str = "") -> dict[str, str] | None:
        if not self.enabled:
            return None
        normalized = normalize_title(name)
        if not normalized:
            return None
        self.ensure_index()
        with sqlite3.connect(self.index_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT *
                FROM names
                WHERE normalized_name = ?
                LIMIT 30
                """,
                (normalized,),
            ).fetchall()
        if not rows:
            return None
        best = max(rows, key=lambda row: _name_lookup_score(row, profession_hint))
        known_for_titles = _known_for_title_names(connection, best["known_for_titles"] or "")
        return {
            "imdb_id": best["nconst"],
            "name": best["primary_name"],
            "birth_year": best["birth_year"] or "",
            "death_year": best["death_year"] or "",
            "primary_profession": best["primary_profession"] or "",
            "known_for_titles": best["known_for_titles"] or "",
            "known_for_title_names": known_for_titles,
        }

    def add_ttcodes_to_snapshot(
        self,
        snapshot: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        targets: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
        for section in snapshot.get("sections", []):
            if not isinstance(section, dict):
                continue
            title_column = _title_column_for_section(section)
            if not title_column:
                continue
            columns = section.get("columns")
            if isinstance(columns, list):
                _ensure_ttcode_column(columns, title_column)
            for row in section.get("rows", []):
                if not isinstance(row, dict):
                    continue
                title = str(row.get(title_column, "")).strip()
                if title:
                    targets.append((section, row, title_column, title))

        if not targets:
            return snapshot

        if not self.enabled:
            # IMDb enrichment is turned off on this deployment (no local index).
            # Return the scraped calendar as-is — the ttcode column is already
            # present (added above) but left blank — instead of trying to build
            # the multi-GB index, which a free host can't hold.
            return snapshot

        if progress:
            progress(95, "Preparing local IMDb title/name index for ttcode lookup")
        self.ensure_index(progress=_scaled_progress(progress, 95, 97) if progress else None)

        matched = 0
        total = len(targets)
        with sqlite3.connect(self.index_path) as connection:
            connection.row_factory = sqlite3.Row
            for index, (section, row, title_column, title) in enumerate(targets, start=1):
                existing_ttcode = _extract_existing_ttcode(row)
                if existing_ttcode:
                    row[TT_CODE_COLUMN] = existing_ttcode
                    matched += 1
                else:
                    match = self._lookup_title_with_connection(
                        connection,
                        title,
                        category=_category_for_ttcode_lookup(snapshot.get("tracker_type", ""), section, row),
                        release_date=_release_date_from_row(row),
                    )
                    row[TT_CODE_COLUMN] = match["imdb_id"] if match else ""
                    if match:
                        matched += 1

                if progress and (index == total or index == 1 or index % 25 == 0):
                    percent = 97 + int((index / total) * 2)
                    progress(min(percent, 99), f"Looking up IMDb ttcode for {title}")

        _append_ttcode_summary(snapshot, matched, total)
        return snapshot

    def _lookup_title_with_connection(
        self,
        connection: sqlite3.Connection,
        title: str,
        category: str = "",
        release_date: date | None = None,
        preferred_types: Iterable[str] | None = None,
    ) -> dict[str, str] | None:
        normalized = normalize_title(title)
        if not normalized:
            return None
        preferred = list(preferred_types or _preferred_title_types(category))
        rows = connection.execute(
            """
            SELECT *
            FROM titles
            WHERE normalized_primary = ? OR normalized_original = ?
            LIMIT 30
            """,
            (normalized, normalized),
        ).fetchall()
        if not rows:
            return None
        best = max(rows, key=lambda row: _title_lookup_score(row, normalized, preferred, release_date))
        return {
            "imdb_id": best["tconst"],
            "title": best["primary_title"],
            "original_title": best["original_title"],
            "title_type": best["title_type"],
            "start_year": best["start_year"] or "",
            "genres": best["genres"] or "",
        }

    def _ensure_dataset(
        self, dataset_key: str, progress: ProgressCallback | None, progress_base: int
    ) -> Path:
        url = IMDB_DATASETS[dataset_key]
        destination = self.cache_dir / url.rsplit("/", 1)[-1]
        if destination.exists() and not self._is_stale(destination):
            if progress:
                progress(progress_base, f"Using cached IMDb dataset {destination.name}")
            return destination
        if progress:
            progress(progress_base, f"Downloading IMDb dataset {destination.name}")
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        self.http_client.download(url, temporary)
        os.replace(temporary, destination)
        return destination

    def _build_index(
        self,
        basics_path: Path,
        episode_path: Path,
        names_path: Path,
        progress: ProgressCallback | None = None,
    ) -> None:
        if progress:
            progress(42, "Building local IMDb title and name index")
        temporary = self.cache_dir / f"{self.index_path.stem}.{os.getpid()}.{int(time.time() * 1000)}.tmp.sqlite3"
        if temporary.exists():
            temporary.unlink()
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute(
                """
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE titles (
                    tconst TEXT PRIMARY KEY,
                    title_type TEXT NOT NULL,
                    primary_title TEXT NOT NULL,
                    original_title TEXT NOT NULL,
                    start_year TEXT,
                    end_year TEXT,
                    genres TEXT,
                    normalized_primary TEXT NOT NULL,
                    normalized_original TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE episodes (
                    parent_tconst TEXT NOT NULL,
                    season_number TEXT,
                    episode_number TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE names (
                    nconst TEXT PRIMARY KEY,
                    primary_name TEXT NOT NULL,
                    birth_year TEXT,
                    death_year TEXT,
                    primary_profession TEXT,
                    known_for_titles TEXT,
                    normalized_name TEXT NOT NULL
                )
                """
            )

            parent_ids = self._load_title_basics(connection, basics_path)
            if progress:
                progress(54, "Building local IMDb episode counts")
            self._load_episode_counts(connection, episode_path, parent_ids)
            if progress:
                progress(60, "Loading local IMDb name basics")
            self._load_name_basics(connection, names_path)
            if progress:
                progress(66, "Indexing IMDb lookup tables")
            connection.execute("CREATE INDEX idx_titles_primary ON titles(normalized_primary)")
            connection.execute("CREATE INDEX idx_titles_original ON titles(normalized_original)")
            connection.execute("CREATE INDEX idx_titles_type_year ON titles(title_type, start_year)")
            connection.execute("CREATE INDEX idx_episodes_parent ON episodes(parent_tconst)")
            connection.execute("CREATE INDEX idx_names_normalized ON names(normalized_name)")
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("index_version", IMDB_INDEX_VERSION),
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary, self.index_path)

    def _load_title_basics(self, connection: sqlite3.Connection, basics_path: Path) -> set[str]:
        parent_ids: set[str] = set()
        batch: list[tuple[str, str, str, str, str, str, str, str, str]] = []
        with gzip.open(basics_path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for record in reader:
                title_type = record.get("titleType", "")
                if title_type not in TITLE_TYPES_FOR_LOOKUP:
                    continue
                if _clean_imdb_value(record.get("isAdult")) == "1":
                    continue
                tconst = record["tconst"]
                primary = _clean_imdb_value(record.get("primaryTitle"))
                original = _clean_imdb_value(record.get("originalTitle"))
                start_year = _clean_imdb_value(record.get("startYear"))
                end_year = _clean_imdb_value(record.get("endYear"))
                genres = _clean_imdb_value(record.get("genres"))
                if title_type in {"tvSeries", "tvMiniSeries"}:
                    parent_ids.add(tconst)
                batch.append(
                    (
                        tconst,
                        title_type,
                        primary,
                        original,
                        start_year,
                        end_year,
                        genres,
                        normalize_title(primary),
                        normalize_title(original),
                    )
                )
                if len(batch) >= 5000:
                    connection.executemany("INSERT INTO titles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()
        if batch:
            connection.executemany("INSERT INTO titles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
        connection.commit()
        return parent_ids

    def _load_name_basics(self, connection: sqlite3.Connection, names_path: Path) -> None:
        batch: list[tuple[str, str, str, str, str, str, str]] = []
        with gzip.open(names_path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for record in reader:
                primary_name = _clean_imdb_value(record.get("primaryName"))
                if not primary_name:
                    continue
                batch.append(
                    (
                        record["nconst"],
                        primary_name,
                        _clean_imdb_value(record.get("birthYear")),
                        _clean_imdb_value(record.get("deathYear")),
                        _clean_imdb_value(record.get("primaryProfession")),
                        _clean_imdb_value(record.get("knownForTitles")),
                        normalize_title(primary_name),
                    )
                )
                if len(batch) >= 5000:
                    connection.executemany("INSERT INTO names VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()
        if batch:
            connection.executemany("INSERT INTO names VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
        connection.commit()

    def _load_episode_counts(
        self,
        connection: sqlite3.Connection,
        episode_path: Path,
        parent_ids: set[str],
    ) -> None:
        batch: list[tuple[str, str, str]] = []
        with gzip.open(episode_path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for record in reader:
                parent = record.get("parentTconst", "")
                if parent not in parent_ids:
                    continue
                batch.append(
                    (
                        parent,
                        _clean_imdb_value(record.get("seasonNumber")),
                        _clean_imdb_value(record.get("episodeNumber")),
                    )
                )
                if len(batch) >= 10000:
                    connection.executemany("INSERT INTO episodes VALUES (?, ?, ?)", batch)
                    batch.clear()
        if batch:
            connection.executemany("INSERT INTO episodes VALUES (?, ?, ?)", batch)
        connection.commit()

    def _match_series(self, connection: sqlite3.Connection, metacritic_row: dict[str, str]) -> dict[str, str | int]:
        title = metacritic_row["Title Name"]
        normalized = normalize_title(title)
        candidates = self._series_candidates(connection, normalized)

        if not candidates:
            return self._output_row(metacritic_row, "", "", "", "No exact normalized IMDb series match.")

        chosen = self._best_series_candidate(candidates, metacritic_row)
        expected_type = "tvMiniSeries" if metacritic_row.get("Release Type") == "Limited Series" else "tvSeries"
        counts = connection.execute(
            """
            SELECT
                COUNT(*) AS episode_count,
                COUNT(DISTINCT NULLIF(season_number, '')) AS season_count
            FROM episodes
            WHERE parent_tconst = ?
            """,
            (chosen["tconst"],),
        ).fetchone()
        release_year = metacritic_row["Release Date"][:4]
        notes: list[str] = ["Exact normalized title match."]
        if chosen["title_type"] != expected_type:
            notes.append(f"Matched {chosen['title_type']} although {expected_type} was preferred.")
        if chosen["start_year"] and chosen["start_year"] != release_year:
            notes.append(f"IMDb start year {chosen['start_year']} differs from Metacritic release year {release_year}.")
        if not chosen["start_year"]:
            notes.append("IMDb start year unavailable.")
        return self._output_row(
            metacritic_row,
            chosen["tconst"],
            counts["season_count"] or "",
            counts["episode_count"] or "",
            " ".join(notes),
        )

    def _season_episode_row(
        self,
        connection: sqlite3.Connection,
        metacritic_row: dict[str, str],
    ) -> dict[str, str | int]:
        calendar_date = date.fromisoformat(metacritic_row["Release Date"])
        preferred_season = _season_from_metacritic_url(metacritic_row.get("Source URL", ""))
        source_episode_number = _episode_from_metacritic_url(metacritic_row.get("Source URL", ""))
        metacritic_start_date, metacritic_end_date, metacritic_episode_count = self._metacritic_season_context(
            metacritic_row,
            calendar_date,
        )
        candidate = self._select_series_candidate(connection, metacritic_row)
        if not candidate:
            season_number = preferred_season
            if season_number:
                metacritic_start_date, metacritic_end_date, metacritic_episode_count = _merge_season_context(
                    (metacritic_start_date, metacritic_end_date, metacritic_episode_count),
                    self._metacritic_season_context_for_url(
                        _metacritic_season_url_for_number(metacritic_row, season_number),
                        calendar_date,
                    ),
                )
            season_start_date = metacritic_start_date or calendar_date
            episode_count = max(
                metacritic_episode_count or 0,
                source_episode_number or 0,
                _episode_count_from_details(metacritic_row, season_number),
                _known_episode_count_override(metacritic_row, season_number),
            )
            return {
                "release_date": _display_date(season_start_date),
                "title": metacritic_row.get("Title Name", ""),
                "daypart": metacritic_row.get("Daypart", ""),
                "program_type": metacritic_row.get("Program Type", ""),
                "language_type": metacritic_row.get("Language Type", ""),
                "network_distributor": metacritic_row.get("Availability / Network", ""),
                "imdb_id": "",
                "metacritic_url": metacritic_url_for_row(metacritic_row, default_media_type="tv"),
                "latest_season_number": season_number or "",
                "latest_season_episode_count": episode_count or "",
                "latest_season_start_date": _display_date(season_start_date),
                "latest_season_end_date": _display_date(
                    metacritic_end_date or _infer_season_end_date(season_start_date, metacritic_row)
                ),
            }

        season_number, episode_count = self._latest_season_episode_stats(
            connection,
            candidate["tconst"],
            preferred_season,
        )
        if preferred_season and not season_number:
            season_number = preferred_season
        if season_number:
            metacritic_start_date, metacritic_end_date, metacritic_episode_count = _merge_season_context(
                (metacritic_start_date, metacritic_end_date, metacritic_episode_count),
                self._metacritic_season_context_for_url(
                    _metacritic_season_url_for_number(metacritic_row, season_number),
                    calendar_date,
                ),
            )
        season_start_date = metacritic_start_date or calendar_date
        imdb_web_episode_count = (
            self._imdb_web_episode_count(candidate["tconst"], season_number) if season_number and episode_count <= 1 else 0
        )
        episode_count = max(
            episode_count,
            metacritic_episode_count or 0,
            source_episode_number or 0,
            _episode_count_from_details(metacritic_row, season_number),
            imdb_web_episode_count,
            _known_episode_count_override(metacritic_row, season_number),
        )
        season_end_date = metacritic_end_date or _infer_season_end_date(season_start_date, metacritic_row)
        return {
            "release_date": _display_date(season_start_date),
            "title": metacritic_row.get("Title Name", ""),
            "daypart": metacritic_row.get("Daypart", ""),
            "program_type": metacritic_row.get("Program Type", ""),
            "language_type": metacritic_row.get("Language Type", ""),
            "network_distributor": metacritic_row.get("Availability / Network", ""),
            "imdb_id": candidate["tconst"],
            "metacritic_url": metacritic_url_for_row(metacritic_row, default_media_type="tv"),
            "latest_season_number": season_number or "",
            "latest_season_episode_count": episode_count or "",
            "latest_season_start_date": _display_date(season_start_date),
            "latest_season_end_date": _display_date(season_end_date),
        }

    def _select_series_candidate(self, connection: sqlite3.Connection, metacritic_row: dict[str, str]):
        normalized = normalize_title(metacritic_row["Title Name"])
        candidates = self._series_candidates(connection, normalized)
        if not candidates:
            return None
        return self._best_series_candidate(candidates, metacritic_row)

    def _series_candidates(self, connection: sqlite3.Connection, normalized_title_value: str) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT *
            FROM titles
            WHERE (normalized_primary = ? OR normalized_original = ?)
              AND title_type IN ('tvSeries', 'tvMiniSeries')
            LIMIT 20
            """,
            (normalized_title_value, normalized_title_value),
        ).fetchall()

    def _best_series_candidate(
        self,
        candidates: list[sqlite3.Row],
        metacritic_row: dict[str, str],
    ) -> sqlite3.Row:
        normalized = normalize_title(metacritic_row["Title Name"])
        expected_type = "tvMiniSeries" if metacritic_row.get("Release Type") == "Limited Series" else "tvSeries"
        release_date = _parse_iso_date(metacritic_row.get("Release Date", ""))
        return max(
            candidates,
            key=lambda row: _series_lookup_score(row, normalized, expected_type, release_date),
        )

    def _metacritic_season_context(
        self,
        metacritic_row: dict[str, str],
        fallback_start_date: date,
    ) -> tuple[date | None, date | None, int | None]:
        season_url = _metacritic_season_url(metacritic_row.get("Source URL", ""))
        if not season_url:
            return None, None, None
        if season_url in self._metacritic_season_cache:
            return self._metacritic_season_cache[season_url]
        try:
            html = self.http_client.get_text(season_url)
            context = _extract_metacritic_season_context(html, fallback_start_date)
        except Exception:
            context = (None, None, None)
        self._metacritic_season_cache[season_url] = context
        return context

    def _metacritic_season_context_for_url(
        self,
        season_url: str,
        fallback_start_date: date,
    ) -> tuple[date | None, date | None, int | None]:
        if not season_url:
            return None, None, None
        if season_url in self._metacritic_season_cache:
            return self._metacritic_season_cache[season_url]
        try:
            html = self.http_client.get_text(season_url)
            context = _extract_metacritic_season_context(html, fallback_start_date)
        except Exception:
            context = (None, None, None)
        self._metacritic_season_cache[season_url] = context
        return context

    def _imdb_web_episode_count(self, ttcode: str, season_number: int) -> int:
        key = (ttcode, season_number)
        if key in self._imdb_episode_count_cache:
            return self._imdb_episode_count_cache[key]
        try:
            html = self.http_client.get_text(f"https://www.imdb.com/title/{ttcode}/episodes/?season={season_number}")
            count = _extract_imdb_web_episode_count(html, season_number)
        except Exception:
            count = 0
        self._imdb_episode_count_cache[key] = count
        return count

    def _latest_season_episode_stats(
        self,
        connection: sqlite3.Connection,
        ttcode: str,
        preferred_season: int | None = None,
    ) -> tuple[int | None, int]:
        if preferred_season is not None and preferred_season > 0:
            count = self._episode_count_for_season(connection, ttcode, preferred_season)
            if count:
                return preferred_season, count

        row = connection.execute(
            """
            WITH numeric_episodes AS (
                SELECT
                    CAST(season_number AS INTEGER) AS season_number,
                    NULLIF(episode_number, '') AS episode_number
                FROM episodes
                WHERE parent_tconst = ?
                  AND season_number != ''
                  AND season_number GLOB '[0-9]*'
                  AND CAST(season_number AS INTEGER) > 0
            ),
            latest AS (
                SELECT MAX(season_number) AS season_number
                FROM numeric_episodes
            )
            SELECT
                latest.season_number AS latest_season,
                COUNT(DISTINCT numeric_episodes.episode_number) AS episode_count
            FROM latest
            LEFT JOIN numeric_episodes
              ON numeric_episodes.season_number = latest.season_number
             AND numeric_episodes.episode_number IS NOT NULL
            GROUP BY latest.season_number
            """,
            (ttcode,),
        ).fetchone()
        if not row or row["latest_season"] is None:
            return None, 0
        return int(row["latest_season"]), int(row["episode_count"] or 0)

    def _episode_count_for_season(self, connection: sqlite3.Connection, ttcode: str, season_number: int) -> int:
        row = connection.execute(
            """
            SELECT COUNT(DISTINCT NULLIF(episode_number, '')) AS episode_count
            FROM episodes
            WHERE parent_tconst = ?
              AND season_number = ?
              AND NULLIF(episode_number, '') IS NOT NULL
            """,
            (ttcode, str(season_number)),
        ).fetchone()
        return int(row["episode_count"] or 0)

    def _output_row(
        self,
        metacritic_row: dict[str, str],
        ttcode: str,
        total_seasons: str | int,
        total_episodes: str | int,
        note: str,
    ) -> dict[str, str | int]:
        return {
            "Title Name": metacritic_row.get("Title Name", ""),
            "Release Date": metacritic_row.get("Release Date", ""),
            "Total Seasons": total_seasons,
            "Total Episodes": total_episodes,
            "ttcode": ttcode,
            "Release Type": metacritic_row.get("Release Type", ""),
            "Daypart": metacritic_row.get("Daypart", ""),
            "Program Type": metacritic_row.get("Program Type", ""),
            "Language Type": metacritic_row.get("Language Type", ""),
            "Availability / Network": metacritic_row.get("Availability / Network", ""),
            "Metacritic URL": metacritic_url_for_row(metacritic_row, default_media_type="tv"),
            "Lookup Note": note,
        }

    def _is_imdb_eligible(self, row: dict[str, str]) -> bool:
        combined = " ".join(
            [
                row.get("Title Name", ""),
                row.get("Release Type", ""),
                row.get("Content Format", ""),
                row.get("Availability / Network", ""),
                row.get("Other Details", ""),
            ]
        ).lower()
        excluded = ["$", "rent/buy", "movie", "special"]
        return not any(marker in combined for marker in excluded)

    def _index_is_current(self, dataset_paths: list[Path]) -> bool:
        if not self.index_path.exists():
            return False
        index_mtime = self.index_path.stat().st_mtime
        if not all(path.exists() and path.stat().st_mtime <= index_mtime for path in dataset_paths):
            return False
        try:
            with sqlite3.connect(self.index_path) as connection:
                return _index_has_required_schema(connection)
        except sqlite3.Error:
            return False

    def _is_stale(self, path: Path) -> bool:
        age_seconds = time.time() - path.stat().st_mtime
        return age_seconds > self.max_age_days * 24 * 60 * 60


def normalize_title(value: str) -> str:
    value = _clean_imdb_value(value).lower()
    value = re.sub(r"&", " and ", value)
    value = re.sub(r"\[[^\]]+\]|\([^\)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _index_has_required_schema(connection: sqlite3.Connection) -> bool:
    required = {
        "metadata": {"key", "value"},
        "titles": {
            "tconst",
            "title_type",
            "primary_title",
            "original_title",
            "start_year",
            "end_year",
            "genres",
            "normalized_primary",
            "normalized_original",
        },
        "episodes": {"parent_tconst", "season_number", "episode_number"},
        "names": {
            "nconst",
            "primary_name",
            "birth_year",
            "death_year",
            "primary_profession",
            "known_for_titles",
            "normalized_name",
        },
    }
    for table, columns in required.items():
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None:
            return False
        actual = {item[1] for item in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if not columns <= actual:
            return False
    version = connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        ("index_version",),
    ).fetchone()
    if version is None or version[0] != IMDB_INDEX_VERSION:
        return False
    return True


def _preferred_title_types(category: str) -> list[str]:
    normalized = normalize_title(category)
    if normalized in {"game", "games", "video game", "video games"}:
        return ["videoGame"]
    if normalized in {"tv movie", "tv movies"}:
        return ["tvMovie", "movie"]
    if normalized == "movies":
        return ["movie", "tvMovie"]
    if normalized == "tv shows":
        return ["tvSeries", "tvMiniSeries"]
    return ["movie", "tvMovie", "tvSeries", "tvMiniSeries", "videoGame"]


def _title_column_for_section(section: dict[str, Any]) -> str:
    columns = section.get("columns") or []
    for candidate in TITLE_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    rows = section.get("rows") or []
    if rows and isinstance(rows[0], dict):
        for candidate in TITLE_COLUMN_CANDIDATES:
            if candidate in rows[0]:
                return candidate
    return ""


def _ensure_ttcode_column(columns: list[str], title_column: str) -> None:
    if TT_CODE_COLUMN in columns:
        return
    try:
        insert_at = columns.index(title_column) + 1
    except ValueError:
        columns.append(TT_CODE_COLUMN)
        return
    columns.insert(insert_at, TT_CODE_COLUMN)


def _category_for_ttcode_lookup(
    tracker_type: str,
    section: dict[str, Any],
    row: dict[str, Any],
) -> str:
    tracker = normalize_title(tracker_type)
    section_key = normalize_title(str(section.get("key", "")))
    section_title = normalize_title(str(section.get("title", "")))
    row_type = normalize_title(
        " ".join(
            str(row.get(key, ""))
            for key in (
                "Input Type",
                "Release Type",
                "Content Format",
            )
        )
    )
    combined = f"{tracker} {section_key} {section_title} {row_type}"
    if tracker == "game" or section_key == "games" or "video game" in combined:
        return "Video Games"
    if "tv movie" in combined:
        return "TV Movies"
    if tracker in {"boxoffice", "release schedule changes"} or "movie" in combined or "film" in combined:
        return "Movies"
    if "tv" in combined or "series" in combined or "season" in combined or "show" in combined:
        return "TV Shows"
    return ""


def _release_date_from_row(row: dict[str, Any]) -> date | None:
    for key in (
        "Release Date",
        "New Release Date",
        "Old Release Date",
        "Input Release Year",
        "Input Year",
        "release_date",
        "latest_season_start_date",
        "YouTube Release Date",
    ):
        value = str(row.get(key, "") or "")
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", value)
        if not year_match:
            continue
        try:
            return date(int(year_match.group(1)), 1, 1)
        except ValueError:
            return None
    return None


def _extract_existing_ttcode(row: dict[str, Any]) -> str:
    for key in (
        TT_CODE_COLUMN,
        "imdb_id",
        "IMDb ID",
        "imdb_url",
        "IMDb URL",
        "Provided Code",
        "Matched Code",
    ):
        value = str(row.get(key, "") or "")
        match = re.search(r"\btt\d{7,12}\b", value, flags=re.IGNORECASE)
        if match:
            return match.group(0).lower()
    return ""


def _append_ttcode_summary(snapshot: dict[str, Any], matched: int, total: int) -> None:
    addition = (
        f"IMDb ttcode enrichment used the local IMDb title/name index from datasets.imdbws.com "
        f"and matched {matched} of {total} title rows."
    )
    summary = str(snapshot.get("summary", "") or "").strip()
    if addition in summary:
        return
    snapshot["summary"] = f"{summary} {addition}".strip()


def _scaled_progress(
    progress: ProgressCallback,
    target_start: int,
    target_end: int,
) -> ProgressCallback:
    def update(percent: int, message: str) -> None:
        bounded = min(max(percent, 0), 100)
        scaled = target_start + int((bounded / 100) * (target_end - target_start))
        progress(min(max(scaled, target_start), target_end), message)

    return update


def _title_lookup_score(
    row: sqlite3.Row,
    normalized_title_value: str,
    preferred_types: list[str],
    release_date: date | None,
) -> int:
    score = 0
    title_type = row["title_type"]
    if title_type in preferred_types:
        score += 80 - preferred_types.index(title_type)
    if row["normalized_primary"] == normalized_title_value:
        score += 20
    if row["normalized_original"] == normalized_title_value:
        score += 10
    start_year = _safe_int(row["start_year"])
    if release_date and start_year:
        if start_year == release_date.year:
            score += 50
        elif abs(start_year - release_date.year) <= 1:
            score += 15
    elif start_year:
        score += min(start_year - 1900, 125)
    return score


def _series_lookup_score(
    row: sqlite3.Row,
    normalized_title_value: str,
    expected_type: str,
    release_date: date | None,
) -> int:
    score = 0
    if row["normalized_primary"] == normalized_title_value:
        score += 60
    if row["normalized_original"] == normalized_title_value:
        score += 50
    if row["title_type"] == expected_type:
        score += 30
    elif row["title_type"] in {"tvSeries", "tvMiniSeries"}:
        score += 12

    start_year = _safe_int(row["start_year"])
    if release_date and start_year:
        year_delta = abs(start_year - release_date.year)
        if year_delta == 0:
            score += 120
        elif year_delta == 1:
            score += 35
        else:
            score -= min(year_delta * 3, 90)
    elif start_year:
        score += min(max(start_year - 1900, 0), 125)
    return score


def _name_lookup_score(row: sqlite3.Row, profession_hint: str) -> int:
    score = 0
    professions = normalize_title(row["primary_profession"]).split()
    hint_tokens = set(normalize_title(profession_hint).split())
    if hint_tokens and any(token in professions for token in hint_tokens):
        score += 40
    if row["death_year"] == "":
        score += 10
    birth_year = _safe_int(row["birth_year"])
    if birth_year:
        score += min(max(birth_year - 1900, 0), 100)
    return score


def _known_for_title_names(connection: sqlite3.Connection, known_for_titles: str) -> str:
    tconsts = [item.strip() for item in (known_for_titles or "").split(",") if item.strip()]
    if not tconsts:
        return ""
    placeholders = ",".join("?" for _ in tconsts)
    rows = connection.execute(
        f"""
        SELECT tconst, primary_title, start_year
        FROM titles
        WHERE tconst IN ({placeholders})
        """,
        tconsts,
    ).fetchall()
    by_tconst = {row["tconst"]: row for row in rows}
    output = []
    for tconst in tconsts:
        row = by_tconst.get(tconst)
        if not row:
            continue
        title = row["primary_title"] or tconst
        year = row["start_year"] or ""
        output.append(f"{title} ({year})" if year else title)
    return "; ".join(output)


def _safe_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _clean_imdb_value(value: str | None) -> str:
    if not value or value == r"\N":
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _season_from_metacritic_url(source_url: str) -> int | None:
    match = re.search(r"/season-(\d+)", source_url or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _episode_from_metacritic_url(source_url: str) -> int | None:
    match = re.search(r"/episode-(\d+)", source_url or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _metacritic_season_url(source_url: str) -> str:
    text = (source_url or "").strip()
    if not text or "metacritic.com" not in text.lower():
        return ""
    match = re.match(r"^(https?://[^/]+/tv/[^/]+/season-\d+)/?", text, flags=re.IGNORECASE)
    return f"{match.group(1).rstrip('/')}/" if match else ""


def _metacritic_season_url_for_number(metacritic_row: dict[str, str], season_number: int | None) -> str:
    if not season_number:
        return ""
    source_url = metacritic_row.get("Source URL", "")
    source_season_url = _metacritic_season_url(source_url)
    if source_season_url:
        return source_season_url
    base_url = metacritic_url_for_row(metacritic_row, default_media_type="tv")
    match = re.match(r"^(https?://[^/]+/tv/[^/]+)", base_url.rstrip("/"), flags=re.IGNORECASE)
    if not match:
        return ""
    return f"{match.group(1)}/season-{season_number}/"


def _merge_season_context(
    primary: tuple[date | None, date | None, int | None],
    secondary: tuple[date | None, date | None, int | None],
) -> tuple[date | None, date | None, int | None]:
    primary_start, primary_end, primary_count = primary
    secondary_start, secondary_end, secondary_count = secondary
    return (
        primary_start or secondary_start,
        primary_end or secondary_end,
        max(primary_count or 0, secondary_count or 0) or None,
    )


def _extract_metacritic_season_context(
    html: str,
    fallback_start_date: date,
) -> tuple[date | None, date | None, int | None]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    episode_dates: dict[int, date] = {}
    for match in re.finditer(
        r"\bEpisode\s+(\d+)\D{1,12}([A-Za-z]{3,9}\.?\s+\d{1,2},\s+\d{4})",
        text,
        flags=re.IGNORECASE,
    ):
        try:
            parsed = date_parser.parse(match.group(2), default=datetime(fallback_start_date.year, 1, 1)).date()
        except (TypeError, ValueError):
            continue
        episode_dates[int(match.group(1))] = parsed
    if not episode_dates:
        return None, None, None
    dates = sorted(episode_dates.values())
    return dates[0], dates[-1], len(episode_dates)


def _extract_imdb_web_episode_count(html: str, season_number: int) -> int:
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    episode_numbers = {
        int(match)
        for match in re.findall(rf"\bS0*{season_number}\.E(\d+)\b", text, flags=re.IGNORECASE)
    }
    episode_numbers.update(
        int(match)
        for match in re.findall(rf"\bEpisode\s+#0*{season_number}\.(\d+)\b", text, flags=re.IGNORECASE)
    )
    generic_counts = [
        int(match)
        for match in re.findall(r"\b(\d{1,3})\s+episodes?\b", text, flags=re.IGNORECASE)
        if 0 < int(match) < 100
    ]
    return max([*episode_numbers, *generic_counts, 0])


def _episode_count_from_details(metacritic_row: dict[str, str], season_number: int | None) -> int:
    text = " ".join(
        [
            metacritic_row.get("Other Details", ""),
            metacritic_row.get("Release Type", ""),
            metacritic_row.get("Genre", ""),
        ]
    )
    return _episode_count_from_text(text, season_number)


def _episode_count_from_text(text: str, season_number: int | None) -> int:
    if not text:
        return 0
    counts: list[int] = []
    for match in re.finditer(
        r"\b(?P<count>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
        r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)[-\s]+episodes?\b",
        text,
        flags=re.IGNORECASE,
    ):
        count = _number_word_or_int(match.group("count"))
        if count:
            counts.append(count)
    if season_number:
        for match in re.finditer(rf"\bS0*{season_number}\s+episodes?\b", text, flags=re.IGNORECASE):
            before = text[max(0, match.start() - 16) : match.start()]
            count_match = re.search(r"(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\s+$", before, re.I)
            if count_match:
                count = _number_word_or_int(count_match.group(1))
                if count:
                    counts.append(count)
    return max(counts, default=0)


def _known_episode_count_override(metacritic_row: dict[str, str], season_number: int | None) -> int:
    if not season_number:
        return 0
    release_date = _parse_iso_date(metacritic_row.get("Release Date", ""))
    if not release_date:
        return 0
    key = (normalize_title(metacritic_row.get("Title Name", "")), season_number, release_date.year)
    return SERIES_EPISODE_COUNT_OVERRIDES.get(key, 0)


def _number_word_or_int(value: str) -> int:
    value = (value or "").strip().lower()
    if value.isdigit():
        return int(value)
    return {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
    }.get(value, 0)


def _infer_season_end_date(start_date: date, metacritic_row: dict[str, str]) -> date:
    detail_text = " ".join(
        [
            metacritic_row.get("Other Details", ""),
            metacritic_row.get("Availability / Network", ""),
        ]
    )
    explicit_end = _extract_explicit_end_date(detail_text, start_date)
    if explicit_end and explicit_end > start_date:
        return explicit_end
    episode_dates = _extract_episode_release_dates(detail_text, start_date)
    unique_dates = sorted({item for item in episode_dates if item >= start_date})
    if len(unique_dates) > 1 and unique_dates[-1] > unique_dates[0]:
        return unique_dates[-1]
    return start_date + timedelta(days=30)


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _extract_episode_release_dates(text: str, start_date: date) -> list[date]:
    matches = re.findall(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\.?\s+\d{1,2}(?:,\s*\d{4})?",
        text,
        flags=re.IGNORECASE,
    )
    dates: list[date] = []
    for match in matches:
        try:
            parsed = date_parser.parse(match, default=datetime(start_date.year, 1, 1)).date()
        except (TypeError, ValueError):
            continue
        if parsed < start_date:
            parsed = parsed.replace(year=parsed.year + 1)
        dates.append(parsed)
    return dates


def _extract_explicit_end_date(text: str, start_date: date) -> date | None:
    match = re.search(
        r"\b(?:through|thru|until|concludes on|finale on|ending on)\s+"
        r"([A-Za-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    default_year = start_date.year
    try:
        parsed = date_parser.parse(match.group(1), default=datetime(default_year, 1, 1)).date()
    except (TypeError, ValueError):
        return None
    if parsed < start_date:
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed


def _display_date(value: date) -> str:
    return value.strftime("%d-%m-%Y")
