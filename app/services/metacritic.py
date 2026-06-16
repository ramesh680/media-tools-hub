from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import quote, urljoin
import re

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from app.models import GAME_COLUMNS, MOVIE_COLUMNS, TV_COLUMNS, utc_now_iso
from app.services.http_client import HttpClient


TV_PREMIERE_URL = "https://www.metacritic.com/news/tv-premiere-dates/"
GAME_RELEASE_URL = "https://www.metacritic.com/news/major-new-and-upcoming-video-games-ps5-xbox-switch-pc/"
MOVIE_RELEASE_URL = "https://www.metacritic.com/news/upcoming-movie-release-dates-schedule/"
METACRITIC_BASE_URL = "https://www.metacritic.com"

DATE_HEADING_RE = re.compile(
    r"^(?:MON|TUE|WED|THU|FRI|SAT|SUN)\s*/\s*(?P<date>[A-Za-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)$",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"^(20\d{2})$")
SCORE_RE = re.compile(r"^(?P<score>(?:\d{2,3}|tbd))\b", re.IGNORECASE)
GAME_DATE_RE = re.compile(r"^(?P<genre>.+?)\s+-\s+(?P<date>[A-Za-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)(?P<extra>.*)$")
TIME_RE = re.compile(
    r"\b(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5]\d))?\s*(?P<meridiem>[ap])(?:\.?m\.?)?\b",
    re.IGNORECASE,
)
BROADCAST_TIME_RE = re.compile(r"\b(?P<hour>1[0-2]|0?[1-9])/(?P<central>1[0-2]|0?[1-9])c\b", re.IGNORECASE)

NOISE_LINES = {
    "advertisement",
    "register",
    "cookie settings",
    "overview",
    "follow us",
    "additional content by keith kimbell.",
}

PLATFORM_CODES = {
    "PC",
    "PS5",
    "PS4",
    "XBX",
    "XB1",
    "NS2",
    "NS",
    "MOBILE",
    "MOBILE*",
    "MQ",
    "IOS",
    "ANDROID",
}

MOVIE_AVAILABILITY_PATTERNS = [
    r"Streaming\s*\([^)]+\)",
    r"Limited/VOD",
    r"RENT/BUY",
    r"Event screening",
    r"Event screeing",
    r"Rerelease",
    r"Streaming",
    r"Limited",
    r"WIDE",
    r"VOD",
    r"LA/NY",
    r"NY/LA",
    r"NY",
    r"LA",
    r"IMAX",
]

MOVIE_GENRE_TERMS = [
    "Action-Adventure",
    "Action",
    "Adventure",
    "Animation",
    "Anime",
    "Comedy",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "Foreign",
    "Horror",
    "Music",
    "Musical",
    "Mystery",
    "Rom-Com",
    "Sci-Fi",
    "Sports",
    "Thriller",
]

NETWORK_MARKERS = [
    "Prime Video",
    "Apple TV+",
    "Apple TV",
    "HBO Max",
    "Disney+",
    "Paramount+",
    "Netflix",
    "Peacock",
    "Hulu",
    "BritBox",
    "Acorn TV",
    "The Roku Channel",
    "Roku Channel",
    "Discovery+",
    "Screambox",
    "Showtime",
    "Lifetime",
    "Discovery",
    "Food",
    "TLC",
    "Bravo",
    "Oxygen",
    "History",
    "Nat Geo",
    "National Geographic",
    "A&E",
    "ABC",
    "CBS",
    "NBC",
    "Fox",
    "FOX",
    "PBS",
    "CW",
    "FXX",
    "FX",
    "AMC",
    "BBC America",
    "Freevee",
    "MGM+",
    "Starz",
    "BET+",
    "YouTube",
    "RENT/BUY",
]

STREAMING_NETWORKS = {
    "Prime Video",
    "Apple TV+",
    "Apple TV",
    "HBO Max",
    "Disney+",
    "Paramount+",
    "Netflix",
    "Peacock",
    "Hulu",
    "BritBox",
    "Acorn TV",
    "The Roku Channel",
    "Roku Channel",
    "Discovery+",
    "Screambox",
    "Freevee",
    "MGM+",
    "BET+",
}

GENRE_TERMS = [
    "Action",
    "Adventure",
    "Anime",
    "Animation",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "Food",
    "Foreign",
    "Game Show",
    "Horror",
    "Live Event",
    "Music Special",
    "Music",
    "Reality Competition",
    "Reality",
    "Rom-Com",
    "Sci-Fi",
    "Special",
    "Sports",
    "Thriller",
    "Variety",
]


@dataclass
class ParsedLine:
    text: str
    year: int


