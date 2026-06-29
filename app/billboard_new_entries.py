"""Billboard Artist 100 - "new entries" (LW = dash) section.

Scrapes the public Billboard Artist 100 chart (optionally for a specific date),
keeps only the artists whose Last Week (LW) value is a dash "-" -- i.e. acts that
are new to the chart or re-entering with no last-week position -- and returns the
filtered list as an Excel (.xlsx) download.

This is intentionally self-contained and does NOT use the IMDb index, so it runs
on the free hosted tier. It reuses the existing Billboard parser for the chart
HTML; only the fetch URL (to support a custom date) and the filtering/export are
added here.
"""

from __future__ import annotations

import io
import sys
from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook

from app.config import get_settings
from app.services.billboard import BillboardArtist100Service, BILLBOARD_ARTIST_100_URL
from app.services.http_client import HttpClient
from app.services.imdb import IMDbEnrichmentService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_settings = get_settings()
_http_client = HttpClient(_settings.user_agent, _settings.request_timeout_seconds)
# Shared IMDb dataset service: when IMDB_ENABLED is on, talent (people / groups)
# get their IMDb "nm" code from the official name.basics.tsv.gz dataset; otherwise
# we fall back to the Wikidata lookup so the tool still works on a light deployment.
_imdb_service = IMDbEnrichmentService(
    _settings.imdb_cache_dir,
    _settings.imdb_dataset_max_age_days,
    _http_client,
    _settings.tmdb_api_key,
    _settings.tmdb_read_access_token,
)
_billboard = BillboardArtist100Service(_http_client, _imdb_service)

# Cap how many weekly charts a single date-range request will fetch, to keep runs
# responsive and avoid hammering billboard.com.
MAX_WEEKS_PER_RANGE = 27

# Columns included in the exported workbook.
EXPORT_COLUMNS = [
    "Rank",
    "Artist Name",
    "Last Week",
    "Peak Position",
    "Weeks on Chart",
    "Chart Date",
    "IMDb nmcode",
    "IMDb URL",
    "Wikipedia URL",
    "Billboard Artist URL",
]


