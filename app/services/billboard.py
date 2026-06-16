from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote, urljoin
import json
import re

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from app.models import BILLBOARD_ARTIST_100_COLUMNS, utc_now_iso
from app.services.http_client import HttpClient
from app.services.imdb import IMDbEnrichmentService, normalize_title


BILLBOARD_ARTIST_100_URL = "https://www.billboard.com/charts/artist-100/"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
STAT_TOKEN_RE = re.compile(r"^(?:\d+|-|NEW|RE-ENTRY|RE ENTRY|RE- ENTRY)$", re.IGNORECASE)
MUSIC_DESCRIPTION_TERMS = {
    "artist",
    "band",
    "composer",
    "dj",
    "duo",
    "group",
    "musician",
    "rapper",
    "singer",
    "songwriter",
    "vocalist",
}
WIKIDATA_MUSIC_PROFESSION_HINT = "music artist singer songwriter musician rapper composer band"


class BillboardArtist100Service:
    def __init__(self, http_client: HttpClient, imdb_service: IMDbEnrichmentService | None = None) -> None:
        self.http_client = http_client
        self.imdb_service = imdb_service
        self._wikidata_search_cache: dict[str, dict[str, str]] = {}
        self._wikidata_entity_cache: dict[str, dict[str, str]] = {}
        self._wikidata_label_cache: dict[str, str] = {}
        self._billboard_detail_cache: dict[str, str] = {}

    def fetch_artist_100(self, progress=None) -> dict[str, Any]:
        if progress:
            progress(8, "Fetching Billboard Artist 100 chart")
        html = self.http_client.get_text(BILLBOARD_ARTIST_100_URL)
        if progress:
            progress(45, "Parsing Billboard chart rows")
        rows = self.parse_artist_100(html)
        if not rows:
            raise ValueError(
                "No Billboard Artist 100 rows were found. The public page may have changed or blocked the request."
            )
        if progress:
            progress(62, "Looking up IMDb nmcodes and artist metadata")
        rows = self.enrich_artist_rows(rows, progress=progress)
        if progress:
            progress(90, "Preparing Billboard Artist 100 exports")
        chart_date = rows[0].get("Chart Date", "") if rows else ""
        summary = (
            f"Fetched {len(rows)} Billboard Artist 100 rows"
            + (f" for chart date {chart_date}." if chart_date else ".")
        )
        return {
            "tracker_type": "billboard_artist_100",
            "title": "Billboard Artist 100",
            "created_at": utc_now_iso(),
            "source_url": BILLBOARD_ARTIST_100_URL,
            "summary": summary,
            "sections": [
                {
                    "key": "billboard_artist_100",
                    "title": "Billboard Artist 100",
                    "columns": BILLBOARD_ARTIST_100_COLUMNS,
                    "rows": rows,
                    "row_count": len(rows),
                    "supports_google": False,
                }
            ],
        }

    def parse_artist_100(self, html: str) -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        chart_date = _extract_chart_date(soup)
        rows = _parse_chart_containers(soup, chart_date)
        if rows:
            return rows
        return _parse_embedded_json(soup, chart_date)

    def enrich_artist_rows(self, rows: list[dict[str, str]], progress=None) -> list[dict[str, str]]:
        total = max(len(rows), 1)
        for index, row in enumerate(rows, start=1):
            artist_name = row.get("Artist Name", "")
            if progress and (index == 1 or index == total or index % 10 == 0):
                progress(62 + int((index / total) * 24), f"Enriching Billboard artist {artist_name}")
            self._enrich_with_imdb(row, artist_name)
            self._enrich_with_wikidata(row, artist_name)
            self._enrich_with_billboard_details(row)
        return rows

    def _enrich_with_imdb(self, row: dict[str, str], artist_name: str) -> None:
        if not self.imdb_service or not artist_name:
            return
        try:
            match = self.imdb_service.lookup_name(artist_name, profession_hint=WIKIDATA_MUSIC_PROFESSION_HINT)
        except Exception as exc:
            row["IMDb Lookup Note"] = f"IMDb lookup failed: {exc}"
            return
        if not match:
            return
        nmcode = match.get("imdb_id", "")
        row["IMDb nmcode"] = nmcode
        row["IMDb URL"] = f"https://www.imdb.com/name/{nmcode}/" if nmcode else ""
        row["IMDb Primary Profession"] = _titleize_professions(match.get("primary_profession", ""))
        row["IMDb Known For Titles"] = match.get("known_for_title_names") or match.get("known_for_titles", "")

    def _enrich_with_wikidata(self, row: dict[str, str], artist_name: str) -> None:
        if not artist_name:
            return
        try:
            entity = self._wikidata_entity_for_name(artist_name)
        except Exception as exc:
            row["Wikidata Description"] = f"Wikidata lookup failed: {exc}"
            return
        if not entity:
            return
        row.update(entity)

    def _enrich_with_billboard_details(self, row: dict[str, str]) -> None:
        artist_url = row.get("Billboard Artist URL", "")
        if not artist_url:
            return
        if artist_url in self._billboard_detail_cache:
            row["Billboard Details"] = self._billboard_detail_cache[artist_url]
            return
        try:
            html = self.http_client.get_text(artist_url)
            details = _billboard_details_from_html(html)
        except Exception:
            details = ""
        self._billboard_detail_cache[artist_url] = details
        row["Billboard Details"] = details

    def _wikidata_entity_for_name(self, artist_name: str) -> dict[str, str] | None:
        key = normalize_title(artist_name)
        if key in self._wikidata_search_cache:
            cached = self._wikidata_search_cache[key]
            return cached or None
        qid = self._wikidata_search_artist(artist_name)
        if not qid:
            self._wikidata_search_cache[key] = {}
            return None
        entity = self._wikidata_entity(qid)
        self._wikidata_search_cache[key] = entity or {}
        return entity

    def _wikidata_search_artist(self, artist_name: str) -> str:
        data = self._wikidata_get(
            {
                "action": "wbsearchentities",
                "search": artist_name,
                "language": "en",
                "uselang": "en",
                "format": "json",
                "limit": "8",
            }
        )
        best_score = -1
        best_qid = ""
        for item in data.get("search", []):
            qid = _clean(item.get("id"))
            label = _clean(item.get("label"))
            description = _clean(item.get("description"))
            if not qid:
                continue
            score = _title_score(artist_name, label)
            description_tokens = set(normalize_title(description).split())
            if description_tokens & MUSIC_DESCRIPTION_TERMS:
                score += 25
            if score > best_score:
                best_score = score
                best_qid = qid
        return best_qid if best_score >= 45 else ""

    def _wikidata_entity(self, qid: str) -> dict[str, str] | None:
        if qid in self._wikidata_entity_cache:
            return self._wikidata_entity_cache[qid]
        data = self._wikidata_get(
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "labels|descriptions|claims|sitelinks",
                "languages": "en",
                "format": "json",
            }
        )
        entity = (data.get("entities") or {}).get(qid)
        if not entity or "missing" in entity:
            return None
        claims = entity.get("claims") or {}
        self._prime_wikidata_labels(_claim_item_ids(claims, ["P21", "P106", "P19", "P27"]))
        result = {
            "Wikidata ID": qid,
            "Wikidata URL": f"https://www.wikidata.org/wiki/{qid}",
            "Wikipedia URL": _english_wikipedia_url(entity),
            "IMDb nmcode (Wikidata P345)": _first_url_claim(claims, "P345"),
            "Gender": self._claim_item_labels(claims, "P21"),
            "Occupations": self._claim_item_labels(claims, "P106"),
            "Birth Date": _first_time_claim(claims, "P569"),
            "Birth Place": self._claim_item_labels(claims, "P19"),
            "Country": self._claim_item_labels(claims, "P27"),
            "Official Website": _first_url_claim(claims, "P856"),
            "Wikidata Description": _clean(((entity.get("descriptions") or {}).get("en") or {}).get("value")),
        }
        self._wikidata_entity_cache[qid] = result
        return result

    def _claim_item_labels(self, claims: dict[str, Any], property_id: str) -> str:
        qids = []
        for claim in claims.get(property_id, []):
            value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {})
            qid = _clean(value.get("id") if isinstance(value, dict) else "")
            if qid:
                qids.append(qid)
        labels = [self._wikidata_label(qid) for qid in qids]
        return "; ".join(label for label in labels if label)

    def _wikidata_label(self, qid: str) -> str:
        if qid in self._wikidata_label_cache:
            return self._wikidata_label_cache[qid]
        data = self._wikidata_get(
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "labels",
                "languages": "en",
                "format": "json",
            }
        )
        label = _clean(
            ((((data.get("entities") or {}).get(qid) or {}).get("labels") or {}).get("en") or {}).get("value")
        )
        self._wikidata_label_cache[qid] = label
        return label

    def _prime_wikidata_labels(self, qids: list[str]) -> None:
        missing = sorted({qid for qid in qids if qid and qid not in self._wikidata_label_cache})
        if not missing:
            return
        data = self._wikidata_get(
            {
                "action": "wbgetentities",
                "ids": "|".join(missing[:50]),
                "props": "labels",
                "languages": "en",
                "format": "json",
            }
        )
        entities = data.get("entities") or {}
        for qid in missing[:50]:
            label = _clean(((entities.get(qid) or {}).get("labels") or {}).get("en", {}).get("value"))
            self._wikidata_label_cache[qid] = label

    def _wikidata_get(self, params: dict[str, str]) -> dict[str, Any]:
        response = self.http_client.session.get(
            WIKIDATA_API_URL,
            params=params,
            timeout=self.http_client.timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response.json()


def _parse_chart_containers(soup: BeautifulSoup, chart_date: str) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for container in soup.select("div.o-chart-results-list-row-container, div.chart-results-list-row-container"):
        artist_node = container.select_one("h3#title-of-a-story, h3.c-title")
        artist_name = _clean(artist_node.get_text(" ", strip=True) if artist_node else "")
        if not artist_name:
            continue
        artist_url = _artist_url_from_container(container, artist_node)

        tokens = [_clean(text) for text in container.stripped_strings]
        tokens = [token for token in tokens if token and token.lower() not in {"last week", "peak pos.", "wks on chart"}]
        rank = _first_rank_token(tokens)
        if not rank:
            continue

        stats = _stat_tokens_after_artist(tokens, artist_name)
        output.append(
            _row(
                rank=rank,
                artist_name=artist_name,
                last_week=stats[0] if len(stats) >= 1 else "",
                peak=stats[1] if len(stats) >= 2 else "",
                weeks=stats[2] if len(stats) >= 3 else "",
                chart_date=chart_date,
                artist_url=artist_url,
                details="Parsed from Billboard chart row.",
            )
        )
    return _dedupe_rows(output)


def _parse_embedded_json(soup: BeautifulSoup, chart_date: str) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False)
        if not text or "Artist 100" not in text:
            continue
        output.extend(_rows_from_json_text(text, chart_date))
    return _dedupe_rows(output)


