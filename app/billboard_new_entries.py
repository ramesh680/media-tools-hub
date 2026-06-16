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
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook

from app.config import get_settings
from app.services.billboard import BillboardArtist100Service, BILLBOARD_ARTIST_100_URL
from app.services.http_client import HttpClient

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_settings = get_settings()
_http_client = HttpClient(_settings.user_agent, _settings.request_timeout_seconds)
_billboard = BillboardArtist100Service(_http_client)

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

    Both come from a single Wikidata lookup per artist: Wikidata maps a person to
    their canonical IMDb id (property P345 -> the "nm" code) and to their English
    Wikipedia page. This needs no local IMDb dataset, so it runs on the free tier.
    Artists without a Wikidata match (or without those links) just get blank cells.
    """
    for row in rows:
        artist = str(row.get("Artist Name", "")).strip()
        nmcode = ""
        wikipedia_url = ""
        if artist:
            try:
                entity = _billboard._wikidata_entity_for_name(artist) or {}
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
def billboard_new_entries_run(request: Request, chart_date: str = Form("")):
    chart_date = (chart_date or "").strip()

    # Validate the optional date.
    if chart_date:
        try:
            date.fromisoformat(chart_date)
        except ValueError:
            return _render_form(
                request,
                error_message="Enter the date as YYYY-MM-DD, or leave it blank for the latest chart.",
                status_code=400,
            )

    # Fetch + parse the chart.
    try:
        html = _http_client.get_text(_chart_url(chart_date))
        rows = _billboard.parse_artist_100(html)
    except Exception:
        return _render_form(
            request,
            error_message=(
                "Could not fetch the Billboard chart. The site sometimes blocks requests from "
                "cloud servers, or the page layout changed. Try again in a minute."
            ),
            status_code=502,
        )

    if not rows:
        return _render_form(
            request,
            error_message="No chart rows were found for that date. Try a different date or leave it blank.",
            status_code=404,
        )

    new_entries = [row for row in rows if _is_new_entry(row)]
    if not new_entries:
        resolved = rows[0].get("Chart Date", "") or chart_date or "the latest chart"
        return _render_form(
            request,
            error_message=f"No artists had a dash (-) in Last Week for {resolved}. Every charting act had a prior-week position.",
            status_code=404,
        )

    # Enrich each artist with IMDb nmcode + URL and Wikipedia URL (via Wikidata;
    # no local IMDb dataset needed, so this works on the free hosted tier).
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

    resolved_date = new_entries[0].get("Chart Date", "") or chart_date or date.today().isoformat()
    safe_date = resolved_date.replace("/", "-").replace(" ", "_") or "latest"
    filename = f"billboard_artist100_new_entries_{safe_date}.xlsx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
