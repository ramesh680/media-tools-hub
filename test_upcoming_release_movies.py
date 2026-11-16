"""Box Office Mojo calendar parsing for the standalone Upcoming Release Movies app.

The fixture is real markup captured from boxofficemojo.com/calendar/2026-12-01/
in Nov 2026 (posters/icons stripped). BOM switched its date headers from
"December 2, 2026" to "Wed Dec 2, 2026" and its genres to "A · B" — the old
parser read neither, so every month returned 0 movies.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "upcoming_movies"))
import upcoming_release_movies_app as app  # noqa: E402

FIXTURE = """<div id="table" class="a-section imdb-scroll-table mojo-gutter"><div class="a-section imdb-scroll-table-inner"><table class="mojo-body-table"><tbody>
<tr><th>Release</th><th>Distributor</th><th>Scale</th></tr>
<tr class="mojo-group-label"><th colspan="3" class="a-size-large mojo-table-header">Wed Dec 2, 2026</th></tr>
<tr><td><a href="/release/rl1214152961/"><div></div></a><div><a href="/release/rl1214152961/"><h3>A Fox Under a Pink Moon</h3></a><div class="a-section a-spacing-none mojo-schedule-genres"> Animation · Biography · Documentary </div><div><a href="/title/tt18163224">Cast, Crew, and Company Info</a></div></div></td><td><a href="/company/co0291948/boxoffice/">Kino Lorber</a></td><td>Limited</td></tr>
<tr class="mojo-group-label"><th colspan="3" class="a-size-large mojo-table-header">Fri Dec 4, 2026</th></tr>
<tr><td><a href="/release/rl1958641665/"><div></div></a><div><a href="/release/rl1958641665/"><h3>Behemoth!</h3></a><div class="a-section a-spacing-none mojo-schedule-genres"> Drama · Music </div><div><span>With: </span>Pedro Pascal, Olivia Wilde, Eva Victor, Alexa Swinton</div><div>2 hr 4 min</div><div><a href="/title/tt36415529">Cast, Crew, and Company Info</a></div></div></td><td><a href="/company/co0226183/boxoffice/">Walt Disney Studios Motion Pictures</a></td><td>Wide</td></tr>
<tr><td><a href="/release/rl1918861569/"><div></div></a><div><a href="/release/rl1918861569/"><h3>Romeo + Juliet</h3></a><div><span>30th Anniversary</span></div><div class="a-section a-spacing-none mojo-schedule-genres"> Drama · Romance </div><div><span>With: </span>Leonardo DiCaprio, Claire Danes, John Leguizamo, Harold Perrineau</div><div>2 hr</div><div><a href="/title/tt0117509">Cast, Crew, and Company Info</a></div></div></td><td>-</td><td>Limited</td></tr>
</tbody></table></div></div>"""


def test_display_date_formats():
    assert app.parse_display_date("Wed Dec 2, 2026") == date(2026, 12, 2)
    assert app.parse_display_date("December 2, 2026") == date(2026, 12, 2)
    assert app.parse_display_date("Thu Sept 3, 2026") == date(2026, 9, 3)
    assert app.parse_display_date("Release") is None


def test_current_calendar_markup_parses():
    movies = app.parse_calendar_html(FIXTURE, "https://www.boxofficemojo.com/calendar/2026-12-01/")
    assert [m["title"] for m in movies] == ["A Fox Under a Pink Moon", "Behemoth!", "Romeo + Juliet"]
    fox, behemoth, romeo = movies
    assert fox["release_date"] == "2026-12-02"
    assert fox["genres"] == ["Animation", "Biography", "Documentary"]
    assert fox["tt_code"] == "tt18163224"
    assert fox["distributor_network"] == "Kino Lorber"
    assert fox["release_scale"] == "Limited"
    assert behemoth["release_date"] == "2026-12-04"
    assert behemoth["genre"] == "Drama, Music"
    assert behemoth["cast"].startswith("Pedro Pascal")
    assert behemoth["runtime"] == "2 hr 4 min"
    assert behemoth["box_office_mojo_release_id"] == "rl1958641665"
    assert romeo["release_note"] == "30th Anniversary"
    assert romeo["release_type"] == "Anniversary"
    assert romeo["genres"] == ["Drama", "Romance"]
