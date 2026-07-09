"""Wave 3 foundation UI seam (issue #43, ADR-0006).

Exercises the shared chrome every screen now builds on: the base layout, the
sticky app header, the four-cell bottom nav, and the detail-screen (back-arrow)
variant. The genuine external boundary is HTTP (``TestClient``); assertions
pin partner-visible structure via the ``<!--section:...-->`` anchors, not CSS
classes or token values.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tangerine.web.app import create_app
from tangerine.web.auth import SESSION_COOKIE

_TEST_PASSPHRASE = "foundation-ui-test-passphrase"
_TEST_SIGNING_SECRET = "foundation-ui-test-signing-secret"


def _write_assignees(tmp_path: Path) -> Path:
    path = tmp_path / "assignees.yaml"
    path.write_text(
        """
assignees:
  - assignee_id: daniel
    name: Daniel
  - assignee_id: noi
    name: Noi
""",
        encoding="utf-8",
    )
    return path


def _write_configs(tmp_path: Path) -> tuple[Path, Path, Path]:
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(
        """
recipes:
  - sku_id: chang-draft-500
    name: Chang Draft 500ml
    segment: bar
    ingredients:
      - { sku_id: chang-keg, quantity: "500" }
""",
        encoding="utf-8",
    )
    costs = tmp_path / "costs.yaml"
    costs.write_text(
        """
costs:
  chang-keg: { price: "0.07", updated_at: "2026-06-01" }
""",
        encoding="utf-8",
    )
    assignees = _write_assignees(tmp_path)
    return recipes, costs, assignees


def _build_app(tmp_path: Path):
    recipes, costs, assignees = _write_configs(tmp_path)
    return create_app(
        db_path=str(tmp_path / "test.db"),
        recipes_path=str(recipes),
        costs_path=str(costs),
        assignees_path=str(assignees),
        passphrase=_TEST_PASSPHRASE,
        signing_secret=_TEST_SIGNING_SECRET,
    )


def _authed_client(app) -> TestClient:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    client.post(
        "/login",
        data={"passphrase": _TEST_PASSPHRASE, "assignee_id": "daniel"},
        follow_redirects=False,
    )
    return client


def _section(html: str, anchor: str) -> str:
    start = f"<!--section:{anchor}-->"
    end = f"<!--/section:{anchor}-->"
    return html.split(start, 1)[1].split(end, 1)[0]


def test_main_screen_renders_shared_chrome(tmp_path: Path) -> None:
    """Main screens carry the app header and four-cell bottom nav."""
    client = _authed_client(_build_app(tmp_path))

    html = client.get("/skus").text

    assert "Tangerine Books" in _section(html, "app-header")
    nav = _section(html, "bottom-nav")
    assert "Today" in nav
    assert "Reports" in nav
    assert "Stock" in nav
    assert "Log" in nav


def test_today_screen_marks_today_active_and_shows_out_logout(
    tmp_path: Path,
) -> None:
    """Today is the active nav cell and the header offers an OUT logout."""
    client = _authed_client(_build_app(tmp_path))

    html = client.get("/review?mode=day&day=2026-07-08").text

    nav = _section(html, "bottom-nav")
    assert 'tb-bottomnav__cell--active' in nav
    assert ">Today</a>" in nav
    header = _section(html, "app-header")
    assert "Daniel" in header
    assert 'action="/logout"' in header
    assert ">Out</button>" in header


def test_bottom_nav_marks_active_cell_per_destination(tmp_path: Path) -> None:
    """Each primary destination marks its own nav cell active."""
    client = _authed_client(_build_app(tmp_path))

    cases = (
        ("/review?mode=month&month=2026-07", "Reports"),
        ("/skus", "Stock"),
        ("/audit", "Log"),
    )
    for url, label in cases:
        nav = _section(client.get(url).text, "bottom-nav")
        assert f'tb-bottomnav__cell--active' in nav
        assert f">{label}</a>" in nav


def test_detail_screen_shows_back_arrow_without_bottom_nav(
    tmp_path: Path,
) -> None:
    """Detail chrome swaps the bottom nav for a back arrow in the header."""
    client = _authed_client(_build_app(tmp_path))

    html = client.get("/upload").text

    header = _section(html, "app-header")
    assert "tb-header__back" in header
    assert "tb-header__partner" not in header
    assert "<!--section:bottom-nav-->" not in html


def test_login_screen_has_bare_chrome(tmp_path: Path) -> None:
    """Login drops the shared header and bottom nav."""
    client = TestClient(_build_app(tmp_path))

    html = client.get("/login").text

    assert "<!--section:app-header-->" not in html
    assert "<!--section:bottom-nav-->" not in html
    assert "/static/app.css" in html
    assert "fonts.googleapis.com" not in html


def test_base_layout_links_vendored_stylesheets_not_review_css(
    tmp_path: Path,
) -> None:
    """The old review.css is replaced by tokens + fonts + app.css."""
    client = _authed_client(_build_app(tmp_path))

    html = client.get("/").text

    assert "/static/tokens/colors.css" in html
    assert "/static/tokens/typography.css" in html
    assert "/static/tokens/spacing.css" in html
    assert "/static/fonts.css" in html
    assert "/static/app.css" in html
    assert "/static/review.css" not in html
    assert "fonts.googleapis.com" not in html
    assert "Kavoon" not in html
