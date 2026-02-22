#!/usr/bin/env python3
"""jfdups — Jellyfin Duplicate Detector & Cleanup Tool.

CLI entry point, config loading, orchestration.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Config loading (tomli for Python < 3.11)
# ---------------------------------------------------------------------------

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: Dict[str, Any] = {
    "jellyfin": {
        "url": "http://localhost:8096",
        "api_key": "",
        "timeout": 10,
    },
    "media": {
        "paths": [],
        "extensions": ["mkv", "mp4", "avi", "m4v", "mov"],
    },
    "detection": {
        "fuzzy_threshold": 0.85,
        "use_jellyfin_api": True,
    },
    "trash": {
        "dir": "",
        "preserve_structure": True,
    },
    "output": {
        "log_file": "~/.config/jfdups/jfdups.log",
    },
}

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "jfdups.toml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursively."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(config_path: Path) -> Dict[str, Any]:
    if tomllib is None:
        print(
            "ERROR: TOML library not found. Install it with: pip3 install tomli",
            file=sys.stderr,
        )
        sys.exit(1)

    cfg = _DEFAULT_CONFIG.copy()

    if config_path.exists():
        with open(config_path, "rb") as f:
            user_cfg = tomllib.load(f)
        cfg = _deep_merge(cfg, user_cfg)
    else:
        print(
            f"[yellow]Config file not found:[/] {config_path}\n"
            f"Using built-in defaults. Run `jfdups config` to see current settings."
        )

    # Environment variable overrides config (never stored on disk)
    env_key = os.environ.get("JFDUPS_API_KEY")
    if env_key:
        cfg["jellyfin"]["api_key"] = env_key

    return cfg


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------


def cmd_config(cfg: Dict[str, Any]) -> None:
    """Print the resolved configuration."""
    from rich.console import Console
    from rich.pretty import Pretty

    console = Console()
    console.print("[bold]Resolved configuration:[/]")
    console.print(Pretty(cfg))


def cmd_scan(cfg: Dict[str, Any], args: argparse.Namespace) -> None:
    """Full scan + detect + interactive review."""
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

    import scanner
    import detector
    import reviewer

    console = Console()
    dry_run: bool = args.dry_run
    use_api: bool = cfg["detection"]["use_jellyfin_api"] and not args.no_api
    threshold: float = args.threshold if args.threshold is not None else cfg["detection"]["fuzzy_threshold"]
    trash_root = Path(cfg["trash"]["dir"])
    preserve_structure: bool = cfg["trash"]["preserve_structure"]
    auto_accept: bool = args.yes
    log_file = Path(os.path.expanduser(cfg["output"]["log_file"]))

    # ---- Phase 1: Scan filesystem ----
    console.print("[bold cyan]Phase 1:[/] Scanning filesystem…")
    media_paths: List[str] = cfg["media"]["paths"]
    extensions: List[str] = cfg["media"]["extensions"]

    all_files: List[scanner.MediaFile] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("[progress.percentage]{task.completed} files"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning…", total=None)
        for mf in scanner.walk_paths(media_paths, extensions):
            all_files.append(mf)
            progress.update(task, completed=len(all_files))

    console.print(f"  Found [bold]{len(all_files)}[/] media files.")

    if not all_files:
        console.print("[yellow]No media files found. Check your paths in jfdups.toml.[/]")
        return

    # ---- Phase 2: Jellyfin API enrichment ----
    if use_api:
        console.print("[bold cyan]Phase 2:[/] Enriching via Jellyfin API…")
        jf = cfg["jellyfin"]
        with console.status("Querying Jellyfin API…"):
            scanner.enrich_from_jellyfin(
                all_files,
                url=jf["url"],
                api_key=jf["api_key"],
                timeout=jf["timeout"],
            )
    else:
        console.print("[bold cyan]Phase 2:[/] Skipping Jellyfin API (--no-api).")

    # ---- Phase 3: Detect duplicates ----
    console.print("[bold cyan]Phase 3:[/] Detecting duplicates…")
    with console.status("Analyzing…"):
        groups = detector.find_duplicates(all_files, fuzzy_threshold=threshold, use_api=use_api)

    console.print(f"  Found [bold]{len(groups)}[/] duplicate group(s).")

    if not groups:
        console.print("[green]No duplicates detected.[/]")
        return

    # ---- Phase 4: Quality probe ----
    console.print("[bold cyan]Phase 4:[/] Probing file quality (ffprobe)…")
    dup_files: List[scanner.MediaFile] = []
    for g in groups:
        dup_files.extend(g.files)
    dup_files = list(set(dup_files))  # deduplicate

    # Check ffprobe availability once; show warning if missing
    ffprobe_ok = scanner._ffprobe_available()
    if not ffprobe_ok:
        scanner.FFPROBE_MISSING_WARNED = False  # reset so warning shows here
        scanner.probe_files([dup_files[0]] if dup_files else [])  # triggers warning
        console.print("  [dim]Skipping ffprobe — ranking by file size.[/]")
    else:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Probing…", total=len(dup_files))
            for mf in dup_files:
                scanner._probe_one(mf)
                mf.probed = True
                progress.advance(task)

    # Re-rank after probing
    for g in groups:
        g.compute_best()

    # ---- Phase 5: Interactive review ----
    console.print("[bold cyan]Phase 5:[/] Interactive review.\n")
    reviewer.interactive_review(
        groups=groups,
        trash_root=trash_root,
        preserve_structure=preserve_structure,
        dry_run=dry_run,
        auto_accept=auto_accept,
        log_file=log_file,
    )


def cmd_list(cfg: Dict[str, Any], args: argparse.Namespace) -> None:
    """List duplicates without interactive review."""
    from rich.console import Console
    from rich.table import Table

    import scanner
    import detector

    console = Console()
    use_api = cfg["detection"]["use_jellyfin_api"] and not getattr(args, "no_api", False)
    threshold = cfg["detection"]["fuzzy_threshold"]

    console.print("[bold]Scanning…[/]")
    all_files = list(scanner.walk_paths(cfg["media"]["paths"], cfg["media"]["extensions"]))
    console.print(f"Found {len(all_files)} media files.")

    if use_api:
        jf = cfg["jellyfin"]
        scanner.enrich_from_jellyfin(all_files, url=jf["url"], api_key=jf["api_key"], timeout=jf["timeout"])

    groups = detector.find_duplicates(all_files, fuzzy_threshold=threshold, use_api=use_api)

    if not groups:
        console.print("[green]No duplicates found.[/]")
        return

    table = Table(title=f"Duplicate Groups ({len(groups)})", show_lines=True)
    table.add_column("Method", style="cyan", width=8)
    table.add_column("Key", overflow="ellipsis", no_wrap=True, ratio=3)
    table.add_column("Files", width=6)
    table.add_column("Paths", overflow="fold", ratio=7)

    for g in groups:
        paths = "\n".join(str(mf.path) for mf in g.files)
        table.add_row(g.method, g.match_key, str(len(g.files)), paths)

    console.print(table)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jfdups",
        description="Jellyfin Duplicate Detector & Cleanup Tool",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=str(_DEFAULT_CONFIG_PATH),
        help="Path to jfdups.toml config file (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate all moves without touching the filesystem",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Skip Jellyfin API enrichment (filesystem + fuzzy only)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Fuzzy match threshold 0.0–1.0 (default: from config, usually 0.85)",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Auto-accept best suggestion for all groups (non-interactive)",
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scan", help="Scan, detect, and interactively review duplicates (default)")
    sub.add_parser("list", help="List duplicates without interactive review")
    sub.add_parser("config", help="Show resolved configuration and exit")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    log_file = Path(os.path.expanduser(cfg["output"]["log_file"]))
    _setup_logging(log_file.parent / "jfdups_debug.log")

    command = args.command or "scan"

    if command == "config":
        cmd_config(cfg)
    elif command == "list":
        cmd_list(cfg, args)
    else:
        cmd_scan(cfg, args)


if __name__ == "__main__":
    main()