def _rows_from_json_text(text: str, chart_date: str) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for payload in _json_payloads(text):
        for item in _walk_json(payload):
            if not isinstance(item, dict):
                continue
            rank = _clean(item.get("rank") or item.get("position") or item.get("chartPosition"))
            artist_name = _clean(
                item.get("artist")
                or item.get("artistName")
                or item.get("title")
                or item.get("name")
            )
            if not rank.isdigit() or not artist_name:
                continue
            output.append(
                _row(
                    rank=rank,
                    artist_name=artist_name,
                    last_week=_clean(item.get("lastWeek") or item.get("last_week") or item.get("last")),
                    peak=_clean(item.get("peak") or item.get("peakPosition") or item.get("peak_pos")),
                    weeks=_clean(item.get("weeks") or item.get("weeksOnChart") or item.get("weeks_on_chart")),
                    chart_date=chart_date,
                    artist_url=_billboard_artist_url_from_item(item),
                    details="Parsed from embedded Billboard page data.",
                )
            )
    if output:
        return output

    pattern = re.compile(
        r'"rank"\s*:\s*"?(\d{1,3})"?.{0,500}?"(?:artistName|artist|title|name)"\s*:\s*"([^"]+)"',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        output.append(
            _row(
                rank=match.group(1),
                artist_name=_decode_json_text(match.group(2)),
                last_week="",
                peak="",
                weeks="",
                chart_date=chart_date,
                artist_url="",
                details="Parsed from embedded Billboard text.",
            )
        )
    return output


def _json_payloads(text: str) -> list[Any]:
    payloads: list[Any] = []
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            payloads.append(json.loads(stripped))
        except json.JSONDecodeError:
            pass
    next_match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, flags=re.DOTALL)
    if next_match:
        try:
            payloads.append(json.loads(next_match.group(1)))
        except json.JSONDecodeError:
            pass
    return payloads