class MetacriticParser:
    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client

    def fetch_tv_calendar(
        self,
        today: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        progress=None,
    ) -> dict:
        today = today or date.today()
        start_date = start_date or today
        end_date = end_date or today + timedelta(days=365)
        if end_date < start_date:
            raise ValueError("End date must be the same as or later than the start date.")
        if progress:
            progress(8, "Fetching the public TV premiere calendar")
        html = self.http_client.get_text(TV_PREMIERE_URL)
        if progress:
            progress(28, "Parsing date-grouped TV premiere sections")
        rows = _filter_tv_premiere_rows(self.parse_tv_calendar(html, today=today))
        selected_rows = [
            row for row in rows if start_date <= date.fromisoformat(row["Release Date"]) <= end_date
        ]
        daily_date = today if start_date <= today <= end_date else start_date
        daily = [row for row in selected_rows if row["Release Date"] == daily_date.isoformat()]
        if progress:
            progress(72, "Preparing daily and selected calendar snapshots")
        return {
            "tracker_type": "tv",
            "title": "TV Premiere Calendar",
            "created_at": utc_now_iso(),
            "source_url": TV_PREMIERE_URL,
            "summary": (
                f"Scanned public Metacritic premiere rows from {start_date.isoformat()} "
                f"through {end_date.isoformat()}. Daily Data is focused on {daily_date.isoformat()}."
            ),
            "sections": [
                {
                    "key": "daily",
                    "title": "Daily Data",
                    "columns": TV_COLUMNS,
                    "rows": daily,
                    "row_count": len(daily),
                    "supports_google": True,
                },
                {
                    "key": "yearly",
                    "title": "Yearly Data" if (end_date - start_date).days >= 365 else "Selected Window Data",
                    "columns": TV_COLUMNS,
                    "rows": selected_rows,
                    "row_count": len(selected_rows),
                    "supports_google": True,
                },
            ],
        }

    def fetch_game_calendar(
        self,
        today: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        progress=None,
    ) -> dict:
        today = today or date.today()
        start_date = start_date or today
        end_date = end_date or today + timedelta(days=365)
        if end_date < start_date:
            raise ValueError("End date must be the same as or later than the start date.")
        if progress:
            progress(8, "Fetching the public game release calendar")
        html = self.http_client.get_text(GAME_RELEASE_URL)
        if progress:
            progress(30, "Parsing notable game release blocks")
        rows = self.parse_game_calendar(html, today=today)
        rows = [row for row in rows if start_date <= date.fromisoformat(row["Release Date"]) <= end_date]
        rows.sort(key=lambda item: (item["Release Date"], item["Title Name"].lower()))
        if progress:
            progress(78, "Preparing game release snapshot")
        return {
            "tracker_type": "game",
            "title": "Game Release Calendar",
            "created_at": utc_now_iso(),
            "source_url": GAME_RELEASE_URL,
            "summary": (
                f"Scanned public Metacritic game release rows from {start_date.isoformat()} "
                f"through {end_date.isoformat()}."
            ),
            "sections": [
                {
                    "key": "games",
                    "title": "Game Release Calendar",
                    "columns": GAME_COLUMNS,
                    "rows": rows,
                    "row_count": len(rows),
                    "supports_google": True,
                }
            ],
        }

    def fetch_movie_calendar(
        self,
        today: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        progress=None,
    ) -> dict:
        today = today or date.today()
        start_date = start_date or today
        end_date = end_date or today + timedelta(days=365)
        if end_date < start_date:
            raise ValueError("End date must be the same as or later than the start date.")
        if progress:
            progress(8, "Fetching the public movie release calendar")
        html = self.http_client.get_text(MOVIE_RELEASE_URL)
        if progress:
            progress(28, "Parsing date-grouped movie release sections")
        rows = _filter_movie_release_rows(self.parse_movie_calendar(html, today=today))
        selected_rows = [
            row for row in rows if start_date <= date.fromisoformat(row["Release Date"]) <= end_date
        ]
        selected_rows.sort(key=lambda item: (item["Release Date"], item["Title Name"].lower()))
        daily_date = today if start_date <= today <= end_date else start_date
        daily = [row for row in selected_rows if row["Release Date"] == daily_date.isoformat()]
        if progress:
            progress(72, "Preparing daily and selected movie release snapshots")
        return {
            "tracker_type": "movie",
            "title": "Movie Release Calendar",
            "created_at": utc_now_iso(),
            "source_url": MOVIE_RELEASE_URL,
            "summary": (
                f"Scanned public Metacritic movie release rows from {start_date.isoformat()} "
                f"through {end_date.isoformat()}. Daily Data is focused on {daily_date.isoformat()}."
            ),
            "sections": [
                {
                    "key": "daily",
                    "title": "Daily Data",
                    "columns": MOVIE_COLUMNS,
                    "rows": daily,
                    "row_count": len(daily),
                    "supports_google": True,
                },
                {
                    "key": "yearly",
                    "title": "Yearly Data" if (end_date - start_date).days >= 365 else "Selected Window Data",
                    "columns": MOVIE_COLUMNS,
                    "rows": selected_rows,
                    "row_count": len(selected_rows),
                    "supports_google": True,
                },
            ],
        }

    def parse_tv_calendar(self, html: str, today: date | None = None) -> list[dict[str, str]]:
        today = today or date.today()
        soup = _clean_soup(html)
        table_rows = _parse_tv_tables(soup, today=today)
        if table_rows:
            return _dedupe_rows(table_rows)

        url_by_title = _build_anchor_title_map(soup, TV_PREMIERE_URL)
        lines = _article_lines(soup)
        rows: list[dict[str, str]] = []
        current_date: date | None = None
        current_year = today.year
        current_item: list[str] = []

        def finalize_item() -> None:
            nonlocal current_item
            if current_date and current_item:
                row = _parse_tv_item(current_item, current_date, url_by_title)
                if row:
                    rows.append(row)
            current_item = []

        for parsed_line in lines:
            text = parsed_line.text
            year_match = YEAR_RE.match(text)
            if year_match:
                finalize_item()
                current_year = int(year_match.group(1))
                continue

            heading = DATE_HEADING_RE.match(text)
            if heading:
                finalize_item()
                parsed = _parse_article_date(heading.group("date"), current_year, today)
                current_date = parsed
                if "," in heading.group("date"):
                    current_year = parsed.year
                continue

            if current_date is None or _is_noise(text):
                continue

            if _looks_like_tv_title_line(text, current_item):
                finalize_item()
                current_item = [text]
            elif current_item:
                current_item.append(text)

        finalize_item()
        return _dedupe_rows(rows)

    def parse_movie_calendar(self, html: str, today: date | None = None) -> list[dict[str, str]]:
        today = today or date.today()
        soup = _clean_soup(html)
        url_by_title = _build_anchor_title_map(soup, MOVIE_RELEASE_URL)
        lines = _article_lines(soup)
        rows: list[dict[str, str]] = []
        current_date: date | None = None
        current_year = today.year
        current_item: list[str] = []

        def finalize_item() -> None:
            nonlocal current_item
            if current_date and current_item:
                row = _parse_movie_item(current_item, current_date, url_by_title)
                if row:
                    rows.append(row)
            current_item = []

        for parsed_line in lines:
            text = parsed_line.text
            year_match = YEAR_RE.match(text)
            if year_match:
                finalize_item()
                current_year = int(year_match.group(1))
                continue

            heading = DATE_HEADING_RE.match(text)
            if heading:
                finalize_item()
                parsed = _parse_article_date(heading.group("date"), current_year, today)
                current_date = parsed
                if "," in heading.group("date"):
                    current_year = parsed.year
                continue

            if current_date is None or _is_noise(text):
                continue

            if _looks_like_movie_title_line(text, current_item):
                finalize_item()
                current_item = [text]
            elif current_item:
                current_item.append(text)

        finalize_item()
        return _dedupe_rows(rows)

    def parse_game_calendar(self, html: str, today: date | None = None) -> list[dict[str, str]]:
        today = today or date.today()
        soup = _clean_soup(html)
        table_rows = _parse_game_tables(soup, today=today)
        if table_rows:
            return _dedupe_rows(table_rows)

        url_by_title = _build_anchor_title_map(soup, GAME_RELEASE_URL)
        lines = [line.text for line in _article_lines(soup) if not _is_noise(line.text)]
        rows: list[dict[str, str]] = []
        index = 0
        while index < len(lines) - 1:
            item_line = lines[index]
            detail_line = lines[index + 1]
            if _looks_like_game_item_line(item_line) and GAME_DATE_RE.match(detail_line):
                row = _parse_game_item(item_line, detail_line, url_by_title, today)
                if row:
                    rows.append(row)
                index += 2
                continue
            index += 1
        return _dedupe_rows(rows)


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup


def _article_lines(soup: BeautifulSoup) -> list[ParsedLine]:
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = root.get_text("\n")
    lines: list[ParsedLine] = []
    current_year = date.today().year
    for raw in text.splitlines():
        cleaned = _normalize_ws(raw)
        if not cleaned:
            continue
        year_match = YEAR_RE.match(cleaned)
        if year_match:
            current_year = int(year_match.group(1))
        if cleaned.startswith("* * *"):
            continue
        lines.append(ParsedLine(text=cleaned, year=current_year))
    return lines


def _build_anchor_title_map(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for anchor in soup.find_all("a"):
        text = _normalize_ws(anchor.get_text(" "))
        href = anchor.get("href")
        if not text or not href:
            continue
        lowered = text.lower()
        if lowered in {"trailer", "trailer2", "image", "see all", "more"}:
            continue
        if lowered == "tbd" or lowered.isdigit():
            continue
        if "youtube" in href or "youtu.be" in href:
            continue
        if lowered.startswith("image:"):
            continue
        title = _clean_title(text)
        if not title or len(title) < 2:
            continue
        output.setdefault(_title_key(title), urljoin(base_url, href))
    return output


def _parse_tv_item(lines: list[str], release_date: date, url_by_title: dict[str, str]) -> dict[str, str] | None:
    joined = _normalize_ws(" ".join(lines))
    if not joined or "trailer" == joined.lower():
        return None

    score = _extract_score(joined)
    title = _clean_title(lines[0])
    if not title or title.lower().startswith("trailer"):
        return None

    release_type = _infer_tv_release_type(joined)
    content_format = _infer_tv_content_format(release_type, joined)
    genre, availability, details = _parse_tv_details(lines[1:], joined)
    matrix = _tv_matrix_values(release_type, content_format, genre, availability, details, joined)
    source_url = url_by_title.get(_title_key(title), "")
    metacritic_url = metacritic_url_for_row(
        {"Title Name": title, "Release Type": release_type, "Content Format": content_format, "Source URL": source_url},
        default_media_type="tv",
    )

    return {
        "Title Name": title,
        "Studio/Publisher": "",
        "Release Type": release_type,
        "Genre": genre,
        "Release Date": release_date.isoformat(),
        "Content Format": content_format,
        "Daypart": matrix["Daypart"],
        "Program Type": matrix["Program Type"],
        "Language Type": matrix["Language Type"],
        "Availability / Network": availability,
        "Metacritic Score": score,
        "Source URL": source_url,
        "Metacritic URL": metacritic_url,
        "Other Details": details,
    }


def _parse_tv_tables(soup: BeautifulSoup, today: date) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_year = today.year
    root = soup.find("article") or soup.find("main") or soup.body or soup
    for heading in root.find_all(["h2", "h3"]):
        heading_text = _normalize_ws(heading.get_text(" "))
        year_match = YEAR_RE.match(heading_text)
        if year_match:
            current_year = int(year_match.group(1))
            continue
        date_match = DATE_HEADING_RE.match(heading_text)
        if not date_match:
            continue

        release_date = _parse_article_date(date_match.group("date"), current_year, today)
        if "," in date_match.group("date"):
            current_year = release_date.year

        sibling = heading.find_next_sibling()
        while sibling and sibling.name not in {"h2", "h3"}:
            if sibling.name == "table":
                for table_row in sibling.find_all("tr"):
                    parsed = _parse_tv_table_row(table_row, release_date)
                    if parsed:
                        rows.append(parsed)
            sibling = sibling.find_next_sibling()
    return rows


def _parse_tv_table_row(table_row, release_date: date) -> dict[str, str] | None:
    cells = table_row.find_all(["td", "th"])
    if len(cells) < 2:
        return None

    score_cell = cells[0]
    title_cell = cells[1]
    availability_cell = cells[2] if len(cells) > 2 else None
    score = _extract_score(_normalize_ws(score_cell.get_text(" ")))
    title_anchor = _first_title_anchor(title_cell)
    score_anchor = _first_title_anchor(score_cell)
    source_url = ""
    if title_anchor and title_anchor.get("href"):
        source_url = urljoin(TV_PREMIERE_URL, title_anchor["href"])
    elif score_anchor and score_anchor.get("href"):
        source_url = urljoin(TV_PREMIERE_URL, score_anchor["href"])

    cell_text = _normalize_ws(title_cell.get_text(" "))
    release_type = _release_type_from_images(title_cell) or _infer_tv_release_type(cell_text)
    if title_anchor:
        title = _clean_title(title_anchor.get_text(" ", strip=True))
        remainder = _remainder_after_title(cell_text, title)
    else:
        title, remainder = _split_title_and_remainder(cell_text)
    if not title:
        return None

    genre, details = _parse_genre_and_details(remainder)
    availability = _normalize_ws(availability_cell.get_text(" ")) if availability_cell else ""
    if not availability:
        availability = _find_networks(cell_text)
    if not genre:
        genre = _guess_genre(remainder)
    content_format = _infer_tv_content_format(release_type, f"{cell_text} {availability}")
    matrix = _tv_matrix_values(release_type, content_format, genre, availability, details, cell_text)
    metacritic_url = metacritic_url_for_row(
        {"Title Name": title, "Release Type": release_type, "Content Format": content_format, "Source URL": source_url},
        default_media_type="tv",
    )

    return {
        "Title Name": title,
        "Studio/Publisher": "",
        "Release Type": release_type,
        "Genre": genre,
        "Release Date": release_date.isoformat(),
        "Content Format": content_format,
        "Daypart": matrix["Daypart"],
        "Program Type": matrix["Program Type"],
        "Language Type": matrix["Language Type"],
        "Availability / Network": availability,
        "Metacritic Score": score,
        "Source URL": source_url,
        "Metacritic URL": metacritic_url,
        "Other Details": details,
    }


def _first_title_anchor(cell):
    for anchor in cell.find_all("a"):
        text = _normalize_ws(anchor.get_text(" "))
        href = anchor.get("href", "")
        lowered = text.lower()
        if lowered.startswith("trailer") or "youtube" in href or "youtu.be" in href:
            continue
        if lowered == "tbd" or lowered.isdigit():
            continue
        return anchor
    return None


def _release_type_from_images(cell) -> str:
    image_text = " ".join(
        _normalize_ws(str(image.get("alt") or image.get("title") or ""))
        for image in cell.find_all("img")
    ).lower()
    if "limited series" in image_text:
        return "Limited Series"
    if "new series" in image_text:
        return "New Series"
    if "movie" in image_text:
        return "Movie"
    return ""


def _remainder_after_title(cell_text: str, title: str) -> str:
    text = _normalize_ws(cell_text)
    text = re.sub(r"^\(\$\)\s*", "", text)
    if title:
        text = re.sub(rf"^{re.escape(title)}\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*[-\u2013]?\s*Trailer\d?\s*(?:[-\u2013]\s*Trailer\d?\s*)*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTrailer\d?\b", "", text, flags=re.IGNORECASE)
    return _normalize_ws(text.strip(" -"))


def _split_title_and_remainder(cell_text: str) -> tuple[str, str]:
    text = _normalize_ws(cell_text)
    text = re.sub(r"^\(\$\)\s*", "", text)
    trailer_match = re.search(r"\s+[-\u2013]?\s*Trailer\d?\b", text, flags=re.IGNORECASE)
    if trailer_match and trailer_match.start() > 0:
        title = _clean_title(text[: trailer_match.start()])
        remainder = _remainder_after_title(text, title)
        return title, remainder

    best_match = None
    for term in sorted(GENRE_TERMS, key=len, reverse=True):
        match = re.search(rf"\s({re.escape(term)})(?=\b|/|:)", text, flags=re.IGNORECASE)
        if match and (best_match is None or match.start() < best_match.start()):
            best_match = match
    if best_match:
        title = _clean_title(text[: best_match.start()])
        remainder = _normalize_ws(text[best_match.start() :])
        return title, remainder
    return _clean_title(text), ""


def _parse_genre_and_details(remainder: str) -> tuple[str, str]:
    remainder = _normalize_ws(remainder)
    if not remainder:
        return "", ""
    leading_note = ""
    note_match = re.match(r"^(\[[^\]]+\])\s*[-\u2013]?\s*(.*)$", remainder)
    if note_match:
        leading_note = note_match.group(1)
        remainder = _normalize_ws(note_match.group(2))
    if ":" in remainder:
        genre, details = remainder.split(":", 1)
        details = _normalize_ws(" ".join(part for part in [leading_note, details] if part))
        return _normalize_ws(genre), details
    return _normalize_ws(remainder), leading_note


def _parse_game_item(
    item_line: str, detail_line: str, url_by_title: dict[str, str], today: date
) -> dict[str, str] | None:
    score = _extract_score(item_line)
    cleaned_item = re.sub(r"^(?:\d{2,3}|tbd)\s+", "", item_line, flags=re.IGNORECASE).strip()
    title, platforms = _split_game_title_platforms(cleaned_item)
    if not title:
        return None

    detail_match = GAME_DATE_RE.match(detail_line)
    if not detail_match:
        return None
    genre = _normalize_ws(detail_match.group("genre"))
    release_date = _parse_article_date(detail_match.group("date"), today.year, today)
    extra = _normalize_ws(detail_match.group("extra").lstrip(" -"))
    release_type = "Expansion" if "expansion" in genre.lower() or "expansion" in extra.lower() else "Game"
    metacritic_url = metacritic_url_for_row(
        {"Title Name": title, "Content Format": "Video Game", "Source URL": url_by_title.get(_title_key(title), "")},
        default_media_type="game",
    )

    return {
        "Title Name": title,
        "Studio/Publisher": "",
        "Release Type": release_type,
        "Genre": genre,
        "Release Date": release_date.isoformat(),
        "Content Format": "Video Game",
        "Availability / Network": platforms,
        "Metacritic Score": score,
        "Source URL": url_by_title.get(_title_key(title), ""),
        "Metacritic URL": metacritic_url,
        "Other Details": extra,
    }


def _parse_game_tables(soup: BeautifulSoup, today: date) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    root = soup.find("article") or soup.find("main") or soup.body or soup
    for table in root.find_all("table"):
        for table_row in table.find_all("tr"):
            parsed = _parse_game_table_row(table_row, today)
            if parsed:
                rows.append(parsed)
    return rows


def _parse_game_table_row(table_row, today: date) -> dict[str, str] | None:
    cells = table_row.find_all(["td", "th"])
    if len(cells) < 2:
        return None
    score = _extract_score(_normalize_ws(cells[0].get_text(" ")))
    title_cell = cells[1]
    title_anchor = _first_title_anchor(title_cell)
    cell_text = _normalize_ws(title_cell.get_text(" "))
    if title_anchor:
        title = _clean_title(title_anchor.get_text(" ", strip=True))
        source_url = urljoin(GAME_RELEASE_URL, title_anchor.get("href", ""))
        remainder = _normalize_ws(re.sub(rf"^{re.escape(title)}\s*", "", cell_text, flags=re.IGNORECASE))
    else:
        source_url = ""
        title, platforms = _split_game_title_platforms(cell_text)
        remainder = _normalize_ws(cell_text.replace(title, "", 1))

    if not title:
        return None
    platforms, detail = _split_platforms_from_game_remainder(remainder)
    detail_match = GAME_DATE_RE.match(detail)
    if not detail_match:
        return None
    genre = _normalize_ws(detail_match.group("genre"))
    release_date = _parse_article_date(detail_match.group("date"), today.year, today)
    extra = _normalize_ws(detail_match.group("extra").lstrip(" -"))
    release_type = "Expansion" if "expansion" in genre.lower() or "expansion" in extra.lower() else "Game"
    metacritic_url = metacritic_url_for_row(
        {"Title Name": title, "Content Format": "Video Game", "Source URL": source_url},
        default_media_type="game",
    )
    return {
        "Title Name": title,
        "Studio/Publisher": "",
        "Release Type": release_type,
        "Genre": genre,
        "Release Date": release_date.isoformat(),
        "Content Format": "Video Game",
        "Availability / Network": platforms,
        "Metacritic Score": score,
        "Source URL": source_url,
        "Metacritic URL": metacritic_url,
        "Other Details": extra,
    }


def _parse_movie_item(
    lines: list[str],
    release_date: date,
    url_by_title: dict[str, str],
) -> dict[str, str] | None:
    item_line, remaining_lines = _movie_item_line_and_remaining_lines(lines)
    title = _movie_title_from_item_line(item_line)
    if not title:
        return None

    score = _extract_score(item_line)
    studio, detail_lines = _movie_studio_and_details(remaining_lines)
    genre, availability, details = _parse_movie_details(detail_lines)
    content_format = _infer_movie_content_format(availability)
    release_type = _infer_movie_release_type(availability, details)
    source_url = url_by_title.get(_title_key(title), "")
    metacritic_url = metacritic_url_for_row(
        {
            "Title Name": title,
            "Release Type": release_type,
            "Content Format": content_format,
            "Source URL": source_url,
        },
        default_media_type="movie",
    )

    return {
        "Title Name": title,
        "Studio/Publisher": studio,
        "Release Type": release_type,
        "Genre": genre,
        "Release Date": release_date.isoformat(),
        "Content Format": content_format,
        "Availability / Network": availability,
        "Metacritic Score": score,
        "Source URL": source_url,
        "Metacritic URL": metacritic_url,
        "Other Details": details,
    }


def _movie_item_line_and_remaining_lines(lines: list[str]) -> tuple[str, list[str]]:
    if not lines:
        return "", []
    first = _normalize_ws(lines[0])
    if re.fullmatch(r"(?:\d{2,3}|tbd)", first, flags=re.IGNORECASE) and len(lines) > 1:
        return _normalize_ws(f"{first} {lines[1]}"), lines[2:]
    return first, lines[1:]


def _movie_title_from_item_line(item_line: str) -> str:
    cleaned = re.sub(r"^(?:\d{2,3}|tbd)\s+", "", _normalize_ws(item_line), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+[-\u2013]\s+Trailer\d?.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bTrailer\d?\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\[[^\]]+\]\s*$", "", cleaned)
    return _clean_title(cleaned)


def _movie_studio_and_details(lines: list[str]) -> tuple[str, list[str]]:
    studio = ""
    details: list[str] = []
    for line in lines:
        text = _normalize_ws(line)
        if not text:
            continue
        if text in {"-", "\u2013"} or text.lower().startswith("trailer"):
            continue
        if not studio:
            match = re.match(r"^\(([^)]+)\)$", text)
            if match and _looks_like_movie_studio_parenthetical(text):
                studio = _normalize_ws(match.group(1))
                continue
        details.append(text)
    return studio, details


def _looks_like_movie_studio_parenthetical(text: str) -> bool:
    lowered = text.lower()
    if lowered.startswith("(also") or lowered.startswith("(wide") or lowered.startswith("(vod"):
        return False
    if "more cities" in lowered or ":" in text:
        return False
    return True


def _parse_movie_details(detail_lines: list[str]) -> tuple[str, str, str]:
    if not detail_lines:
        return "", "", ""

    joined = _normalize_ws(" ".join(detail_lines))
    availability = _movie_availability_from_text(joined)
    genre = _movie_genre_from_line(detail_lines[0])
    details = joined
    if genre:
        details = re.sub(rf"^{re.escape(genre)}\s*", "", details, flags=re.IGNORECASE)
    for marker in _movie_availability_values(joined):
        details = re.sub(re.escape(marker), "", details, flags=re.IGNORECASE)
    details = _normalize_ws(details.strip(" -;/"))
    return genre, availability, details


def _movie_genre_from_line(line: str) -> str:
    text = _normalize_ws(line)
    if not text:
        return ""
    text = re.sub(r"\s+\([^)]+\)\s*$", "", text)
    text = re.split(r"\s+-\s+", text, maxsplit=1)[0]
    availability_starts = [
        match.start()
        for pattern in MOVIE_AVAILABILITY_PATTERNS
        for match in re.finditer(rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])", text, flags=re.IGNORECASE)
    ]
    if availability_starts:
        text = text[: min(availability_starts)]
    genre = _normalize_ws(text.strip(" -;/"))
    if _looks_like_movie_genre(genre):
        return genre
    return ""


