"""Ingestion and normalization package for Token Ledger.

Exports:
- normalize_observation, normalize_claude_record, normalize_omp_record, NormalizedObservationRecord
- CanonicalIdentity, CrossSourceCandidate, build_canonical_id, build_fallback_identity
- create_canonical_identity, evaluate_cross_source_identity, normalize_model_name
"""

from token_ledger.ingest.identity import (
    CanonicalIdentity,
    CrossSourceCandidate,
    build_canonical_id,
    build_fallback_identity,
    create_canonical_identity,
    evaluate_cross_source_identity,
    normalize_model_name,
)
from token_ledger.ingest.normalize import (
    NormalizedObservationRecord,
    normalize_claude_record,
    normalize_observation,
    normalize_omp_record,
    parse_timestamp,
)
from token_ledger.ingest.archive import (
    ArchiveError,
    ArchivePartitionRecord,
    CombinedPartitionSelection,
    IncompatibleReconciliationError,
    ImportSummary,
    PartitionOverlapError,
    ReconciliationProof,
    ReconciliationResult,
    UnsupportedArchiveArtifactError,
    import_archive_file,
    import_archive_partitions,
    reconcile_archive_partition,
    select_combined_partitions,
    validate_archive_artifact,
)

__all__ = [
    "CanonicalIdentity",
    "CrossSourceCandidate",
    "NormalizedObservationRecord",
    "build_canonical_id",
    "build_fallback_identity",
    "create_canonical_identity",
    "evaluate_cross_source_identity",
    "normalize_claude_record",
    "normalize_model_name",
    "normalize_observation",
    "normalize_omp_record",
    "parse_timestamp",
    "ArchiveError",
    "ArchivePartitionRecord",
    "CombinedPartitionSelection",
    "IncompatibleReconciliationError",
    "ImportSummary",
    "PartitionOverlapError",
    "ReconciliationProof",
    "ReconciliationResult",
    "UnsupportedArchiveArtifactError",
    "import_archive_file",
    "import_archive_partitions",
    "reconcile_archive_partition",
    "select_combined_partitions",
    "validate_archive_artifact",
]
