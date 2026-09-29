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

from . import (
    __version__, classification, discovery, doctor as doctor_mod, environment,
    history, identity as identity_mod, launch_tester as lt, launchers, preparation,
    prepare_run, readiness,
)
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
from .optimiser.rogallylife import SOURCE_BASE, SOURCE_NAME
from .optimiser.rogallylife import bridge as ral_bridge
from .optimiser.rogallylife import capability as ral_capability
from .optimiser.rogallylife import sync as ral_sync
from .optimiser.rogallylife.cache import ProfileCache
from .optimiser.rogallylife.client import FetchError, RogAllyLifeClient
from .optimiser.rogallylife import coverage as ral_coverage
from .optimiser.rogallylife.select import MODE_BALANCED, OPERATING_MODES


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
    if args.explain:
        entries = _load(args)
        print(classification.classification_report(entries))
        return 0
    folders = load_scan_folders(data_file(SCAN_FOLDERS_FILE))
    print("Scanning for installed games…", file=sys.stderr)
    found = discovery.auto_scan(sources=args.sources or None,
                                progress=_progress if args.verbose else None,
                                custom_folders=folders)
    existing = _load(args)
    merged, added, updated = merge_library_updates(existing, found)
    save_library(merged, _library_path(args))
    games, non_games = classification.split_games(merged)
    identities = identity_mod.build_identities(games)
    print(f"Discovered {len(found)} entries: {added} new, {updated} updated.")
    print(f"Library: {len(merged)} entries \u2192 {len(games)} games "
          f"({len(identities)} distinct), {len(non_games)} non-games excluded.")
    print("Run 'travelready scan --explain' to see why anything was excluded.")
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


def _identities(args, entries=None):
    """The library as game identities, merged and classified."""
    entries = entries if entries is not None else _filtered(_load(args), args)
    games, _non_games = classification.split_games(entries)
    return identity_mod.build_identities(games)


def _environment(args):
    return environment.current(check_network=not getattr(args, "offline", False))


def cmd_doctor(args) -> int:
    """Diagnose the application, the environment, the library and the source."""
    report = doctor_mod.run_doctor(
        library_path=_library_path(args),
        check_network=not args.offline)
    print(report.describe())
    if args.fix:
        repairs = doctor_mod.apply_fixes(report)
        print()
        if repairs:
            print("Repaired:")
            for line in repairs:
                print(f"  \u2713 {line}")
        else:
            print("Nothing to repair.")
        print("\nAnything involving credentials, DRM, anti-cheat or account "
              "state is never repaired automatically \u2014 follow the "
              "instructions above instead.")
    return 0 if report.healthy else 1


def cmd_ready(args) -> int:
    """Per-game travel readiness, with the reason for every verdict."""
    identities = _identities(args)
    if not identities:
        print("No games in the library. Run 'travelready scan' first.",
              file=sys.stderr)
        return 2
    env = _environment(args)
    resolver = _resolver(args)
    if args.game:
        found = identity_mod.find_identity(identities, args.game)
        if found is None:
            matches = [i for i in identities
                       if args.game.strip().lower() in i.canonical_title.lower()]
            if len(matches) != 1:
                print(f"'{args.game}' matched {len(matches)} games.", file=sys.stderr)
                for i in matches[:10]:
                    print(f"  - {i.canonical_title}", file=sys.stderr)
                return 2
            found = matches[0]
        print(preparation.assess(found, env=env, resolver=resolver).describe())
        return 0

    reports = preparation.assess_all(identities, env=env, resolver=resolver)
    counts = preparation.summarise(reports)
    print(f"{'GAME':<40}{'LAUNCHER':<11}{'READINESS':<22}SETTINGS")
    print("-" * 88)
    for report in sorted(reports, key=lambda r: (
            preparation.READINESS_ORDER.index(r.readiness), r.game.lower())):
        if args.only and report.readiness != args.only:
            continue
        settings = {"PASS": "profile", "WARN": "none", "UNKNOWN": "not checked",
                    "NOT_APPLICABLE": "-"}.get(report.settings_state, "-")
        print(f"{report.game[:39]:<40}{report.launcher:<11}"
              f"{report.readiness.replace('_', ' '):<22}{settings}")
    print("-" * 88)
    print("  ".join(f"{v.replace('_', ' ')}: {counts[v]}"
                    for v in preparation.READINESS_ORDER if counts.get(v)))
    return 0