def _looks_like_movie_genre(value: str) -> bool:
    if not value or len(value) > 80:
        return False
    parts = re.split(r"[/,]", value)
    if not parts:
        return False
    known = {term.lower() for term in MOVIE_GENRE_TERMS}
    return all(part.strip().lower() in known for part in parts if part.strip())


def _movie_availability_values(text: str) -> list[str]:
    found: list[str] = []
    spans: list[tuple[int, int]] = []
    for pattern in MOVIE_AVAILABILITY_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            if any(match.start() >= start and match.end() <= end for start, end in spans):
                continue
            value = _normalize_ws(match.group(0))
            canonical = value.upper() if value.upper() in {"WIDE", "RENT/BUY", "VOD", "NY", "LA", "IMAX"} else value
            if canonical not in found:
                found.append(canonical)
                spans.append((match.start(), match.end()))
    return found


def _movie_availability_from_text(text: str) -> str:
    return "; ".join(_movie_availability_values(text))


def _infer_movie_content_format(availability: str) -> str:
    lowered = availability.lower()
    if "streaming" in lowered:
        return "Streaming Movie"
    if "rent/buy" in lowered or "vod" in lowered:
        return "VOD Movie"
    return "Movie"


def _infer_movie_release_type(availability: str, details: str) -> str:
    combined = f"{availability} {details}".lower()
    if "rerelease" in combined:
        return "Rerelease"
    if "event screening" in combined or "event screeing" in combined:
        return "Event Screening"
    return "Movie"


