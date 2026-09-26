"""Verification tests for the Route A mirrored memory subpackages across all layers."""

from __future__ import annotations


def test_domain_model_memory_exports_and_identity():
    # Canonical subpackage
    from wright.domain.model.memory import (
        CoreMemory as CanonicalCoreMemory,
        Episode as CanonicalEpisode,
        Fact as CanonicalFact,
        DEFAULT_PERSONA,
    )
    # Direct submodule
    from wright.domain.model.memory.core import CoreMemory as ModCoreMemory
    from wright.domain.model.memory.fact import Fact as ModFact
    from wright.domain.model.memory.episode import Episode as ModEpisode
    # Top-level domain.model
    from wright.domain.model import (
        CoreMemory as TopCoreMemory,
        Episode as TopEpisode,
        Fact as TopFact,
    )

    assert CanonicalCoreMemory is ModCoreMemory is TopCoreMemory
    assert CanonicalFact is ModFact is TopFact
    assert CanonicalEpisode is ModEpisode is TopEpisode
    assert len(DEFAULT_PERSONA) > 0


def test_domain_policy_memory_exports_and_identity():
    # Canonical subpackage
    from wright.domain.policy.memory import (
        CoreMemoryPolicy as CanonicalCorePolicy,
        EpisodePolicy as CanonicalEpisodePolicy,
        FactPolicy as CanonicalFactPolicy,
        MemoryPolicy as CanonicalMemoryPolicy,
        is_safe_fact as canonical_is_safe_fact,
    )
    # Direct submodule
    from wright.domain.policy.memory.core import CoreMemoryPolicy as ModCorePolicy
    from wright.domain.policy.memory.fact import FactPolicy as ModFactPolicy, is_safe_fact as mod_is_safe_fact
    from wright.domain.policy.memory.episode import EpisodePolicy as ModEpisodePolicy
    # Top-level domain.policy
    from wright.domain.policy import (
        CoreMemoryPolicy as TopCorePolicy,
        EpisodePolicy as TopEpisodePolicy,
        FactPolicy as TopFactPolicy,
        MemoryPolicy as TopMemoryPolicy,
    )

    assert CanonicalCorePolicy is ModCorePolicy is TopCorePolicy
    assert CanonicalFactPolicy is ModFactPolicy is TopFactPolicy
    assert CanonicalEpisodePolicy is ModEpisodePolicy is TopEpisodePolicy
    assert CanonicalMemoryPolicy is TopMemoryPolicy
    assert canonical_is_safe_fact is mod_is_safe_fact


def test_domain_gateway_memory_exports_and_identity():
    # Canonical subpackage
    from wright.domain.gateway.memory import (
        ICoreMemoryStore as CanonicalCorePort,
        IEpisodicMemoryStore as CanonicalEpisodePort,
        IFactRepository as CanonicalFactPort,
    )
    # Direct submodule
    from wright.domain.gateway.memory.core import ICoreMemoryStore as ModCorePort
    from wright.domain.gateway.memory.fact import IFactRepository as ModFactPort
    from wright.domain.gateway.memory.episode import IEpisodicMemoryStore as ModEpisodePort
    # Top-level domain.gateway
    from wright.domain.gateway import (
        ICoreMemoryStore as TopCorePort,
        IEpisodicMemoryStore as TopEpisodePort,
        IFactRepository as TopFactPort,
    )

    assert CanonicalCorePort is ModCorePort is TopCorePort
    assert CanonicalFactPort is ModFactPort is TopFactPort
    assert CanonicalEpisodePort is ModEpisodePort is TopEpisodePort


def test_infrastructure_persistence_memory_exports_and_identity():
    # Canonical subpackage
    from wright.infrastructure.persistence.memory import (
        CORE_MEMORY_FILE,
        EpisodeStore as CanonicalEpisodeStore,
        FileCoreMemoryStore as CanonicalCoreStore,
        FileFactRepository as CanonicalFactRepo,
        MEMORY_INDEX,
        memory_dir as canonical_memory_dir,
    )
    # Direct submodules
    from wright.infrastructure.persistence.memory.core import FileCoreMemoryStore as ModCoreStore
    from wright.infrastructure.persistence.memory.fact import FileFactRepository as ModFactRepo
    from wright.infrastructure.persistence.memory.episode import EpisodeStore as ModEpisodeStore
    from wright.infrastructure.persistence.memory.paths import memory_dir as mod_memory_dir
    # Top-level infrastructure.persistence
    from wright.infrastructure.persistence import (
        EpisodeStore as TopEpisodeStore,
        FileCoreMemoryStore as TopCoreStore,
        FileFactRepository as TopFactRepo,
        memory_dir as top_memory_dir,
    )

    assert CanonicalCoreStore is ModCoreStore is TopCoreStore
    assert CanonicalFactRepo is ModFactRepo is TopFactRepo
    assert CanonicalEpisodeStore is ModEpisodeStore is TopEpisodeStore
    assert canonical_memory_dir is mod_memory_dir is top_memory_dir
    assert CORE_MEMORY_FILE == "core_memory.json"
    assert MEMORY_INDEX == "MEMORY.md"


def test_infrastructure_tools_memory_exports_and_identity():
    # Canonical subpackage
    from wright.infrastructure.tools.memory import (
        build_core_memory_tools as canonical_build_core,
        build_episode_tools as canonical_build_episode,
        build_fact_tools as canonical_build_fact,
        build_memory_tools as canonical_build_memory,
        get_core_memory as canonical_get_core,
        update_core_memory as canonical_update_core,
    )
    # Direct submodules
    from wright.infrastructure.tools.memory.core_tools import (
        build_core_memory_tools as mod_build_core,
        get_core_memory as mod_get_core,
        update_core_memory as mod_update_core,
    )
    from wright.infrastructure.tools.memory.fact_tools import build_memory_tools as mod_build_memory
    from wright.infrastructure.tools.memory.episode_tools import build_episode_tools as mod_build_episode

    assert canonical_build_core is mod_build_core
    assert canonical_get_core is mod_get_core
    assert canonical_update_core is mod_update_core
    assert canonical_build_memory is mod_build_memory is canonical_build_fact
    assert canonical_build_episode is mod_build_episode
