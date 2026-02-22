"""reviewer.py — Rich interactive UI, trash-move logic, session log."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from detector import DuplicateGroup
from scanner import MediaFile

logger = logging.getLogger(__name__)
console = Console()

# Companion file extensions to move alongside the main media file
_COMPANION_EXTS = {".jpg", ".jpeg", ".png", ".nfo", ".srt", ".sub", ".ass", ".ssa", ".idx", ".vtt"}


# ---------------------------------------------------------------------------
# Trash move logic
# ---------------------------------------------------------------------------


def _trash_dest(mf: MediaFile, trash_root: Path, preserve_structure: bool) -> Path:
    """Compute the destination path inside the trash root."""
    if preserve_structure:
        # Mirror full absolute path: /Volumes/Media/Movies/film.mkv
        # → /trash/Volumes/Media/Movies/film.mkv
        rel = Path(str(mf.path).lstrip("/"))
        return trash_root / rel
    else:
        return trash_root / mf.path.name


def _unique_dest(dest: Path) -> Path:
    """Return dest, or dest.conflict1, .conflict2 etc. if it already exists."""
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    i = 1
    while True:
        candidate = parent / f"{stem}.conflict{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _find_companions(mf: MediaFile) -> List[Path]:
    """Find companion art/subtitle files with the same stem."""
    companions = []
    parent = mf.path.parent
    stem = mf.path.stem
    try:
        for sibling in parent.iterdir():
            if sibling == mf.path:
                continue
            if sibling.stem == stem and sibling.suffix.lower() in _COMPANION_EXTS:
                companions.append(sibling)
    except OSError:
        pass
    return companions


def _preflight_check(files_to_move: List[MediaFile], trash_root: Path) -> Optional[str]:
    """Return an error string if there's not enough space, else None."""
    total_size = sum(mf.size for mf in files_to_move)
    try:
        usage = shutil.disk_usage(trash_root if trash_root.exists() else trash_root.parent)
        if usage.free < total_size:
            needed_gb = total_size / 1e9
            free_gb = usage.free / 1e9
            return (
                f"Insufficient space on trash volume: need {needed_gb:.2f} GB, "
                f"only {free_gb:.2f} GB free."
            )
    except OSError as exc:
        return f"Could not check disk space: {exc}"
    return None


def move_to_trash(
    files_to_move: List[MediaFile],
    trash_root: Path,
    preserve_structure: bool,
    dry_run: bool,
) -> List[dict]:
    """Move files to trash. Returns list of move-record dicts for the session log."""
    records = []
    trash_root.mkdir(parents=True, exist_ok=True)

    for mf in files_to_move:
        dest = _unique_dest(_trash_dest(mf, trash_root, preserve_structure))
        companions = _find_companions(mf)

        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(mf.path), str(dest))
                logger.info("Moved %s → %s", mf.path, dest)
            except OSError as exc:
                logger.error("Failed to move %s: %s", mf.path, exc)
                console.print(f"[red]ERROR:[/] Could not move {mf.path}: {exc}")
                continue

            # Move companions
            for comp in companions:
                comp_dest = _unique_dest(dest.parent / comp.name)
                try:
                    shutil.move(str(comp), str(comp_dest))
                except OSError as exc:
                    logger.warning("Could not move companion %s: %s", comp, exc)

        records.append(
            {
                "source": str(mf.path),
                "destination": str(dest),
                "size": mf.size,
                "dry_run": dry_run,
                "companions": [str(c) for c in companions],
            }
        )

    return records


# ---------------------------------------------------------------------------
# Rich helpers
# ---------------------------------------------------------------------------


def _fmt_size(size: int) -> str:
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if size >= div:
            return f"{size / div:.1f} {unit}"
    return f"{size} B"


def _fmt_res(mf: MediaFile) -> str:
    if mf.width and mf.height:
        return f"{mf.width}×{mf.height}"
    return "—"


def _fmt_bitrate(mf: MediaFile) -> str:
    if mf.bitrate:
        return f"{mf.bitrate // 1000} kbps"
    return "—"


