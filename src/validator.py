# src/validator.py
from utils import log

BANNED_FIELDS = {"content_score", "crossmodal_score", "provenance_score"}


def validate_cascade(record: dict) -> tuple[bool, list[str]]:
    errors  = []
    root_id  = record.get("root_id")
    items    = record.get("items", [])
    edges    = record.get("edges", [])
    item_ids = {i.get("id") for i in items}

    if not root_id:
        errors.append("root_id is missing.")
    elif root_id not in item_ids:
        errors.append(f"root_id '{root_id}' not found in items.")

    for item in items:
        if not item.get("id"):
            errors.append("Item missing id.")
        bad = set(item.keys()) & BANNED_FIELDS
        if bad:
            errors.append(f"Item '{item.get('id')}' has banned fields: {bad}")

    for edge in edges:
        if len(edge) != 2:
            errors.append(f"Malformed edge: {edge}")
            continue
        p, c = edge
        if p not in item_ids:
            errors.append(f"Edge parent '{p}' not in items.")
        if c not in item_ids:
            errors.append(f"Edge child '{c}' not in items.")

    is_valid = len(errors) == 0

    if errors:
        for e in errors:
            log.error(f"  ✗ {e}")
    else:
        log.info("Cascade validation PASSED.")

    if len(items) <= 1 or not edges:
        log.warning("Degenerate cascade — graph_builder returns None (correct).")

    return is_valid, errors


def validate_and_exit_on_failure(record: dict) -> None:
    is_valid, errors = validate_cascade(record)
    if not is_valid:
        raise ValueError(
            "Cascade invalid:\n" + "\n".join(f"  - {e}" for e in errors)
        )