def cmd_launchers(args) -> int:
    """What each launcher supports, and whether it is installed."""
    env = _environment(args)
    print(launchers.capability_matrix())
    print()
    print("FULL = reliable \u00b7 PARTIAL = works when a precondition holds \u00b7 "
          "NONE = cannot \u00b7 UNKNOWN = not established")
    if env.windows:
        print()
        print("Installed on this machine:")
        for key in ("steam", "xbox", "ea", "epic", "ubisoft", "gog", "battlenet"):
            adapter = launchers.ADAPTERS[key]
            present = adapter.launcher_installed()
            mark = {True: "yes", False: "no", None: "unknown"}[present]
            print(f"  {adapter.display_name:<24}{mark}")
    else:
        print("\nLauncher detection needs Windows.")
    return 0


def cmd_prepare(args) -> int:
    """The primary workflow: prepare the library for offline play."""
    identities = _identities(args)
    if not identities:
        print("No games in the library. Run 'travelready scan' first.",
              file=sys.stderr)
        return 2
    env = _environment(args)
    resolver = _resolver(args)
    options = prepare_run.PrepareOptions(
        launch=not args.no_launch,
        cleanup=not args.no_cleanup,
        smoke_duration=args.smoke_duration,
        reverify_ready=args.reverify,
        stale_days=args.stale_days,
        resume=args.resume,
    )
    if args.resume:
        existing = prepare_run.load_run()
        if existing is None or not existing.resumable:
            print("No interrupted run to resume; starting a fresh one.",
                  file=sys.stderr)
            options.resume = False

    if options.launch and not env.windows:
        print("Launching needs Windows. Assessing without launching instead.",
              file=sys.stderr)
        options.launch = False

    run = prepare_run.run_preparation(
        identities, options, env=env, resolver=resolver,
        on_progress=_progress if args.verbose else None)

    entries = _load(args)
    by_id = {e.id: e for i in identities for e in i.entries}
    save_library([by_id.get(e.id, e) for e in entries], _library_path(args))

    print()
    print(prepare_run.render_run_report(run, identities, resolver=resolver))
    return 0 if run.status == prepare_run.STATUS_COMPLETE else 1


def cmd_report(args) -> int:
    entries = _filtered(_load(args), args)
    summary = readiness.summarize(entries, args.stale_days)
    resolver = _resolver(args) if args.settings else None
    print("=" * 60)
    print("TRAVELREADY TRIP REPORT")
    print("=" * 60)
    if resolver is not None:
        print()
        print(f"{'Game':<34}{'Launcher':<11}{'Settings':<14}Launch")
        print("-" * 72)
        for entry in sorted(entries, key=lambda e: e.name.lower()):
            state = readiness.state_of(entry, args.stale_days)
            launch = "Ready" if state == readiness.STATE_READY else state.title()
            print(f"{entry.name[:33]:<34}{entry.launcher:<11}"
                  f"{readiness.settings_state_of(entry, resolver):<14}{launch}")
        print()
        print("Settings and launch readiness are separate: a game with no published")
        print("profile is still ready to travel.")
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


def _resolver(args) -> ral_bridge.SourceResolver:
    return ral_bridge.SourceResolver(
        ProfileCache(),
        target_device=TARGET_DEVICE,
        mode=getattr(args, "mode", None) or MODE_BALANCED,
        max_watts=getattr(args, "max_watts", None),
    )


def cmd_settings_update(args) -> int:
    """Refresh the local ROG Ally Life cache and report what changed."""
    cache = ProfileCache()
    client = RogAllyLifeClient(delay=args.delay)
    report = ral_sync.sync(
        client, cache,
        device_family=ral_bridge.family_for_device(TARGET_DEVICE),
        titles=args.game or None, force=args.force, limit=args.limit,
        progress=_progress if args.verbose else None,
    )
    print(report.describe())
    if report.blocked:
        print(f"\nThe cache still holds {len(cache.index.entries)} entr(ies) and works "
              f"offline.\nSource: {SOURCE_BASE}", file=sys.stderr)
        return 3
    return 0 if report.ok else 1