def _build_group_table(group: DuplicateGroup, choices: List[bool]) -> Table:
    """Build a Rich table for one duplicate group."""
    table = Table(show_header=True, header_style="bold cyan", expand=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Status", width=10)
    table.add_column("Path", overflow="ellipsis", no_wrap=True, ratio=6)
    table.add_column("Res", width=10)
    table.add_column("Bitrate", width=12)
    table.add_column("Size", width=10)

    for idx, (mf, keep) in enumerate(zip(group.files, choices), start=1):
        is_best = mf is group.best
        if is_best:
            status = Text("★ KEEP", style="bold green")
        elif keep:
            status = Text("KEEP", style="green")
        else:
            status = Text("TRASH", style="red")

        path_str = str(mf.path)
        row_style = "bold" if is_best else ""
        table.add_row(
            str(idx),
            status,
            path_str,
            _fmt_res(mf),
            _fmt_bitrate(mf),
            _fmt_size(mf.size),
            style=row_style,
        )

    return table


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------


_QUIT = object()  # sentinel for quit action


def _review_group(
    group: DuplicateGroup, group_num: int, total: int
):
    """
    Interactively review one duplicate group.

    Returns:
        List[MediaFile]  — files to trash (may be empty if user keeps all)
        None             — skip this group
        _QUIT sentinel   — user wants to quit the review session
    """
    console.rule(
        f"[bold]Group {group_num}/{total}[/]  method=[cyan]{group.method}[/]  "
        f"key=[yellow]{group.match_key[:60]}[/]  "
        f"sim={group.similarity:.0%}",
        style="blue",
    )

    # choices[i] = True means keep, False means trash
    # Start with best=keep, others=trash
    choices = [mf is group.best for mf in group.files]

    while True:
        table = _build_group_table(group, choices)
        console.print(table)
        console.print(
            "[dim]Actions: [bold]k[/]=accept suggestion  [bold]r[/]=reverse  "
            "[bold]s[/]=skip  [bold]1-9[/]=toggle  [bold]a[/]=keep all  [bold]q[/]=quit[/]"
        )

        raw = Prompt.ask("[bold]>").strip().lower()

        if raw == "q":
            return _QUIT

        if raw == "s":
            return None  # skip

        if raw == "k":
            # Accept suggestion: trash all except best
            return [mf for mf in group.files if mf is not group.best]

        if raw == "r":
            # Reverse: keep previously-to-trash, trash previously-kept
            choices = [not c for c in choices]
            continue

        if raw == "a":
            # Keep all: trash nothing
            return []

        if raw.isdigit() and 1 <= int(raw) <= len(group.files):
            idx = int(raw) - 1
            choices[idx] = not choices[idx]
            continue

        # Enter/empty = accept suggestion
        if raw == "":
            return [mf for mf in group.files if mf is not group.best]

        console.print("[red]Unknown action.[/]")


def interactive_review(
    groups: List[DuplicateGroup],
    trash_root: Path,
    preserve_structure: bool,
    dry_run: bool,
    auto_accept: bool,
    log_file: Path,
) -> None:
    """Drive the interactive (or auto) review session."""
    if not groups:
        console.print("[green]No duplicates found![/]")
        return

    console.print(f"\n[bold]Found {len(groups)} duplicate group(s).[/]\n")

    all_to_trash: List[MediaFile] = []
    session_records: List[dict] = []

    if auto_accept:
        # Non-interactive: accept best suggestion for all groups
        for g in groups:
            all_to_trash.extend(g.to_trash)
    else:
        for i, group in enumerate(groups, start=1):
            result = _review_group(group, i, len(groups))
            if result is _QUIT:
                console.print("[yellow]Review stopped by user.[/]")
                break
            if result is None:
                # Skipped
                continue
            all_to_trash.extend(result)

    # De-duplicate (same file might appear in multiple groups)
    seen: set = set()
    unique_to_trash: List[MediaFile] = []
    for mf in all_to_trash:
        if id(mf) not in seen:
            seen.add(id(mf))
            unique_to_trash.append(mf)

    if not unique_to_trash:
        console.print("[green]Nothing to move.[/]")
        return

    # Summary panel
    total_size = sum(mf.size for mf in unique_to_trash)
    summary = Table.grid(padding=(0, 2))
    summary.add_column()
    summary.add_column()
    summary.add_row("[bold]Groups:[/]", str(len(groups)))
    summary.add_row("[bold]Files to trash:[/]", str(len(unique_to_trash)))
    summary.add_row("[bold]Total size:[/]", _fmt_size(total_size))
    summary.add_row("[bold]Dry run:[/]", "YES" if dry_run else "NO")
    console.print(Panel(summary, title="[bold]Summary[/]", border_style="yellow"))

    # Pre-flight space check
    error = _preflight_check(unique_to_trash, trash_root)
    if error:
        console.print(f"[red]ERROR:[/] {error}")
        return

    if not auto_accept and not dry_run:
        confirm = Prompt.ask(
            f"[bold yellow]Proceed with moving {len(unique_to_trash)} files to trash?[/] (yes/no)"
        ).strip().lower()
        if confirm not in ("y", "yes"):
            console.print("[yellow]Aborted.[/]")
            return

    # Execute moves
    with console.status("[bold]Moving files to trash…[/]"):
        records = move_to_trash(unique_to_trash, trash_root, preserve_structure, dry_run)
        session_records.extend(records)

    # Write session log
    _write_session_log(session_records, log_file, dry_run)

    moved = sum(1 for r in records if not r.get("dry_run") or dry_run)
    console.print(
        f"\n[bold green]Done.[/] {'Would move' if dry_run else 'Moved'} "
        f"{moved} file(s) ({_fmt_size(total_size)})."
    )
    if not dry_run:
        console.print(f"Session log: [cyan]{log_file}[/]")


# ---------------------------------------------------------------------------
# Session log
# ---------------------------------------------------------------------------


def _write_session_log(records: List[dict], log_file: Path, dry_run: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "dry_run": dry_run,
        "moves": records,
    }
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as exc:
        logger.warning("Could not write session log: %s", exc)


# ---------------------------------------------------------------------------
# Progress helpers (for use by jfdups.py)
# ---------------------------------------------------------------------------


def make_scan_progress():
    """Return a Rich Progress instance configured for scanning."""
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )
