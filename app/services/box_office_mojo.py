from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit
import re

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from app.models import BOX_OFFICE_COLUMNS, BOX_OFFICE_RELEASE_DATE_CHANGE_COLUMNS, utc_now_iso
from app.services.http_client import HttpClient


BOX_OFFICE_MOJO_CALENDAR_URL = "https://www.boxofficemojo.com/calendar/"

ProgressCallback = Callable[[int, str], None]


class BoxOfficeMojoService:
    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client

    def fetch_us_movie_releases(
        self,
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
        calendar_url = urljoin(BOX_OFFICE_MOJO_CALENDAR_URL, f"{start_date.isoformat()}/")
        if progress:
            progress(8, "Fetching Box Office Mojo domestic release schedule")
        html = self.http_client.get_text(calendar_url)
        if progress:
            progress(28, "Parsing US movie releases for the selected date window")
        rows = self.parse_calendar(html, start_date=start_date, end_date=end_date)

        total = max(len(rows), 1)
        enriched_rows: list[dict[str, str]] = []
        for index, row in enumerate(rows, start=1):
            if progress:
                percent = 28 + int((index / total) * 55)
                progress(min(percent, 83), f"Fetching Box Office Mojo details for {row['Title Name']}")
            enriched_rows.append(self.enrich_release(row))

        enriched_rows.sort(key=lambda item: (item["Release Date"], item["Title Name"].lower()))
        if progress:
            progress(90, "Preparing Box Office Mojo release snapshot")

        return {
            "tracker_type": "boxoffice",
            "title": "Box Office Mojo US Movie Releases",
            "created_at": utc_now_iso(),
            "source_url": calendar_url,
            "summary": (
                f"Scanned Box Office Mojo domestic releases from {start_date.isoformat()} "
                f"through {end_date.isoformat()} and enriched each public release page with Opening "
                "and Widest Release when available."
            ),
            "sections": [
                {
                    "key": "boxoffice",
                    "title": "US Movie Releases",
                    "columns": BOX_OFFICE_COLUMNS,
                    "rows": enriched_rows,
                    "row_count": len(enriched_rows),
                    "supports_google": True,
                }
            ],
        }

    def attach_recent_release_date_changes(
        self,
        snapshot: dict[str, Any],
        previous_snapshots: list[dict[str, Any]],
        lookback_days: int = 14,
    ) -> dict[str, Any]:
        change_rows = _release_date_change_rows(snapshot, previous_snapshots)
        snapshot.setdefault("sections", []).append(
            {
                "key": "boxoffice_release_date_changes",
                "title": f"Release Date Changes Detected in Last {lookback_days} Days",
                "columns": BOX_OFFICE_RELEASE_DATE_CHANGE_COLUMNS[:],
                "rows": change_rows,
                "row_count": len(change_rows),
                "supports_google": True,
            }
        )
        history_note = (
            f"Release Date Changes compares this run with saved Box Office Mojo runs from the last "
            f"{lookback_days} days, so it will be empty until at least one earlier Box Office Mojo run exists."
        )
        summary = str(snapshot.get("summary", "") or "").strip()
        if history_note not in summary:
            snapshot["summary"] = f"{summary} {history_note}".strip()
        return snapshot

    def fetch_release_schedule_changes(
        self,
        previous_snapshots: list[dict[str, Any]],
        today: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        history_lookback_days: int = 14,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        today = today or date.today()
        start_date = start_date or today - timedelta(days=7)
        end_date = end_date or today + timedelta(days=30)
        if end_date < start_date:
            raise ValueError("End date must be the same as or later than the start date.")

        calendar_url = urljoin(BOX_OFFICE_MOJO_CALENDAR_URL, f"{start_date.isoformat()}/")
        if progress:
            progress(8, "Fetching Box Office Mojo schedule for release changes")
        html = self.http_client.get_text(calendar_url)
        if progress:
            progress(45, "Parsing releases from last week through the next month")
        current_rows = self.parse_calendar(html, start_date=start_date, end_date=end_date)
        current_rows.sort(key=lambda item: (item["Release Date"], item["Title Name"].lower()))
        if progress:
            progress(85, "Comparing release dates with recent saved schedules")
        change_rows = _release_date_change_rows_for_rows(current_rows, previous_snapshots)

        return {
            "tracker_type": "release_schedule_changes",
            "title": "Release Schedule Changes",
            "created_at": utc_now_iso(),
            "source_url": calendar_url,
            "boxoffice_schedule_rows": current_rows,
            "summary": (
                f"Compared Box Office Mojo domestic movie releases from {start_date.isoformat()} "
                f"through {end_date.isoformat()} against saved Box Office Mojo schedules from the last "
                f"{history_lookback_days} days. This section shows only movies whose release date changed "
                "between saved runs."
            ),
            "sections": [
                {
                    "key": "release_schedule_changes",
                    "title": "Release Schedule Changes",
                    "columns": BOX_OFFICE_RELEASE_DATE_CHANGE_COLUMNS[:],
                    "rows": change_rows,
                    "row_count": len(change_rows),
                    "supports_google": True,
                }
            ],
        }

    def parse_calendar(self, html: str, start_date: date, end_date: date) -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        rows: list[dict[str, str]] = []
        current_date: date | None = None
        for table_row in soup.find_all("tr"):
            cells = table_row.find_all(["td", "th"])
            if not cells:
                continue
            if len(cells) == 1:
                parsed_date = _try_parse_release_date(cells[0].get_text(" ", strip=True))
                if parsed_date:
                    current_date = parsed_date
                continue
            if len(cells) < 3 or current_date is None:
                continue
            if not (start_date <= current_date <= end_date):
                continue

            release_cell = cells[0]
            title_link = _first_release_link(release_cell)
            title = _clean_text(title_link.get_text(" ", strip=True)) if title_link else ""
            if not title:
                title = _fallback_title(release_cell)
            if not title:
                continue

            source_url = urljoin(BOX_OFFICE_MOJO_CALENDAR_URL, title_link.get("href", "")) if title_link else ""
            rows.append(
                {
                    "Title Name": title,
                    "Distributor": _clean_text(cells[1].get_text(" ", strip=True)),
                    "Scale": _clean_text(cells[2].get_text(" ", strip=True)),
                    "Release Date": current_date.isoformat(),
                    "Opening": "",
                    "Widest Release": "",
                    "Genre": _extract_calendar_genre(release_cell, title),
                    "Running Time": _extract_running_time(release_cell.get_text(" ", strip=True)),
                    "Source URL": source_url,
                    "Other Details": _extract_cast_details(release_cell.get_text(" ", strip=True)),
                }
            )
        return _dedupe_rows(rows)

    def enrich_release(self, row: dict[str, str]) -> dict[str, str]:
        source_url = row.get("Source URL", "")
        if not source_url:
            return row
        try:
            html = self.http_client.get_text(source_url)
        except Exception as exc:
            row["Other Details"] = _append_detail(row.get("Other Details", ""), f"Detail fetch failed: {exc}")
            return row

        soup = BeautifulSoup(html, "html.parser")
        row["Opening"] = _extract_detail_value(soup, "Opening") or row.get("Opening", "")
        row["Widest Release"] = _extract_detail_value(soup, "Widest Release") or row.get("Widest Release", "")
        row["Running Time"] = _extract_detail_value(soup, "Running Time") or row.get("Running Time", "")
        detail_genres = _extract_detail_value(soup, "Genres")
        if detail_genres:
            row["Genre"] = _clean_text(detail_genres)
        return row


def _first_release_link(cell):
    for anchor in cell.find_all("a"):
        href = anchor.get("href", "")
        text = _clean_text(anchor.get_text(" ", strip=True))
        if "/release/" not in href:
            continue
        if not text:
            continue
        return anchor
    return None


def _fallback_title(cell) -> str:
    text = _clean_text(cell.get_text(" ", strip=True))
    text = re.split(r"\b(?:Action|Adventure|Animation|Biography|Comedy|Crime|Documentary|Drama|Family|Fantasy|History|Horror|Music|Mystery|Romance|Sci-Fi|Thriller|Western|With:)\b", text, maxsplit=1)[0]
    return _clean_text(text)


def _extract_calendar_genre(cell, title: str) -> str:
    text = _clean_text(cell.get_text(" ", strip=True))
    after_title = _clean_text(re.sub(rf"^{re.escape(title)}", "", text, flags=re.IGNORECASE))
    genre_text = re.split(r"\bWith:\b|\b\d+\s+hr\b|\bCast,\s*Crew", after_title, maxsplit=1)[0]
    return _clean_text(genre_text)


def _extract_running_time(text: str) -> str:
    match = re.search(r"\b\d+\s+hr(?:\s+\d+\s+min)?\b|\b\d+\s+min\b", text)
    return match.group(0) if match else ""


def _extract_cast_details(text: str) -> str:
    match = re.search(r"\bWith:\s*(.*?)(?:\b\d+\s+hr\b|\b\d+\s+min\b|Cast,\s*Crew|$)", _clean_text(text))
    return _clean_text(f"With: {match.group(1)}") if match else ""


def _extract_detail_value(soup: BeautifulSoup, label: str) -> str:
    node = soup.find(string=lambda value: bool(value and _clean_text(value) == label))
    if not node:
        return ""
    parent = node.parent
    container = parent.parent if parent else None
    text = _clean_text(container.get_text(" ", strip=True) if container else parent.get_text(" ", strip=True))
    text = re.sub(rf"^{re.escape(label)}\s*", "", text, flags=re.IGNORECASE)
    return _clean_text(text)


def _try_parse_release_date(value: str) -> date | None:
    value = _clean_text(value)
    if not re.search(r"\b20\d{2}\b", value):
        return None
    try:
        return date_parser.parse(value, fuzzy=True).date()
    except (TypeError, ValueError):
        return None


def _append_detail(existing: str, addition: str) -> str:
    return "; ".join(part for part in [existing, addition] if part)


def _release_date_change_rows(
    current_snapshot: dict[str, Any],
    previous_snapshots: list[dict[str, Any]],
) -> list[dict[str, str]]:
    return _release_date_change_rows_for_rows(_boxoffice_rows(current_snapshot), previous_snapshots)


def _release_date_change_rows_for_rows(
    current_rows: list[dict[str, Any]],
    previous_snapshots: list[dict[str, Any]],
) -> list[dict[str, str]]:
    previous_by_key = _previous_release_dates_by_key(previous_snapshots)
    changes: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for current_row in current_rows:
        key = _release_identity(current_row)
        if not key:
            continue
        new_date = _clean_text(str(current_row.get("Release Date", "") or ""))
        if not new_date:
            continue
        previous_dates = previous_by_key.get(key, [])
        old_date = next((item["release_date"] for item in previous_dates if item["release_date"] != new_date), "")
        if not old_date:
            continue
        title = _clean_text(str(current_row.get("Title Name", "") or ""))
        change_key = (key, old_date, new_date)
        if change_key in seen:
            continue
        seen.add(change_key)
        changes.append(
            {
                "Title Name": title,
                "Old Release Date": old_date,
                "New Release Date": new_date,
                "Release Date Change": _release_date_change_label(old_date, new_date),
            }
        )
    changes.sort(key=lambda item: (item["New Release Date"], item["Title Name"].lower()))
    return changes


def _release_date_change_label(old_date: str, new_date: str) -> str:
    try:
        old = date.fromisoformat(old_date)
        new = date.fromisoformat(new_date)
    except ValueError:
        return ""
    day_delta = (new - old).days
    if day_delta == 0:
        return "No change"
    direction = "later" if day_delta > 0 else "earlier"
    days = abs(day_delta)
    unit = "day" if days == 1 else "days"
    return f"Moved {direction} by {days} {unit}"


def _previous_release_dates_by_key(previous_snapshots: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    previous_by_key: dict[str, list[dict[str, str]]] = {}
    for snapshot in previous_snapshots:
        run_id = str(snapshot.get("run_id", "") or "")
        created_at = str(snapshot.get("created_at", "") or "")
        for row in _boxoffice_rows(snapshot):
            key = _release_identity(row)
            release_date = _clean_text(str(row.get("Release Date", "") or ""))
            if not key or not release_date:
                continue
            previous_by_key.setdefault(key, []).append(
                {
                    "release_date": release_date,
                    "run_id": run_id,
                    "created_at": created_at,
                }
            )
    return previous_by_key


def _boxoffice_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in snapshot.get("boxoffice_schedule_rows", []):
        if isinstance(row, dict):
            rows.append(row)
    for section in snapshot.get("sections", []):
        if not isinstance(section, dict):
            continue
        if section.get("key") != "boxoffice":
            continue
        for row in section.get("rows", []):
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _release_identity(row: dict[str, Any]) -> str:
    source_url = str(row.get("Source URL", "") or "")
    if source_url:
        path = urlsplit(source_url).path.rstrip("/").lower()
        if "/release/" in path:
            return path
    title = _clean_text(str(row.get("Title Name", "") or "")).lower()
    return re.sub(r"[^a-z0-9]+", " ", title).strip()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, str]] = []
    for row in rows:
        key = (row.get("Title Name", "").lower(), row.get("Release Date", ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output