def _split_platforms_from_game_remainder(remainder: str) -> tuple[str, str]:
    tokens = remainder.split()
    platforms: list[str] = []
    while tokens and tokens[0].rstrip("*") in PLATFORM_CODES:
        platforms.append(tokens.pop(0))
    return " ".join(platforms), _normalize_ws(" ".join(tokens))


def _parse_tv_details(detail_lines: list[str], joined: str) -> tuple[str, str, str]:
    genre = ""
    availability = ""
    details: list[str] = []

    if detail_lines:
        first = detail_lines[0]
        if ":" in first:
            before, after = first.split(":", 1)
            genre = _normalize_ws(before)
            found_networks = _find_networks(after)
            availability = found_networks or _find_networks(joined)
            detail_text = _strip_known_network_suffix(after)
            if detail_text:
                details.append(detail_text)
        else:
            split = _split_genre_and_network(first)
            genre = split[0]
            availability = split[1]
        for extra in detail_lines[1:]:
            if _is_noise(extra):
                continue
            if not availability:
                availability = _find_networks(extra)
            if not _line_is_only_network(extra):
                details.append(_strip_known_network_suffix(extra))

    if not genre:
        genre = _guess_genre(joined)
    if not availability:
        availability = _find_networks(joined)
    if "rent/buy" in joined.lower() and "RENT/BUY" not in availability:
        availability = (availability + "; RENT/BUY").strip("; ")

    detail_text = _normalize_ws(" ".join(part for part in details if part))
    return genre, availability, detail_text


