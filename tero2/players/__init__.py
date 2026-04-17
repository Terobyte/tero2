"""Player roles for the SORA execution cycle."""

from tero2.players.base import BasePlayer, PlayerResult
from tero2.players.scout import ScoutPlayer, ScoutResult
from tero2.players.architect import (
    ArchitectPlayer,
    ArchitectResult,
    SlicePlan,
    Task,
    _parse_slice_plan,
)
from tero2.players.builder import BuilderPlayer, BuilderResult
from tero2.players.verifier import Verdict, VerifierPlayer, VerifierResult

__all__ = [
    "BasePlayer",
    "PlayerResult",
    "ScoutPlayer",
    "ScoutResult",
    "ArchitectPlayer",
    "ArchitectResult",
    "SlicePlan",
    "Task",
    "_parse_slice_plan",
    "BuilderPlayer",
    "BuilderResult",
    "VerifierPlayer",
    "VerifierResult",
    "Verdict",
]
