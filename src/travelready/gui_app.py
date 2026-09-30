"""gui_app.py — the TravelReady desktop interface.

Built for a 7" 1920x1080 handheld touchscreen: large hit targets, a readable
base font, no hover-only affordances, and a layout that still works when
Windows display scaling is 150% or 200%.

Everything the previous build offered is preserved — launcher tabs with live
counts, library filtering, background scanning, batch testing, Prepare for
Travel, the readiness dashboard, diagnostics, the trip report, history,
settings and import/export — with the game-settings optimiser added as a
further tab.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import (
    __version__, appstate, classification, discovery, doctor as doctor_mod,
    environment, history, identity as identity_mod, launch_tester as lt,
    launchers, preparation, prepare_run, readiness, tasks,
)
from . import apppaths
from .apppaths import data_file
from .textnorm import redact_paths
from .library import (
    LIBRARY_FILE, SCAN_FOLDERS_FILE, GameEntry, export_games, import_games,
    infrastructure_entries, load_library, load_scan_folders, merge_library_updates,
    save_library,
)
from .optimiser import diff as opt_diff
from .optimiser import profiles as opt_profiles
from .optimiser import transaction as opt_tx
from .optimiser.model import TARGET_DEVICE
from .optimiser.rogallylife import SOURCE_BASE, SOURCE_NAME
from .optimiser.rogallylife import bridge as ral_bridge
from .optimiser.rogallylife import sync as ral_sync
from .optimiser.rogallylife.cache import ProfileCache
from .optimiser.rogallylife.client import RogAllyLifeClient
from .optimiser.rogallylife import select as ral_select
from .optimiser.rogallylife.select import (
    MODE_BALANCED, MODE_BATTERY, MODE_PERFORMANCE, OPERATING_MODES,
)

SETTINGS_FILE = "gui_settings.json"

BASE_FONT = ("Segoe UI", 11)
HEAD_FONT = ("Segoe UI", 20, "bold")
SUB_FONT = ("Segoe UI", 12)
MONO_FONT = ("Consolas", 10)
TOUCH_PAD = 12          # generous padding so buttons are thumb-sized


@dataclass
class Settings:
    """User preferences, persisted next to the library."""

    stale_days: int = readiness.DEFAULT_STALE_DAYS
    smoke_duration: float = lt.SMOKE_DEFAULT_DURATION
    reverify_ready: bool = False
    cleanup_after_test: bool = True
    confirm_manual: bool = True
    scan_folders: List[str] = field(default_factory=list)
    operating_mode: str = MODE_BALANCED
    max_watts: int = 0

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(data_file(SETTINGS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        payload = {k: getattr(self, k) for k in self.__dataclass_fields__}
        data_file(SETTINGS_FILE).write_text(json.dumps(payload, indent=2), encoding="utf-8")


class TravelReadyGUI:
    """The main window."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = Settings.load()
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        #: One runner for every background operation: scanning, testing,
        #: preparing and refreshing the source. It owns the threads and turns
        #: each outcome into exactly one of succeeded, cancelled or failed.
        self.runner = tasks.TaskRunner()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        #: tree row id -> the game it shows. Rows are identities (a game),
