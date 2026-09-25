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
    # Facades
    from wright.domain.model.core_memory import CoreMemory as FacadeCoreMemory
    from wright.domain.model.fact import Fact as FacadeFact
    from wright.domain.model.episode import Episode as FacadeEpisode
    # Top-level domain.model
    from wright.domain.model import (
        CoreMemory as TopCoreMemory,
        Episode as TopEpisode,
        Fact as TopFact,
    )

    assert CanonicalCoreMemory is ModCoreMemory is FacadeCoreMemory is TopCoreMemory
    assert CanonicalFact is ModFact is FacadeFact is TopFact
    assert CanonicalEpisode is ModEpisode is FacadeEpisode is TopEpisode
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
    # Facades
    from wright.domain.policy.core_memory_policy import CoreMemoryPolicy as FacadeCorePolicy
    from wright.domain.policy.fact_policy import FactPolicy as FacadeFactPolicy, is_safe_fact as facade_is_safe_fact
    from wright.domain.policy.episode_policy import EpisodePolicy as FacadeEpisodePolicy
    from wright.domain.policy.memory import MemoryPolicy as FacadeMemoryPolicy
    # Top-level domain.policy
    from wright.domain.policy import (
        CoreMemoryPolicy as TopCorePolicy,
        EpisodePolicy as TopEpisodePolicy,
        FactPolicy as TopFactPolicy,
        MemoryPolicy as TopMemoryPolicy,
    )

    assert CanonicalCorePolicy is ModCorePolicy is FacadeCorePolicy is TopCorePolicy
    assert CanonicalFactPolicy is ModFactPolicy is FacadeFactPolicy is TopFactPolicy
    assert CanonicalEpisodePolicy is ModEpisodePolicy is FacadeEpisodePolicy is TopEpisodePolicy
    assert CanonicalMemoryPolicy is FacadeMemoryPolicy is TopMemoryPolicy
    assert canonical_is_safe_fact is mod_is_safe_fact is facade_is_safe_fact


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
    # Facades
    from wright.domain.gateway.core_memory_gateway import ICoreMemoryStore as FacadeCorePort
    from wright.domain.gateway.fact_gateway import IFactRepository as FacadeFactPort
    from wright.domain.gateway.episode_gateway import IEpisodicMemoryStore as FacadeEpisodePort
    # Top-level domain.gateway
    from wright.domain.gateway import (
        ICoreMemoryStore as TopCorePort,
        IEpisodicMemoryStore as TopEpisodePort,
        IFactRepository as TopFactPort,
    )

    assert CanonicalCorePort is ModCorePort is FacadeCorePort is TopCorePort
    assert CanonicalFactPort is ModFactPort is FacadeFactPort is TopFactPort
    assert CanonicalEpisodePort is ModEpisodePort is FacadeEpisodePort is TopEpisodePort


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
    # Facades
    from wright.infrastructure.persistence.file_core_memory import FileCoreMemoryStore as FacadeCoreStore
    from wright.infrastructure.persistence.memory_store import FileFactRepository as FacadeFactRepo
    from wright.infrastructure.persistence.episode_store import EpisodeStore as FacadeEpisodeStore
    from wright.infrastructure.persistence.memory_paths import memory_dir as facade_memory_dir
    # Top-level infrastructure.persistence
    from wright.infrastructure.persistence import (
        EpisodeStore as TopEpisodeStore,
        FileCoreMemoryStore as TopCoreStore,
        FileFactRepository as TopFactRepo,
        memory_dir as top_memory_dir,
    )

    assert CanonicalCoreStore is FacadeCoreStore is TopCoreStore
    assert CanonicalFactRepo is FacadeFactRepo is TopFactRepo
    assert CanonicalEpisodeStore is FacadeEpisodeStore is TopEpisodeStore
    assert canonical_memory_dir is facade_memory_dir is top_memory_dir
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
    # Facades
    from wright.infrastructure.tools.core_memory_tools import (
        build_core_memory_tools as facade_build_core,
        get_core_memory as facade_get_core,
        update_core_memory as facade_update_core,
    )
    from wright.infrastructure.tools.memory_tools import build_memory_tools as facade_build_memory
    from wright.infrastructure.tools.episode_tools import build_episode_tools as facade_build_episode

    assert canonical_build_core is facade_build_core
    assert canonical_get_core is facade_get_core
    assert canonical_update_core is facade_update_core
    assert canonical_build_memory is facade_build_memory is canonical_build_fact
    assert canonical_build_episode is facade_build_episode
