"""Guard the operations console against returning to an unstructured wizard.

These tests do not need a browser or a live Cozmo.
"""
from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1] / "cozmo_roverd" / "web"
HTML = (ROOT / "index.html").read_text()
JS = (ROOT / "app.js").read_text()


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []
        self.pages: list[str] = []
        self.nav_targets: list[str] = []
        self.quick_targets: list[str] = []
        self.hidden_pages: set[str] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
        if "data-page" in values:
            self.pages.append(values["id"])
            if "hidden" in values:
                self.hidden_pages.add(values["id"])
        if "data-nav" in values:
            self.nav_targets.append(values["data-nav"])
        if "data-go" in values:
            self.quick_targets.append(values["data-go"])


def test_all_navigation_pages_are_distinct_and_accessible():
    page = Markup()
    page.feed(HTML)
    expected = {"monitor", "adapter", "channel", "options", "diagnostics"}
    assert set(page.pages) == expected
    assert set(page.nav_targets) == expected
    assert set(page.quick_targets) <= expected
    assert page.hidden_pages == expected - {"monitor"}
    assert not [id_ for id_, count in Counter(page.ids).items() if count != 1]


def test_javascript_controls_exist_in_markup():
    page = Markup()
    page.feed(HTML)
    refs = set(re.findall(r'\$\("([a-zA-Z0-9_-]+)"\)', JS))
    assert refs <= set(page.ids), sorted(refs - set(page.ids))


def test_operational_actions_are_separate_from_diagnostics():
    overview = HTML.split('<section id="monitor"', 1)[1].split(
        '<section id="adapter"', 1)[0]
    diagnostics = HTML.split('<section id="diagnostics"', 1)[1].split(
        '<div class="footer-action"', 1)[0]
    assert 'id="start-service"' in overview
    assert 'id="stop-service"' in overview
    assert 'id="telemetry-cards"' in diagnostics
    assert 'id="telemetry-raw"' in diagnostics
    assert 'id="telemetry-cards"' not in overview


def test_save_only_when_dirty_and_no_arbitrary_channel_attempt_setting():
    assert 'id="save-bar" hidden' in HTML
    assert "state.dirty" in JS and 'updateSaveBar()' in JS
    assert "channel-max-attempts" not in HTML + JS
    assert "Complete the four steps" not in HTML + JS
    assert "scrollIntoView" not in JS
    assert 'id="channel-live"' in HTML
    assert 'id="test-dock-switch"' in HTML
