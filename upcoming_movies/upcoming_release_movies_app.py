from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_NAME = "Upcoming Release Movies"
BASE_URL = "https://www.boxofficemojo.com"
DEFAULT_RANGE_MONTHS = 18
CACHE_TTL_SECONDS = 15 * 60
ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"

_PAGE_CACHE: dict[str, tuple[float, str]] = {}


@dataclass
class CalendarCell:
    tag: str
    class_name: str
    text_parts: list[str]
    links: list[str]
    title: str = ""
    genre_parts: list[str] | None = None
    image_url: str = ""

    def clean_text(self) -> str:
        return clean_text(" ".join(self.text_parts))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def attrs_to_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name: value or "" for name, value in attrs}


def has_class(class_name: str, wanted: str) -> bool:
    return wanted in (class_name or "").split()


def absolute_url(href: str) -> str:
    if not href:
        return ""
    return urllib.parse.urljoin(BASE_URL, href)


def metacritic_url_for_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_title = ascii_title.replace("&", " and ")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_title.lower()).strip("-")
    return f"https://www.metacritic.com/movie/{slug}/" if slug else ""


def add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    month_lengths = [31, 29 if is_leap_year(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(start.day, month_lengths[month - 1]))


def is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def month_starts(start: date, end: date) -> list[date]:
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    months: list[date] = []
    while current <= last:
        months.append(current)
        current = add_months(current, 1)
    return months


def parse_date_param(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD.") from exc


def parse_display_date(value: str) -> date | None:
    try:
        return datetime.strptime(clean_text(value), "%B %d, %Y").date()
    except ValueError:
        return None


def calendar_url_for(month_start: date) -> str:
    return f"{BASE_URL}/calendar/{month_start:%Y-%m-%d}/"


def fetch_html(url: str, refresh: bool = False) -> str:
    cached = _PAGE_CACHE.get(url)
    now = time.time()
    if cached and not refresh and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset, errors="replace")
    _PAGE_CACHE[url] = (now, html)
    return html


class BoxOfficeMojoCalendarParser(HTMLParser):
    def __init__(self, source_url: str):
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.inside_table = False
        self.table_div_depth = 0
        self.current_row: dict[str, Any] | None = None
        self.current_cell: CalendarCell | None = None
        self.current_release_date: date | None = None
        self.capture_title = False
        self.title_parts: list[str] = []
        self.genre_depth = 0
        self.movies: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = attrs_to_dict(attrs)
        class_name = attr_map.get("class", "")

        if tag == "div" and attr_map.get("id") == "table":
            self.inside_table = True
            self.table_div_depth = 1
            return

        if self.inside_table and tag == "div":
            self.table_div_depth += 1

        if not self.inside_table:
            return

        if tag == "tr":
            self.current_row = {"class_name": class_name, "cells": []}
            return

        if self.current_row is None:
            return

        if tag in {"td", "th"}:
            self.current_cell = CalendarCell(
                tag=tag,
                class_name=class_name,
                text_parts=[],
                links=[],
                genre_parts=[],
            )
            return

        if self.current_cell is None:
            return

        if self.genre_depth:
            self.genre_depth += 1

        if tag == "h3":
            self.capture_title = True
            self.title_parts = []
        elif tag == "div" and "mojo-schedule-genres" in class_name:
            self.genre_depth = 1
        elif tag == "a":
            href = attr_map.get("href", "")
            if href:
                self.current_cell.links.append(unescape(href))
        elif tag == "img":
            self.current_cell.image_url = attr_map.get("data-a-hires") or attr_map.get("src") or self.current_cell.image_url

    def handle_data(self, data: str) -> None:
        if self.current_cell is None:
            return
        self.current_cell.text_parts.append(data)
        if self.capture_title:
            self.title_parts.append(data)
        if self.genre_depth and self.current_cell.genre_parts is not None:
            self.current_cell.genre_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current_cell is not None:
            if tag == "h3" and self.capture_title:
                self.current_cell.title = clean_text(" ".join(self.title_parts))
                self.capture_title = False
                self.title_parts = []

            if self.genre_depth:
                self.genre_depth -= 1

            if tag == self.current_cell.tag:
                if self.current_row is not None:
                    self.current_row["cells"].append(self.current_cell)
                self.current_cell = None
                return

        if self.inside_table and tag == "tr" and self.current_row is not None:
            self.finish_row()
            self.current_row = None
            return

        if self.inside_table and tag == "div":
            self.table_div_depth -= 1
            if self.table_div_depth <= 0:
                self.inside_table = False

    def finish_row(self) -> None:
        if not self.current_row:
            return

        row_class = self.current_row["class_name"]
        cells: list[CalendarCell] = self.current_row["cells"]
        if has_class(row_class, "mojo-group-label") and cells:
            self.current_release_date = parse_display_date(cells[0].clean_text())
            return

        if self.current_release_date is None or len(cells) < 3:
            return

        movie = build_movie_from_cells(self.current_release_date, cells[0], cells[1], cells[2], self.source_url)
        if movie.get("title"):
            self.movies.append(movie)


def split_genres(cell: CalendarCell) -> list[str]:
    genre_text = clean_text(" ".join(cell.genre_parts or []))
    genres = [item.strip(" ,") for item in genre_text.split() if item.strip(" ,")]
    return list(dict.fromkeys(genres))


def extract_runtime(raw_text: str) -> str:
    match = re.search(r"\b(\d+\s+hr(?:\s+\d+\s+min)?|\d+\s+min)\b", raw_text)
    return clean_text(match.group(1)) if match else ""


def extract_cast(raw_text: str) -> str:
    match = re.search(r"With:\s*(.*?)(?=(?:\d+\s+hr|\d+\s+min|Cast,\s*Crew|$))", raw_text)
    return clean_text(match.group(1).rstrip(",")) if match else ""


def extract_release_note(cell: CalendarCell, title: str, genres: list[str]) -> str:
    genre_set = set(genres)
    lines = [clean_text(part) for part in cell.text_parts]
    lines = [line for line in lines if line and line != "Cast, Crew, and Company Info"]
    note_parts: list[str] = []
    seen_title = False
    for line in lines:
        if not seen_title:
            if line == title:
                seen_title = True
            continue
        if line in genre_set or line == "With:" or line.startswith("With:"):
            if note_parts:
                break
            continue
        if extract_runtime(line) or line.startswith("Cast,"):
            break
        if line not in genre_set:
            note_parts.append(line)
    return clean_text(" ".join(note_parts))


def infer_release_type(release_note: str) -> str:
    note = release_note.lower()
    if "re-release" in note or "rerelease" in note:
        return "Re-release"
    if "anniversary" in note:
        return "Anniversary"
    if "live viewing" in note:
        return "Live Event"
    if "festival" in note:
        return "Festival"
    if "imax" in note:
        return "IMAX"
    return "Original"


def first_matching_link(links: list[str], pattern: str) -> str:
    for href in links:
        if re.search(pattern, href):
            return href
    return ""


def build_movie_from_cells(
    release_date: date,
    release_cell: CalendarCell,
    distributor_cell: CalendarCell,
    scale_cell: CalendarCell,
    source_url: str,
) -> dict[str, Any]:
    title = release_cell.title or release_cell.clean_text().split(" Cast, Crew")[0]
    raw_text = release_cell.clean_text()
    genres = split_genres(release_cell)
    release_note = extract_release_note(release_cell, title, genres)
    imdb_pro_href = first_matching_link(release_cell.links, r"/title/tt\d+")
    release_href = first_matching_link(release_cell.links, r"/release/rl\d+")
    tt_match = re.search(r"/title/(tt\d+)", imdb_pro_href)
    release_id_match = re.search(r"/release/(rl\d+)", release_href)

    distributor = distributor_cell.clean_text() or "N/A"
    scale = scale_cell.clean_text() or "Unknown"

    return {
        "title": title,
        "release_date": release_date.isoformat(),
        "release_date_display": release_date.strftime("%b %d, %Y"),
        "tt_code": tt_match.group(1) if tt_match else "",
        "imdb_url": f"https://www.imdb.com/title/{tt_match.group(1)}/" if tt_match else "",
        "imdb_pro_url": absolute_url(imdb_pro_href),
        "metacritic_url": metacritic_url_for_title(title),
        "box_office_mojo_release_id": release_id_match.group(1) if release_id_match else "",
        "box_office_mojo_url": absolute_url(release_href),
        "poster_url": release_cell.image_url,
        "distributor_network": distributor,
        "distributor_url": absolute_url(distributor_cell.links[0]) if distributor_cell.links else "",
        "genres": genres,
        "genre": ", ".join(genres),
        "cast": extract_cast(raw_text),
        "runtime": extract_runtime(raw_text),
        "release_note": release_note,
        "release_type": infer_release_type(release_note),
        "release_scale": scale,
        "source": "Box Office Mojo",
        "source_calendar_url": source_url,
    }


def parse_calendar_html(html: str, source_url: str) -> list[dict[str, Any]]:
    parser = BoxOfficeMojoCalendarParser(source_url)
    parser.feed(html)
    return parser.movies


def get_upcoming_release_movies(start_date: date, end_date: date, refresh: bool = False) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")

    all_movies: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_urls = [calendar_url_for(month_start) for month_start in month_starts(start_date, end_date)]

    def load_source(source_url: str) -> tuple[str, list[dict[str, Any]]]:
        html = fetch_html(source_url, refresh=refresh)
        return source_url, parse_calendar_html(html, source_url)

    max_workers = min(6, max(1, len(source_urls)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(load_source, source_url): source_url for source_url in source_urls}
        for future in as_completed(future_to_url):
            source_url = future_to_url[future]
            try:
                _, movies = future.result()
                all_movies.extend(movies)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                warnings.append(f"{source_url}: {exc}")

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for movie in all_movies:
        movie_date = datetime.strptime(movie["release_date"], "%Y-%m-%d").date()
        if not start_date <= movie_date <= end_date:
            continue
        key = (
            movie.get("box_office_mojo_release_id") or movie.get("tt_code") or movie["title"],
            movie["release_date"],
            movie.get("release_scale", ""),
        )
        deduped[key] = movie

    movies = sorted(deduped.values(), key=lambda item: (item["release_date"], item["title"].lower()))
    scale_counts = Counter(movie["release_scale"] or "Unknown" for movie in movies)
    type_counts = Counter(movie["release_type"] or "Unknown" for movie in movies)
    distributor_counts = Counter(movie["distributor_network"] or "N/A" for movie in movies)

    return {
        "app_name": APP_NAME,
        "default_range_months": DEFAULT_RANGE_MONTHS,
        "range": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_movies": len(movies),
            "release_scales": dict(scale_counts),
            "release_types": dict(type_counts),
            "top_distributors": dict(distributor_counts.most_common(12)),
        },
        "source_urls": source_urls,
        "warnings": warnings,
        "movies": movies,
    }


class UpcomingReleaseMoviesHandler(BaseHTTPRequestHandler):
    server_version = "UpcomingReleaseMovies/1.0"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "app_name": APP_NAME,
                    "default_range_months": DEFAULT_RANGE_MONTHS,
                },
            )
            return
        if parsed.path == "/api/upcoming-release-movies":
            self.handle_api(parsed.query)
            return
        self.handle_static(parsed.path)

    def handle_api(self, query_string: str) -> None:
        query = urllib.parse.parse_qs(query_string)
        today = date.today()
        default_start = today
        default_end = add_months(today, DEFAULT_RANGE_MONTHS)
        try:
            start_date = parse_date_param(first_query_value(query, "start_date"), default_start)
            end_date = parse_date_param(first_query_value(query, "end_date"), default_end)
            refresh = first_query_value(query, "refresh") in {"1", "true", "yes"}
            payload = get_upcoming_release_movies(start_date, end_date, refresh=refresh)
            status = HTTPStatus.OK
        except ValueError as exc:
            payload = {"error": str(exc)}
            status = HTTPStatus.BAD_REQUEST
        except Exception as exc:  # pragma: no cover - defensive API boundary
            payload = {"error": f"Unable to load upcoming release movies: {exc}"}
            status = HTTPStatus.BAD_GATEWAY
        self.send_json(status, payload)

    def handle_static(self, path: str) -> None:
        if path in {"", "/"}:
            file_path = STATIC_DIR / "index.html"
        else:
            safe_path = path.lstrip("/").replace("\\", "/")
            if ".." in safe_path.split("/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            file_path = STATIC_DIR / safe_path

        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type_for(file_path))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {format % args}")


def first_query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def content_type_for(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
    }.get(suffix, "application/octet-stream")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Run the {APP_NAME} web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), UpcomingReleaseMoviesHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"{APP_NAME} app running at {url}", flush=True)
    print(f"Default range: today through the next {DEFAULT_RANGE_MONTHS} months.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
