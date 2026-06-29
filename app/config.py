from functools import lru_cache
from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")
_load_env_file(BASE_DIR / ".env.example")


class Settings:
    app_name = "Metacritic Release Tracker"
    database_path = Path(os.getenv("TRACKER_DATABASE_PATH", BASE_DIR / "data" / "tracker.sqlite3"))
    export_ttl_seconds = int(os.getenv("TRACKER_EXPORT_TTL_SECONDS", "21600"))
    job_ttl_seconds = int(os.getenv("TRACKER_JOB_TTL_SECONDS", "7200"))
    imdb_cache_dir = Path(os.getenv("IMDB_CACHE_DIR", BASE_DIR / "data" / "imdb"))
    imdb_dataset_max_age_days = int(os.getenv("IMDB_DATASET_MAX_AGE_DAYS", "7"))
    request_timeout_seconds = float(os.getenv("TRACKER_REQUEST_TIMEOUT_SECONDS", "25"))
    user_agent = os.getenv(
        "METACRITIC_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )
    google_application_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    google_service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    tmdb_api_key = os.getenv("TMDB_API_KEY", "")
    omdb_api_key = os.getenv("OMDB_API_KEY", "")
    tmdb_read_access_token = os.getenv("TMDB_READ_ACCESS_TOKEN", "")
    youtube_api_key = os.getenv("YOUTUBE_API_KEY", "")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.imdb_cache_dir.mkdir(parents=True, exist_ok=True)
    return settings
