# src/utils.py
import hashlib
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("apmm")


def compute_sha256(image_path: str) -> str:
    """Hash raw bytes BEFORE any resize or re-encode."""
    with open(image_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def make_case_dir(base: str, case_id: str) -> Path:
    path = Path(base) / case_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_snapshot_dir(case_dir: Path, item_id: str) -> Path:
    path = case_dir / "snapshots" / item_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(data: dict, path: Path) -> None:
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    log.info(f"Saved: {path}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
