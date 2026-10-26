"""Display scaling: the window must be the right size at 100%, 125%, 150% and 200%.

The regression: DPI awareness was requested after ``tk.Tk()`` had already read
the display's DPI, so at 150% (the ROG Ally's default) Tk drew everything at
two-thirds size. Requesting it in time makes the fonts the right size, but
row heights, padding, column widths and window sizes were fixed pixels: at 150%
a 32 px row held 30 px of text, at 200% it clipped it, and the "Travel
readiness" heading no longer fitted its column.

The display tests set Tk's scaling directly, which is what DPI awareness
changes, so they run on any host with a display, whatever its own DPI.
"""
from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from conftest import FIXTURES

tk = pytest.importorskip("tkinter")


# -- the scale factor --------------------------------------------------------

@pytest.mark.parametrize("dpi, scale", [
    (96, 1.0),        # 100%
    (120, 1.25),      # 125%
    (144, 1.5),       # 150%, the ROG Ally default
    (192, 2.0),       # 200%
    (72, 1.0),        # never shrink below the 100% layout
])
def test_ui_scale_follows_the_display_dpi(dpi, scale):
    from travelready.gui_app import ui_scale

    assert ui_scale(dpi) == scale


# -- DPI awareness comes first ------------------------------------------------

def test_dpi_awareness_is_requested_before_the_window_exists(monkeypatch):
    """Tk reads the DPI when it starts; asking afterwards is too late."""
    import travelready.gui_app as gui_app

    order = []

    class FakeRoot:
        def __init__(self):
            order.append("window")

        def mainloop(self):
            order.append("mainloop")

    monkeypatch.setattr(gui_app, "enable_dpi_awareness",
                        lambda: order.append("dpi awareness") or True)
    monkeypatch.setattr(gui_app, "tk", SimpleNamespace(Tk=FakeRoot))
    monkeypatch.setattr(gui_app, "TravelReadyGUI", lambda root: None)
    assert gui_app.launch_gui() == 0
    assert order == ["dpi awareness", "window", "mainloop"]


def test_dpi_awareness_without_the_windows_call_is_a_quiet_no(monkeypatch):
    """Off Windows, ``ctypes`` has no ``windll``; simulated so this runs everywhere.

    No skips in this module: the suite reports any skipped GUI test as "the
    user interface was not tested".
    """
    import ctypes

    from travelready.gui_app import enable_dpi_awareness

    monkeypatch.delattr(ctypes, "windll", raising=False)
    assert enable_dpi_awareness() is False


# -- the window at each scale -------------------------------------------------

SCALES = {"100%": 96, "125%": 120, "150%": 144, "200%": 192}


@pytest.fixture(params=list(SCALES), ids=list(SCALES))
def scaled_app(request, tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVELREADY_DATA_DIR", str(tmp_path))
    shutil.copy(FIXTURES / "games_2026-09-26.json", tmp_path / "games.json")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    dpi = SCALES[request.param]
    root.tk.call("tk", "scaling", dpi / 72.0)       # what DPI awareness sets
    from travelready.gui_app import TravelReadyGUI

    gui = TravelReadyGUI(root)
    root.update()
    yield gui, dpi
    root.destroy()


def _font(app, spec):
    import tkinter.font as tkfont

    return tkfont.Font(root=app.root, font=spec)


def test_the_scale_matches_the_display(scaled_app):
    app, dpi = scaled_app
    assert app.scale == pytest.approx(dpi / 96, abs=0.01)


def test_game_rows_are_tall_enough_for_their_text(scaled_app):
    from travelready.gui_app import BASE_FONT

    app, _ = scaled_app
    rowheight = int(tk.ttk.Style(app.root).lookup("Treeview", "rowheight"))
    assert rowheight >= _font(app, BASE_FONT).metrics("linespace") + 8


def test_column_headings_fit_their_columns(scaled_app):
    app, _ = scaled_app
    heading = _font(app, ("Segoe UI", 11, "bold"))
    for column in ("launcher", "readiness", "verify", "settings", "last"):
        title = app.tree.heading(column, "text")
        assert heading.measure(title) <= int(app.tree.column(column, "width")), title


def test_buttons_stay_thumb_sized(scaled_app):
    from travelready.gui_app import TOUCH_PAD

    app, dpi = scaled_app
    padding = tk.ttk.Style(app.root).lookup("TButton", "padding")
    horizontal = int(str(app.root.tk.splitlist(padding)[0]))
    assert horizontal == round(TOUCH_PAD * dpi / 96)


def test_the_window_fits_on_the_screen(scaled_app):
    app, _ = scaled_app
    app.root.update_idletasks()
    width, height = (int(v) for v in app.root.geometry().split("+")[0].split("x"))
    assert width <= app.root.winfo_screenwidth()
    assert height <= app.root.winfo_screenheight()
    min_width, min_height = (int(v) for v in app.root.minsize())
    assert min_width <= width and min_height <= height


@pytest.mark.parametrize("scaled_app", ["100%"], indirect=True)
def test_pixel_sizes_are_unchanged_at_100_percent(scaled_app):
    """The 100% layout is the one the window was designed and tested at."""
    app, _ = scaled_app
    assert app.px(32) == 32
    assert int(app.tree.column("#0", "width")) == 380
