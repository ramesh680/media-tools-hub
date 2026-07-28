"""
Unit tests for the BDR Ingest service (Idea 1). The upstream Title Automation
API is mocked, so these run offline and assert the start -> poll -> download
flow and request shaping.

Run:  python -m pytest test_bdr_ingest.py -q
"""
import io
from unittest import mock

import pytest

from app.services.bdr_ingest import BdrIngestService, BdrIngestError


class _Resp:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code = status
        self._json = json_data or {}
        self.content = content

    def json(self):
        return self._json


def test_titles_flow_builds_json_and_downloads():
    svc = BdrIngestService(base_url="http://fake")
    posted = {}

    def fake_post(url, json=None, files=None, data=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        posted["files"] = files
        posted["data"] = data
        return _Resp(200, {"job_id": "J1"})

    statuses = [
        _Resp(200, {"status": "running", "done": 1, "total": 4}),
        _Resp(200, {"status": "done", "rows": 8, "enriched": 5}),
    ]

    def fake_get(url, timeout=None):
        if url.endswith("/download"):
            return _Resp(200, content=b"XLSXBYTES")
        return statuses.pop(0)

    with mock.patch("app.services.bdr_ingest.requests.post", side_effect=fake_post), \
         mock.patch("app.services.bdr_ingest.requests.get", side_effect=fake_get), \
         mock.patch("app.services.bdr_ingest.time.sleep", lambda *_: None):
        result = svc.generate(
            bulk_text="Dune 3\nThe Bear",
            title_type="movie",
            talent_profession="",
            progress=None,
        )

    assert posted["json"]["titles"] == ["Dune 3", "The Bear"]
    assert posted["json"]["titles_type"] == {"Dune 3": "movie", "The Bear": "movie"}
    assert result["content"] == b"XLSXBYTES"
    assert result["row_count"] == 8
    assert result["enriched_count"] == 5
    assert result["filename"].startswith("BDR_Ingest_")


def test_talent_profession_passed_as_professions_map():
    svc = BdrIngestService(base_url="http://fake")
    posted = {}

    def fake_post(url, json=None, files=None, data=None, timeout=None):
        posted["json"] = json
        return _Resp(200, {"job_id": "J2"})

    def fake_get(url, timeout=None):
        if url.endswith("/download"):
            return _Resp(200, content=b"X")
        return _Resp(200, {"status": "done", "rows": 1})

    with mock.patch("app.services.bdr_ingest.requests.post", side_effect=fake_post), \
         mock.patch("app.services.bdr_ingest.requests.get", side_effect=fake_get), \
         mock.patch("app.services.bdr_ingest.time.sleep", lambda *_: None):
        svc.generate(bulk_text="Michael Jordan", title_type="talent",
                     talent_profession="basketball")

    assert posted["json"]["professions"] == {"Michael Jordan": "basketball"}


def test_file_flow_sends_multipart_fields():
    svc = BdrIngestService(base_url="http://fake")
    posted = {}

    def fake_post(url, json=None, files=None, data=None, timeout=None):
        posted["files"] = files
        posted["data"] = data
        return _Resp(200, {"job_id": "J3"})

    def fake_get(url, timeout=None):
        if url.endswith("/download"):
            return _Resp(200, content=b"FILEBYTES")
        return _Resp(200, {"status": "done", "rows": 3})

    with mock.patch("app.services.bdr_ingest.requests.post", side_effect=fake_post), \
         mock.patch("app.services.bdr_ingest.requests.get", side_effect=fake_get), \
         mock.patch("app.services.bdr_ingest.time.sleep", lambda *_: None):
        result = svc.generate(file_content=b"rawxlsx", filename="titles.xlsx",
                              title_type="mixed", talent_profession="Actor")

    assert "file" in posted["files"]
    assert posted["data"]["titleType"] == "mixed"
    assert posted["data"]["talentProfession"] == "Actor"
    assert result["content"] == b"FILEBYTES"


def test_upstream_error_status_raises():
    svc = BdrIngestService(base_url="http://fake")

    def fake_post(url, json=None, files=None, data=None, timeout=None):
        return _Resp(200, {"job_id": "J4"})

    def fake_get(url, timeout=None):
        return _Resp(200, {"status": "error", "error": "boom"})

    with mock.patch("app.services.bdr_ingest.requests.post", side_effect=fake_post), \
         mock.patch("app.services.bdr_ingest.requests.get", side_effect=fake_get), \
         mock.patch("app.services.bdr_ingest.time.sleep", lambda *_: None):
        with pytest.raises(BdrIngestError):
            svc.generate(bulk_text="X", title_type="movie")


def test_no_input_raises():
    svc = BdrIngestService(base_url="http://fake")
    with pytest.raises(BdrIngestError):
        svc.generate(bulk_text="", file_content=None)


def test_base_url_env_default(monkeypatch):
    monkeypatch.delenv("TITLE_AUTOMATION_URL", raising=False)
    svc = BdrIngestService()
    assert "title-automation-tool.onrender.com" in svc.base_url
    monkeypatch.setenv("TITLE_AUTOMATION_URL", "https://custom.example.com/")
    assert BdrIngestService().base_url == "https://custom.example.com"
