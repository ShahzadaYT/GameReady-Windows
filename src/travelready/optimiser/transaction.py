"""transaction.py — the ONLY component in TravelReady that writes game files.

Every write goes through :func:`apply_plan`, which runs this sequence and
abandons the whole operation at the first failure:

  1. verify the game is not running;
  2. verify the relevant launcher is not running, when it could rewrite the file;
  3. **re-resolve** the target path;
  4. verify it is the same file that was inspected (size + hash);
  5. re-classify the target — a stale plan cannot write somewhere new;
  6. create a backup and verify the backup by hash;
  7. build the edit in memory and produce a dry-run diff;
  8. require explicit approval for each change;
  9. write atomically;
 10. re-read and verify the result;
 11. record the transaction;
 12. on any failure, restore from the backup.

If restoration itself fails, a critical error is raised and the backup is
deliberately left in place. The user's original configuration is never
silently destroyed.

There is no force-apply path. A change that is not SAFE cannot be applied by
any argument to any function here, and :func:`assert_writable` re-checks the
target immediately before the write regardless of what the plan says.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from ..apppaths import backups_dir, data_file
from ..library import GameEntry
from ..processes import ProcessTable, default_table
from .configio import ConfigError, IniDocument, JsonDocument, load_document, unified_diff
from .model import SAFE, ChangePlan, ProposedChange, TARGET_DEVICE
from .safety import assert_writable, classify_path

TRANSACTION_LOG = "settings_transactions.json"


class TransactionError(Exception):
    """A transaction was abandoned. The file on disk is unchanged."""


class CriticalRestoreError(Exception):
    """A write failed AND the backup could not be restored. Backup preserved."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# backups
# --------------------------------------------------------------------------

@dataclass
class Backup:
    """A verified copy of a configuration file, taken before any write."""

    original_path: str
    backup_path: str
    sha256: str
    size: int
    created_at: str = field(default_factory=_now)
    game_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def verify(self) -> bool:
        """True when the backup still matches the hash recorded when taken."""
        try:
            return (os.path.getsize(self.backup_path) == self.size
                    and file_hash(self.backup_path) == self.sha256)
        except OSError:
            return False


def create_backup(path: str, game_name: str = "",
                  directory: Optional[Path] = None) -> Backup:
    """Copy ``path`` somewhere safe and verify the copy by hash."""
    source = Path(path)
    if not source.is_file():
        raise TransactionError(f"Cannot back up {path}: it is not a file.")
    target_dir = Path(directory) if directory else backups_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() else "-" for c in (game_name or "game")).strip("-")
    target_dir = target_dir / f"{slug}-{stamp}"
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / source.name

    shutil.copy2(source, destination)
    backup = Backup(
        original_path=str(source), backup_path=str(destination),
        sha256=file_hash(str(destination)), size=destination.stat().st_size,
        game_name=game_name,
    )
    if backup.sha256 != file_hash(str(source)):
        raise TransactionError("Backup does not match the original; aborting.")
    if not backup.verify():
        raise TransactionError("Backup could not be verified; aborting.")
    return backup


def restore_backup(backup: Backup) -> None:
    """Put a backed-up file back. Raises if the backup is missing or corrupt."""
    if not backup.verify():
        raise CriticalRestoreError(
            f"Backup {backup.backup_path} is missing or does not match its recorded "
            f"hash; refusing to restore from it. The backup has been preserved.")
    Path(backup.original_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup.backup_path, backup.original_path)
    if file_hash(backup.original_path) != backup.sha256:
        raise CriticalRestoreError(
            f"Restore of {backup.original_path} did not reproduce the original "
            f"contents. The backup at {backup.backup_path} has been preserved.")


