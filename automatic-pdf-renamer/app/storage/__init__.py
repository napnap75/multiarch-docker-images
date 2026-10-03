"""Storage layer for Automatic PDF Renamer.

Provides a unified interface for object storage operations.
Implementations:
- S3Storage: For Garage S3 (production)
- FileStorage: For local filesystem (testing)
"""

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """Abstract base class for storage backends.

    All storage implementations must provide these methods.
    """

    @abstractmethod
    def put_object(self, key: str, body: str | bytes) -> None:
        """Upload an object to the storage.

        Args:
            key: The object key/path.
            body: The object content as string or bytes.
        """
        pass

    @abstractmethod
    def get_object(self, key: str) -> str | bytes | None:
        """Retrieve an object from storage.

        Args:
            key: The object key/path.

        Returns:
            The object content as string or bytes, or None if not found.
        """
        pass

    @abstractmethod
    def delete_object(self, key: str) -> None:
        """Delete an object from storage.

        Args:
            key: The object key/path.
        """
        pass

    @abstractmethod
    def copy_object(self, src_key: str, dst_key: str) -> None:
        """Copy an object within the storage.

        Args:
            src_key: Source object key/path.
            dst_key: Destination object key/path.
        """
        pass

    @abstractmethod
    def object_exists(self, key: str) -> bool:
        """Check if an object exists in storage.

        Args:
            key: The object key/path.

        Returns:
            True if the object exists, False otherwise.
        """
        pass

    @abstractmethod
    def list_objects(self, prefix: str = "") -> list[str]:
        """List all object keys under a prefix.

        Args:
            prefix: The prefix to filter by.

        Returns:
            List of object keys (full paths).
        """
        pass

    @abstractmethod
    def head_object(self, key: str) -> dict[str, Any] | None:
        """Get metadata for an object.

        Args:
            key: The object key/path.

        Returns:
            Dict with metadata (e.g., size, etag, content_type), or None if not found.
        """
        pass


# Convenience function to get the appropriate storage backend
# This will be configured by the application
def get_storage_backend(backend_type: str, **kwargs) -> StorageBackend:
    """Factory function to get a storage backend instance.

    Args:
        backend_type: Either 's3' or 'file'.
        **kwargs: Backend-specific configuration.

    Returns:
        A StorageBackend instance.

    Raises:
        ValueError: If backend_type is not recognized.
    """
    if backend_type == "s3":
        from .s3_storage import S3Storage
        return S3Storage(**kwargs)
    elif backend_type == "file":
        from .file_storage import FileStorage
        return FileStorage(**kwargs)
    else:
        raise ValueError(f"Unknown storage backend type: {backend_type}")
