"""Backward-compatibility shim. Use wright.core.logger instead."""
from .core.logger import get_logger

__all__ = ["get_logger"]
