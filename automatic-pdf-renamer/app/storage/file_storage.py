"""File Storage Backend for testing.

Local filesystem implementation for development and testing purposes.
Mimics S3 behavior using a base directory.
"""

import logging
import os
from pathlib import Path
from typing import Any

from . import StorageBackend

logger = logging.getLogger(__name__)


class FileStorage(StorageBackend):
    """Filesystem-based storage backend for testing.

    Uses a base directory to store objects as files.
    Keys are mapped to filesystem paths relative to the base directory.
    """

    def __init__(self, base_dir: str = "./storage"):
        """Initialize file storage backend.

        Args:
            base_dir: Base directory for storing objects.
        """
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"FileStorage initialized at {self.base_dir}")

    def _make_path(self, key: str) -> Path:
        """Convert a storage key to a filesystem path.

        Args:
            key: The object key/path (may contain forward slashes).

        Returns:
            Absolute Path for the object file.
        """
        # Normalize key: remove leading slash, replace slashes with OS separator
        normalized = key.lstrip("/")
        if os.sep != "/":
            normalized = normalized.replace("/", os.sep)
        return self.base_dir / normalized

    def _ensure_parent_dir(self, path: Path) -> None:
        """Ensure the parent directory of a path exists."""
        path.parent.mkdir(parents=True, exist_ok=True)

    def put_object(self, key: str, body: str | bytes) -> None:
        """Upload an object to the filesystem.

        Args:
            key: The object key/path.
            body: The object content as string or bytes.
        """
        path = self._make_path(key)
        self._ensure_parent_dir(path)

        if isinstance(body, str):
            body = body.encode("utf-8")

        with open(path, "wb") as f:
            f.write(body)
        logger.debug(f"Uploaded {key} to {path}")

    def get_object(self, key: str) -> str | bytes | None:
        """Retrieve an object from the filesystem.

        Args:
            key: The object key/path.

        Returns:
            The object content as bytes, or None if not found.
        """
        path = self._make_path(key)

        if not path.exists():
            logger.debug(f"Object {key} not found at {path}")
            return None

        with open(path, "rb") as f:
            return f.read()

    def delete_object(self, key: str) -> None:
        """Delete an object from the filesystem.

        Args:
            key: The object key/path.
        """
        path = self._make_path(key)

        if path.exists():
            path.unlink()
            logger.debug(f"Deleted {key} from {path}")
        else:
            logger.debug(f"Object {key} not found at {path}, nothing to delete")

    def copy_object(self, src_key: str, dst_key: str) -> None:
        """Copy an object within the filesystem.

        Args:
            src_key: Source object key/path.
            dst_key: Destination object key/path.
        """
        src_path = self._make_path(src_key)
        dst_path = self._make_path(dst_key)

        self._ensure_parent_dir(dst_path)

        if not src_path.exists():
            raise FileNotFoundError(f"Source object {src_key} not found at {src_path}")

        # Use shutil.copy2 to preserve metadata
        import shutil
        shutil.copy2(str(src_path), str(dst_path))
        logger.debug(f"Copied {src_key} to {dst_key}")

    def object_exists(self, key: str) -> bool:
        """Check if an object exists in the filesystem.

        Args:
            key: The object key/path.

        Returns:
            True if the object exists, False otherwise.
        """
        path = self._make_path(key)
        return path.exists()

    def list_objects(self, prefix: str = "") -> list[str]:
        """List all object keys under a prefix.

        Args:
            prefix: The prefix to filter by.

        Returns:
            List of object keys (full paths, with forward slashes).
        """
        # Convert prefix to path
        prefix_path = self._make_path(prefix)

        # Ensure prefix_path is relative to base_dir
        if not str(prefix_path).startswith(str(self.base_dir)):
            raise ValueError(f"Prefix {prefix} escapes base directory")

        objects: list[str] = []

        # Walk the directory tree starting from prefix_path
        if prefix_path.exists() and prefix_path.is_dir():
            for root, _, files in os.walk(prefix_path):
                for file in files:
                    full_path = Path(root) / file
                    # Convert back to storage key format (forward slashes, relative to base_dir)
                    rel_path = full_path.relative_to(self.base_dir)
                    key = str(rel_path).replace(os.sep, "/")
                    objects.append(key)
        elif prefix_path.exists() and prefix_path.is_file():
            # If prefix is a file, return it
            rel_path = prefix_path.relative_to(self.base_dir)
            key = str(rel_path).replace(os.sep, "/")
            objects.append(key)

        return objects

    def head_object(self, key: str) -> dict[str, Any] | None:
        """Get metadata for an object.

        Args:
            key: The object key/path.

        Returns:
            Dict with metadata (size, etag, content_type, last_modified), or None if not found.
        """
        path = self._make_path(key)

        if not path.exists():
            return None

        stat = path.stat()

        # Simple etag: use inode + size (not cryptographic, but sufficient for testing)
        etag = f"{stat.st_ino}-{stat.st_size}"

        return {
            "size": stat.st_size,
            "etag": etag,
            "content_type": "application/octet-stream",  # Default for files
            "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }


# Import datetime for head_object
from datetime import datetime, timezone
