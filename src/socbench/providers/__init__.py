"""Provider adapters for the agent loop.

Re-exports the protocol surface and a lazy adapter factory. Real adapters
(openai / anthropic / gemini) import their SDKs only when actually
constructed, so `from socbench.providers import build_adapter` is cheap
even in environments where the optional SDK isn't installed.
"""
from __future__ import annotations

from socbench.providers.base import (
    Adapter,
    AdapterError,
    AdapterRequest,
    AdapterResponse,
    AdapterToolCall,
    FatalAdapterError,
    FinishReason,
    Message,
    MessageRole,
    RetryableAdapterError,
    build_adapter,
    list_known_providers,
)

__all__ = [
    "Adapter",
    "AdapterError",
    "AdapterRequest",
    "AdapterResponse",
    "AdapterToolCall",
    "FatalAdapterError",
    "FinishReason",
    "Message",
    "MessageRole",
    "RetryableAdapterError",
    "build_adapter",
    "list_known_providers",
]
