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
import queue
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, discovery, history, launch_tester as lt, readiness
from .apppaths import data_file
from .library import (
    LIBRARY_FILE, SCAN_FOLDERS_FILE, GameEntry, export_games, import_games,
    infrastructure_entries, load_library, load_scan_folders, merge_library_updates,
    save_library,
)
from .optimiser import diff as opt_diff
from .optimiser import profiles as opt_profiles
from .optimiser import transaction as opt_tx

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
        self.entries: List[GameEntry] = []
        self.current_tab = "All"
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.iid_to_entry: Dict[str, GameEntry] = {}
        self.profile_store = opt_profiles.ProfileStore.load()
        self.current_plan = None

        root.title(f"TravelReady {__version__}")
        root.geometry("1280x800")
        root.minsize(900, 600)
        self._build_style()
        self._build_widgets()
        self._load_library()
        self.root.after(120, self._poll_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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

    def _build_widgets(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill="x")
        self.header_label = ttk.Label(header, text="TravelReady", font=HEAD_FONT)
        self.header_label.pack(side="left")
        self.dashboard_label = ttk.Label(header, text="", font=SUB_FONT)
        self.dashboard_label.pack(side="right")

        toolbar = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        toolbar.pack(fill="x")
        self.scan_button = ttk.Button(toolbar, text="Re-scan", command=self._on_scan)
        self.scan_button.pack(side="left", padx=4)
        self.test_button = ttk.Button(toolbar, text="Test selected",
                                      command=self._on_test_selected)
        self.test_button.pack(side="left", padx=4)
        self.prepare_button = ttk.Button(toolbar, text="Prepare for Travel",
                                         style="Accent.TButton",
                                         command=self._on_prepare_travel)
        self.prepare_button.pack(side="left", padx=4)
        self.cancel_button = ttk.Button(toolbar, text="Stop", command=self._on_cancel,
                                        state="disabled")
        self.cancel_button.pack(side="left", padx=4)
        ttk.Button(toolbar, text="Trip report",
                   command=self._show_trip_report).pack(side="right", padx=4)
        ttk.Button(toolbar, text="History",
                   command=self._show_history).pack(side="right", padx=4)
        ttk.Button(toolbar, text="Settings",
                   command=self._open_settings).pack(side="right", padx=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="x", padx=12)
        self.tab_frames: Dict[str, ttk.Frame] = {}
        for tab in readiness.TAB_ORDER:
            frame = ttk.Frame(self.notebook)
            self.tab_frames[tab] = frame
            self.notebook.add(frame, text=tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12, pady=8)

        left = ttk.Frame(body)
        body.add(left, weight=3)
        columns = ("launcher", "state", "verify", "last")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="Game")
        self.tree.column("#0", width=380, minwidth=220)
        for name, title, width in (("launcher", "Launcher", 110),
                                   ("state", "Readiness", 120),
                                   ("verify", "Verified by", 120),
                                   ("last", "Last checked", 130)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor="w")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

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

        diag = ttk.Frame(right, padding=8)
        right.add(diag, text="Diagnostics")
        self.diag_text = tk.Text(diag, wrap="word", font=MONO_FONT, height=12,
                                 relief="flat", background="#1e1e1e", foreground="#e6e6e6")
        self.diag_text.pack(fill="both", expand=True)

        status = ttk.Frame(self.root, padding=(12, 6))
        status.pack(fill="x")
        self.status_label = ttk.Label(status, text="Ready.", font=SUB_FONT)
        self.status_label.pack(side="left")
        self.progress = ttk.Progressbar(status, mode="determinate", length=260)
        self.progress.pack(side="right")

    def _build_optimiser_panel(self, parent: ttk.Frame) -> None:
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
        ttk.Button(bar, text="ROG Ally Life",
                   command=self._open_rog_ally_life).pack(side="right", padx=3)
        self.settings_text = tk.Text(parent, wrap="word", font=MONO_FONT,
                                     relief="flat", background="#f7f7f8")
        self.settings_text.pack(fill="both", expand=True)
        self._set_text(self.settings_text,
                       "Select a game, then choose 'Review changes'.\n\n"
                       "Nothing is ever changed without showing you the exact diff "
                       "and asking first.")

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
        return self.worker is not None and self.worker.is_alive()

    # -- library -----------------------------------------------------------

    def _load_library(self) -> None:
        self.entries, message = load_library(self._library_path())
        self._log(message)
        self._set_status(message)
        self._refresh()
        junk = infrastructure_entries(self.entries)
        if junk:
            self._log(f"{len(junk)} library entries look like launcher software rather "
                      f"than games (e.g. {junk[0].name}). Use Settings to review them.")

    def _save_library(self) -> None:
        save_library(self.entries, self._library_path())

    def _visible_entries(self) -> List[GameEntry]:
        if self.current_tab == "All":
            return list(self.entries)
        return [e for e in self.entries
                if readiness.tab_for_launcher(e.launcher) == self.current_tab]

    def _selected_entries(self) -> List[GameEntry]:
        return [self.iid_to_entry[iid] for iid in self.tree.selection()
                if iid in self.iid_to_entry]

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.iid_to_entry.clear()
        for entry in sorted(self._visible_entries(), key=lambda e: e.name.lower()):
            state = readiness.state_of(entry, self.settings.stale_days)
            verified = entry.verification
            if entry.verification == "manual":
                verified = "manual"
            elif not lt.build_detection_plan(entry).has_any_signal:
                verified = "not possible"
            iid = self.tree.insert("", "end", text=entry.name,
                                   values=(entry.launcher, state, verified,
                                           readiness.age_text(entry)))
            self.iid_to_entry[iid] = entry
        self._update_tab_labels()
        self._update_dashboard()

    def _update_tab_labels(self) -> None:
        counts = readiness.tab_counts(self.entries)
        for index, tab in enumerate(readiness.TAB_ORDER):
            label = f"{tab} ({counts[tab]})" if counts[tab] else tab
            self.notebook.tab(index, text=label)

    def _update_dashboard(self) -> None:
        visible = self._visible_entries()
        summary = readiness.summarize(visible, self.settings.stale_days)
        parts = [f"{state} {summary[state]}" for state in readiness.STATES
                 if summary.get(state)]
        verdict = ("TRAVEL READY" if readiness.is_travel_ready(visible, self.settings.stale_days)
                   else "Needs attention")
        self.dashboard_label.configure(text=f"{'   '.join(parts)}      {verdict}")

    # -- events ------------------------------------------------------------

    def _on_tab_changed(self, _event=None) -> None:
        index = self.notebook.index(self.notebook.select())
        self.current_tab = readiness.TAB_ORDER[index]
        self._refresh()

    def _on_select(self, _event=None) -> None:
        selected = self._selected_entries()
        if not selected:
            return
        entry = selected[0]
        plan = lt.build_detection_plan(entry)
        lines = [
            entry.name, "",
            f"Launcher          {entry.launcher}",
            f"Launch method     {entry.launch_method or '(inferred)'}",
            f"Launch target     {entry.display_target or '(none)'}",
            f"Expected process  {', '.join(entry.process_names()) or '(unknown)'}",
            f"Install folder    {entry.install_dir or '(unknown)'}",
            f"Readiness         {readiness.state_of(entry, self.settings.stale_days)}",
            f"Last result       {entry.last_result or 'never tested'}",
            f"Last ready        {readiness.age_text(entry)}",
            "",
        ]
        if plan.has_any_signal:
            signals = []
            if plan.process_names:
                signals.append("process name")
            if plan.install_dir:
                signals.append("install folder")
            if plan.track_launched_pid:
                signals.append("launched process")
            lines.append(f"Can be verified automatically using: {', '.join(signals)}.")
        else:
            lines.append("This game cannot be verified automatically. TravelReady will "
                         "still launch it, then ask you to confirm it started.")
        hint = readiness.failure_hint(entry)
        if hint:
            lines += ["", hint]
        if entry.notes:
            lines += ["", entry.notes]
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
        if self._busy:
            messagebox.showinfo("Busy", "A background task is already running.")
            return
        self.cancel_event.clear()
        self.scan_button.configure(state="disabled")
        self.test_button.configure(state="disabled")
        self.prepare_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self._set_status(label)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _worker_finished(self) -> None:
        self.scan_button.configure(state="normal")
        self.test_button.configure(state="normal")
        self.prepare_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.progress.configure(value=0)

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "progress":
                    done, total = payload
                    self.progress.configure(maximum=max(total, 1), value=done)
                elif kind == "scanned":
                    self._on_scanned(payload)
                elif kind == "tested":
                    self._on_tested(payload)
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
        self.entries, added, updated = merge_library_updates(self.entries, found)
        self._save_library()
        self._refresh()
        self._worker_finished()
        message = (f"Scan complete: {len(found)} found, {added} new, "
                   f"{updated} updated, {len(self.entries)} in library.")
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

    def _on_prepare_travel(self) -> None:
        pool = self._selected_entries() or self._visible_entries()
        targets, skipped = readiness.prepare_targets(
            pool, self.settings.stale_days, self.settings.reverify_ready)
        if skipped:
            names = "\n".join(f"  • {e.name}" for e in skipped[:12])
            self._log(f"{len(skipped)} game(s) cannot be closed safely and were "
                      f"skipped:\n{names}")
        if not targets:
            messagebox.showinfo(
                "All ready",
                "Every game in this tab is already READY.\n\n"
                "Enable 'Re-verify READY games' in Settings to test them again.")
            return
        if not messagebox.askyesno(
                "Prepare for Travel",
                f"TravelReady will launch {len(targets)} game(s) one at a time, "
                f"confirm each one starts, then close it.\n\nContinue?"):
            return
        self._run_tests(targets, lt.MODE_SMOKE, cleanup=True)

    # -- settings optimiser ------------------------------------------------

    def _on_review_settings(self) -> None:
        selected = self._selected_entries()
        if not selected:
            messagebox.showinfo("Select a game", "Choose a game first.")
            return
        entry = selected[0]
        self.profile_store = opt_profiles.ProfileStore.load()
        plan = opt_diff.plan_for_game(entry, self.profile_store)
        self.current_plan = plan
        text = opt_diff.render_plan(plan)
        if plan.profile is None:
            text += (f"\n\nInstalled ROG Ally Life profiles: {len(self.profile_store)}\n"
                     f"Look this game up at:\n  "
                     f"{opt_profiles.rog_ally_life_search_url(entry.name)}\n\n"
                     f"Use 'ROG Ally Life' above to open the site, then import a "
                     f"profile with the command line.")
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
        selected = self._selected_entries()
        url = (opt_profiles.rog_ally_life_search_url(selected[0].name) if selected
               else opt_profiles.ROG_ALLY_LIFE_BASE)
        webbrowser.open(url)

    # -- dialogs -----------------------------------------------------------

    def _show_trip_report(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Trip report")
        window.geometry("820x640")
        text = tk.Text(window, wrap="word", font=BASE_FONT, padx=16, pady=16)
        text.pack(fill="both", expand=True)
        visible = self._visible_entries()
        lines = ["TRAVELREADY TRIP REPORT", "=" * 52, ""]
        for state in readiness.STATES:
            rows = [e for e in visible
                    if readiness.state_of(e, self.settings.stale_days) == state]
            if not rows:
                continue
            lines.append(f"{state} ({len(rows)})")
            lines.append("-" * 52)
            for entry in sorted(rows, key=lambda e: e.name.lower()):
                lines.append(f"  {entry.name} [{entry.launcher}]")
                hint = readiness.failure_hint(entry)
                if hint:
                    lines.append(f"      {hint}")
            lines.append("")
        lines.append("TRAVEL READY" if readiness.is_travel_ready(
            visible, self.settings.stale_days) else "NOT READY")
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        ttk.Button(window, text="Save as…",
                   command=lambda: self._save_report("\n".join(lines))).pack(pady=8)

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
