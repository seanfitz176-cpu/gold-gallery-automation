"""Phase 1 orchestration for a safe, read-only eBay connection check."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict

from .ebay_client import EbayError, EbaySandboxClient, NotConnectedError
from .storage import AppDatabase, ConnectionChangedStorageError


@dataclass(frozen=True)
class ConnectionCheckResult:
    """A deliberately small result that cannot expose tokens or account details."""

    environment: str
    oauth: str
    identity_read: str
    checked_at: str

    def as_dict(self) -> Dict[str, str]:
        return asdict(self)


class ConnectionVerifier:
    """Verifies OAuth and Identity without calling any selling or mutation API."""

    def __init__(
        self,
        ebay: EbaySandboxClient,
        database: AppDatabase,
        environment: str = "Sandbox",
    ) -> None:
        self.ebay = ebay
        self.database = database
        self.environment = environment

    def verify(self) -> ConnectionCheckResult:
        generation = self.database.connection_generation()
        if generation is None:
            raise NotConnectedError(self.environment)

        identity = self.ebay.verify_connection_identity(generation)
        user_id = identity["user_id"]
        bound_user_id = self.database.connection_status().get("user_id")
        if bound_user_id and bound_user_id != user_id:
            raise EbayError(
                f"The Identity response belongs to a different {self.environment} user",
                status=409,
                code="SELLER_MISMATCH",
            )

        try:
            verified_at = self.database.mark_connection_verified(generation, user_id)
        except ConnectionChangedStorageError as exc:
            raise EbayError(
                f"The {self.environment} authorization changed during verification; start again",
                status=409,
                code="CONNECTION_CHANGED",
            ) from exc

        timestamp = datetime.fromtimestamp(verified_at, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        return ConnectionCheckResult(
            environment="sandbox",
            oauth="valid",
            identity_read="successful",
            checked_at=timestamp,
        )
