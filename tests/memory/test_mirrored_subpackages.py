"""Verification tests for the Route A mirrored memory subpackages across all layers."""

from __future__ import annotations


def test_domain_model_memory_exports_and_identity():
    import wright.domain.model as model_package

    assert not hasattr(model_package, "CoreMemory")
    assert not hasattr(model_package, "EpisodeRecord")
    assert not hasattr(model_package, "SemanticMemoryRecord")
    from wright.domain.model.memory import (
        DEFAULT_PERSONA,
    )
    from wright.domain.model.memory import (
        CoreMemory as CanonicalCoreMemory,
    )
    from wright.domain.model.memory import (
        EpisodeRecord as CanonicalEpisodeRecord,
    )
    from wright.domain.model.memory import (
        SemanticMemoryRecord as CanonicalMemoryRecord,
    )

    # Direct submodule
    from wright.domain.model.memory.core import CoreMemory as ModCoreMemory
    from wright.domain.model.memory.episode import EpisodeRecord as ModEpisodeRecord
    from wright.domain.model.memory.semantic import (
        SemanticMemoryRecord as ModMemoryRecord,
    )

    assert CanonicalCoreMemory is ModCoreMemory
    assert CanonicalMemoryRecord is ModMemoryRecord
    assert CanonicalEpisodeRecord is ModEpisodeRecord
    assert len(DEFAULT_PERSONA) > 0


def test_domain_policy_memory_exports_and_identity():
    # Canonical subpackage
    # Top-level domain.policy
    from wright.domain.policy import (
        CoreMemoryPolicy as TopCorePolicy,
    )
    from wright.domain.policy import (
        EpisodePolicy as TopEpisodePolicy,
    )
    from wright.domain.policy import (
        MemoryPolicy as TopMemoryPolicy,
    )
    from wright.domain.policy import (
        SemanticMemoryPolicy as TopSemanticPolicy,
    )
    from wright.domain.policy.memory import (
        CoreMemoryPolicy as CanonicalCorePolicy,
    )
    from wright.domain.policy.memory import (
        EpisodePolicy as CanonicalEpisodePolicy,
    )
    from wright.domain.policy.memory import (
        MemoryPolicy as CanonicalMemoryPolicy,
    )
    from wright.domain.policy.memory import (
        SemanticMemoryPolicy as CanonicalSemanticPolicy,
    )
    from wright.domain.policy.memory import (
        is_safe_memory as canonical_is_safe_memory,
    )

    # Direct submodule
    from wright.domain.policy.memory.core import CoreMemoryPolicy as ModCorePolicy
    from wright.domain.policy.memory.episode import EpisodePolicy as ModEpisodePolicy
    from wright.domain.policy.memory.semantic import (
        SemanticMemoryPolicy as ModSemanticPolicy,
    )
    from wright.domain.policy.memory.semantic import (
        is_safe_memory as mod_is_safe_memory,
    )

    assert CanonicalCorePolicy is ModCorePolicy is TopCorePolicy
    assert CanonicalSemanticPolicy is ModSemanticPolicy is TopSemanticPolicy
    assert CanonicalEpisodePolicy is ModEpisodePolicy is TopEpisodePolicy
    assert CanonicalMemoryPolicy is TopMemoryPolicy
    assert canonical_is_safe_memory is mod_is_safe_memory


def test_domain_gateway_memory_exports_and_identity():
    # Canonical subpackage
    # Top-level domain.gateway
    from wright.domain.gateway import (
        ICoreMemoryStore as TopCorePort,
    )
    from wright.domain.gateway import (
        IEpisodeStore as TopEpisodePort,
    )
    from wright.domain.gateway import (
        ISemanticMemoryStore as TopSemanticPort,
    )
    from wright.domain.gateway.memory import (
        ICoreMemoryStore as CanonicalCorePort,
    )
    from wright.domain.gateway.memory import (
        IEpisodeStore as CanonicalEpisodePort,
    )
    from wright.domain.gateway.memory import (
        ISemanticMemoryStore as CanonicalSemanticPort,
    )

    # Direct submodule
    from wright.domain.gateway.memory.core import ICoreMemoryStore as ModCorePort
    from wright.domain.gateway.memory.episode import (
        IEpisodeStore as ModEpisodePort,
    )
    from wright.domain.gateway.memory.semantic import (
        ISemanticMemoryStore as ModSemanticPort,
    )

    assert CanonicalCorePort is ModCorePort is TopCorePort
    assert CanonicalSemanticPort is ModSemanticPort is TopSemanticPort
    assert CanonicalEpisodePort is ModEpisodePort is TopEpisodePort