#: not entries (one installation of it).
        self.iid_to_identity: Dict[str, identity_mod.GameIdentity] = {}
        self.profile_store = opt_profiles.ProfileStore.load()
        self.current_plan = None
        self.source_cache = ProfileCache()

        #: The single owner of the library and everything derived from it.
        #: The window holds no second copy: `entries`, `identities` and the
        #: readiness cache below are views onto this object, so no handler can
        #: change the library and leave the tree showing the old one. Readiness
        #: is still computed once per game and cached — repainting used to cost
        #: 149 fuzzy matches on the UI thread per tab switch.
        self.state = appstate.AppState(
            environment=environment.current(check_network=False),
            resolver=self._build_resolver(),
            stale_days=self.settings.stale_days)
        self.state.bus.subscribe(appstate.LIBRARY_CHANGED,
                                 lambda payload: self._refresh())
        self.state.bus.subscribe(appstate.READINESS_CHANGED,
                                 lambda payload: self._update_dashboard())

        root.title(f"TravelReady {__version__}")
        root.geometry("1280x800")
        root.minsize(900, 600)
        self._build_style()
        self._build_widgets()
        self._load_library()
        self.root.after(120, self._poll_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- state: one owner, no second copies --------------------------------
    #
    # These delegate to `self.state` rather than shadowing it. Assigning to
    # `self.entries` anywhere in this file therefore invalidates the derived
    # data and repaints, which is what the scan handler previously failed to
    # do by hand.

    @property
    def entries(self) -> List[GameEntry]:
        return self.state.entries

    @entries.setter
    def entries(self, value: Sequence[GameEntry]) -> None:
        self.state.set_entries(value)

    @property
    def identities(self) -> List[identity_mod.GameIdentity]:
        return self.state.identities

    @property
    def current_tab(self) -> str:
        return self.state.tab

    @current_tab.setter
    def current_tab(self, value: str) -> None:
        self.state.set_view(tab=value)

    @property
    def environment(self):
        return self.state.environment

    @environment.setter
    def environment(self, value) -> None:
        self.state.environment = value

    @property
    def resolver(self) -> ral_bridge.SourceResolver:
        return self.state.resolver

    @resolver.setter
    def resolver(self, value) -> None:
        self.state.resolver = value
        self.state.invalidate_readiness()

    def _build_resolver(self) -> ral_bridge.SourceResolver:
        return ral_bridge.SourceResolver(
            self.source_cache, target_device=TARGET_DEVICE,
            mode=self.settings.operating_mode,
            max_watts=(self.settings.max_watts or None))

    # -- chrome ------------------------------------------------------------

    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=BASE_FONT)
        style.configure("TButton", padding=(TOUCH_PAD, TOUCH_PAD - 2), font=BASE_FONT)
        style.configure("Accent.TButton", padding=(TOUCH_PAD + 4, TOUCH_PAD),
                        font=("Segoe UI", 12, "bold"))
        style.configure("Treeview", rowheight=32, font=BASE_FONT)
        style.configure("Treeview.Heading", font=("Segoe UI", 11, "bold"))
        style.configure("TNotebook.Tab", padding=(18, 10), font=BASE_FONT)
        for state, colour in readiness.STATE_COLORS.items():
            style.configure(f"{state}.TLabel", foreground=colour, font=SUB_FONT)
        # Readiness verdicts get the same treatment, so the tree can colour a
        # row by its verdict rather than leaving the user to read every cell.
        for verdict, colour in preparation.READINESS_COLORS.items():
            self.root.option_add(f"*{verdict}", colour)

    def _build_widgets(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill="x")
        left = ttk.Frame(header)
        left.pack(side="left")
        ttk.Label(left, text="TravelReady", font=HEAD_FONT).pack(anchor="w")
        self.verdict_label = ttk.Label(left, text="", font=SUB_FONT)
        self.verdict_label.pack(anchor="w")
        self.dashboard_label = ttk.Label(header, text="", font=SUB_FONT,
                                         justify="right")
        self.dashboard_label.pack(side="right")
        #: Each readiness category is a button that filters the list to it, so
        #: "12 need attention" leads straight to those twelve.
        self.dashboard_bar = ttk.Frame(header)
        self.dashboard_bar.pack(side="right", padx=(0, 10))
        self.dashboard_buttons: Dict[str, ttk.Button] = {}

        toolbar = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        toolbar.pack(fill="x")
        self.prepare_button = ttk.Button(toolbar, text="  Prepare for Travel  ",
                                         style="Accent.TButton",
                                         command=self._on_prepare_travel)
        self.prepare_button.pack(side="left", padx=(4, 16))
        self.resume_button = ttk.Button(toolbar, text="Resume",
                                        command=self._on_resume_travel,
                                        state="disabled")
        self.resume_button.pack(side="left", padx=4)
        self.scan_button = ttk.Button(toolbar, text="Re-scan", command=self._on_scan)
        self.scan_button.pack(side="left", padx=4)
        self.test_button = ttk.Button(toolbar, text="Test selected",
                                      command=self._on_test_selected)
        self.test_button.pack(side="left", padx=4)
        self.cancel_button = ttk.Button(toolbar, text="Stop", command=self._on_cancel,
                                        state="disabled")
        self.cancel_button.pack(side="left", padx=4)
        ttk.Button(toolbar, text="Settings",
                   command=self._open_settings).pack(side="right", padx=4)
        ttk.Button(toolbar, text="History",
                   command=self._show_history).pack(side="right", padx=4)
        ttk.Button(toolbar, text="Trip report",
                   command=self._show_trip_report).pack(side="right", padx=4)
        ttk.Button(toolbar, text="Diagnostics",
                   command=self._show_doctor).pack(side="right", padx=4)
        ttk.Button(toolbar, text="Launchers",
                   command=self._show_launchers).pack(side="right", padx=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="x", padx=12)
        self.tab_frames: Dict[str, ttk.Frame] = {}
        for tab in readiness.TAB_ORDER:
            frame = ttk.Frame(self.notebook)
            self.tab_frames[tab] = frame
            self.notebook.add(frame, text=tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_selection_bar()

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12, pady=8)

        left = ttk.Frame(body)
        body.add(left, weight=3)
        columns = ("launcher", "readiness", "verify", "settings", "last")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="Game")
        self.tree.column("#0", width=380, minwidth=220)
        for name, title, width in (("launcher", "Launcher", 96),
                                   ("readiness", "Travel readiness", 150),
                                   ("verify", "Can verify", 96),
                                   ("settings", "Settings", 100),
                                   ("last", "Last checked", 116)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor="w")
        self.tree_scroll = ttk.Scrollbar(left, orient="vertical",
                                         command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        #: Shown in place of the table when there is nothing to list, so an
        #: empty view always explains itself.
        self.empty_label = ttk.Label(left, text="", font=SUB_FONT,
                                     justify="center", anchor="center",
                                     foreground="#555555")

        right = ttk.Notebook(body)
        body.add(right, weight=2)

        detail = ttk.Frame(right, padding=8)
        right.add(detail, text="Details")
        self.detail_text = tk.Text(detail, wrap="word", font=BASE_FONT, height=12,
                                   relief="flat", background="#f7f7f8")
        self.detail_text.pack(fill="both", expand=True)

        optimise = ttk.Frame(right, padding=8)
        right.add(optimise, text="Game settings")
        self._build_optimiser_panel(optimise)

        find = ttk.Frame(right, padding=8)
        right.add(find, text=f"Find in {SOURCE_NAME}")
        self._build_source_search_panel(find)

        diag = ttk.Frame(right, padding=8)
        right.add(diag, text="Diagnostics")
        self._build_diagnostics_bar(diag)
        self.diag_text = tk.Text(diag, wrap="word", font=MONO_FONT, height=12,
                                 relief="flat", background="#1e1e1e", foreground="#e6e6e6")
        self.diag_text.pack(fill="both", expand=True)

        status = ttk.Frame(self.root, padding=(12, 6))
        status.pack(fill="x")
        self.status_label = ttk.Label(status, text="Ready.", font=SUB_FONT)
        self.status_label.pack(side="left")
        self.progress = ttk.Progressbar(status, mode="determinate", length=260)
        self.progress.pack(side="right")

    def _build_selection_bar(self) -> None:
        """Search, filter, sort and bulk selection.

        A library of 150 games is not usable if preparing a subset means
        clicking each one, so every control here works on the *visible* games:
        selecting all while the Steam tab and a search are active selects those
        games and no others.
        """
        bar = ttk.Frame(self.root, padding=(12, 0))
        bar.pack(fill="x")

        ttk.Label(bar, text="Search:").pack(side="left", padx=(0, 4))
        self.search_var = tk.StringVar()
        search_box = ttk.Entry(bar, textvariable=self.search_var, width=26)
        search_box.pack(side="left", padx=(0, 10))
        # Filtering is cheap (it reads cached verdicts), but rebuilding the
        # tree on every keystroke is not: coalesce bursts of typing.
        self.search_var.trace_add("write", lambda *_: self._on_search_typed())
        self._search_after: Optional[str] = None

        ttk.Label(bar, text="Show:").pack(side="left", padx=(0, 4))
        self.filter_var = tk.StringVar(value=appstate.FILTER_ALL)
        filter_box = ttk.Combobox(bar, textvariable=self.filter_var, width=17,
                                  state="readonly", values=list(appstate.FILTERS))
        filter_box.pack(side="left", padx=(0, 10))
        filter_box.bind("<<ComboboxSelected>>",
                        lambda _e: self._on_view_changed(filter=self.filter_var.get()))

        ttk.Label(bar, text="Sort:").pack(side="left", padx=(0, 4))
        self.sort_var = tk.StringVar(value=appstate.SORT_NAME)
        sort_box = ttk.Combobox(bar, textvariable=self.sort_var, width=13,
                                state="readonly", values=list(appstate.SORTS))
        sort_box.pack(side="left", padx=(0, 10))
        sort_box.bind("<<ComboboxSelected>>",
                      lambda _e: self._on_view_changed(sort=self.sort_var.get()))

        ttk.Button(bar, text="Select all",
                   command=self._on_select_all).pack(side="left", padx=2)
        ttk.Button(bar, text="Select none",
                   command=self._on_select_none).pack(side="left", padx=2)
        ttk.Button(bar, text="Invert",
                   command=self._on_invert_selection).pack(side="left", padx=2)
        ttk.Button(bar, text="Select not ready",
                   command=self._on_select_not_ready).pack(side="left", padx=2)

        self.selection_label = ttk.Label(bar, text="No games selected.",
                                         font=SUB_FONT)
        self.selection_label.pack(side="right")

        # Ctrl+A anywhere in the window selects everything currently visible.
        self.root.bind_all("<Control-a>", self._on_ctrl_a)
        self.root.bind_all("<Control-A>", self._on_ctrl_a)

    # -- view controls -----------------------------------------------------

    def _on_search_typed(self) -> None:
        if self._search_after is not None:
            self.root.after_cancel(self._search_after)
        self._search_after = self.root.after(
            180, lambda: self._on_view_changed(search=self.search_var.get()))

    def _on_view_changed(self, **change) -> None:
        self._search_after = None
        self.state.set_view(**change)
        self._refresh()

    def _on_ctrl_a(self, event=None):
        """Select every visible game — unless the user is typing in a box."""
        widget = self.root.focus_get()
        if isinstance(widget, (ttk.Entry, tk.Entry, tk.Text)):
            return None
        self._on_select_all()
        return "break"

    def _on_select_all(self) -> None:
        self.state.select_all_visible()
        self._apply_selection_to_tree()

    def _on_select_none(self) -> None:
        self.state.select_none()
        self._apply_selection_to_tree()

    def _on_invert_selection(self) -> None:
        self.state.invert_selection()
        self._apply_selection_to_tree()

    def _on_select_not_ready(self) -> None:
        """Select what still needs work — never what merely could not be checked.

        UNKNOWN is not NOT-READY: a game whose readiness could not be
        determined is not silently swept into a batch of fixes.
        """
        self.state.select_where(
            lambda i: self.state.report_for(i).readiness in (
                preparation.ACTION_REQUIRED, preparation.READY_WITH_WARNINGS))
        self._apply_selection_to_tree()

    def _apply_selection_to_tree(self) -> None:
        """Mirror the model's selection into the tree widget."""
        wanted = self.state.selection
        iids = [iid for iid, ident in self.iid_to_identity.items()
                if ident.key in wanted]
        self.tree.selection_set(iids)
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        count = self.state.selection_count
        visible = len(self.state.visible())
        if count == 0:
            text = f"No games selected  ({visible} shown)"
        else:
            text = f"{count} of {visible} selected"
        self.selection_label.configure(text=text)

    def _build_diagnostics_bar(self, parent: ttk.Frame) -> None:
        """Controls for getting the activity log out of the application."""
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Copy log",
                   command=self._on_copy_log).pack(side="left", padx=2)
        ttk.Button(bar, text="Open data folder",
                   command=self._on_open_log_folder).pack(side="left", padx=2)
        ttk.Button(bar, text="Save log…",
                   command=self._on_save_log).pack(side="left", padx=2)
        ttk.Button(bar, text="Clear",
                   command=self._on_clear_log).pack(side="left", padx=2)
        ttk.Label(bar, text="Logs never contain credentials or tokens.",
                  font=SUB_FONT, foreground="#555555").pack(side="right")

    def diagnostics_text(self) -> str:
        """The activity log, with an environment header, ready to share.

        TravelReady holds no credentials or tokens — it reads a public site
        anonymously and never authenticates — so there is nothing of that kind
        to leak here. What the log *does* contain is file paths, which include
        the Windows user name, so the home directory is abbreviated before
        this text leaves the application.
        """
        header = [
            f"TravelReady {__version__}",
            f"Frozen executable: {'yes' if apppaths.is_frozen() else 'no'}",
            f"Data folder: {redact_paths(str(apppaths.data_dir()))}",
            f"Library: {redact_paths(str(self._library_path()))}",
            f"Target device: {TARGET_DEVICE}",
            f"Operating mode: {self.settings.operating_mode}",
            f"Games in library: {len(self.entries)} "
            f"({len(self.identities)} games, {len(self.state.non_games)} other)",
            f"{SOURCE_NAME} cache: {self.source_cache.stats().get('entries', 0)} games",
            "",
        ]
        body = self.diag_text.get("1.0", "end").rstrip()
        return "\n".join(header) + "\n" + redact_paths(body) + "\n"

    def _on_copy_log(self) -> None:
        text = self.diagnostics_text()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status(f"Diagnostics copied to the clipboard "
                         f"({len(text.splitlines())} lines).")

    def _on_open_log_folder(self) -> None:
        """Show the data folder in Explorer.

        Uses the same shell-open route as launching a game: no command line is
        built, so nothing in the path can be interpreted as a second command.
        """
        folder = apppaths.data_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            opener = getattr(os, "startfile", None)
            if opener is not None:
                opener(str(folder))
            else:
                webbrowser.open(folder.as_uri())
            self._set_status(f"Opened {redact_paths(str(folder))}.")
        except OSError as exc:
            messagebox.showerror(
                "Could not open the folder",
                f"{folder}\n\n{type(exc).__name__}: {exc}")

    def _on_save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save diagnostics", defaultextension=".txt",
            initialfile="travelready-diagnostics.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(self.diagnostics_text(), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Could not save the log",
                                 f"{path}\n\n{type(exc).__name__}: {exc}")
            return
        self._set_status(f"Diagnostics saved to {Path(path).name}.")

    def _on_clear_log(self) -> None:
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.configure(state="disabled")
        self._set_status("Activity log cleared.")

    def _build_source_search_panel(self, parent: ttk.Frame) -> None:
        """Search the recommendation source, whether or not a game is installed.

        This is a *reference* view. Looking up a game you do not own, or that
        TravelReady did not detect, must never add it to the library: the
        library records what is installed on this device, and a search result
        is not evidence of that. Nothing here writes ``games.json``.
        """
        ttk.Label(parent, text=f"Search {SOURCE_NAME} for a game's recommended "
                               f"settings.", font=SUB_FONT).pack(anchor="w")

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=6)
        self.source_search_var = tk.StringVar()
        box = ttk.Entry(bar, textvariable=self.source_search_var)
        box.pack(side="left", fill="x", expand=True)
        box.bind("<Return>", lambda _e: self._on_source_search())
        ttk.Button(bar, text="Search",
                   command=self._on_source_search).pack(side="left", padx=4)

        columns = ("installed", "profiles", "updated")
        self.source_results = ttk.Treeview(parent, columns=columns,
                                           show="tree headings", height=8,
                                           selectmode="browse")
        self.source_results.heading("#0", text="Game")
        self.source_results.column("#0", width=240, minwidth=160)
        for name, title, width in (("installed", "In your library", 110),
                                   ("profiles", "Profiles", 70),
                                   ("updated", "Source updated", 110)):
            self.source_results.heading(name, text=title)
            self.source_results.column(name, width=width, anchor="w")
        self.source_results.pack(fill="both", expand=True)
        self.source_results.bind("<<TreeviewSelect>>", self._on_source_result_selected)
        #: row id -> the catalogue entry it shows. Kept apart from the library
        #: on purpose: these games are not claimed to be installed.
        self.source_result_games: Dict[str, object] = {}

        self.source_detail = tk.Text(parent, wrap="word", font=BASE_FONT, height=12,
                                     relief="flat", background="#f7f7f8")
        self.source_detail.pack(fill="both", expand=True, pady=(6, 0))
        self._set_text(self.source_detail,
                       f"Type a game's name and press Search.\n\n"
                       f"This searches the {SOURCE_NAME} data cached on this "
                       f"device, so it works offline. Results are for reference: "
                       f"looking a game up here never adds it to your library.")

    def _on_source_search(self) -> None:
        term = self.source_search_var.get().strip()
        self.source_results.delete(*self.source_results.get_children())
        self.source_result_games.clear()
        if not term:
            return

        matches = self.resolver.search(term, limit=25)
        # Which results correspond to games already in the library? Reported,
        # never acted on.
        installed = {m.canonical_title.lower() for m in self.identities}
        for confidence, game in matches:
            mine = "yes" if game.title.lower() in installed else "no"
            iid = self.source_results.insert(
                "", "end", text=game.title,
                values=(mine, len(game.profiles), game.last_updated or "unknown"))
            self.source_result_games[iid] = game

        if not matches:
            stats = self.source_cache.stats()
            if not stats.get("entries"):
                message = (
                    f"No {SOURCE_NAME} data is cached on this device yet, so "
                    f"there is nothing to search.\n\n"
                    f"Press “Update {SOURCE_NAME}” to download it. "
                    f"This is not a statement that these games have no "
                    f"recommendations — nothing has been checked yet.")
            else:
                message = (
                    f"No game matching “{term}” is in the "
                    f"{SOURCE_NAME} data cached here ({stats['entries']} games).\n\n"
                    f"The source may cover it without this cache being current; "
                    f"run an update, or try a shorter part of the name.")
            self._set_text(self.source_detail, message)
            self._set_status(f"No {SOURCE_NAME} match for “{term}”.")
        else:
            self._set_status(f"{len(matches)} {SOURCE_NAME} result(s) "
                             f"for “{term}”.")

    def _on_source_result_selected(self, _event=None) -> None:
        selected = self.source_results.selection()
        if not selected:
            return
        game = self.source_result_games.get(selected[0])
        if game is None:
            return
        self._set_text(self.source_detail, self._describe_source_game(game))

    def _describe_source_game(self, game) -> str:
        """The published recommendation for one catalogue game, verbatim.

        Settings are shown exactly as the source published them. Nothing is
        extrapolated for a device or a profile the source did not cover.
        """
        lines = [game.title, ""]
        lines.append(f"Source:         {SOURCE_NAME}")
        lines.append(f"URL:            {game.source_url}")
        if game.last_updated:
            lines.append(f"Source updated: {game.last_updated}")
        lines.append(f"Device family:  {game.device_family}")
        lines.append("")

        if not game.profiles:
            lines.append("This page is in the cache, but no usable settings table "
                         "could be read from it.")
            lines.append("")
            lines.append("That is a fact about the page, not a recommendation to "
                         "leave the game unconfigured.")
            return "\n".join(lines)

        selection = ral_select.select_profile(
            game, self.settings.operating_mode,
            max_watts=self.settings.max_watts or None)
        for profile in game.profiles:
            marker = ("  ← best for "
                      f"{self.settings.operating_mode}"
                      if selection.profile is profile else "")
            lines.append(f"{profile.label}{marker}")
            for setting in profile.settings:
                lines.append(f"    {setting.label:<26} {setting.value}")
            lines.append("")

        if selection.reason:
            lines.append(f"Chosen profile: {selection.reason}")
            lines.append("")

        if game.title.lower() in {i.canonical_title.lower() for i in self.identities}:
            lines.append("This game is in your library — open it in the game list "
                         "to compare these settings with the ones on disk.")
        else:
            lines.append("This game is not in your library. TravelReady only "
                         "changes settings for games it found installed on this "
                         "device, and searching here does not add it.")
        return "\n".join(lines)

    def _build_optimiser_panel(self, parent: ttk.Frame) -> None:
        mode_bar = ttk.Frame(parent)
        mode_bar.pack(fill="x", pady=(0, 6))
        ttk.Label(mode_bar, text="Operating mode:").pack(side="left", padx=(0, 6))
        self.mode_var = tk.StringVar(value=self.settings.operating_mode)
        for label, value in (("Travel / battery", MODE_BATTERY),
                             ("Balanced", MODE_BALANCED),
                             ("Performance", MODE_PERFORMANCE)):
            ttk.Radiobutton(mode_bar, text=label, value=value,
                            variable=self.mode_var,
                            command=self._on_mode_changed).pack(side="left", padx=4)
        self.source_label = ttk.Label(mode_bar, text="", font=SUB_FONT)
        self.source_label.pack(side="right")

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Review changes",
                   command=self._on_review_settings).pack(side="left", padx=3)
        self.apply_button = ttk.Button(bar, text="Apply safe settings",
                                       style="Accent.TButton",
                                       command=self._on_apply_settings, state="disabled")
        self.apply_button.pack(side="left", padx=3)
        ttk.Button(bar, text="Restore backup",
                   command=self._on_restore_backup).pack(side="left", padx=3)
        self.view_source_button = ttk.Button(bar, text="View source",
                                             command=self._open_rog_ally_life,
                                             state="disabled")
        self.view_source_button.pack(side="right", padx=3)
        ttk.Button(bar, text=f"Update {SOURCE_NAME}",
                   command=self._on_update_source).pack(side="right", padx=3)
        self.settings_text = tk.Text(parent, wrap="word", font=MONO_FONT,
                                     relief="flat", background="#f7f7f8")
        self.settings_text.pack(fill="both", expand=True)
        self._update_source_label()
        self._set_text(self.settings_text,
                       "Select a game, then choose 'Review changes'.\n\n"
                       "Recommendations come from ROG Ally Life. Nothing is ever "
                       "changed without showing you the exact diff and asking first.")

    def _update_source_label(self) -> None:
        stats = self.source_cache.stats()
        last = stats["last_sync"][:10] if stats["last_sync"] else "never synced"
        self.source_label.configure(
            text=f"{SOURCE_NAME}: {stats['files']} games, {last}")

    def _on_mode_changed(self) -> None:
        self.settings.operating_mode = self.mode_var.get()
        self.settings.save()
        self.resolver = self._build_resolver()
        self.current_plan = None
        self.apply_button.configure(state="disabled")
        self._log(f"Operating mode set to {self.settings.operating_mode}.")

    def _on_update_source(self) -> None:
        """Refresh the recommendation cache in the background.

        The stop event reaches ``sync`` itself, so Cancel stops the run at its
        next checkpoint instead of merely greying out a button; the deadline
        means an unresponsive source ends the run rather than the session.
        """
        def work() -> None:
            try:
                cache = ProfileCache()
                report = ral_sync.sync(
                    RogAllyLifeClient(), cache,
                    device_family=ral_bridge.family_for_device(TARGET_DEVICE),
                    # Stages go to the status line as well as the log, so the
                    # window says what it is doing rather than only "Updating…".
                    progress=lambda m: self.ui_queue.put(("stage", m)),
                    on_item=lambda done, total:
                        self.ui_queue.put(("progress", (done, total))),
                    stop_event=self.cancel_event)
                self.ui_queue.put(("source_synced", report))
            except Exception as exc:
                # Report the failure; never let it read as "no recommendations".
                self.ui_queue.put(("source_failed", exc))

        self._start_worker(work, f"Updating {SOURCE_NAME}\u2026")

    def _on_source_synced(self, report) -> None:
        """A sync finished \u2014 possibly partially. The library is untouched.

        This handler once contained lines spliced in from ``__init__`` by a bad
        search-and-replace, including ``self.identities = []``, so a successful
        update emptied the game list. Re-reading the cache invalidates only
        what depends on the source: the resolver and the cached verdicts.
        """
        self._worker_finished()
        self._log(report.describe())
        self.source_cache = ProfileCache()
        self.resolver = self._build_resolver()
        self.state.source_synced(report)
        self._update_source_label()

        if report.blocked:
            messagebox.showwarning(
                f"{SOURCE_NAME} unreachable",
                f"Could not reach {SOURCE_BASE}.\n\nAnything already cached still "
                f"works offline. Check your connection and try again.")
            self._set_status(f"{SOURCE_NAME} unreachable \u2014 using cached data.")
        elif report.cancelled:
            self._set_status(f"Update cancelled after {report.duration:.0f}s. "
                             f"{report.changed} profile(s) updated before stopping.")
        elif report.timed_out:
            messagebox.showwarning(
                f"{SOURCE_NAME} update incomplete",
                f"The update stopped after {report.duration:.0f}s without "
                f"finishing.\n\nWhat was fetched has been saved, and the rest of "
                f"the cache is unchanged. Running the update again will carry on "
                f"from where it stopped.")
            self._set_status("Update incomplete \u2014 run it again to continue.")
        else:
            messagebox.showinfo(f"{SOURCE_NAME} updated", report.describe())
            self._set_status(f"Update finished in {report.duration:.0f}s: "
                             f"{report.changed} changed, "
                             f"{len(report.unchanged)} unchanged.")

    def _on_source_failed(self, exc: BaseException) -> None:
        """The update raised. Say what went wrong, and keep the cached data.

        "We could not reach the source" is not "this game has no
        recommendations", and the two must never look the same.
        """
        self._worker_finished()
        detail = f"{type(exc).__name__}: {exc}".strip()
        self._log(f"{SOURCE_NAME} update failed. {detail}")
        self._set_status(f"{SOURCE_NAME} update failed \u2014 cached data still in use.")
        messagebox.showerror(
            f"{SOURCE_NAME} update failed",
            f"The update could not be completed.\n\n{detail}\n\n"
            f"Your cached recommendations are unchanged and still available "
            f"offline. This is a problem reaching the source, not a sign that "
            f"these games have no recommendations.")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _set_text(widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _log(self, message: str) -> None:
        self.diag_text.configure(state="normal")
        self.diag_text.insert("end", message.rstrip() + "\n")
        self.diag_text.see("end")
        self.diag_text.configure(state="disabled")

    def _set_status(self, message: str) -> None:
        self.status_label.configure(text=message)

    def _library_path(self) -> Path:
        return data_file(LIBRARY_FILE)

    @property
    def _busy(self) -> bool:
        """Is a background operation running?

        The runner is the authority. Asking the thread would report a worker
        that has returned but whose outcome has not been delivered yet as
        finished, and a second task started in that window would race the
        first one's handler.
        """
        return self.runner.busy

    # -- library -----------------------------------------------------------

    def _load_library(self) -> None:
        entries, message = load_library(self._library_path())
        self.entries = entries
        self._report_non_games()
        self._log(message)
        self._set_status(message)
        self._refresh()
        saved = prepare_run.load_run()
        self.resume_button.configure(
            state="normal" if (saved and saved.resumable) else "disabled")
        if saved and saved.resumable:
            self._log(f"An interrupted preparation run is saved: "
                      f"{saved.completed} of {saved.total} done. Press Resume.")

    def _report_non_games(self) -> None:
        non_games = self.state.non_games
        if non_games:
            self._log(f"{len(non_games)} entries are not games (launchers, Windows "
                      f"apps, utilities) and are excluded from every count.")

    def _report_for(self, identity) -> preparation.PreparationReport:
        """Readiness for one game, computed once and cached by the model."""
        return self.state.report_for(identity)

    def _save_library(self) -> None:
        save_library(self.entries, self._library_path())

    def _visible_identities(self):
        """The games the current tab, search and filter admit."""
        return self.state.visible()

    def _restore_selection(self) -> None:
        """Re-apply the model's selection after the tree is rebuilt."""
        wanted = self.state.selection
        iids = [iid for iid, ident in self.iid_to_identity.items()
                if ident.key in wanted]
        if iids:
            self.tree.selection_set(iids)
        self._update_selection_label()

    def _update_empty_state(self) -> None:
        """Say why the list is empty, and what to do about it.

        An empty table with no explanation is indistinguishable from a broken
        one, and the three reasons need three different actions.
        """
        if self.tree.get_children():
            if self.empty_label.winfo_ismapped():
                self.empty_label.pack_forget()
                self.tree.pack(side="left", fill="both", expand=True)
                self.tree_scroll.pack(side="right", fill="y")
            return
        # Replace the table rather than leaving an empty one below the text:
        # an empty grid reads as a failure even when the message explains it.
        self.tree.pack_forget()
        self.tree_scroll.pack_forget()
        if not self.identities:
            text = ("No games in the library yet.\n\n"
                    "Press Re-scan to look for installed games.")
        elif self.state.search.strip():
            text = (f"No game matches “{self.state.search.strip()}” "
                    f"in this tab.\n\nClear the search, or choose the All tab.")
        elif self.state.filter != appstate.FILTER_ALL:
            text = (f"No game in this tab is “{self.state.filter}”.\n\n"
                    f"Choose a different filter to see the rest.")
        else:
            text = ("No games for this launcher.\n\n"
                    "The All tab shows everything in the library.")
        self.empty_label.configure(text=text)
        if not self.empty_label.winfo_ismapped():
            self.empty_label.pack(fill="both", expand=True, pady=24)

    def _visible_entries(self) -> List[GameEntry]:
        return [i.best_installation().entry for i in self._visible_identities()
                if i.best_installation()]

    def _selected_identities(self):
        return [self.iid_to_identity[iid] for iid in self.tree.selection()
                if iid in self.iid_to_identity]

    def _selected_entries(self) -> List[GameEntry]:
        return [i.best_installation().entry for i in self._selected_identities()
                if i.best_installation()]

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.iid_to_identity.clear()
        settings_labels = {"PASS": "profile", "WARN": "none",
                           "UNKNOWN": "not checked", "NOT_APPLICABLE": "-"}
        for identity in self.state.visible():
            installation = identity.best_installation()
            if installation is None:
                continue
            report = self._report_for(identity)
            verify = "yes" if installation.can_verify else "manual"
            launcher = "+".join(identity.launchers)
            iid = self.tree.insert(
                "", "end", text=identity.canonical_title,
                values=(launcher, report.readiness.replace("_", " ").title(),
                        verify,
                        settings_labels.get(report.settings_state, "-"),
                        readiness.age_text(installation.entry)),
                tags=(report.readiness,))
            self.iid_to_identity[iid] = identity
        for verdict, colour in preparation.READINESS_COLORS.items():
            self.tree.tag_configure(verdict, foreground=colour)
        # A repaint must not silently drop the user's selection: re-apply it
        # to the rows that are still on screen.
        self._restore_selection()
        self._update_empty_state()
        self._update_tab_labels()
        self._update_dashboard()

    def _update_tab_labels(self) -> None:
        counts = readiness.tab_counts(
            [i.best_installation().entry for i in self.identities
             if i.best_installation()])
        for index, tab in enumerate(readiness.TAB_ORDER):
            label = f"{tab} ({counts[tab]})" if counts[tab] else tab
            self.notebook.tab(index, text=label)

    def _update_dashboard(self) -> None:
        """Counts for the current tab, by readiness, each one clickable.

        "Cannot determine" is its own category throughout. A game that has not
        been checked is never counted as not ready, never folded into the
        number needing attention, and never blocks the all-clear on its own —
        it is reported as exactly what it is.
        """
        # Count against the tab, not the active filter: a dashboard that only
        # counted what the filter already showed would always read 100%.
        identities = self._tab_identities()
        reports = [self._report_for(i) for i in identities]
        counts = preparation.summarise(reports)
        self.dashboard_label.configure(text=f"{counts['total']} game(s)")

        categories = [
            ("Ready", preparation.READY, appstate.FILTER_READY),
            ("Warnings", preparation.READY_WITH_WARNINGS, appstate.FILTER_WARNINGS),
            ("Action required", preparation.ACTION_REQUIRED, appstate.FILTER_ACTION),
            ("Cannot determine", preparation.READINESS_UNKNOWN, appstate.FILTER_UNKNOWN),
        ]
        if counts[preparation.UNSUPPORTED]:
            categories.append(("Unsupported", preparation.UNSUPPORTED, None))

        for child in self.dashboard_bar.winfo_children():
            child.destroy()
        self.dashboard_buttons.clear()
        for label, verdict, filter_name in categories:
            count = counts.get(verdict, 0)
            button = ttk.Button(
                self.dashboard_bar, text=f"{label}: {count}", width=19,
                command=(lambda f=filter_name: self._on_dashboard_click(f)))
            if filter_name is None or not count:
                button.state(["disabled"])
            button.pack(side="left", padx=2)
            self.dashboard_buttons[verdict] = button

        needs_attention = counts[preparation.ACTION_REQUIRED] + counts[preparation.NOT_READY]
        unknown = counts[preparation.READINESS_UNKNOWN]
        if not reports:
            verdict_text = "No games yet — press Re-scan."
        elif needs_attention:
            verdict_text = f"{needs_attention} game(s) need attention before you travel."
        elif unknown:
            verdict_text = (f"{unknown} game(s) could not be checked from here. "
                            f"The rest of this tab is ready to travel.")
        else:
            verdict_text = "Everything in this tab is ready to travel."
        self.verdict_label.configure(text=verdict_text)

    def _tab_identities(self):
        """Games in the current launcher tab, ignoring search and filter."""
        if self.current_tab == "All":
            return self.identities
        return [i for i in self.identities
                if any(readiness.tab_for_launcher(l) == self.current_tab
                       for l in i.launchers)]

    def _on_dashboard_click(self, filter_name: Optional[str]) -> None:
        if not filter_name:
            return
        self.filter_var.set(filter_name)
        self._on_view_changed(filter=filter_name)

    # -- events ------------------------------------------------------------

    def _on_tab_changed(self, _event=None) -> None:
        index = self.notebook.index(self.notebook.select())
        self.current_tab = readiness.TAB_ORDER[index]
        self._refresh()

    def _on_select(self, _event=None) -> None:
        # The tree is the user's input; the model is the record. Clicking rows
        # updates the model so bulk actions and the count agree with what is
        # highlighted.
        selected = self._selected_identities()
        self.state.set_selection([i.key for i in selected])
        self._update_selection_label()
        if not selected:
            return
        identity = selected[0]
        installation = identity.best_installation()
        report = self._report_for(identity)
        lines = [identity.canonical_title, ""]
        for launcher in identity.launchers:
            copy = identity.installation_for(launcher)
            mark = "\u2713" if copy and copy.can_verify else "\u26a0"
            lines.append(f"{mark} {launcher}"
                         f"{'' if copy and copy.can_verify else '  (verify manually)'}")
        lines += ["", f"Travel readiness: {report.readiness.replace('_', ' ')}", ""]
        for check in report.checks:
            if check.outcome != preparation.NOT_APPLICABLE:
                lines.append(check.line())
        if report.actions:
            lines += ["", "What to do:"]
            lines += [f"  {i}. {a}" for i, a in enumerate(report.actions, 1)]
        if installation is not None:
            lines += ["", "Installation", "-" * 40,
                      f"Launch target     {installation.launch_target or '(none)'}",
                      f"Verify by         "
                      f"{', '.join(installation.verification_targets) or '(nothing)'}",
                      f"Install folder    {installation.install_dir or '(unknown)'}"]
        self._set_text(self.detail_text, "\n".join(lines))
        self.apply_button.configure(state="disabled")
        self.current_plan = None

    def _on_close(self) -> None:
        self.cancel_event.set()
        try:
            self.settings.save()
            self._save_library()
        finally:
            self.root.destroy()

    def _on_cancel(self) -> None:
        self.cancel_event.set()
        self._set_status("Stopping…")

    # -- background work ---------------------------------------------------

    def _start_worker(self, work: Callable[[], None], label: str) -> None:
        """Run ``work`` on a worker thread through the shared task runner.

        The runner owns the thread, the cancellation flag and the outcome.
        Every background operation goes through here, so none of them can
        quietly swallow an exception or leave the toolbar disabled: a worker
        that raises is reported, and the controls are restored whatever
        happened.
        """
        if self._busy:
            messagebox.showinfo("Busy", "A background task is already running.")
            return
        self.scan_button.configure(state="disabled")
        self.test_button.configure(state="disabled")
        self.prepare_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        # Until the work reports a total, the bar sweeps rather than sitting at
        # zero: "working, no idea how long" and "not started" must look
        # different, or a long first stage reads as a hang.
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(15)
        self._set_status(label)

        task = self.runner.start(
            label.rstrip("… ").rstrip(),
            lambda ctx: work(),
            on_failure=self._on_task_failed,
            on_finished=lambda t: self._worker_finished())
        if task is None:                       # the runner refused: stay honest
            self._worker_finished()
            return
        # The cancel event the existing workers watch is the task's own, so
        # Stop reaches whatever is running without a second flag to keep in
        # step.
        self.cancel_event = task.stop_event
        self.worker = self.runner.thread_for(task)

    def _on_task_failed(self, task) -> None:
        """A background operation raised. Say so; never fail silently."""
        self._log(f"{task.name} failed after {task.duration:.1f}s: "
                  f"{task.error_summary}")
        if task.traceback_text:
            self._log(task.traceback_text.rstrip())
        self._set_status(f"{task.name} failed: {task.error_summary}")
        messagebox.showerror(
            f"{task.name} failed",
            f"{task.error_summary}\n\nNothing was changed by the part that "
            f"failed. The Diagnostics tab has the details, and Copy log puts "
            f"them on the clipboard.")

    def _worker_finished(self) -> None:
        self.scan_button.configure(state="normal")
        self.test_button.configure(state="normal")
        self.prepare_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)

    def _poll_ui_queue(self) -> None:
        # Deliver task outcomes first, so a failure is reported before any
        # domain message that a half-finished worker may have queued.
        self.runner.pump()
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "stage":
                    # A stage is worth both: the status line for "what now",
                    # the log for "what happened".
                    self._log(payload)
                    self._set_status(payload)
                elif kind == "progress":
                    # A known total turns the sweep into a real measure.
                    done, total = payload
                    if str(self.progress.cget("mode")) == "indeterminate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    self.progress.configure(maximum=max(total, 1), value=done)
                elif kind == "scanned":
                    self._on_scanned(payload)
                elif kind == "tested":
                    self._on_tested(payload)
                elif kind == "source_synced":
                    self._on_source_synced(payload)
                elif kind == "source_failed":
                    self._on_source_failed(payload)
                elif kind == "prepared":
                    self._on_prepared(payload)
                elif kind == "doctor":
                    self._on_doctor(payload)
                elif kind == "done":
                    self._worker_finished()
                    self._set_status(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_ui_queue)

    def _on_scan(self) -> None:
        folders = load_scan_folders(data_file(SCAN_FOLDERS_FILE)) or self.settings.scan_folders

        def work() -> None:
            try:
                found = discovery.auto_scan(
                    cancel_event=self.cancel_event,
                    progress=lambda m: self.ui_queue.put(("log", m)),
                    custom_folders=folders)
                self.ui_queue.put(("scanned", found))
            except Exception as exc:                      # never kill the UI thread
                self.ui_queue.put(("log", f"Scan failed: {exc}"))
                self.ui_queue.put(("done", "Scan failed."))

        self._start_worker(work, "Scanning for installed games…")

    def _on_scanned(self, found: Sequence[GameEntry]) -> None:
        """A scan finished: the new games must be on screen immediately.

        Regression: this merged into the entry list but repainted from a
        separately-built identity list, so scanned games only appeared after
        restarting. Handing the merged entries to the model rebuilds the
        derived data and repaints as one step.
        """
        merged, added, updated = merge_library_updates(self.entries, found)
        result = appstate.ScanResult(
            found=len(found), added=added, updated=updated,
            total_entries=len(merged))
        self.state.scan_completed(merged, result)
        result.games = len(self.state.identities)
        result.non_games = len(self.state.non_games)
        self._save_library()
        self._worker_finished()
        message = (f"Scan complete: {len(found)} found, {added} new, "
                   f"{updated} updated, {len(merged)} in library "
                   f"({result.games} games shown).")
        self._log(message)
        self._set_status(message)

    def _run_tests(self, entries: Sequence[GameEntry], mode: str, cleanup: bool) -> None:
        total = len(entries)

        def confirm(entry: GameEntry) -> bool:
            if not self.settings.confirm_manual:
                return False
            answer: List[bool] = []
            done = threading.Event()

            def ask() -> None:
                answer.append(bool(messagebox.askyesno(
                    "Manual check",
                    f"TravelReady launched '{entry.name}' but cannot detect its "
                    f"process.\n\nDid the game start correctly?")))
                done.set()

            self.root.after(0, ask)
            done.wait(timeout=300)
            return bool(answer and answer[0])

        def work() -> None:
            try:
                for index, entry in enumerate(entries, 1):
                    if self.cancel_event.is_set():
                        break
                    self.ui_queue.put(("status", f"[{index}/{total}] {entry.name}"))
                    self.ui_queue.put(("progress", (index - 1, total)))
                    result = lt.run_test(
                        entry, mode=mode, stop_event=self.cancel_event,
                        on_progress=lambda m: self.ui_queue.put(("log", m)),
                        cleanup=cleanup,
                        smoke_duration=self.settings.smoke_duration,
                        manual_confirm=confirm)
                    self.ui_queue.put(("tested", (entry, result)))
                self.ui_queue.put(("progress", (total, total)))
                self.ui_queue.put(("done", "Finished."))
            except Exception as exc:
                self.ui_queue.put(("log", f"Test run failed: {exc}"))
                self.ui_queue.put(("done", "Test run failed."))

        self._start_worker(work, f"Testing {total} game(s)…")

    def _on_tested(self, payload) -> None:
        entry, result = payload
        entry.last_result = result.status
        if result.status == lt.STATUS_PASS:
            entry.last_ready = result.finished_at
        if result.corrected_process and not entry.expected_process:
            entry.expected_process = result.corrected_process
            self._log(f"Learned the real process name for {entry.name}: "
                      f"{result.corrected_process}")
        history.append_results([result], data_file("history.json"))
        self._save_library()
        self._refresh()

    def _on_test_selected(self) -> None:
        entries = self._selected_entries() or self._visible_entries()
        if not entries:
            messagebox.showinfo("Nothing to test", "No games in the current tab.")
            return
        self._run_tests(entries, lt.MODE_STANDARD, self.settings.cleanup_after_test)

    def _on_prepare_travel(self, resume: bool = False) -> None:
        identities = self._selected_identities() or self._visible_identities()
        if not identities:
            messagebox.showinfo("Nothing to prepare", "No games in this tab.")
            return
        options = prepare_run.PrepareOptions(
            launch=self.environment.windows.available,
            cleanup=self.settings.cleanup_after_test,
            smoke_duration=self.settings.smoke_duration,
            reverify_ready=self.settings.reverify_ready,
            resume=resume,
        )
        if not resume:
            targets, skipped = prepare_run.plan_run(
                identities, options, env=self.environment, resolver=self.resolver)
            if not targets:
                messagebox.showinfo(
                    "All ready",
                    "Every game in this tab is already prepared.\n\n"
                    "Enable 'Re-verify' in Settings to check them again.")
                return
            detail = (f"TravelReady will work through {len(targets)} game(s), "
                      f"starting each one, confirming it runs, then closing it.")
            if skipped:
                detail += (f"\n\n{len(skipped)} game(s) will be skipped because "
                           f"TravelReady could not close them safely.")
            if not options.launch:
                detail = (f"Launching needs Windows, so {len(targets)} game(s) will "
                          f"be assessed without being started.")
            if not messagebox.askyesno("Prepare for Travel", detail + "\n\nContinue?"):
                return

        def work() -> None:
            try:
                run = prepare_run.run_preparation(
                    identities, options, env=self.environment,
                    resolver=self.resolver,
                    on_progress=lambda m: self.ui_queue.put(("log", m)),
                    stop_event=self.cancel_event)
                self.ui_queue.put(("prepared", run))
            except Exception as exc:
                self.ui_queue.put(("log", f"Preparation failed: {exc}"))
                self.ui_queue.put(("done", "Preparation failed."))

        self._start_worker(work, f"Preparing {len(identities)} game(s)\u2026")

    def _on_resume_travel(self) -> None:
        self._on_prepare_travel(resume=True)

    def _on_prepared(self, run) -> None:
        self._worker_finished()
        self._save_library()
        # Preparation records results on the entries themselves, so the
        # verdicts derived from them are now stale.
        self.state.invalidate_readiness()
        self._refresh()
        self.resume_button.configure(state="normal" if run.resumable else "disabled")
        text = prepare_run.render_run_report(run, self.identities,
                                             resolver=self.resolver)
        self._log(text)
        self._set_status("Preparation finished."
                         if run.status == prepare_run.STATUS_COMPLETE
                         else "Preparation interrupted — press Resume.")
        self._show_text_window("Travel preparation", text)

    # -- settings optimiser ---    # -- settings optimiser ------------------------------------------------

    def _on_review_settings(self) -> None:
        selected = self._selected_entries()
        if not selected:
            messagebox.showinfo("Select a game", "Choose a game first.")
            return
        entry = selected[0]
        self.profile_store = opt_profiles.ProfileStore.load()
        plan = opt_diff.plan_for_game(entry, self.profile_store,
                                      resolver=self.resolver,
                                      mode=self.settings.operating_mode)
        self.current_plan = plan
        self._source_url = (plan.profile.source_url if plan.profile else "")
        self.view_source_button.configure(
            state="normal" if self._source_url else "disabled")
        text = opt_diff.render_plan(plan)
        if plan.profile is None:
            stats = self.source_cache.stats()
            text += (f"\n\nCached {SOURCE_NAME} games: {stats['files']}"
                     f"   locally imported profiles: {len(self.profile_store)}\n"
                     f"Last sync: {stats['last_sync'] or 'never'}\n\n"
                     f"Use 'Update {SOURCE_NAME}' above to refresh, or look this "
                     f"game up at:\n  "
                     f"{opt_profiles.rog_ally_life_search_url(entry.name)}")
        else:
            for path in sorted({c.file_path for c in plan.applicable()}):
                run = opt_tx.dry_run(plan, path)
                text += f"\n\n--- exact changes to {path} ---\n"
                text += run.diff if run.ok else "  " + "; ".join(run.errors)
        self._set_text(self.settings_text, text)
        self.apply_button.configure(state="normal" if plan.applicable() else "disabled")

    def _on_apply_settings(self) -> None:
        plan = self.current_plan
        selected = self._selected_entries()
        if plan is None or not selected:
            return
        entry = selected[0]
        applicable = plan.applicable()
        if not applicable:
            messagebox.showinfo("Nothing to apply", "There are no SAFE changes to make.")
            return
        listing = "\n".join(f"  • {c.describe()}" for c in applicable)
        if not messagebox.askyesno(
                "Apply safe settings",
                f"Apply these {len(applicable)} change(s) to '{entry.name}'?\n\n"
                f"{listing}\n\nA verified backup is taken first and can be restored "
                f"at any time."):
            return
        for change in applicable:
            change.approved = True
        for path in sorted({c.file_path for c in applicable}):
            try:
                record = opt_tx.apply_plan(entry, plan, path, applicable)
                self._log(f"Applied {', '.join(record.applied)} to {path}")
                self._log(f"Backup: {record.backup['backup_path']}")
                messagebox.showinfo("Applied",
                                    f"Applied {len(record.applied)} setting(s).\n\n"
                                    f"Backup:\n{record.backup['backup_path']}")
            except opt_tx.CriticalRestoreError as exc:
                messagebox.showerror("Critical", str(exc))
                return
            except opt_tx.TransactionError as exc:
                self._log(f"Not applied: {exc}")
                messagebox.showwarning("Not applied", str(exc))
        self._on_review_settings()

    def _on_restore_backup(self) -> None:
        backups = opt_tx.list_backups()
        if not backups:
            messagebox.showinfo("No backups", "No configuration backups have been taken.")
            return
        selected = self._selected_entries()
        if selected:
            name = selected[0].name.lower()
            backups = [b for b in backups if name in b.game_name.lower()] or backups
        backup = backups[0]
        if not messagebox.askyesno(
                "Restore backup",
                f"Restore this file from the backup taken {backup.created_at}?\n\n"
                f"{backup.original_path}"):
            return
        try:
            opt_tx.restore_game_settings(backup)
        except opt_tx.CriticalRestoreError as exc:
            messagebox.showerror("Critical", str(exc))
            return
        self._log(f"Restored {backup.original_path}")
        messagebox.showinfo("Restored", f"Restored:\n{backup.original_path}")

    def _open_rog_ally_life(self) -> None:
        """Open the exact page a recommendation came from, when one is loaded."""
        url = getattr(self, "_source_url", "")
        if not url:
            selected = self._selected_entries()
            url = (opt_profiles.rog_ally_life_search_url(selected[0].name)
                   if selected else SOURCE_BASE)
        webbrowser.open(url)

    # -- dialogs -----------------------------------------------------------

    def _show_text_window(self, title: str, content: str,
                          *, width: str = "900x680") -> None:
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry(width)
        text = tk.Text(window, wrap="word", font=MONO_FONT, padx=16, pady=16)
        text.pack(fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")
        bar = ttk.Frame(window, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Save as\u2026",
                   command=lambda: self._save_report(content)).pack(side="right")
        ttk.Button(bar, text="Close",
                   command=window.destroy).pack(side="right", padx=6)

    def _show_doctor(self) -> None:
        self._set_status("Running diagnostics\u2026")

        def work() -> None:
            try:
                report = doctor_mod.run_doctor(library_path=self._library_path())
                self.ui_queue.put(("doctor", report))
            except Exception as exc:
                self.ui_queue.put(("log", f"Diagnostics failed: {exc}"))
                self.ui_queue.put(("done", "Diagnostics failed."))

        self._start_worker(work, "Running diagnostics\u2026")

    def _on_doctor(self, report) -> None:
        self._worker_finished()
        self._set_status("Diagnostics finished.")
        window = tk.Toplevel(self.root)
        window.title("Diagnostics")
        window.geometry("900x720")
        text = tk.Text(window, wrap="word", font=MONO_FONT, padx=16, pady=16)
        text.pack(fill="both", expand=True)
        text.insert("1.0", report.describe())
        text.configure(state="disabled")
        bar = ttk.Frame(window, padding=8)
        bar.pack(fill="x")

        def repair() -> None:
            done = doctor_mod.apply_fixes(report)
            messagebox.showinfo(
                "Repairs",
                ("\n".join(done) if done else "Nothing needed repairing.")
                + "\n\nCredentials, DRM, anti-cheat and account state are never "
                  "changed automatically.")
            window.destroy()

        if report.repairable:
            ttk.Button(bar, text=f"Repair {len(report.repairable)} item(s)",
                       style="Accent.TButton", command=repair).pack(side="left")
        ttk.Button(bar, text="Close", command=window.destroy).pack(side="right")

    def _show_launchers(self) -> None:
        lines = [launchers.capability_matrix(), "",
                 "FULL = reliable   PARTIAL = works when a precondition holds",
                 "NONE = cannot     UNKNOWN = not established", ""]
        if self.environment.windows:
            lines.append("Installed on this machine:")
            for key in ("steam", "xbox", "ea", "epic", "ubisoft", "gog", "battlenet"):
                adapter = launchers.ADAPTERS[key]
                present = adapter.launcher_installed()
                lines.append(f"  {adapter.display_name:<26}"
                             f"{ {True: 'yes', False: 'no', None: 'unknown'}[present]}")
        else:
            lines.append("Launcher detection needs Windows.")
        self._show_text_window("Launchers", "\n".join(lines), width="760x560")

    def _show_trip_report(self) -> None:
        identities = self._visible_identities()
        reports = [self._report_for(i) for i in identities]
        counts = preparation.summarise(reports)
        lines = ["TRAVELREADY TRIP REPORT", "=" * 58, ""]
        lines.append(f"Games: {counts['total']}")
        lines.append("")
        for verdict in preparation.READINESS_ORDER:
            if counts.get(verdict):
                lines.append(f"  {verdict.replace('_', ' '):<24}{counts[verdict]:>4}")
        for verdict in preparation.READINESS_ORDER:
            rows = [r for r in reports if r.readiness == verdict]
            if not rows:
                continue
            lines += ["", verdict.replace("_", " "), "-" * 58]
            for report in sorted(rows, key=lambda r: r.game.lower()):
                lines.append(f"  {report.game} [{report.launcher}]")
                for check in report.failures + report.warnings:
                    lines.append(f"      {check.mark} {check.label}: {check.reason}")
        lines += ["", "Settings and launch readiness are separate: a game with no",
                  "published profile is still ready to travel."]
        self._show_text_window("Trip report", "\n".join(lines))

    def _save_report(self, content: str) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            filetypes=[("Text", "*.txt")])
        if path:
            Path(path).write_text(content, encoding="utf-8")
            self._log(f"Saved trip report to {path}")

    def _show_history(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Test history")
        window.geometry("900x560")
        columns = ("when", "status", "outcome")
        tree = ttk.Treeview(window, columns=columns, show="tree headings")
        tree.heading("#0", text="Game")
        for name, title in zip(columns, ("When", "Status", "Outcome")):
            tree.heading(name, text=title)
        tree.column("#0", width=300)
        tree.pack(fill="both", expand=True)
        for row in history.recent(data_file("history.json"), 300):
            tree.insert("", "end", text=row.get("name", ""),
                        values=(row.get("finished_at", ""), row.get("status", ""),
                                row.get("outcome", "")))

    def _open_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Settings")
        window.geometry("560x420")
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill="both", expand=True)

        stale = tk.IntVar(value=self.settings.stale_days)
        smoke = tk.DoubleVar(value=self.settings.smoke_duration)
        reverify = tk.BooleanVar(value=self.settings.reverify_ready)
        cleanup = tk.BooleanVar(value=self.settings.cleanup_after_test)
        confirm = tk.BooleanVar(value=self.settings.confirm_manual)

        ttk.Label(frame, text="A pass goes stale after (days)").grid(row=0, column=0,
                                                                    sticky="w", pady=8)
        ttk.Spinbox(frame, from_=1, to=90, textvariable=stale, width=8).grid(row=0, column=1)
        ttk.Label(frame, text="Smoke test duration (seconds)").grid(row=1, column=0,
                                                                   sticky="w", pady=8)
        ttk.Spinbox(frame, values=lt.SMOKE_PRESETS, textvariable=smoke,
                    width=8).grid(row=1, column=1)
        ttk.Checkbutton(frame, text="Re-verify games that are already READY",
                        variable=reverify).grid(row=2, column=0, columnspan=2,
                                                sticky="w", pady=8)
        ttk.Checkbutton(frame, text="Close games automatically after testing",
                        variable=cleanup).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(frame, text="Ask me to confirm games that cannot be detected",
                        variable=confirm).grid(row=4, column=0, columnspan=2,
                                               sticky="w", pady=8)

        def save() -> None:
            self.settings.stale_days = int(stale.get())
            self.settings.smoke_duration = float(smoke.get())
            self.settings.reverify_ready = bool(reverify.get())
            self.settings.cleanup_after_test = bool(cleanup.get())
            self.settings.confirm_manual = bool(confirm.get())
            self.settings.save()
            self._refresh()
            window.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, pady=20, sticky="e")
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="right", padx=6)
        ttk.Button(buttons, text="Save", style="Accent.TButton",
                   command=save).pack(side="right")

        io_frame = ttk.Frame(frame)
        io_frame.grid(row=5, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Button(io_frame, text="Import games…",
                   command=self._on_import).pack(side="left", padx=4)
        ttk.Button(io_frame, text="Export library…",
                   command=self._on_export).pack(side="left", padx=4)

    def _on_import(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Library", "*.json"), ("CSV", "*.csv"), ("Text", "*.txt")])
        if not path:
            return
        try:
            imported, message = import_games(Path(path))
        except (ValueError, OSError) as exc:
            messagebox.showerror("Import failed", str(exc))
            return
        self.entries, added, updated = merge_library_updates(self.entries, imported)
        self._save_library()
        self._refresh()
        self._log(f"{message} {added} new, {updated} updated.")

    def _on_export(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Library", "*.json"), ("CSV", "*.csv")])
        if path:
            self._log(export_games(self.entries, Path(path)))


def launch_gui() -> int:
    root = tk.Tk()
    try:                                        # crisp text on a scaled Windows display
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    TravelReadyGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch_gui())
