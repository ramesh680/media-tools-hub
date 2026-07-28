"""
bdr_ingest.py
-------------
Generate an ingest-ready **BDR report** from a list of titles, reusing the
existing Title Automation tool's ingest-template logic instead of duplicating
it here.

Rather than re-implement the row builders, reference-template loading and
auto-discovery (all of which already live in the title-automation-tool
service), this talks to that service's public API:

    POST /api/generate_async      -> {"job_id": "..."}
    GET  /api/job/<id>            -> {"status": running|done|error, ...}
    GET  /api/job/<id>/download   -> the generated .xlsx

That keeps one source of truth for the ingest templates: when Ops drops new
templates into title-automation-tool/reference/, every consumer (this Daily
Asana hub, the Yearly Asana tool, the Title Automation UI) gets them for free.

The same class is intended to be copied verbatim into the yearly-asana-task
repo -- only the calling route changes.
"""
from __future__ import annotations

import io
import os
import time
from typing import Callable, Optional

import requests


ProgressCallback = Callable[[int, str], None]

DEFAULT_BASE_URL = "https://title-automation-tool.onrender.com"

# render free tier can cold-start slowly + auto-discovery adds seconds/title
POLL_INTERVAL_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("BDR_INGEST_TIMEOUT_SECONDS", "1500"))
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class BdrIngestError(RuntimeError):
    """Raised when the upstream Title Automation service cannot produce a report."""


class BdrIngestService:
    def __init__(
        self,
        base_url: Optional[str] = None,
        request_timeout_seconds: float = 60.0,
        total_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = (base_url or os.getenv("TITLE_AUTOMATION_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.request_timeout = request_timeout_seconds
        self.total_timeout = total_timeout_seconds

    # ---- public API ------------------------------------------------------
    def generate(
        self,
        *,
        bulk_text: str = "",
        file_content: Optional[bytes] = None,
        filename: str = "",
        title_type: str = "mixed",
        include_dar: bool = True,
        auto_fetch: bool = True,
        talent_profession: str = "",
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        """Kick off a generation run upstream, wait for it, and return a result
        dict shaped for the BDR ingest templates/partials:

            {filename, content(bytes), media_type, row_count, enriched_count,
             title_count, source_label}
        """
        def _tick(pct: int, msg: str) -> None:
            if progress:
                progress(pct, msg)

        titles = [t.strip() for t in (bulk_text or "").splitlines() if t.strip()]
        if not file_content and not titles:
            raise BdrIngestError("Provide a file or at least one title.")

        _tick(3, f"Contacting Title Automation service ({self.base_url})")
        job_id = self._start_job(
            titles=titles,
            file_content=file_content,
            filename=filename,
            title_type=title_type,
            include_dar=include_dar,
            auto_fetch=auto_fetch,
            talent_profession=talent_profession,
        )
        _tick(8, "Generation job started; building rows from ingest templates")

        status = self._poll(job_id, _tick)
        _tick(94, "Downloading generated BDR report")
        content = self._download(job_id)

        out_name = self._output_name(filename)
        return {
            "filename": out_name,
            "content": content,
            "media_type": _XLSX_MIME,
            "row_count": status.get("rows") or 0,
            "enriched_count": status.get("enriched") or 0,
            "title_count": len(titles) if titles else None,
            "source_label": filename or f"{len(titles)} pasted titles",
            "upstream": self.base_url,
        }

    # ---- internals -------------------------------------------------------
    def _start_job(
        self,
        *,
        titles: list[str],
        file_content: Optional[bytes],
        filename: str,
        title_type: str,
        include_dar: bool,
        auto_fetch: bool,
        talent_profession: str,
    ) -> str:
        url = f"{self.base_url}/api/generate_async"
        try:
            if file_content:
                files = {"file": (filename or "titles.xlsx", io.BytesIO(file_content))}
                data = {
                    "includeDar": str(include_dar).lower(),
                    "autoFetch": str(auto_fetch).lower(),
                    "titleType": title_type,
                    "talentProfession": talent_profession or "",
                }
                resp = requests.post(url, files=files, data=data,
                                     timeout=self.request_timeout)
            else:
                payload = {
                    "titles": titles,
                    "includeDar": include_dar,
                    "autoFetch": auto_fetch,
                    "titles_type": {t: title_type for t in titles}
                    if title_type and title_type != "mixed" else {},
                }
                if talent_profession:
                    payload["professions"] = {t: talent_profession for t in titles}
                resp = requests.post(url, json=payload, timeout=self.request_timeout)
        except requests.RequestException as exc:
            raise BdrIngestError(
                f"Could not reach the Title Automation service at {self.base_url}: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise BdrIngestError(
                f"Title Automation service returned HTTP {resp.status_code} starting the job."
            )
        job_id = (resp.json() or {}).get("job_id")
        if not job_id:
            raise BdrIngestError("Title Automation service did not return a job id.")
        return job_id

    def _poll(self, job_id: str, tick: ProgressCallback) -> dict:
        url = f"{self.base_url}/api/job/{job_id}"
        deadline = time.time() + self.total_timeout
        while time.time() < deadline:
            try:
                resp = requests.get(url, timeout=self.request_timeout)
            except requests.RequestException:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if resp.status_code == 404:
                raise BdrIngestError("The upstream generation job expired before it finished.")
            data = resp.json() or {}
            status = data.get("status")
            if status == "done":
                return data
            if status == "error":
                raise BdrIngestError(data.get("error") or "Upstream generation failed.")
            done, total = data.get("done") or 0, data.get("total") or 0
            if total:
                # map upstream 0..100% into our 10..92% band
                pct = 10 + int((done / total) * 82)
                tick(min(92, pct), f"Generating rows ({done}/{total} titles)")
            else:
                tick(10, "Waiting for the generation service to start rows")
            time.sleep(POLL_INTERVAL_SECONDS)
        raise BdrIngestError("Timed out waiting for the BDR report to generate.")

    def _download(self, job_id: str) -> bytes:
        url = f"{self.base_url}/api/job/{job_id}/download"
        try:
            resp = requests.get(url, timeout=self.request_timeout)
        except requests.RequestException as exc:
            raise BdrIngestError(f"Could not download the generated report: {exc}") from exc
        if resp.status_code != 200 or not resp.content:
            raise BdrIngestError("The generated BDR report could not be downloaded.")
        return resp.content

    @staticmethod
    def _output_name(filename: str) -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return f"BDR_Ingest_{stamp}.xlsx"
