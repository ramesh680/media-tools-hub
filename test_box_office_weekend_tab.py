"""Tests for the release-page Domestic Weekend tab parser used by the
'Last Week's US Box Office Openings' tool to pull Rank, Weekend gross and
Theater count directly from a title's Box Office Mojo release/weekend page."""

from app.services.box_office_mojo import (
    BoxOfficeMojoService,
    _parse_release_weekend_tab,
    _release_weekend_tab_url,
)


# Mirrors the real /release/rl#######/weekend/ "Domestic Weekend" table:
# Date | Rank | Weekend | %± LW | Theaters | Change | Avg | To Date | Weekend(#)
WEEKEND_TAB_HTML = """
<html><body>
<table class="a-bordered mojo-body-table mojo-table-annotated">
  <tr>
    <th>Date</th><th>Rank</th><th>Weekend</th><th>%± LW</th>
    <th>Theaters</th><th>Change</th><th>Avg</th><th>To Date</th><th>Weekend</th>
  </tr>
  <tr>
    <td><a href="/weekend/2026W33/">Aug 14-16</a></td>
    <td>5</td>
    <td>$3,950,000</td>
    <td>-</td>
    <td>754</td>
    <td>-</td>
    <td>$5,238</td>
    <td>$3,950,000</td>
    <td>1</td>
  </tr>
</table>
</body></html>
"""

# Same film a week later: two weekend rows, opening figures must still win.
WEEKEND_TAB_HTML_TWO_ROWS = """
<html><body>
<table class="mojo-body-table">
  <tr>
    <th>Date</th><th>Rank</th><th>Weekend</th><th>%± LW</th>
    <th>Theaters</th><th>Change</th><th>Avg</th><th>To Date</th><th>Weekend</th>
  </tr>
  <tr>
    <td>Aug 14-16</td><td>5</td><td>$3,950,000</td><td>-</td>
    <td>754</td><td>-</td><td>$5,238</td><td>$3,950,000</td><td>1</td>
  </tr>
  <tr>
    <td>Aug 21-23</td><td>9</td><td>$1,200,000</td><td>-69.6%</td>
    <td>500</td><td>-254</td><td>$2,400</td><td>$6,100,000</td><td>2</td>
  </tr>
</table>
</body></html>
"""


def test_release_weekend_tab_url_from_release_page():
    assert (
        _release_weekend_tab_url("https://www.boxofficemojo.com/release/rl1941929985/?ref_=x")
        == "https://www.boxofficemojo.com/release/rl1941929985/weekend/"
    )


def test_release_weekend_tab_url_ignores_non_release_urls():
    assert _release_weekend_tab_url("https://www.boxofficemojo.com/title/tt123/") == ""
    assert _release_weekend_tab_url("") == ""


def test_parse_release_weekend_tab_opening_row():
    stats = _parse_release_weekend_tab(WEEKEND_TAB_HTML)
    assert stats["rank"] == "5"
    assert stats["weekend_gross"] == "$3,950,000"
    assert stats["theaters"] == "754"


def test_parse_release_weekend_tab_prefers_opening_weekend():
    stats = _parse_release_weekend_tab(WEEKEND_TAB_HTML_TWO_ROWS)
    # Must return the run-number "1" (opening) row, not the later weekend.
    assert stats["rank"] == "5"
    assert stats["weekend_gross"] == "$3,950,000"
    assert stats["theaters"] == "754"


def test_parse_release_weekend_tab_no_table():
    assert _parse_release_weekend_tab("<html><body><p>no table</p></body></html>") == {}


class _StubHttpClient:
    """Serves the release summary page then the weekend tab page."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get_text(self, url):
        self.requested.append(url)
        return self.pages.get(url, "")


def test_recent_opening_row_uses_weekend_tab_for_rank():
    summary_url = "https://www.boxofficemojo.com/release/rl1941929985/"
    weekend_url = "https://www.boxofficemojo.com/release/rl1941929985/weekend/"
    summary_html = """
    <html><body>
      <div><span>Opening</span><span>$3,950,000</span> <span>754 theaters</span></div>
    </body></html>
    """
    http = _StubHttpClient({summary_url: summary_html, weekend_url: WEEKEND_TAB_HTML})
    service = BoxOfficeMojoService(http)
    calendar_row = {
        "Title Name": "Example Concert Film",
        "Distributor": "Trafalgar Releasing",
        "Release Date": "2026-08-12",
        "Source URL": summary_url,
    }
    row = service._recent_opening_row(calendar_row)
    assert row["Domestic Opening Weekend Rank"] == "5"
    assert row["Opening Gross"] == "$3,950,000"
    assert row["Opening Theaters"] == "754"
    assert weekend_url in http.requested


if __name__ == "__main__":
    import sys

    failures = 0
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            try:
                obj()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
