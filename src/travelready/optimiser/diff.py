"""diff.py — compare a profile's recommendations with the game's current settings.

Read-only. Produces a :class:`~travelready.optimiser.model.ChangePlan` in which
every entry carries an explicit safety classification and the reason for it, so
the UI can always show exactly what would change and exactly why anything is
being withheld.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from ..library import GameEntry
from .inspector import Inspection, inspect_game
from .model import (
    BLOCKED, CATEGORY_DEVICE, CAUTION, MANUAL, RESEARCH_REQUIRED, SAFE,
    TARGET_DEVICE, ChangePlan, GameProfile, ProposedChange, SettingValue,
    category_of,
)
from .profiles import ProfileStore
from .safety import classify_target, classify_device


def build_plan(entry: GameEntry, profile: Optional[GameProfile],
               inspection: Optional[Inspection] = None,
               target_device: str = TARGET_DEVICE) -> ChangePlan:
    """Compare ``profile`` with the game's current settings.

    Never writes, never launches anything, and never returns a SAFE change for
    a setting whose file, key or device it could not fully vet.
    """
    plan = ChangePlan(game_name=entry.name, profile=profile)
    if profile is None:
        plan.warnings.append(
            "No ROG Ally Life profile is installed for this game, so there is "
            "nothing to compare against.")
        return plan

    inspection = inspection if inspection is not None else inspect_game(entry)
    plan.config_files = [f.path for f in inspection.files if f.readable]
    plan.warnings.extend(inspection.warnings)

    device_verdict = classify_device(profile.device, target_device)
    if device_verdict.safety != SAFE:
        plan.warnings.append(device_verdict.reason)

    for rec in profile.recommendations:
        current = inspection.current(rec.key)
        holder = inspection.file_for(rec.key)
        file_path = holder.path if holder else ""
        section = config_key = ""
        if holder is not None and holder.schema is not None:
            mapping = holder.schema.mapping_for(rec.key)
            if mapping is not None:
                section, config_key = mapping.section, mapping.config_key

        if rec.category == CATEGORY_DEVICE:
            safety, reason = MANUAL, (
                "Device-wide setting. TravelReady shows it so you can apply it "
                "yourself; it does not change TDP, fan, VRAM, Armoury Crate, "
                "Adrenalin or Windows power settings.")
        elif not file_path:
            safety, reason = RESEARCH_REQUIRED, (
                "This game's configuration file for this setting was not found, "
                "so the current value cannot be read or changed.")
        else:
            verdict = classify_target(rec.key, rec.value, file_path,
                                      profile.device, target_device,
                                      must_exist=True, check_content=True)
            safety, reason = verdict.safety, verdict.reason
            if safety == SAFE and not config_key:
                safety, reason = RESEARCH_REQUIRED, (
                    "The configuration key for this setting is not known in this "
                    "file's format.")

        plan.changes.append(ProposedChange(
            key=rec.key,
            current=current,
            recommended=rec.value,
            safety=safety,
            reason=reason,
            category=rec.category,
            file_path=file_path,
            section=section,
            config_key=config_key,
            note=rec.note,
        ))
    return plan


def plan_for_game(entry: GameEntry, store: Optional[ProfileStore] = None,
                  target_device: str = TARGET_DEVICE) -> ChangePlan:
    """Look up the profile for ``entry`` and build its plan. Read-only."""
    store = store or ProfileStore.load()
    profile = store.find(entry.name, target_device, entry.launcher)
    return build_plan(entry, profile, target_device=target_device)


def render_plan(plan: ChangePlan) -> str:
    """The human-readable summary shown before anything is applied."""
    lines: List[str] = []
    lines.append("-" * 51)
    lines.append("GAME")
    lines.append("-" * 51)
    lines.append("")
    lines.append(plan.game_name)
    lines.append("")
    if plan.profile is None:
        lines.append("ROG Ally Life profile:")
        lines.append("NOT FOUND")
        lines.append("")
        for warning in plan.warnings:
            lines.append(f"! {warning}")
        return "\n".join(lines)

    lines.append("ROG Ally Life profile:")
    lines.append("FOUND ✓")
    lines.append(f"Source: {plan.profile.attribution}")
    lines.append(f"Device: {plan.profile.device}")
    lines.append("")

    def section(title: str, rows: Sequence[ProposedChange], marker: str) -> None:
        lines.append("-" * 51)
        lines.append(title)
        lines.append("-" * 51)
        lines.append("")
        if not rows:
            lines.append(f"{marker} None")
        for change in rows:
            lines.append(f"{marker} {change.key:<18} "
                         f"{change.current.display()} → {change.recommended}")
            if change.safety != SAFE:
                lines.append(f"{'':<3}{change.reason}")
        lines.append("")

    section("SAFE CHANGES", plan.safe, "✓")
    section("NEEDS REVIEW", plan.caution, "△")
    section("MANUAL", plan.manual, "○")
    section("RESEARCH REQUIRED", plan.research, "?")
    section("PROTECTED", plan.blocked, "\U0001f512")

    if plan.already_correct:
        lines.append(f"Already matching the profile: "
                     f"{', '.join(c.key for c in plan.already_correct)}")
        lines.append("")
    for warning in plan.warnings:
        lines.append(f"! {warning}")
    return "\n".join(lines)