def _split_genre_and_network(line: str) -> tuple[str, str]:
    normalized = _normalize_ws(line)
    best_index = None
    best_marker = ""
    for marker in NETWORK_MARKERS:
        match = _marker_search(marker, normalized)
        if match and (best_index is None or match.start() < best_index):
            best_index = match.start()
            best_marker = normalized[match.start() :]
    if best_index is None:
        return normalized, ""
    return _normalize_ws(normalized[:best_index]), _normalize_ws(best_marker)


def _strip_known_network_suffix(line: str) -> str:
    text = _normalize_ws(line)
    for marker in sorted(NETWORK_MARKERS, key=len, reverse=True):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(marker)}(?![A-Za-z0-9]).*$"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
    return _normalize_ws(text)


def _find_networks(text: str) -> str:
    found: list[str] = []
    for marker in NETWORK_MARKERS:
        if _marker_search(marker, text):
            canonical = "RENT/BUY" if marker.upper() == "RENT/BUY" else marker
            if canonical not in found:
                found.append(canonical)
    return "; ".join(found)


def _line_is_only_network(line: str) -> bool:
    cleaned = _normalize_ws(line)
    return bool(cleaned) and cleaned == _find_networks(cleaned)


def _starts_with_network(line: str) -> bool:
    cleaned = _normalize_ws(line)
    return any(_marker_search(marker, cleaned, start_only=True) for marker in NETWORK_MARKERS)


