from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lobster_ledger.types import PaymentRequest, Rail

if TYPE_CHECKING:
    from collections.abc import Awaitable

_VALID_NETWORKS = frozenset({"base", "base-sepolia"})


@dataclass(frozen=True)
class CdpConfig:
    network: str
    account_name: str
    paymaster_url: str | None = None
    receipt_timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> CdpConfig:
        # Default to testnet so a misconfigured deploy cannot silently spend mainnet USDC.
        network = os.environ.get("LL_CDP_NETWORK", "base-sepolia")
        allow_mainnet = os.environ.get("LL_ALLOW_MAINNET") == "1"
        if network == "base" and not allow_mainnet:
            raise RuntimeError(  # noqa: TRY003
                "mainnet blocked: set LL_ALLOW_MAINNET=1 to permit base mainnet transfers"
            )
        if network not in _VALID_NETWORKS:
            raise ValueError(  # noqa: TRY003
                f"invalid network {network!r}; must be 'base' or 'base-sepolia'"
            )
        account_name = os.environ.get("LL_CDP_ACCOUNT_NAME")
        if not account_name:
            raise RuntimeError("LL_CDP_ACCOUNT_NAME is required")  # noqa: TRY003
        paymaster = os.environ.get("LL_CDP_PAYMASTER_URL") or None
        timeout = float(os.environ.get("LL_CDP_RECEIPT_TIMEOUT", "60"))
        return cls(
            network=network,
            account_name=account_name,
            paymaster_url=paymaster,
            receipt_timeout_seconds=timeout,
        )


def _serialize_receipt(receipt: Any) -> str:
    # CDP receipts may be pydantic models or plain dicts; default=str handles HexBytes and enums.
    if hasattr(receipt, "model_dump"):
        payload = receipt.model_dump()
    else:
        try:
            payload = dict(receipt)
        except (TypeError, ValueError):
            payload = {"receipt": str(receipt)}
    return json.dumps(payload, default=str)


def _error_json(exc: BaseException | str) -> str:
    return json.dumps({"error": str(exc)})


class CdpBaseUsdcAdapter:
    rail: Rail = "base"

    def __init__(self, config: CdpConfig | None = None) -> None:
        self._config = config if config is not None else CdpConfig.from_env()

    def pay(self, request: PaymentRequest) -> tuple[bool, str | None, str | None]:
        return asyncio.run(self._pay_async(request))

    async def _pay_async(self, request: PaymentRequest) -> tuple[bool, str | None, str | None]:
        if request.counterparty is None:
            return False, None, _error_json("counterparty is required for base rail")
        if request.amount_usdc_micro is None:
            return False, None, _error_json("amount_usdc_micro is required for base rail")

        try:
            to_address = self._validate_address(request.counterparty)
        except (ValueError, TypeError) as exc:
            return False, None, _error_json(exc)

        # Lazy import: the CDP SDK pulls in aiohttp and sets up analytics on import, so keep
        # it out of module import so tests and policy-only callers do not pay the cost.
        try:
            from cdp import CdpClient  # type: ignore[import-untyped]
        except ImportError as exc:
            return False, None, _error_json(exc)

        tx_hash: str | None = None
        try:
            async with CdpClient() as cdp:
                account = await cdp.evm.get_account(name=self._config.account_name)
                transfer_kwargs: dict[str, Any] = {
                    "to": to_address,
                    "amount": request.amount_usdc_micro,
                    "token": "usdc",
                    "network": self._config.network,
                }
                if self._config.paymaster_url is not None:
                    transfer_kwargs["paymaster_url"] = self._config.paymaster_url
                tx_hash = await account.transfer(**transfer_kwargs)
                scoped = account.use_network(self._config.network)
                receipt_awaitable: Awaitable[Any] = scoped.wait_for_transaction_receipt(
                    transaction_hash=tx_hash,
                    timeout_seconds=self._config.receipt_timeout_seconds,
                )
                receipt = await asyncio.wait_for(
                    receipt_awaitable,
                    timeout=self._config.receipt_timeout_seconds,
                )
        except TimeoutError as exc:
            return False, tx_hash, _error_json(exc)
        except Exception as exc:
            return False, tx_hash, _error_json(exc)

        status = _extract_status(receipt)
        receipt_json = _serialize_receipt(receipt)
        success = status in (1, "success", "0x1")
        return success, tx_hash, receipt_json

    @staticmethod
    def _validate_address(address: str) -> str:
        # eth_utils.to_checksum_address raises ValueError on bad hex / wrong length, which is
        # exactly the error surface we want for a PaymentAdapter failure path.
        from eth_utils.address import to_checksum_address

        return str(to_checksum_address(address))


def _extract_status(receipt: Any) -> Any:
    if receipt is None:
        return None
    if hasattr(receipt, "status"):
        return receipt.status
    if isinstance(receipt, dict):
        return receipt.get("status")
    return None
