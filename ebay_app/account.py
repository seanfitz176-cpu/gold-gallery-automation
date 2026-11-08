"""Phase 2 service for a deliberately small, read-only account summary."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict

from .ebay_client import EbayError, EbaySandboxClient
from .storage import AppDatabase


MARKETPLACE_PATTERN = re.compile(r"^EBAY_[A-Z0-9_]{2,24}$")


@dataclass(frozen=True)
class AccountSummary:
    """The only Identity fields Phase 2 is allowed to expose."""

    environment: str
    account_type: str
    registration_marketplace: str
    retrieved_at: str

    def as_dict(self) -> Dict[str, str]:
        return asdict(self)


class AccountService:
    """Retrieve a verified identity without retaining raw profile information."""

    def __init__(
        self,
        ebay: EbaySandboxClient,
        database: AppDatabase,
        now=time.time,
        environment: str = "Sandbox",
    ) -> None:
        self.ebay = ebay
        self.database = database
        self.now = now
        self.environment = environment

    def retrieve(self) -> AccountSummary:
        binding = self.database.verified_connection_binding()
        if binding is None:
            raise EbayError(
                f"Verify the eBay {self.environment} connection before viewing account "
                "information",
                status=409,
                code="CONNECTION_NOT_VERIFIED",
            )

        identity = self.ebay.read_verified_account_identity(binding)
        raw_account_type = identity.get("account_type")
        account_type = (
            {
                "BUSINESS": "Business",
                "INDIVIDUAL": "Individual",
            }.get(raw_account_type, "Unavailable")
            if isinstance(raw_account_type, str)
            else "Unavailable"
        )

        raw_marketplace = identity.get("registration_marketplace")
        registration_marketplace = (
            raw_marketplace
            if isinstance(raw_marketplace, str)
            and MARKETPLACE_PATTERN.fullmatch(raw_marketplace)
            else "Unavailable"
        )
        retrieved_at = datetime.fromtimestamp(self.now(), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        return AccountSummary(
            environment=self.environment,
            account_type=account_type,
            registration_marketplace=registration_marketplace,
            retrieved_at=retrieved_at,
        )
