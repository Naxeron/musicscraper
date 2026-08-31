"""
Unified reporting, Rich console formatting, tables, and format exporters (JSON, CSV, MD, TXT).
"""

import csv
import json
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

console = Console()


class BaseReportExporter:
    """Helper utilities for exporting audit, scan, and download data."""

    @staticmethod
    def export_json(data: Dict[str, Any], output_path: Path) -> None:
        """Exports dictionary data to formatted JSON."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        console.print(f"[green]✔ Exported JSON report to:[/green] [bold]{output_path}[/bold]")

    @staticmethod
    def export_csv(headers: List[str], rows: List[List[Any]], output_path: Path) -> None:
        """Exports tabular data to CSV."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
        console.print(f"[green]✔ Exported CSV report to:[/green] [bold]{output_path}[/bold]")

    @staticmethod
    def export_text(lines: List[str], output_path: Path, header_title: str = "Report") -> None:
        """Exports lines to a plain text file with timestamped header."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# {header_title}\n")
            f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for line in lines:
                f.write(f"{line}\n")
        console.print(f"[green]✔ Exported text report to:[/green] [bold]{output_path}[/bold]")

    @staticmethod
    def export_markdown(content: str, output_path: Path) -> None:
        """Exports markdown document to file."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        console.print(f"[green]✔ Exported Markdown report to:[/green] [bold]{output_path}[/bold]")
