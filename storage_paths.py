"""Toolbox-owned storage and restart-safe migration from standalone modules."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
from contextlib import closing

from astrbot.api import logger
from astrbot.api.star import StarTools

from .runtime import LEGACY_BY_KEY, PLUGIN_NAME

_LOCK = threading.Lock()


def module_data_dir(key: str) -> Path:
    if key not in LEGACY_BY_KEY:
        raise ValueError("Unknown toolbox module")
    path = StarTools.get_data_dir(PLUGIN_NAME) / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _copy_file(source: Path, target: Path) -> bool:
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".migration.tmp")
    try:
        with source.open("rb") as stream:
            is_sqlite = stream.read(16) == b"SQLite format 3\x00"
        if is_sqlite:
            temporary.unlink(missing_ok=True)
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old:
                with closing(sqlite3.connect(temporary)) as new:
                    old.backup(new)
                    if new.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("Migrated SQLite integrity check failed")
        else:
            shutil.copy2(source, temporary)
        temporary.replace(target)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def migrate_legacy_data() -> dict:
    """Copy once before modules open their files; leave all original data intact."""
    with _LOCK:
        root = StarTools.get_data_dir(PLUGIN_NAME)
        marker = root / "migration-v1.json"
        if marker.exists():
            return json.loads(marker.read_text(encoding="utf-8"))
        copied, retained = 0, 0
        plugin_data = root.parent
        for key, legacy in LEGACY_BY_KEY.items():
            source = plugin_data / legacy
            if not source.is_dir() or source.is_symlink():
                continue
            for path in source.rglob("*"):
                if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
                    continue
                if path.name.endswith(("-wal", "-shm", "-journal")):
                    continue
                target = module_data_dir(key) / path.relative_to(source)
                if _copy_file(path, target):
                    copied += 1
                else:
                    retained += 1
        image_dir = module_data_dir("image") / "images"
        image_map = {}
        for source in (plugin_data.parent / "绘图图片", plugin_data / "绘图图片"):
            if not source.is_dir() or source.is_symlink():
                continue
            for path in source.rglob("*"):
                if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
                    continue
                target = image_dir / path.relative_to(source)
                if target.exists() and target.read_bytes() != path.read_bytes():
                    suffix = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
                    target = target.with_name(target.stem + "-" + suffix + target.suffix)
                if _copy_file(path, target):
                    copied += 1
                image_map[str(path.resolve())] = str(target.resolve())
        jobs = module_data_dir("image") / "image_jobs.json"
        if jobs.exists() and image_map:
            rows = json.loads(jobs.read_text(encoding="utf-8-sig"))
            changed = False
            for row in rows:
                original = row.get("image_files", [])
                updated = [image_map.get(str(Path(path).resolve()), path) for path in original]
                if updated != original:
                    row["image_files"] = updated
                    changed = True
            if changed:
                temporary = jobs.with_suffix(".migration.tmp")
                temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(jobs)
        observatory = module_data_dir("observatory")
        legacy_blobs = plugin_data / LEGACY_BY_KEY["observatory"] / "blobs"
        for database in (observatory / "observatory.db", observatory / "observatory.db.bak"):
            if not database.is_file():
                continue
            with closing(sqlite3.connect(database)) as db, db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='blob'").fetchone():
                    continue
                for blob_id, old_path in db.execute("SELECT blob_id, path FROM blob").fetchall():
                    if not old_path:
                        continue
                    try:
                        relative = Path(old_path).resolve().relative_to(legacy_blobs.resolve())
                    except ValueError:
                        continue
                    target = observatory / "blobs" / relative
                    if target.is_file():
                        db.execute("UPDATE blob SET path=? WHERE blob_id=?", (str(target.resolve()), blob_id))
        report = {"version": 1, "copied_files": copied, "existing_files_retained": retained,
                  "originals_retained": True, "image_paths_updated": len(image_map)}
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(marker)
        logger.info("[QQToolbox] 数据目录迁移完成：复制 %s 个文件，原目录保留。", copied)
        return report
