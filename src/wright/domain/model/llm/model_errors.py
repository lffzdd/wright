"""Provider-neutral model error text and transport choice."""

from __future__ import annotations

from urllib.parse import urlparse


def describe_model_error(exception: BaseException) -> str:
    """One readable line for a failed model request, safe to show to people.

    SDK errors carry a dict body whose repr is noisy; prefer the provider's
    own message and the HTTP status when they exist.
    """
    status_code = getattr(exception, "status_code", None)
    if status_code is not None:
        message = ""
        body = getattr(exception, "body", None)
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                message = str(error.get("message") or "")
            elif isinstance(error, str):
                message = error
        message = message or str(getattr(exception, "message", "") or exception)
        return f"Model request failed (HTTP {status_code}): {message}"[:2_000]
    if any(cls.__name__ == "APIConnectionError" for cls in type(exception).__mro__):
        return f"Could not reach the model endpoint: {exception}"[:2_000]
    text = str(exception) or type(exception).__name__
    return f"{type(exception).__name__}: {text}"[:2_000]


def resolve_transport(base_url: str, requested: str | None) -> str:
    """Resolve automatic transport without speculative duplicate API calls."""
    choice = (requested or "auto").strip().lower()
    if choice not in {"auto", "chat", "responses"}:
        raise ValueError("WRIGHT_LLM_TRANSPORT must be auto, chat, or responses")
    if choice != "auto":
        return choice
    return "responses" if urlparse(base_url).hostname == "api.openai.com" else "chat"