def _walk_json(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _first_rank_token(tokens: list[str]) -> str:
    for token in tokens[:8]:
        if token.isdigit() and 1 <= int(token) <= 100:
            return token
    return ""


def _stat_tokens_after_artist(tokens: list[str], artist_name: str) -> list[str]:
    try:
        start_index = next(index for index, token in enumerate(tokens) if token == artist_name)
    except StopIteration:
        start_index = 0
    stats = []
    for token in tokens[start_index + 1 :]:
        normalized = token.upper().replace("  ", " ")
        if STAT_TOKEN_RE.match(normalized):
            stats.append(token)
        if len(stats) >= 3:
            break
    return stats


def _extract_chart_date(soup: BeautifulSoup) -> str:
    text = _clean(soup.get_text(" ", strip=True))
    match = re.search(r"\bWeek of\s+([A-Za-z]+\.?\s+\d{1,2},\s*\d{4})", text, flags=re.IGNORECASE)
    if match:
        parsed = _parse_date(match.group(1))
        if parsed:
            return parsed.isoformat()
    canonical = soup.find("link", rel="canonical")
    href = canonical.get("href", "") if canonical else ""
    match = re.search(r"/charts/artist-100/(\d{4}-\d{2}-\d{2})/?", href)
    if match:
        return match.group(1)
    return ""


def _parse_date(value: str) -> date | None:
    try:
        return date_parser.parse(value).date()
    except (TypeError, ValueError):
        return None


def _artist_url_from_container(container, artist_node) -> str:
    if artist_node:
        anchor = artist_node.find_parent("a", href=True)
        if anchor:
            return urljoin(BILLBOARD_ARTIST_100_URL, anchor["href"])
    for anchor in container.find_all("a", href=True):
        href = anchor["href"]
        if "/artist/" in href:
            return urljoin(BILLBOARD_ARTIST_100_URL, href)
    return ""


def _billboard_artist_url_from_item(item: dict[str, Any]) -> str:
    for key in ["artistUrl", "artistURL", "artist_url", "artistLink", "artist_link", "url", "link"]:
        value = _clean(item.get(key))
        if value and ("/artist/" in value or "billboard.com" in value):
            return urljoin(BILLBOARD_ARTIST_100_URL, value)
    artist = item.get("artist")
    if isinstance(artist, dict):
        return _billboard_artist_url_from_item(artist)
    return ""


def _billboard_details_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    details: list[str] = []
    for selector in [
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"},
    ]:
        meta = soup.find("meta", selector)
        content = _clean(meta.get("content", "") if meta else "")
        if content:
            details.append(content)
    for item in _jsonld_items(soup):
        if not isinstance(item, dict):
            continue
        description = _clean(item.get("description"))
        genre = _jsonld_text(item.get("genre"))
        birth_date = _clean(item.get("birthDate"))
        birth_place = _jsonld_text(item.get("birthPlace"))
        if description:
            details.append(description)
        extras = []
        if genre:
            extras.append(f"Genre: {genre}")
        if birth_date:
            extras.append(f"Birth date: {birth_date}")
        if birth_place:
            extras.append(f"Birth place: {birth_place}")
        if extras:
            details.append("; ".join(extras))
    output = []
    seen = set()
    for detail in details:
        key = normalize_title(detail)
        if key and key not in seen:
            seen.add(key)
            output.append(detail)
    return " | ".join(output)[:1200]


def _jsonld_items(soup: BeautifulSoup) -> list[Any]:
    output = []
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text("", strip=False)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            output.extend(data)
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            output.extend(data["@graph"])
        else:
            output.append(data)
    return output


def _jsonld_text(value: Any) -> str:
    if isinstance(value, list):
        values = [_jsonld_text(item) for item in value]
        return "; ".join(item for item in values if item)
    if isinstance(value, dict):
        return _clean(value.get("name") or value.get("@id") or value.get("url"))
    return _clean(value)


def _titleize_professions(value: str) -> str:
    return "; ".join(part.replace("_", " ").title() for part in _clean(value).split(",") if part.strip())


def _title_score(left: str, right: str) -> int:
    left_norm = normalize_title(left)
    right_norm = normalize_title(right)
    if not left_norm or not right_norm:
        return 0
    if left_norm == right_norm:
        return 70
    if left_norm in right_norm or right_norm in left_norm:
        return 45
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = len(left_tokens & right_tokens)
    return int((overlap / max(len(left_tokens), len(right_tokens))) * 40)


def _first_time_claim(claims: dict[str, Any], property_id: str) -> str:
    for claim in claims.get(property_id, []):
        value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {})
        time_value = _clean(value.get("time") if isinstance(value, dict) else "")
        if time_value:
            return time_value.lstrip("+").split("T", 1)[0]
    return ""