def cmd_settings_status(args) -> int:
    """Cache health, match coverage over the library, capability matrix."""
    cache = ProfileCache()
    stats = cache.stats()
    print(f"{SOURCE_NAME} cache")
    print(f"  location         {stats['root']}")
    print(f"  cached games     {stats['files']}")
    print(f"  profiles         {stats['profiles']}")
    print(f"  last sync        {stats['last_sync'] or 'never'}")
    print(f"  parser version   {stats['parser_version']}")
    stale = cache.needs_reparse(ral_sync.PARSER_VERSION)
    if stale:
        print(f"  needs re-parse   {len(stale)} (run: travelready settings update --force)")

    entries = _filtered(_load(args), args)
    if entries:
        resolver = _resolver(args)
        counts = {"matched": 0, "review": 0, "no_profile": 0, "no_profiles_published": 0}
        for resolution in resolver.resolve_all(entries):
            counts[resolution.status] = counts.get(resolution.status, 0) + 1
        print(f"\nLibrary coverage ({len(entries)} games)")
        print(f"  with a profile   {counts['matched']}")
        print(f"  needs review     {counts['review']}")
        print(f"  page but no data {counts['no_profiles_published']}")
        print(f"  no recommendation{counts['no_profile']:>4}")

    if args.capabilities:
        print()
        print(ral_capability.describe_matrix())
    return 0


def cmd_settings_coverage(args) -> int:
    """How much of the library ROG Ally Life covers."""
    entries = _filtered(_load(args), args)
    cache = ProfileCache()
    if cache.index.entries or args.cached_only:
        report = ral_coverage.build_report(entries, _resolver(args),
                                           mode=getattr(args, "mode", None))
    else:
        print("The ROG Ally Life cache is empty — matching against the observed "
              "post-title list instead.\nRun 'travelready settings update' to fetch "
              "the real recommendations.\n", file=sys.stderr)
        report = ral_coverage.title_only_report(entries)
    print(report.describe(detail=args.detail))
    return 0


def cmd_settings_search(args) -> int:
    """Search the cached source data by title."""
    resolver = _resolver(args)
    rows = resolver.search(args.term, limit=args.limit)
    if not rows:
        print(f"Nothing cached matching '{args.term}'.")
        print("Run 'travelready settings update' first, or the source has no page for it.")
        return 2
    for confidence, game in rows:
        marker = "auto " if confidence >= 0.90 else "review"
        print(f"  {confidence:.2f} {marker}  {game.title}")
        print(f"                {len(game.profiles)} profile(s): "
              f"{', '.join(game.profile_labels()) or '(none)'}")
        print(f"                {game.source_url}")
    return 0


def cmd_settings_source(args) -> int:
    """Show the raw source record and attribution for a game."""
    entry = _entry_for(args)
    if entry is None:
        return 2
    resolution = _resolver(args).resolve(entry, accept_review=True)
    if resolution.match is None:
        print(f"{entry.name}\nNO PROFILE FOUND")
        print(f"\n{SOURCE_NAME} has no cached recommendation for this game.")
        print("TravelReady does not substitute settings from any other source.")
        return 2
    print(resolution.describe())
    game = resolution.source_game
    if game is None:
        return 0
    print()
    print(f"Source:             {game.source_name}")
    print(f"URL:                {game.source_url}")
    print(f"Retrieved:          {game.retrieved_at}")
    print(f"Source last updated:{game.last_updated or 'unknown'}")
    print(f"Parser version:     {game.parser_version}")
    print(f"Content hash:       {game.content_hash[:16]}")
    if game.performance_rating is not None:
        print(f"Performance rating: {game.performance_rating} ({game.rating_word})")
    print()
    print("Published profiles:")
    for profile in game.profiles:
        print(f"  {profile.label}")
        for setting in profile.settings:
            cap = ral_capability.capability_for(setting.canonical)
            status = cap.status if cap else "informational"
            print(f"      {setting.label:28} {setting.value:16} [{status}]")
    return 0


def cmd_settings_show(args) -> int:
    entry = _entry_for(args)
    if entry is None:
        return 2
    store = opt_profiles.ProfileStore.load()
    if store.errors:
        for error in store.errors:
            print(f"! profile error: {error}", file=sys.stderr)
    plan = opt_diff.plan_for_game(entry, store, resolver=_resolver(args),
                                  mode=getattr(args, "mode", None))
    print(opt_diff.render_plan(plan))
    if plan.profile is None:
        cache = ProfileCache()
        print(f"\nCached {SOURCE_NAME} games: {len(cache.index.entries)}"
              f"   locally imported profiles: {len(store)}")
        print("Run 'travelready settings update' to refresh the cache.")
        print(f"Look it up at: {opt_profiles.rog_ally_life_search_url(entry.name)}")
    return 0


