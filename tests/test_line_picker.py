"""The line dialog: after logging in, the account's lines are offered as a list instead of typing ids."""
import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gateway.auth import LoginError  # noqa: E402
from gateway.lines import AvailableLine, LinesError  # noqa: E402
from gateway.ui import settings  # noqa: E402

LINES = [
    AvailableLine("4", "Staplercup", "1", "Regie"),
    AvailableLine("4", "Staplercup", "5", "PGM"),
    AvailableLine("9", "Pixmade", "2", "Kamera"),
]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, until, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return False


def dialog(line=None):
    d = settings.LineDialog(line or {}, "http://manager.test", "synthetic", [], [], False)
    d.user.setText("gw-regie")
    d.pw.setText("secret")
    return d


def fake_fetch(monkeypatch, result=None, error=None):
    calls = []

    async def fetch(url, user, password):
        calls.append((url, user, password))
        if error:
            raise error
        return result

    monkeypatch.setattr(settings, "fetch_available_lines", fetch)
    return calls


def test_before_login_the_list_asks_to_log_in(app):
    d = dialog()
    assert d.line_pick.count() == 1
    assert "anmelden" in d.line_pick.itemText(0).lower()


def test_login_fills_the_list_and_picking_sets_ids_and_name(app, monkeypatch):
    calls = fake_fetch(monkeypatch, LINES)
    d = dialog()
    d.btn_test.click()
    assert pump(app, lambda: d.line_pick.count() > 1), d.test_result.text()
    assert calls == [("http://manager.test", "gw-regie", "secret")]
    assert [d.line_pick.itemText(i) for i in range(1, d.line_pick.count())] == [ln.label for ln in LINES]
    assert "3 Leitungen" in d.test_result.text()

    d.line_pick.setCurrentIndex(2)  # Staplercup > PGM
    assert (d.prod.text(), d.line_id.text(), d.name.text()) == ("4", "5", "PGM")
    d._accept()
    assert d.result_line["production_id"] == "4" and d.result_line["line_id"] == "5"
    assert d.result_line["name"] == "PGM" and d.result_line["username"] == "gw-regie"


def test_picking_replaces_an_automatic_name_but_never_a_typed_one(app, monkeypatch):
    fake_fetch(monkeypatch, LINES)
    d = dialog()
    d.btn_test.click()
    assert pump(app, lambda: d.line_pick.count() > 1)
    d.line_pick.setCurrentIndex(1)
    assert d.name.text() == "Regie"
    d.line_pick.setCurrentIndex(3)
    assert d.name.text() == "Kamera"  # still the automatic name: follows the pick
    d.name.setText("Meine Regie")
    d.line_pick.setCurrentIndex(2)
    assert d.name.text() == "Meine Regie"
    assert d.line_id.text() == "5"


def test_configured_line_stays_selected_when_it_is_in_the_list(app, monkeypatch):
    fake_fetch(monkeypatch, LINES)
    d = dialog({"name": "PGM", "production_id": "4", "line_id": "5", "username": "gw-regie"})
    assert "aktuell" in d.line_pick.itemText(0)
    d.btn_test.click()
    assert pump(app, lambda: d.line_pick.count() > 1)
    assert d.line_pick.currentText() == "Staplercup  ›  PGM"
    assert (d.prod.text(), d.line_id.text()) == ("4", "5")


def test_configured_line_missing_from_the_list_is_kept_and_marked(app, monkeypatch):
    fake_fetch(monkeypatch, LINES)
    d = dialog({"name": "Alt", "production_id": "77", "line_id": "8", "username": "gw-regie"})
    d.btn_test.click()
    assert pump(app, lambda: d.line_pick.count() > 1)
    assert "nicht in der Liste" in d.line_pick.currentText()
    assert (d.prod.text(), d.line_id.text()) == ("77", "8")  # nothing is silently replaced


@pytest.mark.parametrize("error", [LoginError("login rejected for 'x' (HTTP 401) - check username/password", 401),
                                   LinesError("Server nicht erreichbar: boom")])
def test_errors_are_shown_and_manual_entry_still_works(app, monkeypatch, error):
    fake_fetch(monkeypatch, error=error)
    d = dialog()
    d.btn_test.click()
    assert pump(app, lambda: d.btn_test.isEnabled() and d.test_result.text() not in ("", "Melde an …"))
    assert d.test_result.text() == str(error)
    assert d.line_pick.count() == 1
    d.name.setText("Regie")
    d.btn_manual.setChecked(True)
    assert d.manual.isVisibleTo(d)
    d.prod.setText("4")
    d.line_id.setText("1")
    d._accept()
    assert d.result_line["production_id"] == "4" and d.result_line["line_id"] == "1"


def test_account_without_lines_says_so(app, monkeypatch):
    fake_fetch(monkeypatch, [])
    d = dialog()
    d.btn_test.click()
    assert pump(app, lambda: "keine Leitung" in d.test_result.text())


def test_saving_without_a_line_explains_what_to_do(app):
    d = dialog()
    d.name.setText("Regie")
    d._accept()
    assert d.result_line is None
    assert "Leitung" in d.error.text()
