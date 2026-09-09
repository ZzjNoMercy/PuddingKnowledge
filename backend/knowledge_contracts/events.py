"""Framework-neutral domain-event contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from ._immutables import freeze_mapping, thaw_value

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SAFE_TEXT = re.compile(
    r"(?:https?://|file:|[A-Za-z]:[\\/]|\\\\|(?:^|[\s(])/(?:[^\s]+)|(?:^|[\s(])~/|"
    r"(?:^|[\s(])[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+(?:[\s;,) ]|$)|"
    r"(?:^|[\s(])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:[/?#][^\s]*)?|"
    r"(?:session|query)_id\s*=|(?:password|api[_ -]?key|secret|token|authorization|cookie|private[_ -]?key|path)\s*[:=]|"
    r"(?:sk_live|AKIA)[A-Za-z0-9_-]*|\bbearer\s+)",
    re.IGNORECASE,
)
_PAYLOAD_KEYS = frozenset({"job_id", "dimension_id", "status", "session_id_digest", "query_id_digest"})
_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_for_publish_confirmation",
        "waiting_for_baseline_change_confirmation",
        "published",
        "failed",
        "cancelled",
    }
)


def _validate_payload(payload: Mapping[str, object]) -> None:
    unknown = sorted(set(str(key) for key in payload) - _PAYLOAD_KEYS)
    if unknown:
        raise ValueError(f"NotificationEvent payload contains unsupported keys: {unknown}")
    for key, value in payload.items():
        key = str(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"NotificationEvent payload {key} must be a non-empty string")
        if key in {"session_id_digest", "query_id_digest"}:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise ValueError(f"NotificationEvent payload {key} must be a digest")
        elif key == "status":
            if value not in _STATUSES:
                raise ValueError("NotificationEvent payload status is unsupported")
        elif not _SAFE_ID.fullmatch(value) or re.search(
            r"(?:secret|token|password|session|query|sk_live|akia|bearer)", value, re.IGNORECASE
        ):
            raise ValueError(f"NotificationEvent payload {key} is unsafe")


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """A Platform notification event consumed by a product-owned inbox."""

    event_id: str
    event_type: str
    subject_type: str
    subject_id: str
    title: str
    body: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)
    occurred_at: str = ""

    def __post_init__(self) -> None:
        for field_name in ("event_id", "event_type", "subject_type", "subject_id", "title", "occurred_at"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"NotificationEvent.{field_name} must not be empty")
        if not _SAFE_ID.fullmatch(self.event_id) or len(self.event_id) > 160:
            raise ValueError("NotificationEvent.event_id is unsafe or too long")
        for field_name, max_length in (("event_type", 100), ("subject_type", 120), ("subject_id", 160)):
            value = getattr(self, field_name)
            if len(value) > max_length or not _SAFE_ID.fullmatch(value):
                raise ValueError(f"NotificationEvent.{field_name} is unsafe or too long")
        if len(self.title) > 300 or len(self.body) > 4000:
            raise ValueError("NotificationEvent display text is too long")
        if _SAFE_TEXT.search(self.title) or _SAFE_TEXT.search(self.body):
            raise ValueError("NotificationEvent display text contains a non-portable reference")
        if not isinstance(self.payload, Mapping):
            raise ValueError("NotificationEvent.payload must be an object")
        _validate_payload(self.payload)
        object.__setattr__(self, "payload", freeze_mapping(self.payload))

    def to_dict(self) -> dict[str, object]:
        return thaw_value(asdict(self))
