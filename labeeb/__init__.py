"""Labeeb Orchestrator V1.

Modular architecture separating:
- Storage and file locking
- Provider adapters (Orchestrator, Jules, Claude, Git, Fakes)
- Core orchestration engine and state machine
- Headless HTTP/SSE API
- Backward-compatible CLI
"""

__version__ = "1.0.0"
