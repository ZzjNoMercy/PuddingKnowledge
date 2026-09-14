from pathlib import Path

class KnowledgeServiceError(ValueError):
    pass

def _resolve_virtual_knowledge_path(root: Path, virtual_path: str) -> Path:
    normalized = (virtual_path or "").strip()
    if not normalized:
        raise KnowledgeServiceError("File path is required.")
    if normalized.startswith("/knowledge/"):
        normalized = normalized.removeprefix("/knowledge/")
    elif normalized == "/knowledge":
        normalized = ""
    elif normalized.startswith("knowledge/"):
        normalized = normalized.removeprefix("knowledge/")
    else:
        raise KnowledgeServiceError("Only files inside the knowledge directory can be opened.")

    target = (root / normalized).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise KnowledgeServiceError("Only files inside the knowledge directory can be opened.")
    return target
