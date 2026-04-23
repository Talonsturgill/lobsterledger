from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from lobster_ledger.types import PaymentRequest, Rail


class PaymentAdapter(Protocol):
    rail: Rail

    def pay(self, request: PaymentRequest) -> tuple[bool, str | None, str | None]:
        """Return (success, external_id, raw_proof).

        Stubs return success=True without side effects.
        """
        ...


class StubLightningAdapter:
    rail: Rail = "lightning"

    def pay(self, request: PaymentRequest) -> tuple[bool, str | None, str | None]:
        return True, f"stub-{uuid4().hex[:8]}", "stub-proof"


class StubBaseAdapter:
    rail: Rail = "base"

    def pay(self, request: PaymentRequest) -> tuple[bool, str | None, str | None]:
        return True, f"stub-{uuid4().hex[:8]}", "stub-proof"


class StubManualAdapter:
    rail: Rail = "manual"

    def pay(self, request: PaymentRequest) -> tuple[bool, str | None, str | None]:
        # Manual rail rarely hits the adapter (ledger-only entries); return a sensible no-op.
        return True, f"manual-{uuid4().hex[:8]}", None


_ADAPTERS: dict[Rail, PaymentAdapter] = {
    "lightning": StubLightningAdapter(),
    "base": StubBaseAdapter(),
    "manual": StubManualAdapter(),
}


def get_adapter(rail: Rail) -> PaymentAdapter:
    return _ADAPTERS[rail]