def _first_url_claim(claims: dict[str, Any], property_id: str) -> str:
    for claim in claims.get(property_id, []):
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        url = _clean(value if isinstance(value, str) else "")
        if url:
            return url
    return ""


def _english_wikipedia_url(entity: dict[str, Any]) -> str:
    title = _clean((((entity.get("sitelinks") or {}).get("enwiki") or {}).get("title")))
    if not title:
        return ""
    return f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='()_')}"


def _claim_item_ids(claims: dict[str, Any], property_ids: list[str]) -> list[str]:
    qids = []
    for property_id in property_ids:
        for claim in claims.get(property_id, []):
            value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {})
            qid = _clean(value.get("id") if isinstance(value, dict) else "")
            if qid:
                qids.append(qid)
    return qids


def _row(
    rank: str,
    artist_name: str,
    last_week: str,
    peak: str,
    weeks: str,
    chart_date: str,
    artist_url: str,
    details: str,
) -> dict[str, str]:
    return {
        "Rank": rank,
        "Artist Name": artist_name,
        "IMDb nmcode": "",
        "IMDb URL": "",
        "IMDb Primary Profession": "",
        "IMDb Known For Titles": "",
        "Wikidata ID": "",
        "Wikidata URL": "",
        "Wikipedia URL": "",
        "Gender": "",
        "Occupations": "",
        "Birth Date": "",
        "Birth Place": "",
        "Country": "",
        "Official Website": "",
        "Wikidata Description": "",
        "Billboard Artist URL": artist_url,
        "Billboard Details": "",
        "Last Week": last_week,
        "Peak Position": peak,
        "Weeks on Chart": weeks,
        "Chart Date": chart_date,
        "Source URL": BILLBOARD_ARTIST_100_URL,
        "Other Details": details,
    }


def _dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    output = []
    for row in rows:
        key = (row.get("Rank", ""), row.get("Artist Name", ""))
        if key in seen or not key[0] or not key[1]:
            continue
        seen.add(key)
        output.append(row)
    return sorted(output, key=lambda item: int(item["Rank"]) if item["Rank"].isdigit() else 999)


def _decode_json_text(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()
