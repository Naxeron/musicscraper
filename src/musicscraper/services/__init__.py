"""
High-level domain workflows and service orchestrators.
"""

from musicscraper.services.reconciler import DiscographyReconciler, deduplicate_candidate_tracks
from musicscraper.services.auditor import AuditorService, AudioFileScanner
from musicscraper.services.cleaner import FolderCleanerService
from musicscraper.services.tagger import GenreTaggerService
from musicscraper.services.soulseek import SlskdArtistScraper, PeerCandidateIndex, CandidateDir, CandidateFile
from musicscraper.services.quality import LocalLibraryQualityScanner, SoulseekQualityUpgrader, QualityUpgradeCandidate
from musicscraper.services.artist import ArtistDownloadOrchestrator
from musicscraper.services.library import LibraryReleaseService

__all__ = [
    "DiscographyReconciler",
    "deduplicate_candidate_tracks",
    "AuditorService",
    "AudioFileScanner",
    "FolderCleanerService",
    "GenreTaggerService",
    "SlskdArtistScraper",
    "PeerCandidateIndex",
    "CandidateDir",
    "CandidateFile",
    "LocalLibraryQualityScanner",
    "SoulseekQualityUpgrader",
    "QualityUpgradeCandidate",
    "ArtistDownloadOrchestrator",
    "LibraryReleaseService",
]