def list_backups(directory: Optional[Path] = None) -> List[Backup]:
    """Every backup on disk, newest first."""
    root = Path(directory) if directory else backups_dir()
    out: List[Backup] = []
    if not root.is_dir():
        return out
    for record in sorted(root.glob("*/backup.json"), reverse=True):
        try:
            out.append(Backup(**json.loads(record.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return out


def _write_backup_record(backup: Backup) -> None:
    record = Path(backup.backup_path).parent / "backup.json"
    record.write_text(json.dumps(backup.to_dict(), indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------

#: Launchers that rewrite game configuration on exit, so must be closed first.
_CONFIG_REWRITING_LAUNCHERS = {"ea", "ubisoft", "epic"}


def check_preconditions(entry: GameEntry, table: Optional[ProcessTable] = None) -> List[str]:
    """Reasons the game's settings must not be written right now.

    An empty list means it is safe to proceed. TravelReady never terminates a
    process to make a write possible — if something is running, the user closes
    it.
    """
    table = table or default_table()
    problems: List[str] = []
    for name in entry.process_names():
        if table.pids_by_name(name):
            problems.append(f"'{entry.name}' is running ({name}). Close the game first.")
            break
    if entry.launcher in _CONFIG_REWRITING_LAUNCHERS:
        from ..processes import LAUNCHER_PROCESSES

        running = [n for n in sorted(LAUNCHER_PROCESSES)
                   if n.startswith(("ea", "origin", "upc", "uplay", "ubisoft", "epic"))
                   and table.pids_by_name(n)]
        if running:
            problems.append(
                f"The {entry.launcher} launcher is running ({running[0]}) and may "
                f"rewrite this file when it closes. Close it first.")
    return problems


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------

@dataclass
class DryRun:
    """Exactly what a write would do, computed without touching the file."""

    file_path: str
    diff: str
    changes: List[ProposedChange] = field(default_factory=list)
    before_hash: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.changes)


def _apply_to_document(document, change: ProposedChange, holder_schema=None) -> None:
    """Set one value on an in-memory document, translating to the file's format."""
    if isinstance(document, JsonDocument):
        document.set_value(change.config_key or change.key, change.recommended)
        return
    value = change.recommended
    if holder_schema is not None:
        mapping = holder_schema.mapping_for(change.key)
        if mapping is not None:
            value = mapping.to_file_value(change.recommended)
    document.set_value(change.config_key, value, change.section or None)


def dry_run(plan: ChangePlan, file_path: str,
            changes: Optional[Sequence[ProposedChange]] = None) -> DryRun:
    """Compute the diff a write would produce. Performs no writes."""
    from .inspector import schema_for_file

    selected = [c for c in (changes if changes is not None else plan.applicable())
                if c.file_path == file_path]
    result = DryRun(file_path=file_path, diff="", changes=list(selected))
    if not selected:
        result.errors.append("No applicable changes for this file.")
        return result

    verdict = classify_path(file_path, must_exist=True)
    if verdict.safety != SAFE:
        result.errors.append(f"[{verdict.safety}] {verdict.reason}")
        return result

    try:
        document = load_document(file_path)
        before = document.to_text()
        result.before_hash = file_hash(file_path)
    except (ConfigError, OSError) as exc:
        result.errors.append(str(exc))
        return result

    if getattr(document, "parse_ok", True) is False:
        result.errors.append(getattr(document, "parse_note", "File could not be parsed."))
        return result

    schema = schema_for_file(file_path)
    for change in selected:
        if change.safety != SAFE:
            result.errors.append(
                f"{change.key} is {change.safety}, not SAFE; it cannot be applied.")
            continue
        try:
            _apply_to_document(document, change, schema)
        except ConfigError as exc:
            result.errors.append(f"{change.key}: {exc}")

    result.diff = unified_diff(before, document.to_text(), file_path)
    if not result.diff.strip():
        result.errors.append("The proposed changes would not alter the file.")
    return result


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

@dataclass
class TransactionRecord:
    """The audit record written after every apply attempt."""

    game_name: str
    file_path: str
    applied: List[str] = field(default_factory=list)
    backup: Optional[dict] = None
    diff: str = ""
    succeeded: bool = False
    restored: bool = False
    error: str = ""
    at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


def record_transaction(record: TransactionRecord, path: Optional[Path] = None) -> None:
    target = Path(path) if path else data_file(TRANSACTION_LOG)
    try:
        existing = json.loads(Path(target).read_text(encoding="utf-8"))
        rows = existing.get("transactions", []) if isinstance(existing, dict) else []
    except (OSError, ValueError):
        rows = []
    rows.append(record.to_dict())
    tmp = Path(target).with_name(Path(target).name + ".tmp")
    tmp.write_text(json.dumps({"transactions": rows[-500:]}, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def apply_plan(entry: GameEntry, plan: ChangePlan, file_path: str,
               approved: Sequence[ProposedChange],
               *, table: Optional[ProcessTable] = None,
               backup_dir: Optional[Path] = None,
               log_path: Optional[Path] = None,
               target_device: str = TARGET_DEVICE) -> TransactionRecord:
    """Apply the approved SAFE changes for one file, or change nothing at all.

    ``approved`` must be changes the user explicitly selected. Anything not
    SAFE is refused here even if it appears in ``approved``, and
    :func:`~travelready.optimiser.safety.assert_writable` re-checks the target
    immediately before the write.
    """
    from .inspector import schema_for_file

    record = TransactionRecord(game_name=entry.name, file_path=file_path)

    selected = [c for c in approved if c.file_path == file_path]
    if not selected:
        record.error = "No approved changes for this file."
        record_transaction(record, log_path)
        raise TransactionError(record.error)

    not_safe = [c for c in selected if c.safety != SAFE]
    if not_safe:
        record.error = ("Refusing to apply non-SAFE changes: "
                        + ", ".join(f"{c.key} [{c.safety}]" for c in not_safe))
        record_transaction(record, log_path)
        raise TransactionError(record.error)

    unapproved = [c for c in selected if not c.approved]
    if unapproved:
        record.error = ("These changes were not approved by the user: "
                        + ", ".join(c.key for c in unapproved))
        record_transaction(record, log_path)
        raise TransactionError(record.error)

    # 1 & 2 — nothing that could fight us over the file may be running
    problems = check_preconditions(entry, table)
    if problems:
        record.error = " ".join(problems)
        record_transaction(record, log_path)
        raise TransactionError(record.error)

    # 3 & 4 — the file must still be the one that was inspected
    resolved = os.path.abspath(file_path) if os.path.isabs(file_path) else file_path
    if not os.path.isfile(resolved):
        record.error = f"{file_path} no longer exists."
        record_transaction(record, log_path)
        raise TransactionError(record.error)

    preview = dry_run(plan, file_path, selected)
    if not preview.ok:
        record.error = "; ".join(preview.errors) or "Dry run produced no change."
        record_transaction(record, log_path)
        raise TransactionError(record.error)
    if file_hash(resolved) != preview.before_hash:
        record.error = "The file changed while it was being prepared; aborting."
        record_transaction(record, log_path)
        raise TransactionError(record.error)

    # 5 — re-classify every target immediately before writing
    device = plan.profile.device if plan.profile else target_device
    for change in selected:
        try:
            assert_writable(resolved, change.key, change.recommended, device, target_device)
        except PermissionError as exc:
            record.error = str(exc)
            record_transaction(record, log_path)
            raise TransactionError(record.error) from exc

    # 6 — backup, verified
    backup = create_backup(resolved, entry.name, backup_dir)
    _write_backup_record(backup)
    record.backup = backup.to_dict()
    record.diff = preview.diff

    # 7 & 8 — build the new content in memory
    try:
        document = load_document(resolved)
        schema = schema_for_file(resolved)
        for change in selected:
            _apply_to_document(document, change, schema)
        new_bytes = document.to_bytes()
    except (ConfigError, OSError) as exc:
        record.error = f"Could not prepare the edit: {exc}"
        record_transaction(record, log_path)
        raise TransactionError(record.error) from exc

    # 9 — atomic write
    tmp = Path(resolved).with_name(Path(resolved).name + ".travelready-tmp")
    try:
        tmp.write_bytes(new_bytes)
        os.replace(tmp, resolved)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        record.error = f"Write failed: {exc}"
        _rollback(record, backup, log_path)
        raise TransactionError(record.error) from exc

    # 10 — verify by reading the file back
    try:
        written = load_document(resolved)
        failures = _verify_written(written, selected, schema)
    except (ConfigError, OSError) as exc:
        record.error = f"File could not be re-read after writing: {exc}"
        _rollback(record, backup, log_path)
        raise TransactionError(record.error) from exc

    if failures:
        record.error = "Verification failed after writing: " + "; ".join(failures)
        _rollback(record, backup, log_path)
        raise TransactionError(record.error)

    for change in selected:
        change.applied = True
    record.applied = [c.key for c in selected]
    record.succeeded = True
    record_transaction(record, log_path)
    return record


def _verify_written(document, changes: Sequence[ProposedChange], schema) -> List[str]:
    failures: List[str] = []
    for change in changes:
        expected = change.recommended
        if schema is not None and not isinstance(document, JsonDocument):
            mapping = schema.mapping_for(change.key)
            if mapping is not None:
                expected = mapping.to_file_value(change.recommended)
        if isinstance(document, JsonDocument):
            actual = document.get(change.config_key or change.key)
        else:
            actual = document.get(change.config_key, change.section or None)
        if str(actual).strip() != str(expected).strip():
            failures.append(f"{change.key} is '{actual}', expected '{expected}'")
    return failures


def _rollback(record: TransactionRecord, backup: Backup,
              log_path: Optional[Path]) -> None:
    """Restore from backup after a failed write, or escalate loudly."""
    try:
        restore_backup(backup)
        record.restored = True
    except CriticalRestoreError as exc:
        record.error = f"{record.error} | CRITICAL: {exc}"
        record_transaction(record, log_path)
        raise
    record_transaction(record, log_path)


def restore_game_settings(backup: Backup, log_path: Optional[Path] = None) -> TransactionRecord:
    """User-initiated restore of a previous backup."""
    record = TransactionRecord(game_name=backup.game_name,
                               file_path=backup.original_path,
                               backup=backup.to_dict())
    try:
        restore_backup(backup)
    except CriticalRestoreError as exc:
        record.error = str(exc)
        record_transaction(record, log_path)
        raise
    record.restored = True
    record.succeeded = True
    record_transaction(record, log_path)
    return record
