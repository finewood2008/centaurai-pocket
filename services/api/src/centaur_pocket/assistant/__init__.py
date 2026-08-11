"""Assistant tool surface and proposal queue (DESIGN_SYSTEM_V2.md §3).

The agent never writes business data. Read-only tools project governed state;
proposal tools park a structured proposal in :class:`ProposalStore`, and only
the owner's explicit apply — with their device id and idempotency key — turns
a proposal into a workspace write. Forbidden capabilities (sending mail,
issuing or closing tasks, rotating tokens, widening permissions) simply do not
exist in this tool surface.

§3.4 adds the orchestration half: model providers run server-side
(:mod:`provider`), the loop enforces 6 rounds / 30 s / 64 KiB
(:mod:`loop`), and cloud calls require a one-time owner ticket
(:mod:`tickets`).
"""

from .loop import AssistantLoop
from .proposals import ProposalStore
from .provider import (
    ProviderError,
    build_cloud_provider,
    build_local_provider,
)
from .tickets import CloudTicketStore
from .tools import build_assistant_tools

__all__ = [
    "AssistantLoop",
    "CloudTicketStore",
    "ProposalStore",
    "ProviderError",
    "build_assistant_tools",
    "build_cloud_provider",
    "build_local_provider",
]
