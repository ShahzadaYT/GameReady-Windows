"""main.py — application entry point."""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    """Start the GUI, or the CLI when arguments are given."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv:
        from .cli import main as cli_main
        return cli_main(argv)
    try:
        from .gui_app import launch_gui
    except ImportError as exc:                 # tkinter missing
        print(f"Graphical interface unavailable ({exc}).", file=sys.stderr)
        print("Run 'travelready --help' for the command line.", file=sys.stderr)
        return 1
    return launch_gui()


if __name__ == "__main__":
    raise SystemExit(main())
