import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------
# Make an incomplete run say so
# --------------------------------------------------------------------------
#
# The GUI regressions in this project reached a user's desktop while the suite
# was green, because tkinter is missing from the default interpreter here and
# the whole GUI module was skipped — reported as a single quiet "1 skipped".
#
# A summary that says "750 passed" when the user interface was never exercised
# is not a true account of the run, so an unexercised GUI is reported loudly,
# with the command that fixes it.

_GUI_MODULES = ("test_gui.py", "test_gui_regressions.py")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    skipped = terminalreporter.stats.get("skipped", [])
    missed = sorted({
        name
        for report in skipped
        for name in _GUI_MODULES
        if name in str(getattr(report, "nodeid", ""))
    })
    if not missed:
        return
    write = terminalreporter.write_line
    write("")
    write("=" * 70, yellow=True)
    write("THE USER INTERFACE WAS NOT TESTED IN THIS RUN", yellow=True, bold=True)
    write("=" * 70, yellow=True)
    write(f"Skipped: {', '.join(missed)}")
    write("")
    write("tkinter or a display is unavailable, so every GUI test was skipped.")
    write("A green run above does NOT mean the interface works — this is exactly")
    write("how the scan-refresh and sync regressions reached a desktop.")
    write("")
    write("Run them with:")
    write("    PYTHONPATH=src xvfb-run -a python3.12 -m pytest tests/")
    write("")
