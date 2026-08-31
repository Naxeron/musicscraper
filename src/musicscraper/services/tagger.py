"""
Last.fm genre tagging service orchestrating tag fetching, cascading, and Mutagen writing.
"""

import os
from pathlib import Path
from typing import Dict, List, Set, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn, MofNCompleteColumn
from rich import box

from musicscraper.core.constants import AUDIO_EXTENSIONS
from musicscraper.core.audio import AudioMetadata, AudioMetadataHandler
from musicscraper.core.report import console
from musicscraper.clients.lastfm import LastFMClient, GenreNormalizer


class GenreTaggerService:
    """Orchestrates tag fetching, cascading, filtering, and writing for music files."""

    def __init__(
        self,
        client: Optional[LastFMClient] = None,
        normalizer: Optional[GenreNormalizer] = None,
        strategy: str = "cascade",
        limit: int = 3,
        mode: str = "overwrite",
        separator: str = "; ",
        multi_value: bool = False,
        dry_run: bool = False,
        threads: int = 8
    ):
        self.client = client or LastFMClient()
        self.normalizer = normalizer or GenreNormalizer()
        self.strategy = strategy
        self.limit = limit
        self.mode = mode
        self.separator = separator
        self.multi_value = multi_value
        self.dry_run = dry_run
        self.threads = max(1, threads)

    def resolve_genres(self, meta: AudioMetadata) -> List[str]:
        """Fetches and cascades/blends Last.fm tags for an audio file."""
        artist = meta.artist or meta.album_artist
        album = meta.album
        title = meta.title

        if not artist:
            return []

        if self.strategy == "artist":
            raw = self.client.get_artist_tags(artist)
            return self.normalizer.filter_and_format(raw, artist=artist, limit=self.limit)

        if self.strategy == "album":
            raw_album = self.client.get_album_tags(artist, album) if album else []
            genres = self.normalizer.filter_and_format(raw_album, artist=artist, album=album, limit=self.limit)
            if not genres:
                raw_artist = self.client.get_artist_tags(artist)
                genres = self.normalizer.filter_and_format(raw_artist, artist=artist, limit=self.limit)
            return genres

        if self.strategy == "track":
            raw_track = self.client.get_track_tags(artist, title) if title else []
            return self.normalizer.filter_and_format(raw_track, artist=artist, track=title, limit=self.limit)

        if self.strategy == "blend":
            raw_track = self.client.get_track_tags(artist, title) if title else []
            raw_album = self.client.get_album_tags(artist, album) if album else []
            raw_artist = self.client.get_artist_tags(artist)

            weighted_scores: Dict[str, float] = {}
            for t in raw_track:
                name = self.normalizer.clean_tag_name(t.get("name", ""))
                cnt = float(t.get("count", 100))
                weighted_scores[name] = weighted_scores.get(name, 0.0) + (cnt * 1.5)

            for t in raw_album:
                name = self.normalizer.clean_tag_name(t.get("name", ""))
                cnt = float(t.get("count", 100))
                weighted_scores[name] = weighted_scores.get(name, 0.0) + (cnt * 1.0)

            for t in raw_artist:
                name = self.normalizer.clean_tag_name(t.get("name", ""))
                cnt = float(t.get("count", 100))
                weighted_scores[name] = weighted_scores.get(name, 0.0) + (cnt * 0.7)

            sorted_tags = sorted(
                [{"name": k, "count": int(v)} for k, v in weighted_scores.items()],
                key=lambda x: x["count"],
                reverse=True
            )
            return self.normalizer.filter_and_format(sorted_tags, artist=artist, album=album, track=title, limit=self.limit)

        # Default: Cascade (Track -> Album -> Artist)
        raw_track = self.client.get_track_tags(artist, title) if title else []
        track_genres = self.normalizer.filter_and_format(raw_track, artist=artist, track=title, limit=self.limit)
        if len(track_genres) >= self.limit:
            return track_genres

        raw_album = self.client.get_album_tags(artist, album) if album else []
        album_genres = self.normalizer.filter_and_format(raw_album, artist=artist, album=album, limit=self.limit)

        combined = list(track_genres)
        for g in album_genres:
            if g.lower() not in [x.lower() for x in combined]:
                combined.append(g)
            if len(combined) >= self.limit:
                return combined

        raw_artist = self.client.get_artist_tags(artist)
        artist_genres = self.normalizer.filter_and_format(raw_artist, artist=artist, limit=self.limit)
        for g in artist_genres:
            if g.lower() not in [x.lower() for x in combined]:
                combined.append(g)
            if len(combined) >= self.limit:
                break

        return combined

    def process_file(self, file_path: Path) -> Dict[str, Any]:
        """Reads metadata, queries Last.fm, and applies tags."""
        meta = AudioMetadataHandler.read_metadata(file_path)
        existing_genres = list(meta.genres)

        if self.mode == "skip_existing" and existing_genres:
            return {
                "path": file_path,
                "artist": meta.artist,
                "album": meta.album,
                "title": meta.title,
                "old_genres": existing_genres,
                "new_genres": existing_genres,
                "status": "skipped_existing",
                "changed": False
            }

        resolved_genres = self.resolve_genres(meta)
        if not resolved_genres:
            return {
                "path": file_path,
                "artist": meta.artist,
                "album": meta.album,
                "title": meta.title,
                "old_genres": existing_genres,
                "new_genres": existing_genres,
                "status": "no_tags_found",
                "changed": False
            }

        if self.mode == "append":
            final_genres = list(existing_genres)
            for g in resolved_genres:
                if g.lower() not in [x.lower() for x in final_genres]:
                    final_genres.append(g)
        else:
            final_genres = resolved_genres

        changed = (existing_genres != final_genres)
        success = True

        if changed and not self.dry_run:
            success = AudioMetadataHandler.write_genres(
                file_path=file_path,
                genres=final_genres,
                mode=self.mode,
                separator=self.separator,
                multi_value=self.multi_value
            )

        status = "dry_run" if self.dry_run else ("updated" if (changed and success) else "unchanged")
        if changed and not success and not self.dry_run:
            status = "write_failed"

        return {
            "path": file_path,
            "artist": meta.artist,
            "album": meta.album,
            "title": meta.title,
            "old_genres": existing_genres,
            "new_genres": final_genres,
            "status": status,
            "changed": changed
        }

    def process_target(self, target_path: Path) -> List[Dict[str, Any]]:
        """Processes a single file or entire directory tree."""
        target_path = Path(target_path).resolve()
        if not target_path.exists():
            console.print(f"[red]Error: Target path does not exist: {target_path}[/red]")
            return []

        audio_files: List[Path] = []
        if target_path.is_file():
            if target_path.suffix.lower() in AUDIO_EXTENSIONS:
                audio_files.append(target_path)
        else:
            for root, _, files in os.walk(target_path):
                for f in sorted(files):
                    f_path = Path(root) / f
                    if f_path.suffix.lower() in AUDIO_EXTENSIONS:
                        audio_files.append(f_path)

        if not audio_files:
            console.print(f"[yellow]No supported audio files found in {target_path}[/yellow]")
            return []

        title_prefix = "[DRY RUN] " if self.dry_run else ""
        console.print(Panel(
            Text.from_markup(
                f"[bold cyan]Last.fm Genre Tagger[/bold cyan]\n"
                f"[dim]Target:[/dim] {target_path}\n"
                f"[dim]Files Found:[/dim] {len(audio_files)}\n"
                f"[dim]Strategy:[/dim] {self.strategy} | [dim]Mode:[/dim] {self.mode} | [dim]Limit:[/dim] {self.limit} genres\n"
                f"[dim]Dry-Run:[/dim] {'Yes' if self.dry_run else 'No'}"
            ),
            title=f"[bold]{title_prefix}Tagging Configuration[/bold]",
            border_style="cyan",
            box=box.ROUNDED
        ))

        results: List[Dict[str, Any]] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            task = progress.add_task("[cyan]Tagging music files...", total=len(audio_files))

            with ThreadPoolExecutor(max_workers=self.threads) as executor:
                future_to_file = {executor.submit(self.process_file, f): f for f in audio_files}
                for future in as_completed(future_to_file):
                    try:
                        res = future.result()
                        results.append(res)
                    except Exception:
                        pass
                    finally:
                        progress.advance(task)

        # Print Summary Table
        table = Table(title="Genre Tagging Results", box=box.ROUNDED, header_style="bold cyan")
        table.add_column("File", style="bold white", min_width=25)
        table.add_column("Artist / Album", style="dim")
        table.add_column("Old Genres", style="dim red")
        table.add_column("New Genres", style="bold green")
        table.add_column("Status", justify="center")

        updated_count = sum(1 for r in results if r["changed"] and r["status"] in ("updated", "dry_run"))
        skipped_count = sum(1 for r in results if r["status"] == "skipped_existing")
        not_found_count = sum(1 for r in results if r["status"] == "no_tags_found")

        for r in sorted(results, key=lambda x: str(x["path"])):
            if r["changed"] or r["status"] == "updated":
                status_markup = "[green]✔ Updated[/green]" if not self.dry_run else "[yellow]Would Update[/yellow]"
                table.add_row(
                    r["path"].name,
                    f"{r['artist']} - {r['album']}",
                    ", ".join(r["old_genres"]) or "-",
                    ", ".join(r["new_genres"]) or "-",
                    status_markup
                )

        if updated_count > 0:
            console.print(table)

        score_text = Text()
        score_text.append(f"Total Files Scanned: ", style="bold")
        score_text.append(f"{len(results)}\n", style="bold cyan")
        score_text.append(f"Tags Updated / Changed: ", style="bold")
        score_text.append(f"{updated_count}\n", style="bold green")
        score_text.append(f"Skipped (Already Tagged): ", style="bold")
        score_text.append(f"{skipped_count}\n", style="bold yellow")
        score_text.append(f"No Tags Found: ", style="bold")
        score_text.append(f"{not_found_count}\n", style="dim")

        console.print(Panel(score_text, title="[bold]Summary[/bold]", border_style="cyan", box=box.ROUNDED))
        return results
