"""cli.py — command-line interface to TravelReady.

Every capability the GUI offers is available here, which keeps the engine
testable and makes the tool usable over a remote session on the handheld.

Commands
--------
``scan``              discover installed games and update the library
``list``              show the library, optionally filtered by launcher
``test``              launch and verify games
``prepare``           Prepare-for-Travel over a launcher tab or the whole library
``status``            readiness dashboard
``report``            the trip report
``history``           recent test results
``settings show``     read-only: current vs recommended, with safety classes
``settings dry-run``  print the exact diff a write would produce
``settings apply``    apply approved SAFE changes (asks first unless --yes)
``settings restore``  restore a configuration backup
``profile-template``  emit a blank ROG Ally Life profile to fill in
``profile-import``    validate and install a profile
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, discovery, history, launch_tester as lt, readiness
from .apppaths import data_file
from .library import (
    GameEntry, LIBRARY_FILE, SCAN_FOLDERS_FILE, LAUNCHERS, export_games,
    import_games, load_library, load_scan_folders, merge_library_updates,
    save_library,
)
from .optimiser import diff as opt_diff
from .optimiser import profiles as opt_profiles
from .optimiser import transaction as opt_tx
from .optimiser.model import TARGET_DEVICE


def _library_path(args) -> Path:
    return Path(args.library) if getattr(args, "library", None) else data_file(LIBRARY_FILE)


def _history_path(args) -> Path:
    return Path(args.history) if getattr(args, "history", None) else data_file("history.json")


def _load(args) -> List[GameEntry]:
    entries, message = load_library(_library_path(args))
    if not getattr(args, "quiet", False):
        print(message, file=sys.stderr)
    return entries


def _filtered(entries: Sequence[GameEntry], args) -> List[GameEntry]:
    out = list(entries)
    if getattr(args, "launcher", None):
        out = [e for e in out if e.launcher == args.launcher]
    if getattr(args, "name", None):
        needle = args.name.strip().lower()
        out = [e for e in out if needle in e.name.lower()]
    return out


def _progress(message: str) -> None:
    print(f"  {message}", file=sys.stderr)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_scan(args) -> int:
    folders = load_scan_folders(data_file(SCAN_FOLDERS_FILE))
    print("Scanning for installed games…", file=sys.stderr)
    found = discovery.auto_scan(sources=args.sources or None,
                                progress=_progress if args.verbose else None,
                                custom_folders=folders)
    existing = _load(args)
    merged, added, updated = merge_library_updates(existing, found)
    save_library(merged, _library_path(args))
    print(f"Discovered {len(found)} games: {added} new, {updated} updated, "
          f"{len(merged)} in library.")
    return 0


def cmd_list(args) -> int:
    entries = _filtered(_load(args), args)
    if args.json:
        print(json.dumps([e.to_dict() for e in entries], indent=2))
        return 0
    counts = readiness.tab_counts(entries)
    print(f"{'GAME':<44} {'LAUNCHER':<10} {'STATE':<10} {'VERIFY':<8} LAST")
    print("-" * 92)
    for entry in sorted(entries, key=lambda e: (e.launcher, e.name.lower())):
        state = readiness.state_of(entry)
        print(f"{entry.name[:43]:<44} {entry.launcher:<10} {state:<10} "
              f"{entry.verification:<8} {readiness.age_text(entry)}")
    print("-" * 92)
    print("  ".join(f"{tab}:{counts[tab]}" for tab in readiness.TAB_ORDER if counts[tab]))
    return 0


def cmd_status(args) -> int:
    entries = _filtered(_load(args), args)
    summary = readiness.summarize(entries, args.stale_days)
    print(f"Library: {summary['total']} games")
    for state in readiness.STATES:
        if summary.get(state):
            print(f"  {state:<10} {summary[state]}")
    blind = [e for e in entries if not lt.build_detection_plan(e).has_any_signal]
    if blind:
        print(f"\n{len(blind)} game(s) cannot be verified automatically:")
        for entry in blind[:20]:
            print(f"  - {entry.name} ({entry.launcher})")
    print("\nTRAVEL READY" if readiness.is_travel_ready(entries, args.stale_days)
          else "\nNOT READY — some games still need attention.")
    return 0


def _run_tests(entries: Sequence[GameEntry], args, mode: str) -> int:
    results = []
    for index, entry in enumerate(entries, 1):
        print(f"[{index}/{len(entries)}] {entry.name}", file=sys.stderr)
        result = lt.run_test(
            entry, mode=mode,
            on_progress=_progress if args.verbose else None,
            cleanup=args.cleanup,
            smoke_duration=getattr(args, "smoke_duration", None),
            manual_confirm=_ask_manual if not args.no_prompt else None,
        )
        results.append(result)
        entry.last_result = result.status
        if result.status == lt.STATUS_PASS:
            entry.last_ready = result.finished_at
        if result.corrected_process and not entry.expected_process:
            entry.expected_process = result.corrected_process
        marker = "PASS" if result.status == lt.STATUS_PASS else result.status
        print(f"    {marker}  {result.outcome}")
        if result.failure_hint:
            print(f"    hint: {result.failure_hint}")
    history.append_results(results, _history_path(args))
    entries_all, _ = load_library(_library_path(args))
    by_id = {e.id: e for e in entries}
    save_library([by_id.get(e.id, e) for e in entries_all], _library_path(args))
    summary = lt.summarize(results)
    print("\n" + "  ".join(f"{k}:{v}" for k, v in summary.items() if v))
    return 0 if summary.get(lt.STATUS_PASS) == summary.get("total") else 1


def _ask_manual(entry: GameEntry) -> bool:
    answer = input(f"    Did '{entry.name}' start correctly? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def cmd_test(args) -> int:
    entries = _filtered(_load(args), args)
    if not entries:
        print("No games matched.", file=sys.stderr)
        return 2
    return _run_tests(entries, args, args.mode)


def cmd_prepare(args) -> int:
    entries = _filtered(_load(args), args)
    targets, skipped = readiness.prepare_targets(
        entries, args.stale_days, reverify_ready=args.reverify)
    if skipped:
        print(f"Skipped {len(skipped)} game(s) that cannot be closed safely:",
              file=sys.stderr)
        for entry in skipped:
            print(f"  - {entry.name}: {readiness.failure_hint(entry)}", file=sys.stderr)
    if not targets:
        print("Nothing to prepare — everything is already READY.")
        return 0
    print(f"Preparing {len(targets)} game(s) with a "
          f"{args.smoke_duration or lt.SMOKE_DEFAULT_DURATION:.0f}s smoke test.",
          file=sys.stderr)
    args.cleanup = True
    return _run_tests(targets, args, lt.MODE_SMOKE)


def cmd_report(args) -> int:
    entries = _filtered(_load(args), args)
    summary = readiness.summarize(entries, args.stale_days)
    print("=" * 60)
    print("TRAVELREADY TRIP REPORT")
    print("=" * 60)
    for state in readiness.STATES:
        rows = [e for e in entries if readiness.state_of(e, args.stale_days) == state]
        if not rows:
            continue
        print(f"\n{state} ({len(rows)})")
        print("-" * 60)
        for entry in sorted(rows, key=lambda e: e.name.lower()):
            line = f"  {entry.name} [{entry.launcher}]"
            hint = readiness.failure_hint(entry)
            print(f"{line}\n      {hint}" if hint else line)
    print("\n" + ("TRAVEL READY" if readiness.is_travel_ready(entries, args.stale_days)
                  else "NOT READY"))
    return 0


def cmd_history(args) -> int:
    rows = (history.for_game(_history_path(args), args.name, args.limit)
            if args.name else history.recent(_history_path(args), args.limit))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        print(f"{row.get('finished_at', ''):<26} {row.get('status', ''):<16} "
              f"{row.get('name', '')}")
    return 0


def cmd_export(args) -> int:
    print(export_games(_filtered(_load(args), args), Path(args.path)))
    return 0


def cmd_import(args) -> int:
    imported, message = import_games(Path(args.path))
    existing = _load(args)
    merged, added, updated = merge_library_updates(existing, imported)
    save_library(merged, _library_path(args))
    print(f"{message} {added} new, {updated} updated.")
    return 0


# -- settings ---------------------------------------------------------------

def _entry_for(args) -> Optional[GameEntry]:
    entries = _load(args)
    needle = args.game.strip().lower()
    exact = [e for e in entries if e.name.strip().lower() == needle]
    partial = exact or [e for e in entries if needle in e.name.lower()]
    if not partial:
        print(f"No game matching '{args.game}'.", file=sys.stderr)
        return None
    if len(partial) > 1 and not exact:
        print(f"'{args.game}' matched {len(partial)} games:", file=sys.stderr)
        for entry in partial[:10]:
            print(f"  - {entry.name}", file=sys.stderr)
        return None
    return partial[0]


def cmd_settings_show(args) -> int:
    entry = _entry_for(args)
    if entry is None:
        return 2
    store = opt_profiles.ProfileStore.load()
    if store.errors:
        for error in store.errors:
            print(f"! profile error: {error}", file=sys.stderr)
    plan = opt_diff.plan_for_game(entry, store)
    print(opt_diff.render_plan(plan))
    if plan.profile is None:
        print(f"\nLook it up at: {opt_profiles.rog_ally_life_search_url(entry.name)}")
        print(f"Installed profiles: {len(store)}")
    return 0


def cmd_settings_dry_run(args) -> int:
    entry = _entry_for(args)
    if entry is None:
        return 2
    plan = opt_diff.plan_for_game(entry)
    if not plan.applicable():
        print("No SAFE changes to make.")
        return 0
    for path in sorted({c.file_path for c in plan.applicable()}):
        run = opt_tx.dry_run(plan, path)
        print(f"--- {path}")
        print(run.diff if run.ok else "  " + "; ".join(run.errors))
    return 0


def cmd_settings_apply(args) -> int:
    entry = _entry_for(args)
    if entry is None:
        return 2
    plan = opt_diff.plan_for_game(entry)
    applicable = plan.applicable()
    if not applicable:
        print("No SAFE changes to apply.")
        print(opt_diff.render_plan(plan))
        return 0

    print(opt_diff.render_plan(plan))
    for path in sorted({c.file_path for c in applicable}):
        run = opt_tx.dry_run(plan, path)
        print(f"\n--- exact changes to {path} ---")
        print(run.diff if run.ok else "; ".join(run.errors))

    if not args.yes:
        answer = input("\nApply these SAFE changes? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing was changed.")
            return 0
    for change in applicable:
        change.approved = True

    exit_code = 0
    for path in sorted({c.file_path for c in applicable}):
        try:
            record = opt_tx.apply_plan(entry, plan, path, applicable)
            print(f"Applied {', '.join(record.applied)} to {path}")
            print(f"Backup: {record.backup['backup_path']}")
        except opt_tx.TransactionError as exc:
            print(f"Not applied: {exc}", file=sys.stderr)
            exit_code = 1
        except opt_tx.CriticalRestoreError as exc:
            print(f"CRITICAL: {exc}", file=sys.stderr)
            return 3
    return exit_code


def cmd_settings_restore(args) -> int:
    backups = opt_tx.list_backups()
    if not backups:
        print("No backups found.")
        return 2
    if args.list or not args.game:
        for index, backup in enumerate(backups, 1):
            print(f"{index:>3}. {backup.created_at}  {backup.game_name}  "
                  f"{backup.original_path}")
        return 0
    needle = args.game.strip().lower()
    matches = [b for b in backups if needle in b.game_name.lower()]
    if not matches:
        print(f"No backup for '{args.game}'.", file=sys.stderr)
        return 2
    backup = matches[0]
    if not args.yes:
        answer = input(f"Restore {backup.original_path} from "
                       f"{backup.created_at}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return 0
    opt_tx.restore_game_settings(backup)
    print(f"Restored {backup.original_path}")
    return 0


def cmd_profile_template(args) -> int:
    print(json.dumps(opt_profiles.blank_profile_template(args.game, args.device), indent=2))
    print(f"\n# Fill in from {opt_profiles.rog_ally_life_search_url(args.game)}",
          file=sys.stderr)
    return 0


def cmd_profile_import(args) -> int:
    try:
        path = opt_profiles.import_profile_file(Path(args.path))
    except opt_profiles.ProfileError as exc:
        print(f"Profile rejected: {exc}", file=sys.stderr)
        return 1
    print(f"Installed {path}")
    return 0


def cmd_profile_list(args) -> int:
    store = opt_profiles.ProfileStore.load()
    for error in store.errors:
        print(f"! {error}", file=sys.stderr)
    if not store.profiles:
        print("No ROG Ally Life profiles are installed.")
        print("Add one with: travelready profile-template \"<Game>\" > game.json")
        return 0
    for profile in store.profiles:
        print(f"{profile.game_name} [{profile.device}] — {profile.attribution}")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="travelready",
        description="Prepare a Windows gaming handheld for offline travel.")
    parser.add_argument("--version", action="version", version=f"TravelReady {__version__}")
    parser.add_argument("--library", help="path to games.json")
    parser.add_argument("--history", help="path to history.json")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_filters(p):
        p.add_argument("--launcher", choices=LAUNCHERS)
        p.add_argument("--name", help="substring match on the game name")

    p = sub.add_parser("scan", help="discover installed games")
    p.add_argument("--sources", nargs="*", choices=list(discovery.AUTO_SCAN_SOURCES))
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("list", help="show the library")
    add_filters(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="readiness dashboard")
    add_filters(p)
    p.add_argument("--stale-days", type=int, default=readiness.DEFAULT_STALE_DAYS)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("test", help="launch and verify games")
    add_filters(p)
    p.add_argument("--mode", choices=lt.ALL_MODES, default=lt.MODE_STANDARD)
    p.add_argument("--cleanup", action="store_true", help="close the game afterwards")
    p.add_argument("--smoke-duration", type=float)
    p.add_argument("--no-prompt", action="store_true",
                   help="never ask for manual confirmation")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("prepare", help="Prepare for Travel")
    add_filters(p)
    p.add_argument("--stale-days", type=int, default=readiness.DEFAULT_STALE_DAYS)
    p.add_argument("--smoke-duration", type=float, default=lt.SMOKE_DEFAULT_DURATION)
    p.add_argument("--reverify", action="store_true", help="re-test READY games too")
    p.add_argument("--no-prompt", action="store_true")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("report", help="trip report")
    add_filters(p)
    p.add_argument("--stale-days", type=int, default=readiness.DEFAULT_STALE_DAYS)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("history", help="recent results")
    p.add_argument("--name")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("export", help="export the library")
    add_filters(p)
    p.add_argument("path")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="import games")
    p.add_argument("path")
    p.set_defaults(func=cmd_import)

    settings = sub.add_parser("settings", help="game settings optimisation")
    ssub = settings.add_subparsers(dest="settings_command", required=True)

    sp = ssub.add_parser("show", help="current vs recommended (read-only)")
    sp.add_argument("game")
    sp.set_defaults(func=cmd_settings_show)

    sp = ssub.add_parser("dry-run", help="print the exact diff a write would make")
    sp.add_argument("game")
    sp.set_defaults(func=cmd_settings_dry_run)

    sp = ssub.add_parser("apply", help="apply approved SAFE changes")
    sp.add_argument("game")
    sp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sp.set_defaults(func=cmd_settings_apply)

    sp = ssub.add_parser("restore", help="restore a configuration backup")
    sp.add_argument("game", nargs="?")
    sp.add_argument("--list", action="store_true")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_settings_restore)

    p = sub.add_parser("profile-template", help="blank ROG Ally Life profile")
    p.add_argument("game")
    p.add_argument("--device", default=TARGET_DEVICE)
    p.set_defaults(func=cmd_profile_template)

    p = sub.add_parser("profile-import", help="validate and install a profile")
    p.add_argument("path")
    p.set_defaults(func=cmd_profile_import)

    p = sub.add_parser("profile-list", help="installed profiles")
    p.set_defaults(func=cmd_profile_list)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
