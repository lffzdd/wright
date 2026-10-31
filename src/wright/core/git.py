"""Git invocations for commands Wright runs outside the Shell sandbox.

A sandboxed command can write to a repository, so repository-local config and
hooks are untrusted input: ``core.fsmonitor``, filters and similar keys would
otherwise run code on the host. User and system config stay trusted.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_FIXED_OVERRIDES = (
    "core.fsmonitor=false",
    f"core.hooksPath={os.devnull}",
    "protocol.ext.allow=never",
)
# Only these local keys keep their value. Everything else, including keys that
# are added to Git later, is neutralized, so this list must never name a key
# whose value Git executes.
_SAFE_LOCAL_KEY = re.compile(
    r"core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase"
    r"|precomposeunicode|symlinks|autocrlf|eol|safecrlf|abbrev|quotepath"
    r"|sparsecheckout|sparsecheckoutcone|untrackedcache|splitindex|worktree"
    r"|sharedrepository|commitgraph|multipackindex|preloadindex|fscache"
    r"|checkstat|trustctime)"
    r"|(branch|user|extensions|init|pack|index|feature|fetch|pull|push"
    r"|rebase|status|color|log|commit|tag)\..*"
    r"|remote\.[^=]*\.(url|pushurl|fetch|push|mirror|prune|tagopt)"
    r"|merge\.(ff|conflictstyle)|gc\.auto"
    # --includes lists the included keys on their own, and -c cannot unset these.
    r"|include\.path|includeif\..*",
    re.IGNORECASE,
)
_LOCAL_SCOPES = frozenset({"local", "worktree"})


def git_command(root: Path | str, *args: str, flags: tuple[str, ...] = ()) -> list[str]:
    """Build argv for running Git on the host against an untrusted repository."""
    overrides = (*_FIXED_OVERRIDES, *(f"{key}=" for key in _untrusted_local_keys(root)))
    return ["git", *flags, "-C", str(root), *(item for value in overrides for item in ("-c", value)), *args]


def _untrusted_local_keys(root: Path | str) -> tuple[str, ...]:
    # Listing config parses it without running any of it. On failure the real
    # command meets the same broken or missing repository and stops there.
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--includes", "--show-scope", "--name-only", "--list", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return ()
    if result.returncode != 0:
        return ()
    # With -z and --name-only, each entry is a scope and a name, NUL-separated.
    fields = result.stdout.decode("utf-8", errors="surrogateescape").replace("\t", "\0").split("\0")
    keys = dict.fromkeys(
        key for scope, key in zip(fields[::2], fields[1::2], strict=False)
        if scope in _LOCAL_SCOPES and not _SAFE_LOCAL_KEY.fullmatch(key)
    )
    return tuple(keys)
