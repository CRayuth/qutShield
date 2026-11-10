from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaAsset:
    kind: str
    path: Path
    title: str
    platform: str
    source_url: str
    remote_url: str = ""
    photo_path: Path | None = None
