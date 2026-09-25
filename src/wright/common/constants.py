"""Common system-wide constants and defaults."""

from __future__ import annotations

APP_NAME = "wright"
APP_VERSION = "0.1.0"

# LLM defaults
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_CONTEXT_LIMIT = 128_000
DEFAULT_TEMPERATURE = 0.0

# Performance & Token estimation
CHARS_PER_TOKEN = 4
DEFAULT_TIMEOUT = 60.0
DEFAULT_POLL_INTERVAL = 0.5

# Step limits
DEFAULT_MAX_STEPS = 50
MAX_TOOL_CALLS_PER_TURN = 20
