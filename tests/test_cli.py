"""
Unit tests for CLI parsing and subcommands.
"""

import pytest
from musicscraper.cli.main import build_parser


def test_cli_parser_commands():
    parser = build_parser()

    # Audit subcommand
    args = parser.parse_args(["audit", "Massive Attack", "--missing-only"])
    assert args.command == "audit"
    assert args.artist == "Massive Attack"
    assert args.missing_only is True

    # Soulseek subcommand
    args = parser.parse_args(["soulseek", "Aphex Twin", "--dry-run", "-f", "flac"])
    assert args.command == "soulseek"
    assert args.artist == "Aphex Twin"
    assert args.dry_run is True
    assert args.format == "flac"

    # Quality upgrade subcommand
    args = parser.parse_args(["upgrade", "--dry-run", "-f", "mp3-320"])
    assert args.command == "upgrade"
    assert args.dry_run is True
    assert args.format == "mp3-320"

    # Genre tagger subcommand
    args = parser.parse_args(["tag", "/music", "--strategy", "blend", "--limit", "5"])
    assert args.command == "tag"
    assert args.path == "/music"
    assert args.strategy == "blend"
    assert args.limit == 5

    # Bandcamp subcommand
    args = parser.parse_args(["bandcamp", "https://artist.bandcamp.com", "--overwrite"])
    assert args.command == "bandcamp"
    assert args.targets == ["https://artist.bandcamp.com"]
    assert args.overwrite is True

    # Clean subcommand
    args = parser.parse_args(["clean", "/music", "-y", "-v"])
    assert args.command == "clean"
    assert args.path == "/music"
    assert args.execute is True
    assert args.verbose is True
