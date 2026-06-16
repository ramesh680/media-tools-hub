from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs
import json
import os
import uuid

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.services.cache import TTLCache
from app.services.billboard import BillboardArtist100Service
from app.services.box_office_mojo import BoxOfficeMojoService
from app.services.exporting import ExportService
from app.services.excel_validator import ExcelValidatorService
from app.services.history import HistoryRepository
from app.services.http_client import HttpClient
from app.services.imdb import IMDbEnrichmentService
from app.services.imdb_verifier import IMDbBulkVerifierService
from app.services.jobs import JobManager
from app.services.metacritic import MetacriticParser, metacritic_url_for_row
from app.services.validator_history import ValidatorHistoryRepository
from app.services.validator_jobs import ValidatorJobManager
from app.services.youtube_release_verifier import YouTubeReleaseVerifierService


settings = get_settings()
history_repository = HistoryRepository(settings.database_path)
validator_history_repository = ValidatorHistoryRepository(settings.database_path)
http_client = HttpClient(settings.user_agent, settings.request_timeout_seconds)
metacritic_parser = MetacriticParser(http_client)
imdb_service = IMDbEnrichmentService(settings.imdb_cache_dir, settings.imdb_dataset_max_age_days, http_client)
imdb_verifier_service = IMDbBulkVerifierService(imdb_service)
box_office_service = BoxOfficeMojoService(http_client)
billboard_service = BillboardArtist100Service(http_client, imdb_service)
youtube_release_verifier_service = YouTubeReleaseVerifierService(http_client, settings.youtube_api_key)
excel_validator_service = ExcelValidatorService(
    http_client,
    metacritic_parser,
    imdb_service,
    settings.tmdb_api_key,
    settings.tmdb_read_access_token,
    settings.youtube_api_key,
)
export_service = ExportService(settings.export_ttl_seconds, settings.google_service_account_json)
job_manager = JobManager(settings.job_ttl_seconds, history_repository)
validator_job_manager = ValidatorJobManager(settings.job_ttl_seconds)
validator_artifacts = TTLCache(settings.export_ttl_seconds)

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Billboard Artist 100 "new entries" (LW = dash) section -> /billboard-new-entries
from app.billboard_new_entries import router as billboard_new_entries_router  # noqa: E402

app.include_router(billboard_new_entries_router)

RELEASE_SCHEDULE_CHANGE_HISTORY_LOOKBACK_DAYS = 14
RELEASE_SCHEDULE_CHANGE_PAST_DAYS = 7
RELEASE_SCHEDULE_CHANGE_FUTURE_DAYS = 30


@app.on_event("startup")
def startup() -> None:
    history_repository.initialize()
    validator_history_repository.initialize()


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "recent_runs": history_repository.list_recent(),
            "today_iso": date.today().isoformat(),
            "upcoming_movies_url": os.getenv("UPCOMING_MOVIES_URL", ""),
            "imdb_enabled": os.getenv("IMDB_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"},
        },
    )


@app.get("/excel-validator", response_class=HTMLResponse)
def excel_validator(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "excel_validator.html",
        {
            "result": None,
            "error_message": "",
            "tmdb_enabled": bool(settings.tmdb_api_key or settings.tmdb_read_access_token),
            "youtube_api_enabled": bool(settings.youtube_api_key),
            "recent_validator_runs": validator_history_repository.list_recent(),
            "default_rules_json": _default_validator_rules_json(),
        },
    )


@app.get("/imdb-verifier", response_class=HTMLResponse)
def imdb_verifier(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "imdb_verifier.html",
        {
            "recent_runs": history_repository.list_recent(),
        },
    )


@app.get("/youtube-release-verifier", response_class=HTMLResponse)
def youtube_release_verifier(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "youtube_release_verifier.html",
        {
            "recent_runs": history_repository.list_recent(),
            "youtube_api_enabled": bool(settings.youtube_api_key),
        },
    )