def test_infrastructure_persistence_memory_exports_and_identity():
    # Canonical subpackage
    # Top-level infrastructure.persistence
    from wright.infrastructure.persistence import (
        EpisodeStore as TopEpisodeStore,
    )
    from wright.infrastructure.persistence import (
        FileCoreMemoryStore as TopCoreStore,
    )
    from wright.infrastructure.persistence import (
        SemanticMemoryStore as TopSemanticStore,
    )
    from wright.infrastructure.persistence import (
        memory_dir as top_memory_dir,
    )
    from wright.infrastructure.persistence.memory import (
        CORE_MEMORY_FILE,
        MEMORY_INDEX,
    )
    from wright.infrastructure.persistence.memory import (
        EpisodeStore as CanonicalEpisodeStore,
    )
    from wright.infrastructure.persistence.memory import (
        FileCoreMemoryStore as CanonicalCoreStore,
    )
    from wright.infrastructure.persistence.memory import (
        SemanticMemoryStore as CanonicalSemanticStore,
    )
    from wright.infrastructure.persistence.memory import (
        memory_dir as canonical_memory_dir,
    )

    # Direct submodules
    from wright.infrastructure.persistence.memory.core import (
        FileCoreMemoryStore as ModCoreStore,
    )
    from wright.infrastructure.persistence.memory.episode import (
        EpisodeStore as ModEpisodeStore,
    )
    from wright.infrastructure.persistence.memory.paths import (
        memory_dir as mod_memory_dir,
    )
    from wright.infrastructure.persistence.memory.semantic import (
        SemanticMemoryStore as ModSemanticStore,
    )

    assert CanonicalCoreStore is ModCoreStore is TopCoreStore
    assert CanonicalSemanticStore is ModSemanticStore is TopSemanticStore
    assert CanonicalEpisodeStore is ModEpisodeStore is TopEpisodeStore
    assert canonical_memory_dir is mod_memory_dir is top_memory_dir
    assert CORE_MEMORY_FILE == "core_memory.json"
    assert MEMORY_INDEX == "MEMORY.md"


def test_infrastructure_tools_memory_exports_and_identity():
    # Canonical subpackage
    from wright.infrastructure.tools.memory import (
        build_core_memory_tools as canonical_build_core,
    )
    from wright.infrastructure.tools.memory import (
        build_episode_tools as canonical_build_episode,
    )
    from wright.infrastructure.tools.memory import (
        build_memory_tools as canonical_build_memory,
    )
    from wright.infrastructure.tools.memory import (
        get_core_memory as canonical_get_core,
    )
    from wright.infrastructure.tools.memory import (
        update_core_memory as canonical_update_core,
    )

    # Direct submodules
    from wright.infrastructure.tools.memory.core_tools import (
        build_core_memory_tools as mod_build_core,
    )
    from wright.infrastructure.tools.memory.core_tools import (
        get_core_memory as mod_get_core,
    )
    from wright.infrastructure.tools.memory.core_tools import (
        update_core_memory as mod_update_core,
    )
    from wright.infrastructure.tools.memory.episode_tools import (
        build_episode_tools as mod_build_episode,
    )
    from wright.infrastructure.tools.memory.semantic_tools import (
        build_memory_tools as mod_build_memory,
    )

    assert canonical_build_core is mod_build_core
    assert canonical_get_core is mod_get_core
    assert canonical_update_core is mod_update_core
    assert canonical_build_memory is mod_build_memory
    assert canonical_build_episode is mod_build_episode
