"""Assistant tool surface and proposal queue (DESIGN_SYSTEM_V2.md §3).

The agent never writes business data. Read-only tools project governed state;
proposal tools park a structured proposal in :class:`ProposalStore`, and only
the owner's explicit apply — with their device id and idempotency key — turns
a proposal into a workspace write. Forbidden capabilities (sending mail,
issuing or closing tasks, rotating tokens, widening permissions) simply do not
exist in this tool surface.
"""

from .proposals import ProposalStore
from .tools import build_assistant_tools

__all__ = ["ProposalStore", "build_assistant_tools"]