def _marker_search(marker: str, text: str, start_only: bool = False):
    prefix = r"^" if start_only else r"(?<![A-Za-z0-9])"
    pattern = prefix + re.escape(marker) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE)


def _looks_like_tv_title_line(text: str, current_item: list[str]) -> bool:
    lowered = text.lower()
    if not current_item:
        return not _is_noise(text) and not lowered.startswith("did you miss")
    if "image:" in lowered or " trailer" in lowered or lowered.endswith("trailer"):
        return True
    if re.match(r"^(?:\d{2,3}|tbd|\(\$\)|\$)", text, flags=re.IGNORECASE):
        return True
    if ":" in text:
        return False
    if _line_is_only_network(text):
        return False
    if _starts_with_network(text) or text.lower().startswith("(also on"):
        return False
    genreish = _guess_genre(text)
    if genreish and (genreish == text or text.lower().startswith(genreish.lower())):
        return False
    return True


def _looks_like_game_item_line(text: str) -> bool:
    if _is_noise(text) or text.startswith("#"):
        return False
    if " - " in text:
        return False
    tokens = text.split()
    return any(token.rstrip("*") in PLATFORM_CODES for token in tokens)


def _looks_like_movie_title_line(text: str, current_item: list[str]) -> bool:
    if _is_noise(text):
        return False
    lowered = text.lower()
    if text in {"-", "\u2013"} or lowered.startswith("trailer"):
        return False
    if text.startswith("(") or _line_is_only_movie_availability(text):
        return False
    if _looks_like_movie_genre(_movie_genre_from_line(text)):
        return False
    if re.fullmatch(r"(?:\d{2,3}|tbd)", text, flags=re.IGNORECASE):
        return True
    if re.match(r"^(?:\d{2,3}|tbd)\s+", text, flags=re.IGNORECASE):
        return True
    if "trailer" in lowered:
        return True
    return not current_item and not any(marker in lowered for marker in ("looking for previous", "release calendar"))


def _line_is_only_movie_availability(text: str) -> bool:
    cleaned = _normalize_ws(text)
    if not cleaned:
        return False
    availability = _movie_availability_from_text(cleaned)
    if availability == cleaned:
        return True
    return cleaned.upper() in {"WIDE", "RENT/BUY", "VOD", "NY", "LA", "IMAX"}


def _split_game_title_platforms(text: str) -> tuple[str, str]:
    tokens = text.split()
    platform_tokens: list[str] = []
    while tokens and tokens[-1].rstrip("*") in PLATFORM_CODES:
        platform_tokens.insert(0, tokens.pop())
    title = _clean_title(" ".join(tokens))
    platforms = " ".join(platform_tokens)
    return title, platforms


def _infer_tv_release_type(text: str) -> str:
    lowered = text.lower()
    if "limited series" in lowered:
        return "Limited Series"
    if "new series" in lowered:
        return "New Series"
    if "image: movie" in lowered or re.search(r"\bmovie\b", lowered):
        return "Movie"
    if re.search(r"\bspecial\b", lowered):
        return "Special"
    if re.search(r"\bseason\s+\d+|\bs\d+\b|\bnew season\b", lowered):
        return "Returning Series"
    if "live event" in lowered:
        return "Live Event"
    return "TV Series"


def _infer_tv_content_format(release_type: str, text: str) -> str:
    lowered = text.lower()
    if release_type == "Movie":
        return "TV Movie"
    if release_type == "Special":
        return "Special"
    if "vod" in lowered or "rent/buy" in lowered:
        return "VOD"
    if release_type == "Live Event":
        return "Live Event"
    return "TV Series"


def _tv_matrix_values(
    release_type: str,
    content_format: str,
    genre: str,
    availability: str,
    details: str,
    source_text: str,
) -> dict[str, str]:
    if not _is_series_like_release(release_type, content_format):
        return {"Daypart": "", "Program Type": "", "Language Type": ""}

    combined = _normalize_ws(" ".join([genre, availability, details, source_text]))
    return {
        "Daypart": _infer_daypart(combined),
        "Program Type": _infer_program_type(release_type, content_format, combined),
        "Language Type": _infer_language_type(genre, details, combined),
    }


def _is_series_like_release(release_type: str, content_format: str) -> bool:
    combined = f"{release_type} {content_format}".lower()
    if re.search(r"\b(movie|special|live event)\b", combined):
        return False
    return "series" in combined or "tv show" in combined


def _infer_daypart(text: str) -> str:
    hour = _extract_hour_24(text)
    if hour is not None:
        if 5 <= hour < 9:
            return "Early Morning"
        if 9 <= hour < 16:
            return "Daytime"
        if 16 <= hour < 19:
            return "Early Fringe"
        if 19 <= hour < 23:
            return "Primetime"
        return "Late Night"
    if _is_streaming_release(text):
        return "Streaming"
    return ""


