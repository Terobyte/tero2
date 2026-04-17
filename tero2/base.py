"""Base player interface for SORA roles."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from tero2.providers.chain import ProviderChain
from tero2.disk_layer import DiskLayer


@dataclass
class PlayerResult:
    """Common result shape returned by every player."""

    success: bool
    output_file: str = ""
    captured_output: str = ""
    error: str = ""


class BasePlayer(ABC):
    """Abstract base for all SORA player roles.

    Subclasses implement ``run`` which executes one phase of the SORA cycle
    (scout, architect, builder, verifier, etc.) and returns a ``PlayerResult``.
    """

    role: str = ""

    def __init__(
        self,
        chain: ProviderChain,
        disk: DiskLayer,
        *,
        working_dir: str = "",
    ) -> None:
        self.chain = chain
        self.disk = disk
        self.working_dir = working_dir

    @abstractmethod
    async def run(self, **kwargs: Any) -> PlayerResult:
        """Execute this player's phase and return a result.

        Intended calling convention (used by phase handlers):

            result = await player.run(
                chain=chain,          # ProviderChain for this role
                prompt=prompt,        # AssembledPrompt from ContextAssembler
                checkpoint=checkpoint,# CheckpointManager
                state=state,          # AgentState
                config=config,        # Config
            )

        Concrete implementations accept ``**kwargs`` for backward
        compatibility. Phase handlers must pass all five named arguments.
        """
        ...

    async def _run_prompt(self, prompt: str) -> str:
        return await self.chain.run_prompt_collected(prompt)
