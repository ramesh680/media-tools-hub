from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import re
import uuid


TV_MATRIX_COLUMNS = [
    "Daypart",
    "Program Type",
    "Language Type",
]

TV_COLUMNS = [
    "Title Name",
    "Studio/Publisher",
    "Release Type",
    "Genre",
    "Release Date",
    "Content Format",
    *TV_MATRIX_COLUMNS,
    "Availability / Network",
    "Metacritic Score",
    "Source URL",
    "Metacritic URL",
    "Other Details",
]

GAME_COLUMNS = [
    "Title Name",
    "Studio/Publisher",
    "Release Type",
    "Genre",
    "Release Date",
    "Content Format",
    "Availability / Network",
    "Metacritic Score",
    "Source URL",
    "Metacritic URL",
    "Other Details",
]

MOVIE_COLUMNS = [
    "Title Name",
    "Studio/Publisher",
    "Release Type",
    "Genre",
    "Release Date",
    "Content Format",
    "Availability / Network",
    "Metacritic Score",
    "Source URL",
    "Metacritic URL",
    "Other Details",
]

BOX_OFFICE_COLUMNS = [
    "Title Name",
    "Distributor",
    "Scale",
    "Release Date",
    "Opening",
    "Widest Release",
    "Genre",
    "Running Time",
    "Source URL",
    "Other Details",
]

BOX_OFFICE_RELEASE_DATE_CHANGE_COLUMNS = [
    "Title Name",
    "Old Release Date",
    "New Release Date",
    "Release Date Change",
    "Old Scale",
    "New Scale",
    "Change Type",
]

# Columns for the Release Schedule Changes tool, which reads Box Office Mojo's
# dedicated "/calendar/changes/" page directly (Release, Distributor, Scale,
# Old Date, New Date) plus the date the change was published on that page.
BOX_OFFICE_SCHEDULE_CHANGE_COLUMNS = [
    "Release",
    "Distributor",
    "Scale",
    "Old Date",
    "New Date",
    "Change Posted",
]

BOX_OFFICE_OPENING_COLUMNS = [
    "Input Title",
    "Matched Title",
    "Year",
    "Opening Gross",
    "Opening Theaters",
    "Widest Release Theaters",
    "Domestic Total Gross",
    "Source URL",
    "Lookup Note",
]

BOX_OFFICE_RECENT_OPENING_COLUMNS = [
    "Title Name",
    "Distributor",
    "Release Date",
    "Opening Gross",
    "Opening Theaters",
    "Domestic Opening Weekend Rank",
    "Source URL",
]

BILLBOARD_ARTIST_100_COLUMNS = [
    "Rank",
    "Artist Name",
    "IMDb nmcode",
    "IMDb URL",
    "IMDb Primary Profession",
    "IMDb Known For Titles",
    "Wikidata ID",
    "Wikidata URL",
    "Wikipedia URL",
    "Gender",
    "Occupations",
    "Birth Date",
    "Birth Place",
    "Country",
    "Official Website",
    "Wikidata Description",
    "Billboard Artist URL",
    "Billboard Details",
    "Last Week",
    "Peak Position",
    "Weeks on Chart",
    "Chart Date",
    "Source URL",
    "Other Details",
]

IMDB_COLUMNS = [
    "Title Name",
    "Release Date",
    "Total Seasons",
    "Total Episodes",
    "ttcode",
    "Release Type",
    *TV_MATRIX_COLUMNS,
    "Availability / Network",
    "Metacritic URL",
    "Lookup Note",
]

TV_SEASON_EPISODE_COLUMNS = [
    "release_date",
    "title",
    "daypart",
    "program_type",
    "language_type",
    "network_distributor",
    "imdb_id",
    "metacritic_url",
    "latest_season_number",
    "latest_season_episode_count",
    "latest_season_start_date",
    "latest_season_end_date",
]


YOUTUBE_RELEASE_COLUMNS = [
    "Input Title",
    "Input Type",
    "Input Network / Distributor",
    "Input Release Year",
    "Confirmation",
    "Confidence",
    "Official Trailer Network",
    "YouTube Channel",
    "Channel ID",
    "Video Title",
    "YouTube Release Date",
    "YouTube URL",
    "Matched Keywords",
    "Lookup Note",
]

TRACKER_TITLES = {
    "tv": "TV Premiere Calendar",
    "imdb": "IMDb-Enriched TV Series Snapshot",
    "tv_seasons": "TV Seasons and Episodes Snapshot",
    "game": "Game Release Calendar",
    "movie": "Movie Release Calendar",
    "boxoffice": "Box Office Mojo US Movie Releases",
    "boxoffice_opening": "Box Office Mojo Opening Weekend Lookup",
    "boxoffice_recent_opening": "Last Week's US Box Office Openings",
    "release_schedule_changes": "Release Schedule Changes",
    "imdb_verifier": "IMDb Bulk Verification",
    "billboard_artist_100": "Billboard Artist 100",
    "youtube_release_verifier": "YouTube Release Verifier",
}


@dataclass
class ExportPayload:
    title: str
    columns: list[str]
    rows: list[dict[str, Any]]
    tracker_type: str
    section_key: str
    supports_google: bool = False


@dataclass
class Job:
    tracker_type: str
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "pending"
    progress_percent: int = 0
    message: str = "Queued"
    result: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "tracker_type": self.tracker_type,
            "status": self.status,
            "progress_percent": self.progress_percent,
            "message": self.message,
            "result": self.result,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(value: str, suffix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    cleaned = cleaned[:90] or "export"
    return f"{cleaned}.{suffix}"
