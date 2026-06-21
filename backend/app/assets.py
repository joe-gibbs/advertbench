import hashlib
from pathlib import Path

from .model_config import AdSize
from .settings import get_settings


def asset_root() -> Path:
    return get_settings().data_dir / "assets"


def ensure_asset_root() -> None:
    asset_root().mkdir(parents=True, exist_ok=True)


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_asset(output_set_id: str, size: AdSize, png: bytes) -> dict[str, str]:
    ensure_asset_root()
    directory = asset_root() / output_set_id
    directory.mkdir(parents=True, exist_ok=True)
    storage_path = directory / f"{size.key}.png"
    storage_path.write_bytes(png)
    return {
        "storage_path": str(storage_path),
        "public_path": f"/api/assets/{output_set_id}/{size.key}.png",
        "checksum": checksum(png),
    }


def read_asset(storage_path: Path) -> bytes:
    resolved = storage_path.resolve()
    root = asset_root().resolve()
    if root not in resolved.parents:
        raise ValueError("Asset path escapes DATA_DIR")
    return resolved.read_bytes()
