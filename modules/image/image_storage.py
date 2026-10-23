"""Keep drawing inputs and outputs in one persistent, flat image directory."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil


def archive_image(source: str | Path, directory: str | Path, role: str = "群友") -> str:
    source = Path(source).resolve(strict=True)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if source.parent == directory:
        return str(source)
    with source.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    target = directory / f"{role}_{digest}{source.suffix.lower()}"
    if target.exists():
        with target.open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != digest:
                raise ValueError("图片归档文件内容不一致，请检查保存目录。")
    else:
        shutil.copy2(source, target)
    return str(target)
