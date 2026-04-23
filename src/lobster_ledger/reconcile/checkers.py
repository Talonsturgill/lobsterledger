from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

CheckStatus = Literal["settled", "failed", "pending"]


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    raw_proof: str | None = None
    reason: str | None = None


@runtime_checkable
class RailChecker(Protocol):
    def check(self, tx: dict[str, Any]) -> CheckResult: ...


class ManualChecker:
    # Manual-entry transactions never reconcile through external rails.
    def check(self, tx: dict[str, Any]) -> CheckResult:
        return CheckResult(status="pending", reason="manual rail skipped")


class NWCLightningChecker:
    # Runs a NIP-47 lookup_invoice against the configured wallet to resolve
    # ambiguity left behind by a pay_invoice timeout.
    def check(self, tx: dict[str, Any]) -> CheckResult:
        uri = os.environ.get("NWC_CONNECTION_URI")
        if not uri:
            return CheckResult(status="pending", reason="no NWC config")

        payment_hash = tx.get("external_id")
        if not payment_hash:
            return CheckResult(status="pending", reason="missing payment hash")

        try:
            import asyncio

            from nostr_sdk import (  # type: ignore[import-untyped]
                LookupInvoiceRequest,
                NostrWalletConnectUri,
                Nwc,
                TransactionState,
            )
        except Exception as exc:  # noqa: BLE001
            return CheckResult(status="pending", reason=f"nwc import failed: {exc}")

        async def _lookup() -> tuple[Any, Any]:
            parsed = NostrWalletConnectUri.parse(uri)
            client = Nwc(parsed)
            result = await client.lookup_invoice(
                LookupInvoiceRequest(payment_hash=str(payment_hash), invoice=None)
            )
            return result, TransactionState

        try:
            lookup, tx_state = asyncio.run(_lookup())
        except Exception as exc:  # noqa: BLE001
            return CheckResult(status="pending", reason=f"nwc lookup failed: {exc}")

        state = getattr(lookup, "state", None)
        preimage = getattr(lookup, "preimage", None)

        settled_states = {
            getattr(tx_state, "SETTLED", None),
            getattr(tx_state, "PAID", None),
        }
        failed_states = {
            getattr(tx_state, "EXPIRED", None),
            getattr(tx_state, "CANCELED", None),
            getattr(tx_state, "FAILED", None),
        }

        if state in settled_states and state is not None and preimage:
            return CheckResult(
                status="settled",
                raw_proof=json.dumps({"preimage": str(preimage)}),
            )
        if state in failed_states and state is not None:
            return CheckResult(
                status="failed",
                reason=f"lightning invoice {getattr(state, 'name', str(state)).lower()}",
            )
        return CheckResult(status="pending", reason="lightning invoice still pending")


class BaseUsdcChecker:
    # Independent receipt fetch via web3.py so we do not rely on whatever the
    # CDP SDK saw at broadcast time; chain reorgs can flip a short-lived success.
    def check(self, tx: dict[str, Any]) -> CheckResult:
        tx_hash = tx.get("external_id")
        if not tx_hash:
            return CheckResult(status="pending", reason="missing tx hash")

        rpc_url = os.environ.get("LL_BASE_RPC_URL")
        if not rpc_url:
            network = os.environ.get("LL_BASE_NETWORK", "mainnet").lower()
            rpc_url = (
                "https://sepolia.base.org"
                if network in ("sepolia", "testnet", "base-sepolia")
                else "https://mainnet.base.org"
            )

        try:
            from web3 import HTTPProvider, Web3
            from web3.exceptions import TransactionNotFound
        except Exception as exc:  # noqa: BLE001
            return CheckResult(status="pending", reason=f"web3 import failed: {exc}")

        try:
            w3 = Web3(HTTPProvider(rpc_url))
            receipt = w3.eth.get_transaction_receipt(str(tx_hash))  # type: ignore[arg-type]
        except TransactionNotFound:
            return CheckResult(status="pending", reason="tx not yet mined")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(status="pending", reason=f"rpc error: {exc}")

        if isinstance(receipt, dict):
            status = receipt.get("status")
            block_number = receipt.get("blockNumber")
        else:
            status = getattr(receipt, "status", None)
            block_number = getattr(receipt, "blockNumber", None)

        if status == 0:
            return CheckResult(status="failed", reason="evm tx reverted")

        try:
            head = int(w3.eth.block_number)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(status="pending", reason=f"head fetch failed: {exc}")

        if block_number is None:
            return CheckResult(status="pending", reason="receipt missing blockNumber")

        confirmations = head - int(block_number) + 1
        if status == 1 and confirmations >= 1:
            return CheckResult(
                status="settled",
                raw_proof=json.dumps(
                    {
                        "tx_hash": str(tx_hash),
                        "block_number": int(block_number),
                        "confirmations": int(confirmations),
                    }
                ),
            )
        return CheckResult(status="pending", reason="awaiting confirmations")


def get_checker(rail: str) -> RailChecker | None:
    if rail == "lightning":
        return NWCLightningChecker()
    if rail == "base":
        return BaseUsdcChecker()
    if rail == "manual":
        return ManualChecker()
    return None
