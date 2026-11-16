"""Common validation for saved, archived and worker-consumed revision inputs."""
from rag_modules.api.dto.document import DocumentSegmentUpdate


def validate_revision_segments(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("Revision items must be a list.")
    for item in value["items"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not 1 <= len(item["id"]) <= 255 or type(item.get("position")) is not int
                or item["position"] < 0):
            raise ValueError("Revision segment identifier or position is invalid.")
    payload = DocumentSegmentUpdate(segments=value["items"])
    items = [item.model_dump(mode="json") for item in payload.segments]
    seen = {}
    for item in items:
        if item["id"] in seen or not item["content"].strip():
            raise ValueError("Revision segments must have unique IDs and nonblank content.")
        if item["index_type"] not in {"general", "parent", "child"}:
            raise ValueError("Revision segment type is invalid.")
        parent = item["parent_id"]
        if item["index_type"] == "child":
            if parent not in seen or seen[parent] != "parent":
                raise ValueError("A child must reference an earlier parent segment.")
        elif parent is not None:
            raise ValueError("Only child segments may reference a parent.")
        seen[item["id"]] = item["index_type"]
    types = set(seen.values())
    if "general" in types and types != {"general"}:
        raise ValueError("Revision cannot mix general and parent-child segments.")
    if "parent" in types and "child" not in types:
        raise ValueError("Parent-child revisions require a child segment.")
    return {"items": items}
