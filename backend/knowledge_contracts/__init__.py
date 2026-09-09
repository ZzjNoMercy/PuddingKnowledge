"""Framework-neutral contracts shared by Knowledge Platform adapters.

This package intentionally imports only the Python standard library.  REST,
MCP, Workspace and legacy adapters may depend on these contracts; the
contracts must never depend on an adapter, an Agent runtime, or an ORM.
"""

from .artifacts import (
    MAX_BLOB_READ_BYTES,
    BlobReadRequest,
    BlobReadResult,
    CitationCandidate,
    TraceDimension,
    TraceEvent,
    is_valid_knowledge_uri,
    validate_blob_read_result,
)
from .delegation import (
    CapabilityDescriptor,
    CapabilityInventory,
    ResearchDelegationPlan,
    ResearchDelegationRequest,
)
from .events import NotificationEvent
from .lease import LeaseCompletion, LeaseError, LeaseLostError, LeaseRecord, LeaseStatus, new_fencing_token
from .protocol import AgentProtocolEnvelope, HarnessProtocolVersion, project_legacy_payload
from .query import (
    CAPABILITIES,
    Capability,
    Correlation,
    Evidence,
    Job,
    JobStatus,
    Principal,
    Provenance,
    QueryError,
    QueryErrorCode,
    QueryPlan,
    QueryPlanValidation,
    QueryResult,
    QueryWarning,
)

__all__ = [
    "CAPABILITIES",
    "Capability",
    "Correlation",
    "Evidence",
    "Job",
    "JobStatus",
    "Principal",
    "Provenance",
    "QueryError",
    "QueryErrorCode",
    "QueryPlan",
    "QueryPlanValidation",
    "QueryResult",
    "QueryWarning",
    "NotificationEvent",
    "CapabilityDescriptor",
    "CapabilityInventory",
    "ResearchDelegationPlan",
    "ResearchDelegationRequest",
    "MAX_BLOB_READ_BYTES",
    "BlobReadRequest",
    "BlobReadResult",
    "CitationCandidate",
    "TraceDimension",
    "TraceEvent",
    "is_valid_knowledge_uri",
    "validate_blob_read_result",
    "LeaseCompletion",
    "LeaseError",
    "LeaseLostError",
    "LeaseRecord",
    "LeaseStatus",
    "new_fencing_token",
    "AgentProtocolEnvelope",
    "HarnessProtocolVersion",
    "project_legacy_payload",
]
