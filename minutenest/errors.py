"""Shared exception types (tiny module so any layer can import it)."""

from __future__ import annotations


class CancelledError(RuntimeError):
    """A running job (or its model download) was cancelled by the user."""