@app.post("/imdb-verifier/start", response_class=HTMLResponse)
async def start_imdb_verifier(
    request: Request,
    bulk_text: str = Form(""),
    bulk_file: UploadFile | None = File(None),
) -> HTMLResponse:
    filename = bulk_file.filename if bulk_file and bulk_file.filename else ""
    file_content = await bulk_file.read() if bulk_file and bulk_file.filename else None
    job = job_manager.start(
        "imdb_verifier",
        lambda progress: imdb_verifier_service.verify_bulk(
            bulk_text=bulk_text,
            file_content=file_content,
            filename=filename,
            progress=progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/youtube-release-verifier/start", response_class=HTMLResponse)
async def start_youtube_release_verifier(
    request: Request,
    bulk_text: str = Form(""),
    youtube_api_key: str = Form(""),
    bulk_file: UploadFile | None = File(None),
) -> HTMLResponse:
    filename = bulk_file.filename if bulk_file and bulk_file.filename else ""
    file_content = await bulk_file.read() if bulk_file and bulk_file.filename else None
    job = job_manager.start(
        "youtube_release_verifier",
        lambda progress: _with_imdb_ttcodes(
            youtube_release_verifier_service.verify_bulk(
                bulk_text=bulk_text,
                file_content=file_content,
                filename=filename,
                api_key_override=youtube_api_key,
                progress=progress,
            ),
            progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/excel-validator/validate", response_class=HTMLResponse)
async def validate_excel(
    request: Request,
    workbook: UploadFile = File(...),
    run_by: str = Form(""),
    google_sheet_url: str = Form(""),
    rules_json: str = Form(""),
    rules_file: UploadFile | None = File(None),
) -> HTMLResponse:
    filename = workbook.filename or "uploaded workbook"
    if not filename.lower().endswith((".xlsx", ".xlsm", ".csv")):
        return _validator_error_response(
            request,
            "Upload an .xlsx, .xlsm, or .csv workbook source file.",
        )
    content = await workbook.read()
    ip_address = request.client.host if request.client else "127.0.0.1"
    run_label = run_by.strip() or "-"
    source_label = filename
    if google_sheet_url.strip():
        source_label = f"{filename} + Google Sheet reference"
    if rules_file and rules_file.filename:
        source_label = f"{source_label} + {rules_file.filename}"

    job = validator_job_manager.start(
        lambda progress: _run_excel_validation_job(
            content,
            filename,
            run_label,
            ip_address,
            source_label,
            progress,
        )
    )
    return _validator_progress_response(request, job.to_dict())


@app.get("/excel-validator/progress/{job_id}", response_class=HTMLResponse)
def excel_validator_progress(request: Request, job_id: str) -> HTMLResponse:
    job = validator_job_manager.get(job_id)
    if job is None:
        return _validator_error_response(
            request,
            "This validation job expired. Start a new validation run from the form above.",
        )
    if job.status == "completed" and job.result:
        return templates.TemplateResponse(
            request,
            "partials/validator_result_response.html",
            {
                "result": job.result,
                "recent_validator_runs": validator_history_repository.list_recent(),
            },
        )
    if job.status == "failed":
        return _validator_error_response(
            request,
            job.error_message or "The workbook could not be validated.",
        )
    return _validator_progress_response(request, job.to_dict())


@app.get("/excel-validator/download/{artifact_id}")
def download_validated_workbook(artifact_id: str) -> Response:
    artifact = validator_artifacts.get(artifact_id)
    if not artifact:
        return HTMLResponse(
            "Validated file expired. Please run the validator again.",
            status_code=404,
        )
    filename = artifact.get("filename") or "validated_file"
    return Response(
        content=artifact.get("content", b""),
        media_type=artifact.get("media_type") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _run_excel_validation_job(
    content: bytes,
    filename: str,
    run_by: str,
    ip_address: str,
    source_label: str,
    progress,
) -> dict:
    result = excel_validator_service.validate_workbook(content, filename, progress=progress)
    result = _register_validated_workbook_artifact(result)
    result = _register_excel_validator_exports(result)
    validated_file = result.get("validated_file") or f"{filename.rsplit('.', 1)[0]}_validated.xlsx"
    run_id = validator_history_repository.add_run(
        result,
        run_by=run_by,
        ip_address=ip_address,
        source_file=source_label,
        validated_file=validated_file,
    )
    result["validator_run_id"] = run_id
    result["run_by"] = run_by
    result["ip_address"] = ip_address
    return result


def _with_imdb_ttcodes(snapshot: dict, progress) -> dict:
    return imdb_service.add_ttcodes_to_snapshot(snapshot, progress=progress)


def _recent_box_office_schedule_snapshots() -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=RELEASE_SCHEDULE_CHANGE_HISTORY_LOOKBACK_DAYS)
    snapshots: list[dict] = []
    for tracker_type in ("boxoffice", "release_schedule_changes"):
        snapshots.extend(
            history_repository.list_snapshots(
                tracker_type,
                since=since,
                limit=100,
            )
        )
    return sorted(
        snapshots,
        key=lambda item: (str(item.get("created_at", "")), int(item.get("run_id", 0) or 0)),
        reverse=True,
    )


def _legacy_validate_excel_response(request: Request, content: bytes, filename: str) -> HTMLResponse:
    try:
        result = excel_validator_service.validate_workbook(content, filename)
        result = _register_validated_workbook_artifact(result)
        result = _register_excel_validator_exports(result)
        error_message = ""
    except Exception as exc:
        result = None
        error_message = f"The workbook could not be validated: {exc}"
    return templates.TemplateResponse(
        request,
        "excel_validator.html",
        {
            "result": result,
            "error_message": error_message,
            "tmdb_enabled": bool(settings.tmdb_api_key or settings.tmdb_read_access_token),
            "youtube_api_enabled": bool(settings.youtube_api_key),
            "recent_validator_runs": validator_history_repository.list_recent(),
            "default_rules_json": _default_validator_rules_json(),
        },
    )


@app.post("/tv-premiere-calendar/start", response_class=HTMLResponse)
async def start_tv_calendar(request: Request) -> HTMLResponse:
    date_window = await _date_window_from_request(request)
    if isinstance(date_window, HTMLResponse):
        return date_window
    job = job_manager.start(
        "tv",
        lambda progress: _with_imdb_ttcodes(
            metacritic_parser.fetch_tv_calendar(
                start_date=date_window["start_date"],
                end_date=date_window["end_date"],
                progress=progress,
            ),
            progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/tv-premiere-imdb-series/start", response_class=HTMLResponse)
async def start_imdb_series(request: Request) -> HTMLResponse:
    date_window = await _date_window_from_request(request)
    if isinstance(date_window, HTMLResponse):
        return date_window
    job = job_manager.start(
        "imdb",
        lambda progress: imdb_service.fetch_snapshot(
            metacritic_parser,
            start_date=date_window["start_date"],
            end_date=date_window["end_date"],
            progress=progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/tv-season-episodes/start", response_class=HTMLResponse)
async def start_tv_season_episodes(request: Request) -> HTMLResponse:
    date_window = await _date_window_from_request(request)
    if isinstance(date_window, HTMLResponse):
        return date_window
    job = job_manager.start(
        "tv_seasons",
        lambda progress: imdb_service.fetch_season_episode_snapshot(
            metacritic_parser,
            start_date=date_window["start_date"],
            end_date=date_window["end_date"],
            progress=progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/game-release-calendar/start", response_class=HTMLResponse)
async def start_game_calendar(request: Request) -> HTMLResponse:
    date_window = await _date_window_from_request(request)
    if isinstance(date_window, HTMLResponse):
        return date_window
    job = job_manager.start(
        "game",
        lambda progress: _with_imdb_ttcodes(
            metacritic_parser.fetch_game_calendar(
                start_date=date_window["start_date"],
                end_date=date_window["end_date"],
                progress=progress,
            ),
            progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/movie-release-calendar/start", response_class=HTMLResponse)
async def start_movie_calendar(request: Request) -> HTMLResponse:
    date_window = await _date_window_from_request(request)
    if isinstance(date_window, HTMLResponse):
        return date_window
    job = job_manager.start(
        "movie",
        lambda progress: _with_imdb_ttcodes(
            metacritic_parser.fetch_movie_calendar(
                start_date=date_window["start_date"],
                end_date=date_window["end_date"],
                progress=progress,
            ),
            progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/box-office-mojo-movies/start", response_class=HTMLResponse)
async def start_box_office_mojo_movies(request: Request) -> HTMLResponse:
    date_window = await _date_window_from_request(request)
    if isinstance(date_window, HTMLResponse):
        return date_window
    job = job_manager.start(
        "boxoffice",
        lambda progress: _with_imdb_ttcodes(
            box_office_service.fetch_us_movie_releases(
                start_date=date_window["start_date"],
                end_date=date_window["end_date"],
                progress=progress,
            ),
            progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/release-schedule-changes/start", response_class=HTMLResponse)
async def start_release_schedule_changes(request: Request) -> HTMLResponse:
    today = date.today()
    start_date = today - timedelta(days=RELEASE_SCHEDULE_CHANGE_PAST_DAYS)
    end_date = today + timedelta(days=RELEASE_SCHEDULE_CHANGE_FUTURE_DAYS)
    job = job_manager.start(
        "release_schedule_changes",
        lambda progress: _with_imdb_ttcodes(
            box_office_service.fetch_release_schedule_changes(
                previous_snapshots=_recent_box_office_schedule_snapshots(),
                start_date=start_date,
                end_date=end_date,
                history_lookback_days=RELEASE_SCHEDULE_CHANGE_HISTORY_LOOKBACK_DAYS,
                progress=progress,
            ),
            progress,
        ),
    )
    return _progress_response(request, job.to_dict())


@app.post("/billboard-artist-100/start", response_class=HTMLResponse)
async def start_billboard_artist_100(request: Request) -> HTMLResponse:
    job = job_manager.start(
        "billboard_artist_100",
        lambda progress: billboard_service.fetch_artist_100(progress=progress),
    )
    return _progress_response(request, job.to_dict())


@app.get("/progress/{job_id}", response_class=HTMLResponse)
def progress(request: Request, job_id: str) -> HTMLResponse:
    job = job_manager.get(job_id)
    if job is None:
        return _error_response(
            request,
            "Tracker run expired",
            "This in-memory job expired. Completed runs can still be opened from Recent tracker runs.",
        )

    job_dict = job.to_dict()
    if job.status == "completed" and job.result:
        snapshot = export_service.register_snapshot_exports(_ensure_metacritic_links(deepcopy(job.result)))
        return _result_response(request, snapshot, oob_history=True)
    if job.status == "failed":
        return _error_response(
            request,
            "Tracker run failed",
            job.error_message or "The tracker hit an unexpected error.",
        )
    return _progress_response(request, job_dict)


@app.get("/history/{run_id}", response_class=HTMLResponse)
def history(request: Request, run_id: int) -> HTMLResponse:
    snapshot = history_repository.get_run(run_id)
    if snapshot is None:
        return _error_response(
            request,
            "Run not found",
            "That historical run is no longer available in SQLite.",
        )
    snapshot = export_service.register_snapshot_exports(_ensure_metacritic_links(deepcopy(snapshot)))
    return _result_response(request, snapshot, oob_history=False)


@app.get("/export/{export_id}/{fmt}")
def export(export_id: str, fmt: str) -> Response:
    return export_service.render_export(export_id, fmt)


def _progress_response(request: Request, job: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/progress.html", {"job": job})


def _validator_progress_response(request: Request, job: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/validator_progress.html", {"job": job})


def _validator_error_response(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/validator_error.html",
        {
            "message": message,
        },
    )


def _register_validated_workbook_artifact(result: dict) -> dict:
    workbook_bytes = result.pop("validated_workbook_bytes", None)
    validated_filename = result.get("validated_filename") or f"{result.get('filename', 'workbook').rsplit('.', 1)[0]}_validated.xlsx"
    result["validated_file"] = validated_filename
    if workbook_bytes:
        artifact_id = uuid.uuid4().hex
        validator_artifacts.set(
            artifact_id,
            {
                "filename": validated_filename,
                "content": workbook_bytes,
                "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        )
        result["validated_workbook_id"] = artifact_id
    return result


def _register_excel_validator_exports(result: dict) -> dict:
    if not result.get("issues"):
        return result
    snapshot = {
        "tracker_type": "excel_validator",
        "title": f"Excel Validator Report - {result['filename']}",
        "sections": [
            {
                "key": "issues",
                "title": f"Excel Validator Report - {result['filename']}",
                "columns": result["report_columns"],
                "rows": result["issues"],
                "supports_google": False,
            }
        ],
    }
    snapshot = export_service.register_snapshot_exports(snapshot)
    result["export_id"] = snapshot["sections"][0]["export_id"]
    return result


def _default_validator_rules_json() -> str:
    return json.dumps(
        {
            "rules": [
                {
                    "sheet": "*",
                    "column": "title",
                    "check": "not_blank_and_not_placeholder",
                    "tokens": ["#NA", "N/A"],
                    "message": "Title cannot be blank, #NA, or N/A.",
                },
                {
                    "sheet": "*",
                    "column": "title_category",
                    "check": "approved_category",
                    "message": "title_category must be present and approved.",
                },
                {
                    "sheet": "*",
                    "column": "brand_set",
                    "check": "dar_or_competitive_brand_set",
                    "message": "DAR titles need Pristine DAR Brands; other titles need Competitive View.",
                },
                {
                    "sheet": "*",
                    "column": "imdb_id",
                    "alternate_column": "imdb_url",
                    "check": "lookup_imdb_ttcode_from_title",
                    "applies_to": ["Movies", "TV Shows"],
                    "message": "IMDb ttcode or IMDb title URL is checked and suggested from the row title.",
                },
                {
                    "sheet": "*",
                    "column": "metacritic_url",
                    "check": "lookup_metacritic_url_from_title",
                    "applies_to": ["Movies", "TV Shows"],
                    "message": "Metacritic URL is checked and suggested from the row title.",
                },
                {
                    "sheet": "*",
                    "column": "youtube_url",
                    "check": "youtube_url_or_channel_pipe_title",
                    "message": "Use a YouTube URL or channel URL|title.",
                },
                {
                    "sheet": "*",
                    "column": "youtube_channel_company",
                    "check": "verify_youtube_channel_with_data_api",
                    "requires": "YOUTUBE_API_KEY",
                    "message": "Validate YouTube channel URL, handle, username, or channel id with YouTube Data API v3.",
                },
                {
                    "sheet": "*",
                    "column": "wikipedia_url",
                    "check": "wikidata_english_wikipedia_url_matches_title",
                    "accepted_host": "en.wikipedia.org",
                    "example": "https://en.wikipedia.org/wiki/A_Place_in_Hell",
                    "message": "Wikipedia URLs must be English Wikipedia article URLs and should match the English Wikipedia sitelink returned by Wikidata for the row title.",
                },
                {
                    "sheet": "*",
                    "column": "url_managers",
                    "check": "contains_companies_and_platform_accounts",
                    "company_column": "companies",
                    "exclude_company_values": ["Unknown", "Pristine Brand"],
                    "platform_columns": [
                        "facebook_page",
                        "youtube_channel_company",
                        "instagram_account",
                        "twitter_account",
                        "tiktok_account",
                        "threads_account",
                    ],
                    "message": "url_managers must include non-excluded companies and platform accounts present elsewhere in the row.",
                },
            ]
        },
        indent=2,
    )


def _result_response(request: Request, snapshot: dict, oob_history: bool) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/result_response.html",
        {
            "snapshot": snapshot,
            "recent_runs": history_repository.list_recent(),
            "oob_history": oob_history,
        },
    )


def _ensure_metacritic_links(snapshot: dict) -> dict:
    tracker_type = snapshot.get("tracker_type", "")
    if tracker_type not in {"tv", "imdb", "tv_seasons", "game", "movie"}:
        return snapshot

    if tracker_type == "game":
        default_media_type = "game"
    elif tracker_type == "movie":
        default_media_type = "movie"
    else:
        default_media_type = "tv"
    for section in snapshot.get("sections", []):
        columns = section.get("columns")
        if not isinstance(columns, list):
            continue

        if tracker_type == "tv_seasons":
            _ensure_column(columns, "metacritic_url", after="imdb_id")
            for row in section.get("rows", []):
                row["metacritic_url"] = row.get("metacritic_url") or metacritic_url_for_row(
                    {
                        "title": row.get("title", ""),
                        "Source URL": row.get("Source URL", ""),
                        "Metacritic URL": row.get("Metacritic URL", ""),
                    },
                    default_media_type=default_media_type,
                )
            continue

        _ensure_column(columns, "Metacritic URL", after="Source URL")
        for row in section.get("rows", []):
            row["Metacritic URL"] = row.get("Metacritic URL") or metacritic_url_for_row(
                row,
                default_media_type=default_media_type,
            )
    return snapshot


def _ensure_column(columns: list[str], column: str, after: str = "") -> None:
    if column in columns:
        return
    if after and after in columns:
        columns.insert(columns.index(after) + 1, column)
    else:
        columns.append(column)


def _error_response(request: Request, title: str, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/error.html",
        {
            "title": title,
            "message": message,
        },
    )


async def _date_window_from_request(request: Request) -> dict[str, date] | HTMLResponse:
    form = _decode_urlencoded_form(await request.body())
    try:
        return _resolve_date_window(
            form.get("date_window", "current"),
            form.get("custom_start_date", ""),
            form.get("custom_end_date", ""),
        )
    except ValueError as exc:
        return _error_response(request, "Invalid date range", str(exc))


def _decode_urlencoded_form(raw_body: bytes) -> dict[str, str]:
    parsed = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _resolve_date_window(window: str, custom_start: str, custom_end: str) -> dict[str, date]:
    today = date.today()
    window = (window or "current").strip()
    if window == "current":
        return {"start_date": today, "end_date": today}
    if window == "next_7":
        return {"start_date": today, "end_date": today + timedelta(days=7)}
    if window == "one_year":
        return {"start_date": today, "end_date": today + timedelta(days=365)}
    if window != "custom":
        raise ValueError("Choose Current date, Next 7 days, One year, or Custom date range.")

    if not custom_start or not custom_end:
        raise ValueError("Custom date range needs both a start date and an end date.")
    try:
        start_date = date.fromisoformat(custom_start)
        end_date = date.fromisoformat(custom_end)
    except ValueError as exc:
        raise ValueError("Custom dates must use YYYY-MM-DD format.") from exc
    if end_date < start_date:
        raise ValueError("Custom end date must be the same as or later than the start date.")
    return {"start_date": start_date, "end_date": end_date}
