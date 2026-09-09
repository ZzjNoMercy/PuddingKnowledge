"""Optional, rebuildable gbrain projection boundary for Platform Wiki."""

from .local import LocalGbrainProjectionService
from .ports import GbrainProjectionRequest, GbrainProjectionResult, GbrainProjectionService

__all__ = [
    "GbrainProjectionRequest",
    "GbrainProjectionResult",
    "GbrainProjectionService",
    "LocalGbrainProjectionService",
]
