"""Backward compatibility shim for session_models.

Model selection logic has moved to infrastructure.llm.model_adapters.
"""

from ..infrastructure.llm.model_adapters import available_models, process_model_name

__all__ = ["available_models", "process_model_name"]