def _enrich_imdb_and_wikipedia(rows: list[dict]) -> None:
    """Add IMDb nmcode + URL and Wikipedia URL to each row, in place.

    Billboard Artist 100 acts are *talent* (people or groups), so their IMDb code is
    an "nm" name code looked up from the official name.basics.tsv.gz dataset
    (datasets.imdbws.com) when IMDb dataset features are enabled. If a name isn't
    found in name.basics (or the dataset is disabled on this deployment), we fall
    back to Wikidata, which maps an artist to their canonical IMDb id (property P345).
    The Wikipedia URL always comes from Wikidata. Artists with no match get blank cells.
    """
    imdb_dataset_enabled = bool(_imdb_service and _imdb_service.enabled)
    for row in rows:
        artist = str(row.get("Artist Name", "")).strip()
        nmcode = ""
        wikipedia_url = ""
        if artist:
            # 1) Preferred source for talent: IMDb name.basics dataset.
            if imdb_dataset_enabled:
                try:
                    match = _imdb_service.lookup_name(
                        artist, profession_hint="music artist singer songwriter musician band"
                    )
                    if match and str(match.get("imdb_id", "")).startswith("nm"):
                        nmcode = str(match["imdb_id"]).strip()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[billboard-new-entries] name.basics lookup failed for {artist!r}: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            # 2) Fallback / Wikipedia URL: Wikidata.
            try:
                entity = _billboard._wikidata_entity_for_name(artist) or {}
                if not nmcode:
                    nmcode = str(entity.get("IMDb nmcode (Wikidata P345)", "")).strip()
                wikipedia_url = str(entity.get("Wikipedia URL", "")).strip()
            except Exception as exc:  # noqa: BLE001
                # A single artist's lookup failing should never break the report,
                # but log it so empty cells aren't a silent mystery (visible in Render logs).
                print(
                    f"[billboard-new-entries] Wikidata lookup failed for {artist!r}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        row["IMDb nmcode"] = nmcode
        row["IMDb URL"] = f"https://www.imdb.com/name/{nmcode}/" if nmcode else ""
        row["Wikipedia URL"] = wikipedia_url


def _weekly_chart_dates(start_date: date, end_date: date) -> list[str]:
    """Billboard charts are weekly, so step through the range one week at a time.

    Billboard resolves any date in a chart week to that week's chart, so iterating in
    7-day steps from the start date covers each distinct weekly chart in the range.
    """
    dates: list[str] = []
    cursor = start_date
    while cursor <= end_date and len(dates) < MAX_WEEKS_PER_RANGE:
        dates.append(cursor.isoformat())
        cursor += timedelta(days=7)
    # Make sure the final week is included even if the step overshoots it.
    if dates and end_date.isoformat() not in dates and len(dates) < MAX_WEEKS_PER_RANGE:
        last = date.fromisoformat(dates[-1])
        if last < end_date:
            dates.append(end_date.isoformat())
    return dates

# A "dash" in Last Week can show up as a few different characters.
_DASH_VALUES = {"-", "", "—", "–", "--"}


def _chart_url(chart_date: str) -> str:
    """Billboard serves a specific week at /charts/artist-100/YYYY-MM-DD/."""
    if chart_date:
        return f"{BILLBOARD_ARTIST_100_URL}{chart_date}/"
    return BILLBOARD_ARTIST_100_URL


def _is_new_entry(row: dict) -> bool:
    return str(row.get("Last Week", "")).strip() in _DASH_VALUES


def _render_form(request: Request, *, error_message: str = "", status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "billboard_new_entries.html",
        {"error_message": error_message, "today_iso": date.today().isoformat()},
        status_code=status_code,
    )


@router.get("/billboard-new-entries", response_class=HTMLResponse)
def billboard_new_entries_page(request: Request) -> HTMLResponse:
    return _render_form(request)


@router.post("/billboard-new-entries")
def billboard_new_entries_run(
    request: Request,
    chart_date: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
):
    chart_date = (chart_date or "").strip()
    start_date = (start_date or "").strip()
    end_date = (end_date or "").strip()

    # Decide which weekly charts to scan:
    #   - a start/end range  -> every weekly chart in the range
    #   - a single chart_date -> just that week
    #   - nothing            -> the latest chart
    chart_dates: list[str] = []
    if start_date or end_date:
        if not (start_date and end_date):
            return _render_form(
                request,
                error_message="Enter both a start date and an end date, or leave both blank for the latest chart.",
                status_code=400,
            )
        try:
            range_start = date.fromisoformat(start_date)
            range_end = date.fromisoformat(end_date)
        except ValueError:
            return _render_form(
                request,
                error_message="Enter dates as YYYY-MM-DD.",
                status_code=400,
            )
        if range_end < range_start:
            return _render_form(
                request,
                error_message="The end date must be the same as or later than the start date.",
                status_code=400,
            )
        chart_dates = _weekly_chart_dates(range_start, range_end)
    elif chart_date:
        try:
            date.fromisoformat(chart_date)
        except ValueError:
            return _render_form(
                request,
                error_message="Enter the date as YYYY-MM-DD, or leave it blank for the latest chart.",
                status_code=400,
            )
        chart_dates = [chart_date]
    else:
        chart_dates = [""]  # latest chart

    # Fetch + parse each weekly chart, collecting new entries (LW = "-").
    new_entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    fetch_errors = 0
    for single_date in chart_dates:
        try:
            html = _http_client.get_text(_chart_url(single_date))
            rows = _billboard.parse_artist_100(html)
        except Exception:
            fetch_errors += 1
            continue
        for row in rows:
            if not _is_new_entry(row):
                continue
            key = (
                str(row.get("Artist Name", "")).strip().lower(),
                str(row.get("Chart Date", "")).strip() or single_date,
            )
            if key in seen:
                continue
            seen.add(key)
            new_entries.append(row)

    if not new_entries:
        if fetch_errors and fetch_errors == len(chart_dates):
            return _render_form(
                request,
                error_message=(
                    "Could not fetch the Billboard chart. The site sometimes blocks requests from "
                    "cloud servers, or the page layout changed. Try again in a minute."
                ),
                status_code=502,
            )
        scanned = (
            f"{chart_dates[0]} to {chart_dates[-1]}"
            if len(chart_dates) > 1
            else (chart_dates[0] or "the latest chart")
        )
        return _render_form(
            request,
            error_message=f"No artists had a dash (-) in Last Week for {scanned}.",
            status_code=404,
        )

    # Enrich each artist with their IMDb nmcode + URL (name.basics dataset, with a
    # Wikidata fallback) and Wikipedia URL.
    _enrich_imdb_and_wikipedia(new_entries)

    # Build the Excel workbook.
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Artist100 New Entries"
    sheet.append(EXPORT_COLUMNS)
    for row in new_entries:
        sheet.append([row.get(column, "") for column in EXPORT_COLUMNS])
    # Sensible column widths.
    widths = [8, 32, 10, 14, 16, 14, 16, 40, 48, 48]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    if len(chart_dates) > 1:
        safe_date = f"{chart_dates[0]}_to_{chart_dates[-1]}"
    else:
        resolved_date = new_entries[0].get("Chart Date", "") or chart_dates[0] or date.today().isoformat()
        safe_date = resolved_date.replace("/", "-").replace(" ", "_") or "latest"
    filename = f"billboard_artist100_new_entries_{safe_date}.xlsx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