def cmd_settings_dry_run(args) -> int:
    entry = _entry_for(args)
    if entry is None:
        return 2
    plan = opt_diff.plan_for_game(entry, resolver=_resolver(args),
                                  mode=getattr(args, "mode", None))
    if not plan.applicable():
        print("No SAFE changes to make.")
        for warning in plan.warnings:
            print(f"  {warning}")
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
    plan = opt_diff.plan_for_game(entry, resolver=_resolver(args),
                                  mode=getattr(args, "mode", None))
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
    p.add_argument("--explain", action="store_true",
                   help="show how each library entry was classified, and why")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("doctor", help="diagnose the application and environment")
    p.add_argument("--fix", action="store_true",
                   help="repair TravelReady's own local state where it is safe to")
    p.add_argument("--offline", action="store_true", help="skip the network check")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("ready", help="per-game travel readiness, with reasons")
    add_filters(p)
    p.add_argument("game", nargs="?", help="one game, for the full check list")
    p.add_argument("--only", choices=list(preparation.READINESS_ORDER),
                   help="show only games with this verdict")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--mode", choices=list(OPERATING_MODES), default=MODE_BALANCED)
    p.add_argument("--max-watts", type=int)
    p.set_defaults(func=cmd_ready)

    p = sub.add_parser("launchers", help="what each launcher supports")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_launchers)

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

    p = sub.add_parser("prepare", help="prepare the library for offline play")
    add_filters(p)
    p.add_argument("--resume", action="store_true",
                   help="continue an interrupted run instead of starting over")
    p.add_argument("--no-launch", action="store_true",
                   help="assess only; do not start any game")
    p.add_argument("--no-cleanup", action="store_true",
                   help="leave games running after verifying them")
    p.add_argument("--stale-days", type=int, default=30)
    p.add_argument("--smoke-duration", type=float, default=lt.SMOKE_DEFAULT_DURATION)
    p.add_argument("--reverify", action="store_true",
                   help="re-verify games that already passed")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--mode", choices=list(OPERATING_MODES), default=MODE_BALANCED)
    p.add_argument("--max-watts", type=int)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("report", help="trip report")
    add_filters(p)
    p.add_argument("--stale-days", type=int, default=readiness.DEFAULT_STALE_DAYS)
    p.add_argument("--settings", action="store_true",
                   help="include a ROG Ally Life settings column")
    p.add_argument("--mode", choices=list(OPERATING_MODES), default=MODE_BALANCED)
    p.add_argument("--max-watts", type=int)
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

    def add_mode(p_):
        p_.add_argument("--mode", choices=list(OPERATING_MODES), default=MODE_BALANCED,
                        help="which published profile to prefer")
        p_.add_argument("--max-watts", type=int,
                        help="never choose a profile above this wattage")

    sp = ssub.add_parser("show", help="current vs recommended (read-only)")
    sp.add_argument("game")
    add_mode(sp)
    sp.set_defaults(func=cmd_settings_show)

    sp = ssub.add_parser("update", help=f"refresh the {SOURCE_NAME} cache")
    sp.add_argument("game", nargs="*", help="limit to games whose title contains this")
    sp.add_argument("--force", action="store_true", help="re-fetch even if unchanged")
    sp.add_argument("--limit", type=int, help="stop after this many posts")
    sp.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests (never below the site's Crawl-delay)")
    sp.set_defaults(func=cmd_settings_update)

    sp = ssub.add_parser("status", help="cache health and library coverage")
    add_filters(sp)
    add_mode(sp)
    sp.add_argument("--capabilities", action="store_true",
                    help="also print the settings capability matrix")
    sp.set_defaults(func=cmd_settings_status)

    sp = ssub.add_parser("coverage", help="how much of the library the source covers")
    add_filters(sp)
    add_mode(sp)
    sp.add_argument("--detail", action="store_true", help="list every game")
    sp.add_argument("--cached-only", action="store_true",
                    help="never fall back to the observed title list")
    sp.set_defaults(func=cmd_settings_coverage)

    sp = ssub.add_parser("search", help="search the cached source data")
    sp.add_argument("term")
    sp.add_argument("--limit", type=int, default=10)
    add_mode(sp)
    sp.set_defaults(func=cmd_settings_search)

    sp = ssub.add_parser("source", help="the raw source record and attribution")
    sp.add_argument("game")
    add_mode(sp)
    sp.set_defaults(func=cmd_settings_source)

    sp = ssub.add_parser("dry-run", help="print the exact diff a write would make")
    sp.add_argument("game")
    add_mode(sp)
    sp.set_defaults(func=cmd_settings_dry_run)

    sp = ssub.add_parser("apply", help="apply approved SAFE changes")
    sp.add_argument("game")
    sp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    add_mode(sp)
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
