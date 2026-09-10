"""Provider-neutral document parser result contracts."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedMedia:
    relative_path: str
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class ParsedDocument:
    markdown: bytes
    assets: tuple[ParsedMedia, ...]
    parser_id: str
    version: str
