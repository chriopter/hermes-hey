"""Hermes standalone plugin entry point."""

try:  # Hermes filesystem plugin namespace
    from .hey_platform.plugin import register
except ImportError:  # Direct source checkout / pytest collection
    from hey_platform.plugin import register

__all__ = ["register"]
