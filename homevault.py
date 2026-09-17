#!/usr/bin/env python3
"""HomeVault: a small, auditable, content-addressed household backup tool."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

CHUNK_SIZE = 1024 * 1024
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
    digest TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    digest TEXT NOT NULL REFERENCES objects(digest),
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_files_digest ON files(digest);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def snapshot_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class Vault:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.objects = self.root / "objects" / "sha256"
        self.db_path = self.root / "index.sqlite3"

    def initialize(self) -> None:
        self.objects.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        if not self.db_path.exists() and not self.root.exists():
            raise RuntimeError(f"vault does not exist: {self.root}")
        db = sqlite3.connect(self.db_path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest[2:]

    def store_object(self, source: Path) -> tuple[str, int, bool]:
        hasher = hashlib.sha256()
        size = 0
        self.objects.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".incoming-", dir=self.objects)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as src:
                while chunk := src.read(CHUNK_SIZE):
                    hasher.update(chunk)
                    target.write(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())

            digest = hasher.hexdigest()
            final_path = self.object_path(digest)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if final_path.exists():
                temp_path.unlink()
                return digest, size, False
            os.replace(temp_path, final_path)
            return digest, size, True
        finally:
            temp_path.unlink(missing_ok=True)

    def backup(self, source: Path) -> dict[str, int | str]:
        source = source.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError("source must be a directory")
        if self.root == source or self.root.is_relative_to(source):
            raise ValueError("vault must not be located inside the source directory")

        self.initialize()
        sid = snapshot_id()
        file_count = total_bytes = new_objects = skipped_links = 0
        rows: list[tuple[str, str, int, int]] = []

        for current, dirs, files in os.walk(source, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
            for name in sorted(files):
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    skipped_links += 1
                    continue
                digest, size, created = self.store_object(path)
                stat = path.stat()
                rel = path.relative_to(source).as_posix()
                rows.append((rel, digest, size, stat.st_mtime_ns))
                file_count += 1
                total_bytes += size
                new_objects += int(created)

        with self.connect() as db:
            db.execute(
                "INSERT INTO snapshots(id, created_at, source) VALUES (?, ?, ?)",
                (sid, utc_now(), str(source)),
            )
            for rel, digest, size, mtime_ns in rows:
                db.execute(
                    "INSERT OR IGNORE INTO objects(digest, size, created_at) VALUES (?, ?, ?)",
                    (digest, size, utc_now()),
                )
                db.execute(
                    "INSERT INTO files(snapshot_id, relative_path, digest, size, mtime_ns) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sid, rel, digest, size, mtime_ns),
                )
        return {
            "snapshot": sid,
            "files": file_count,
            "bytes": total_bytes,
            "new_objects": new_objects,
            "skipped_links": skipped_links,
        }

    def verify(self) -> dict[str, int]:
        checked = missing = corrupt = 0
        with self.connect() as db:
            objects = db.execute("SELECT digest, size FROM objects ORDER BY digest").fetchall()
        for digest, expected_size in objects:
            path = self.object_path(digest)
            if not path.exists():
                missing += 1
                continue
            hasher = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while chunk := handle.read(CHUNK_SIZE):
                    hasher.update(chunk)
                    size += len(chunk)
            checked += 1
            if size != expected_size or hasher.hexdigest() != digest:
                corrupt += 1
        return {"checked": checked, "missing": missing, "corrupt": corrupt}

    def restore(self, sid: str, destination: Path, overwrite: bool = False) -> int:
        destination = destination.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            rows = db.execute(
                "SELECT relative_path, digest, mtime_ns FROM files "
                "WHERE snapshot_id = ? ORDER BY relative_path",
                (sid,),
            ).fetchall()
        if not rows:
            raise ValueError(f"snapshot not found or empty: {sid}")

        restored = 0
        for rel, digest, mtime_ns in rows:
            pure = PurePosixPath(rel)
            if pure.is_absolute() or ".." in pure.parts:
                raise RuntimeError(f"unsafe path in index: {rel}")
            target = destination.joinpath(*pure.parts)
            if target.exists() and not overwrite:
                raise FileExistsError(f"refusing to overwrite: {target}")
            source = self.object_path(digest)
            if not source.exists():
                raise FileNotFoundError(f"missing object: {digest}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.utime(target, ns=(mtime_ns, mtime_ns))
            restored += 1
        return restored

    def snapshots(self) -> list[tuple[str, str, str, int]]:
        with self.connect() as db:
            return db.execute(
                "SELECT s.id, s.created_at, s.source, COUNT(f.relative_path) "
                "FROM snapshots s LEFT JOIN files f ON f.snapshot_id = s.id "
                "GROUP BY s.id ORDER BY s.created_at DESC"
            ).fetchall()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Content-addressed household backup")
    parser.add_argument("--vault", type=Path, required=True, help="vault directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    backup = sub.add_parser("backup")
    backup.add_argument("source", type=Path)
    sub.add_parser("verify")
    sub.add_parser("list")
    restore = sub.add_parser("restore")
    restore.add_argument("snapshot")
    restore.add_argument("destination", type=Path)
    restore.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vault = Vault(args.vault)
    try:
        if args.command == "init":
            vault.initialize()
            print(f"initialized: {vault.root}")
        elif args.command == "backup":
            print(vault.backup(args.source))
        elif args.command == "verify":
            result = vault.verify()
            print(result)
            return 2 if result["missing"] or result["corrupt"] else 0
        elif args.command == "list":
            for row in vault.snapshots():
                print("\t".join(map(str, row)))
        elif args.command == "restore":
            print({"restored": vault.restore(args.snapshot, args.destination, args.overwrite)})
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"homevault: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
