"""S3 Storage Backend for Garage S3.

Production storage implementation using boto3 for Garage S3 compatibility.
"""

import logging
from typing import Any

try:
    import boto3
    from botocore.client import BaseClient
    from botocore.exceptions import ClientError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False
    BaseClient = None
    ClientError = Exception

from . import StorageBackend

logger = logging.getLogger(__name__)


class S3Storage(StorageBackend):
    """S3-compatible storage backend for Garage.

    Uses boto3 to interact with Garage S3 API.
    Configuration via environment variables or constructor kwargs.
    """

    def __init__(
        self,
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        bucket_name: str | None = None,
        region_name: str | None = None,
    ):
        """Initialize S3 storage backend.

        Args:
            endpoint_url: Garage S3 endpoint URL.
            aws_access_key_id: S3 access key.
            aws_secret_access_key: S3 secret key.
            bucket_name: Name of the S3 bucket.
            region_name: AWS region (default: us-east-1).
        """
        self.endpoint_url = endpoint_url
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.bucket_name = bucket_name
        self.region_name = region_name or "us-east-1"

        # Initialize boto3 client
        self._client: BaseClient | None = None
        self._connect()

    def _connect(self) -> None:
        """Initialize the boto3 S3 client."""
        if not HAS_BOTO3:
            raise ImportError("boto3 is required for S3Storage. Install with: pip install boto3")
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.region_name,
        )
        logger.info(
            f"Connected to S3 at {self.endpoint_url} with bucket {self.bucket_name}"
        )

    def _ensure_bucket(self) -> None:
        """Ensure the bucket is accessible."""
        if self.bucket_name is None:
            raise ValueError("bucket_name is required for S3Storage")

    def _make_key(self, key: str) -> str:
        """Normalize a key by removing leading slashes."""
        return key.lstrip("/")

    def put_object(self, key: str, body: str | bytes) -> None:
        """Upload an object to S3.

        Args:
            key: The object key/path.
            body: The object content as string or bytes.
        """
        self._ensure_bucket()
        normalized_key = self._make_key(key)

        if isinstance(body, str):
            body = body.encode("utf-8")

        try:
            self._client.put_object(
                Bucket=self.bucket_name,
                Key=normalized_key,
                Body=body,
            )
            logger.debug(f"Uploaded {normalized_key} to bucket {self.bucket_name}")
        except ClientError as e:
            logger.error(f"Failed to upload {normalized_key}: {e}")
            raise

    def get_object(self, key: str) -> str | bytes | None:
        """Retrieve an object from S3.

        Args:
            key: The object key/path.

        Returns:
            The object content as bytes, or None if not found.
        """
        self._ensure_bucket()
        normalized_key = self._make_key(key)

        try:
            response = self._client.get_object(
                Bucket=self.bucket_name,
                Key=normalized_key,
            )
            return response["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                logger.debug(f"Object {normalized_key} not found")
                return None
            logger.error(f"Failed to get {normalized_key}: {e}")
            raise

    def delete_object(self, key: str) -> None:
        """Delete an object from S3.

        Args:
            key: The object key/path.
        """
        self._ensure_bucket()
        normalized_key = self._make_key(key)

        try:
            self._client.delete_object(
                Bucket=self.bucket_name,
                Key=normalized_key,
            )
            logger.debug(f"Deleted {normalized_key} from bucket {self.bucket_name}")
        except ClientError as e:
            logger.error(f"Failed to delete {normalized_key}: {e}")
            raise

    def copy_object(self, src_key: str, dst_key: str) -> None:
        """Copy an object within S3.

        Uses S3's copy_object operation which is atomic.

        Args:
            src_key: Source object key/path.
            dst_key: Destination object key/path.
        """
        self._ensure_bucket()
        src = self._make_key(src_key)
        dst = self._make_key(dst_key)

        # Get the source bucket (for cross-bucket copy support)
        # For now, assume same bucket
        copy_source = {"Bucket": self.bucket_name, "Key": src}

        try:
            self._client.copy_object(
                Bucket=self.bucket_name,
                Key=dst,
                CopySource=copy_source,
            )
            logger.debug(f"Copied {src} to {dst} in bucket {self.bucket_name}")
        except ClientError as e:
            logger.error(f"Failed to copy {src} to {dst}: {e}")
            raise

    def object_exists(self, key: str) -> bool:
        """Check if an object exists in S3.

        Args:
            key: The object key/path.

        Returns:
            True if the object exists, False otherwise.
        """
        self._ensure_bucket()
        normalized_key = self._make_key(key)

        try:
            self._client.head_object(
                Bucket=self.bucket_name,
                Key=normalized_key,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            logger.error(f"Failed to check existence of {normalized_key}: {e}")
            raise

    def list_objects(self, prefix: str = "") -> list[str]:
        """List all object keys under a prefix.

        Args:
            prefix: The prefix to filter by.

        Returns:
            List of object keys (full paths, without leading slash).
        """
        self._ensure_bucket()
        normalized_prefix = self._make_key(prefix)

        objects: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")

        try:
            for page in paginator.paginate(
                Bucket=self.bucket_name,
                Prefix=normalized_prefix,
            ):
                if "Contents" in page:
                    for obj in page["Contents"]:
                        # Return keys without leading slash
                        objects.append(obj["Key"].lstrip("/"))
            return objects
        except ClientError as e:
            logger.error(f"Failed to list objects with prefix {normalized_prefix}: {e}")
            raise

    def head_object(self, key: str) -> dict[str, Any] | None:
        """Get metadata for an object.

        Args:
            key: The object key/path.

        Returns:
            Dict with metadata (size, etag, content_type, last_modified), or None if not found.
        """
        self._ensure_bucket()
        normalized_key = self._make_key(key)

        try:
            response = self._client.head_object(
                Bucket=self.bucket_name,
                Key=normalized_key,
            )
            return {
                "size": response.get("ContentLength"),
                "etag": response.get("ETag"),
                "content_type": response.get("ContentType"),
                "last_modified": response.get("LastModified"),
            }
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return None
            logger.error(f"Failed to get metadata for {normalized_key}: {e}")
            raise
