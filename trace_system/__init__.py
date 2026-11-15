"""Reusable local tracing SDK; no imports from the application being observed.

Events are append-only through this API. A local hash chain detects accidental
changes, not an administrator rewriting both the database and its hashes.
"""
from .core import (TraceError, Tracer, Span, get_tracer, register_secret,
                   current_context, use_context, trace, span, event, traced)

__all__ = ["TraceError", "Tracer", "Span", "get_tracer", "register_secret",
           "current_context", "use_context", "trace", "span", "event", "traced"]
