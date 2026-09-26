"""
API Key Management System for NeuroNews API - Issue #61.

This module implements secure API key generation, storage, and management:
1. Allow users to generate & revoke API keys
2. Store API keys securely in DynamoDB
3. Implement API key expiration & renewal policies
4. Provide API endpoints for key management
"""

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

try:
    import boto3
    from botocore.exceptions import ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

logger = logging.getLogger(__name__)


class APIKeyStatus(Enum):
    """API key status enumeration."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    SUSPENDED = "suspended"


@dataclass
class APIKey:
    """API key data structure."""

    key_id: str
    user_id: str
    key_prefix: str  # First 8 characters for identification
    key_hash: str  # SHA256 hash of the full key
    name: str
    status: APIKeyStatus
    created_at: datetime
    expires_at: Optional[datetime]
    last_used_at: Optional[datetime]
    usage_count: int = 0
    permissions: Optional[List[str]] = None
    rate_limit: Optional[int] = None  # requests per minute

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "key_id": self.key_id,
            "user_id": self.user_id,
            "key_prefix": self.key_prefix,
            "key_hash": self.key_hash,
            "name": self.name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": (
                self.last_used_at.isoformat() if self.last_used_at else None
            ),
            "usage_count": self.usage_count,
            "permissions": self.permissions,
            "rate_limit": self.rate_limit,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "APIKey":
        """Create from dictionary."""
        return cls(
            key_id=data["key_id"],
            user_id=data["user_id"],
            key_prefix=data["key_prefix"],
            key_hash=data["key_hash"],
            name=data["name"],
            status=APIKeyStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=(
                datetime.fromisoformat(data["expires_at"])
                if data.get("expires_at")
                else None
            ),
            last_used_at=(
                datetime.fromisoformat(data["last_used_at"])
                if data.get("last_used_at")
                else None
            ),
            usage_count=data.get("usage_count", 0),
            permissions=data.get("permissions"),
            rate_limit=data.get("rate_limit"),
        )


class APIKeyGenerator:
    """Secure API key generation utility."""

    @staticmethod
    def generate_api_key() -> str:
        """Generate a cryptographically secure API key."""
        # Generate 32 bytes (256 bits) of random data
        secrets.token_bytes(32)
        # Convert to base64-like string but URL safe
        api_key = secrets.token_urlsafe(32)
        # Add prefix to identify as NeuroNews API key
        return "nn_{0}".format(api_key)

    @staticmethod
    def generate_key_id() -> str:
        """Generate a unique key ID."""
        return "key_{0}".format(secrets.token_hex(16))

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        """Create secure hash of API key for storage."""
        salt = os.getenv("API_KEY_SALT", "neuronews_api_key_salt").encode()
        # Use hashlib.pbkdf2_hmac instead of pbkdf2_hex
        key_hash = hashlib.pbkdf2_hmac("sha256", api_key.encode(), salt, 100000)
        return key_hash.hex()

    @staticmethod
    def verify_api_key(api_key: str, key_hash: str) -> bool:
        """Verify API key against stored hash."""
        computed_hash = APIKeyGenerator.hash_api_key(api_key)
        return hmac.compare_digest(computed_hash, key_hash)


class DynamoDBAPIKeyStore:
    """DynamoDB storage for API keys."""

    def __init__(self):
        """Initialize DynamoDB connection."""
        self.table_name = os.getenv("API_KEYS_DYNAMODB_TABLE", "neuronews_api_keys")
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.dynamodb = None
        self.table = None

        if BOTO3_AVAILABLE:
            try:
                from src.utils.local_cloud import get_resource

                self.dynamodb = get_resource("dynamodb", region_name=self.region)
                self.table = self.dynamodb.Table(self.table_name)
                self._ensure_table_exists()
            except Exception as e:
                if "Unable to locate credentials" not in str(e):
                    logger.warning("Failed to initialize DynamoDB: {0}".format(e))
                self.dynamodb = None
                self.table = None
        else:
            logger.warning("boto3 not available - DynamoDB features disabled")

    def _ensure_table_exists(self):
        """Ensure the API keys table exists."""
        if not self.dynamodb:
            return

        try:
            # Check if table exists
            self.table.load()
            logger.info("DynamoDB table {0} exists".format(self.table_name))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info("Creating DynamoDB table {0}".format(self.table_name))
                self._create_table()
            else:
                logger.error("Error checking table: {0}".format(e))

    def _create_table(self):
        """Create the API keys table."""
        if not self.dynamodb:
            return

        try:
            table = self.dynamodb.create_table(
                TableName=self.table_name,
                KeySchema=[{"AttributeName": "key_id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "key_id", "AttributeType": "S"},
                    {"AttributeName": "user_id", "AttributeType": "S"},
                    {"AttributeName": "key_prefix", "AttributeType": "S"},
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "user-id-index",
                        "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                    {
                        "IndexName": "key-prefix-index",
                        "KeySchema": [{"AttributeName": "key_prefix", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ],
                BillingMode="PAY_PER_REQUEST",
                Tags=[
                    {"Key": "Application", "Value": "NeuroNews"},
                    {"Key": "Component", "Value": "APIKeyManagement"},
                ],
            )

            # Wait for table to be created
            table.wait_until_exists()
            self.table = table
            logger.info("Created DynamoDB table {0}".format(self.table_name))

        except Exception as e:
            logger.error("Failed to create table: {0}".format(e))

    async def store_api_key(self, api_key: APIKey) -> bool:
        """Store API key in DynamoDB."""
        if not self.table:
            logger.warning("DynamoDB not available - API key not stored")
            return False

        try:
            self.table.put_item(Item=api_key.to_dict())
            logger.info(
                "Stored API key {0} for user {1}".format(
                    api_key.key_id, api_key.user_id
                )
            )
            return True

        except Exception as e:
            logger.error("Failed to store API key {0}: {1}".format(api_key.key_id, e))
            return False

    async def get_api_key(self, key_id: str) -> Optional[APIKey]:
        """Get API key by ID."""
        if not self.table:
            return None

        try:
            response = self.table.get_item(Key={"key_id": key_id})
            if "Item" in response:
                return APIKey.from_dict(response["Item"])
            return None

        except Exception as e:
            logger.error("Failed to get API key {0}: {1}".format(key_id, e))
            return None

    async def get_user_api_keys(self, user_id: str) -> List[APIKey]:
        """Get all API keys for a user."""
        if not self.table:
            return []

        try:
            response = self.table.query(
                IndexName="user-id-index",
                KeyConditionExpression="user_id =:user_id",
                ExpressionAttributeValues={":user_id": user_id},
            )

            return [APIKey.from_dict(item) for item in response.get("Items", [])]

        except Exception as e:
            logger.error("Failed to get API keys for user {0}: {1}".format(user_id, e))
            return []

    async def find_by_prefix(self, key_prefix: str) -> List[APIKey]:
        """Candidate keys sharing a prefix (the hash decides which one matches).

        Uses the ``key-prefix-index`` GSI; tables created before that index
        existed fall back to a filtered scan.
        """
        if not self.table:
            return []
        values = {":prefix": key_prefix}
        try:
            response = self.table.query(
                IndexName="key-prefix-index",
                KeyConditionExpression="key_prefix = :prefix",
                ExpressionAttributeValues=values,
            )
        except Exception:  # noqa: BLE001 - index missing on older tables
            try:
                response = self.table.scan(
                    FilterExpression="key_prefix = :prefix",
                    ExpressionAttributeValues=values,
                )
            except Exception as e:
                logger.error("Failed to look up API key prefix: {0}".format(e))
                return []
        return [APIKey.from_dict(item) for item in response.get("Items", [])]

    async def update_api_key_usage(self, key_id: str) -> bool:
        """Update API key last used time and usage count."""
        if not self.table:
            return False

        try:
            now = datetime.now(timezone.utc)
            self.table.update_item(
                Key={"key_id": key_id},
                UpdateExpression="SET last_used_at =:last_used, usage_count = usage_count +:inc",
                ExpressionAttributeValues={":last_used": now.isoformat(), ":inc": 1},
            )
            return True

        except Exception as e:
            logger.error("Failed to update API key usage {0}: {1}".format(key_id, e))
            return False

    async def revoke_api_key(self, key_id: str) -> bool:
        """Revoke an API key."""
        if not self.table:
            return False

        try:
            self.table.update_item(
                Key={"key_id": key_id},
                UpdateExpression="SET #status =:status",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status": APIKeyStatus.REVOKED.value},
            )
            logger.info("Revoked API key {0}".format(key_id))
            return True

        except Exception as e:
            logger.error("Failed to revoke API key {0}: {1}".format(key_id, e))
            return False

    async def delete_api_key(self, key_id: str) -> bool:
        """Delete an API key."""
        if not self.table:
            return False

        try:
            self.table.delete_item(Key={"key_id": key_id})
            logger.info("Deleted API key {0}".format(key_id))
            return True

        except Exception as e:
            logger.error("Failed to delete API key {0}: {1}".format(key_id, e))
            return False


class APIKeyManager:
    """Main API key management class."""

    def __init__(self):
        """Initialize API key manager."""
        self.store = DynamoDBAPIKeyStore()
        self.default_expiry_days = int(os.getenv("API_KEY_DEFAULT_EXPIRY_DAYS", "365"))
        self.max_keys_per_user = int(os.getenv("API_KEY_MAX_PER_USER", "10"))

    async def generate_api_key(
        self,
        user_id: str,
        name: str,
        expires_in_days: Optional[int] = None,
        permissions: Optional[List[str]] = None,
        rate_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Generate a new API key for a user.

        Args:
            user_id: User ID
            name: Human-readable name for the key
            expires_in_days: Days until expiration (default: 365)
            permissions: List of permissions for this key
            rate_limit: Rate limit in requests per minute

        Returns:
            Dictionary with key details and the actual key
        """
        # Check if user has too many keys
        existing_keys = await self.store.get_user_api_keys(user_id)
        active_keys = [k for k in existing_keys if k.status == APIKeyStatus.ACTIVE]

        if len(active_keys) >= self.max_keys_per_user:
            raise ValueError(
                "User has reached maximum API key limit ({0})".format(
                    self.max_keys_per_user
                )
            )

        # Generate new API key
        api_key_value = APIKeyGenerator.generate_api_key()
        key_id = APIKeyGenerator.generate_key_id()
        key_hash = APIKeyGenerator.hash_api_key(api_key_value)
        key_prefix = api_key_value[:8]  # First 8 characters for identification

        # Set expiration
        now = datetime.now(timezone.utc)
        expires_at = None
        if expires_in_days:
            expires_at = now + timedelta(days=expires_in_days)
        elif self.default_expiry_days > 0:
            expires_at = now + timedelta(days=self.default_expiry_days)

        # Create API key object
        api_key = APIKey(
            key_id=key_id,
            user_id=user_id,
            key_prefix=key_prefix,
            key_hash=key_hash,
            name=name,
            status=APIKeyStatus.ACTIVE,
            created_at=now,
            expires_at=expires_at,
            last_used_at=None,
            usage_count=0,
            permissions=permissions,
            rate_limit=rate_limit,
        )

        # Store in DynamoDB
        success = await self.store.store_api_key(api_key)
        if not success:
            raise RuntimeError("Failed to store API key")

        return {
            "key_id": key_id,
            "api_key": api_key_value,  # Only returned once
            "key_prefix": key_prefix,
            "name": name,
            "status": api_key.status.value,
            "created_at": api_key.created_at.isoformat(),
            "expires_at": (
                api_key.expires_at.isoformat() if api_key.expires_at else None
            ),
            "permissions": permissions,
            "rate_limit": rate_limit,
            "message": "Store this API key securely - it will not be shown again",
        }

    async def verify_api_key(self, api_key: str) -> Optional[APIKey]:
        """
        Verify an API key and return key details if valid.

        Candidates are found by the stored 8-character prefix; the PBKDF2 hash
        comparison (constant time) decides the match. Revoked, inactive and
        expired keys are rejected.

        Args:
            api_key: The API key to verify

        Returns:
            APIKey object if valid, None otherwise
        """
        if not api_key or not api_key.startswith("nn_"):
            return None
        now = datetime.now(timezone.utc)
        for candidate in await self.store.find_by_prefix(api_key[:8]):
            if candidate.status != APIKeyStatus.ACTIVE:
                continue
            expires_at = candidate.expires_at
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at < now:
                    continue
            if APIKeyGenerator.verify_api_key(api_key, candidate.key_hash):
                return candidate
        return None

    async def get_user_api_keys(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all API keys for a user (without the actual key values)."""
        keys = await self.store.get_user_api_keys(user_id)

        return [
            {
                "key_id": key.key_id,
                "key_prefix": key.key_prefix,
                "name": key.name,
                "status": key.status.value,
                "created_at": key.created_at.isoformat(),
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                "last_used_at": (
                    key.last_used_at.isoformat() if key.last_used_at else None
                ),
                "usage_count": key.usage_count,
                "permissions": key.permissions,
                "rate_limit": key.rate_limit,
                "is_expired": (
                    key.expires_at and key.expires_at < datetime.now(timezone.utc)
                    if key.expires_at
                    else False
                ),
            }
            for key in keys
        ]

    async def revoke_api_key(self, user_id: str, key_id: str) -> bool:
        """Revoke an API key (must be owned by the user)."""
        # Verify ownership
        api_key = await self.store.get_api_key(key_id)
        if not api_key or api_key.user_id != user_id:
            return False

        return await self.store.revoke_api_key(key_id)

    async def delete_api_key(self, user_id: str, key_id: str) -> bool:
        """Delete an API key (must be owned by the user)."""
        # Verify ownership
        api_key = await self.store.get_api_key(key_id)
        if not api_key or api_key.user_id != user_id:
            return False

        return await self.store.delete_api_key(key_id)

    async def renew_api_key(
        self, user_id: str, key_id: str, extends_days: int = None
    ) -> Dict[str, Any]:
        """
        Renew an API key by extending its expiration.

        Args:
            user_id: User ID
            key_id: Key ID to renew
            extends_days: Days to extend (default: original expiry period)

        Returns:
            Updated key information
        """
        # Get existing key
        api_key = await self.store.get_api_key(key_id)
        if not api_key or api_key.user_id != user_id:
            raise ValueError("API key not found or not owned by user")

        if api_key.status != APIKeyStatus.ACTIVE:
            raise ValueError("Cannot renew inactive API key")

        # Calculate new expiration
        extend_days = extends_days or self.default_expiry_days
        new_expires_at = datetime.now(timezone.utc) + timedelta(days=extend_days)

        # Update in database
        if self.store.table:
            try:
                self.store.table.update_item(
                    Key={"key_id": key_id},
                    UpdateExpression="SET expires_at =:expires_at",
                    ExpressionAttributeValues={
                        ":expires_at": new_expires_at.isoformat()
                    },
                )
            except Exception as e:
                logger.error("Failed to renew API key {0}: {1}".format(key_id, e))
                raise RuntimeError("Failed to renew API key")

        return {
            "key_id": key_id,
            "key_prefix": api_key.key_prefix,
            "name": api_key.name,
            "old_expires_at": (
                api_key.expires_at.isoformat() if api_key.expires_at else None
            ),
            "new_expires_at": new_expires_at.isoformat(),
            "extended_days": extend_days,
            "status": "renewed",
        }

    async def cleanup_expired_keys(self) -> int:
        """Clean up expired API keys (admin operation)."""
        # This would be implemented as a scheduled job
        # For now, just return 0 as placeholder
        return 0


# Global API key manager instance
api_key_manager = APIKeyManager()