def _extract_hour_24(text: str) -> int | None:
    match = TIME_RE.search(text)
    if match:
        hour = int(match.group("hour"))
        meridiem = match.group("meridiem").lower()
        if meridiem == "a":
            return 0 if hour == 12 else hour
        return hour if hour == 12 else hour + 12

    broadcast_match = BROADCAST_TIME_RE.search(text)
    if broadcast_match:
        hour = int(broadcast_match.group("hour"))
        if 7 <= hour <= 11:
            return hour + 12
        return hour
    return None


def _is_streaming_release(text: str) -> bool:
    return any(_marker_search(marker, text) for marker in STREAMING_NETWORKS)


def _infer_program_type(release_type: str, content_format: str, text: str) -> str:
    combined = f"{release_type} {content_format} {text}".lower()
    if "limited series" in combined or "miniseries" in combined or "mini series" in combined:
        return "Mini Series"
    if "tv show" in combined:
        return "TV Show"
    if "series" in combined:
        return "Series"
    return ""


def _infer_language_type(genre: str, details: str, text: str) -> str:
    combined = f"{genre} {details} {text}".lower()
    if re.search(r"\b(foreign|non-english|non english|subtitled)\b", combined):
        return "Foreign"
    return "English"


def _guess_genre(text: str) -> str:
    genre_terms = [
        "Action",
        "Adventure",
        "Anime",
        "Animation",
        "Comedy",
        "Crime",
        "Documentary",
        "Drama",
        "Family",
        "Fantasy",
        "Food",
        "Foreign",
        "Game Show",
        "Horror",
        "Live Event",
        "Music",
        "Reality Competition",
        "Reality",
        "Rom-Com",
        "Sci-Fi",
        "Sports",
        "Thriller",
        "Variety",
    ]
    for term in sorted(genre_terms, key=len, reverse=True):
        if re.match(rf"^{re.escape(term)}(?:\b|/)", text, flags=re.IGNORECASE):
            split = _split_genre_and_network(text)
            return split[0]
    return ""


def _parse_article_date(value: str, current_year: int, today: date) -> date:
    cleaned = value.replace(".", "")
    default = datetime(current_year, 1, 1)
    parsed = date_parser.parse(cleaned, default=default, fuzzy=True).date()
    if "," not in cleaned and parsed < today - timedelta(days=45):
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed


def _extract_score(text: str) -> str:
    match = SCORE_RE.match(text.strip())
    if not match:
        return ""
    score = match.group("score")
    return score.lower() if score.lower() == "tbd" else score


def _clean_title(text: str) -> str:
    cleaned = _normalize_ws(text)
    cleaned = re.sub(r"^\d{2,3}\s+(?=\(\$\))", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^tbd\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\(\$\)\s*", "", cleaned)
    cleaned = re.sub(r"\s*\(\$\)\s*", " ", cleaned)
    cleaned = re.sub(r"\s+[-\u2013]\s+Trailer\d?.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bTrailer\d?\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"Image:\s*(?:new series|limited series|movie|bar)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\d+p(?:\s+[A-Z]{2})?(?:\s*/\s*\d+p\s+[A-Z]{2})?$", "", cleaned, flags=re.IGNORECASE)
    cleaned = _normalize_ws(cleaned)
    return cleaned.strip(" -:")


def _title_key(title: str) -> str:
    title = _clean_title(title).lower()
    title = re.sub(r"&", " and ", title)
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return _normalize_ws(title)


def metacritic_url_for_row(row: dict[str, str], default_media_type: str = "tv") -> str:
    direct_url = row.get("Metacritic URL") or row.get("Source URL")
    if direct_url:
        return direct_url
    title = row.get("Title Name") or row.get("title") or ""
    slug = _metacritic_slug(title)
    if not slug:
        return ""
    media_type = _metacritic_media_type(row, default_media_type)
    return f"{METACRITIC_BASE_URL}/{media_type}/{slug}/"


def _metacritic_media_type(row: dict[str, str], default_media_type: str) -> str:
    combined = " ".join(
        [
            row.get("Release Type", ""),
            row.get("Content Format", ""),
            row.get("Other Details", ""),
        ]
    ).lower()
    if "video game" in combined or default_media_type == "game":
        return "game"
    if re.search(r"\bmovie\b", combined):
        return "movie"
    return default_media_type or "tv"


def _metacritic_slug(title: str) -> str:
    title = _clean_title(title)
    title = re.sub(r"&", " and ", title)
    title = re.sub(r"['\u2019]", "", title)
    title = re.sub(r"[^A-Za-z0-9]+", "-", title)
    title = title.strip("-").lower()
    return quote(title)


def _normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _is_noise(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    if lowered in NOISE_LINES:
        return True
    if lowered.startswith("image: bar"):
        return True
    if lowered.startswith("video and images"):
        return True
    if lowered.startswith("movie title data"):
        return True
    if lowered.startswith("copyright") or lowered.startswith("\u00a9"):
        return True
    return False


def _dedupe_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, str]] = []
    for row in rows:
        key = (row.get("Title Name", "").lower(), row.get("Release Date", ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def _filter_tv_premiere_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in rows if not _is_excluded_tv_premiere_title(row.get("Title Name", ""))]


def _filter_movie_release_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if "rent/buy" not in row.get("Availability / Network", "").lower()
    ]


def _is_excluded_tv_premiere_title(title: str) -> bool:
    return bool(re.search(r"\b(?:special|speical|live\s+event)\b", title or "", flags=re.IGNORECASE))
