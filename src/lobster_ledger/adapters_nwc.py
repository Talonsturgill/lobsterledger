from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import bolt11
import httpx
from nostr_sdk import (  # type: ignore[import-untyped]
    LookupInvoiceRequest,
    NostrWalletConnectUri,
    Nwc,
    PayInvoiceRequest,
    TransactionState,
)

from lobster_ledger.types import PaymentRequest, Rail

# NIP-47 pay_invoice timeouts must be retry-safe: we lookup_invoice on timeout
# rather than blindly re-sending, because NWC wallets may settle server-side
# even when the response event never reaches us.
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_FEE_MSAT = 10_000
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_BOLT11_PREFIXES = ("lnbc", "lntb", "lnbcrt", "lnsb")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_STRINGS


@dataclass(frozen=True)
class NWCConfig:
    connection_uri: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_fee_msat_floor: int = _DEFAULT_MAX_FEE_MSAT
    allow_lnurl: bool = True

    @classmethod
    def from_env(cls) -> NWCConfig:
        uri = os.environ.get("NWC_CONNECTION_URI")
        if not uri:
            raise ValueError("NWC_CONNECTION_URI is required")  # noqa: TRY003

        timeout_raw = os.environ.get("NWC_TIMEOUT_SECONDS")
        timeout = float(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT_SECONDS

        fee_raw = os.environ.get("NWC_MAX_FEE_MSAT")
        fee_floor = int(fee_raw) if fee_raw else _DEFAULT_MAX_FEE_MSAT

        allow_lnurl = _env_bool("NWC_ALLOW_LNURL", True)

        return cls(
            connection_uri=uri,
            timeout_seconds=timeout,
            max_fee_msat_floor=fee_floor,
            allow_lnurl=allow_lnurl,
        )


def _is_bolt11(s: str) -> bool:
    lower = s.lower()
    return any(lower.startswith(p) for p in _BOLT11_PREFIXES)


def _is_lightning_address(s: str) -> bool:
    # LUD-16 form: user@domain.tld. Keep the check cheap: one "@", no spaces,
    # and a dot in the domain.
    if "@" not in s or s.count("@") != 1 or " " in s:
        return False
    local, _, domain = s.partition("@")
    return bool(local) and "." in domain


def _is_lnurl(s: str) -> bool:
    return s.lower().startswith("lnurl1")


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


class NWCLightningAdapter:
    rail: Rail = "lightning"

    def __init__(self, config: NWCConfig | None = None) -> None:
        self._config = config if config is not None else NWCConfig.from_env()

    def pay(self, request: PaymentRequest) -> tuple[bool, str | None, str | None]:
        # Sync facade: any asyncio-plumbing failure surfaces as a structured
        # failure proof so the ledger can record the attempt.
        try:
            return asyncio.run(self._pay_async(request))
        except Exception as exc:  # noqa: BLE001
            return False, None, _err(str(exc))

    async def _pay_async(self, request: PaymentRequest) -> tuple[bool, str | None, str | None]:
        if request.amount_sats is None or request.amount_sats <= 0:
            return False, None, _err("amount_sats must be positive for lightning rail")
        if not request.counterparty:
            return False, None, _err("counterparty is required")

        amount_msat = request.amount_sats * 1000

        try:
            invoice_str = await self._resolve_invoice(request.counterparty, amount_msat)
        except Exception as exc:  # noqa: BLE001
            return False, None, _err(f"invoice resolution failed: {exc}")

        try:
            decoded = bolt11.decode(invoice_str)
        except Exception as exc:  # noqa: BLE001
            return False, None, _err(f"bolt11 decode failed: {exc}")

        decoded_amount = int(decoded.amount_msat) if decoded.amount_msat else 0
        if decoded_amount == 0:
            return False, None, _err("invoice has zero/ambiguous amount")
        if decoded_amount != amount_msat:
            return (
                False,
                None,
                _err(
                    f"invoice amount mismatch: expected {amount_msat} msat, "
                    f"got {decoded_amount} msat"
                ),
            )
        if self._invoice_expired(decoded):
            return False, None, _err("invoice is expired")

        payment_hash: str | None = None
        try:
            payment_hash = decoded.payment_hash
        except Exception:  # noqa: BLE001
            payment_hash = None

        return await self._send_via_nwc(invoice_str, payment_hash)

    def _invoice_expired(self, decoded: Any) -> bool:
        # bolt11.has_expired relies on module-level time.time, but we also
        # defensively guard against malformed expiry tags with a direct check.
        try:
            return bool(decoded.has_expired())
        except Exception:  # noqa: BLE001
            try:
                return time.time() > int(decoded.date) + int(decoded.expiry)
            except Exception:  # noqa: BLE001
                return False

    async def _send_via_nwc(
        self, invoice_str: str, payment_hash: str | None
    ) -> tuple[bool, str | None, str | None]:
        try:
            uri = NostrWalletConnectUri.parse(self._config.connection_uri)
            client = Nwc(uri)
        except Exception as exc:  # noqa: BLE001
            return False, payment_hash, _err(f"nwc client init failed: {exc}")

        req = PayInvoiceRequest(id=None, invoice=invoice_str, amount=None)

        try:
            response = await asyncio.wait_for(
                client.pay_invoice(req), timeout=self._config.timeout_seconds
            )
        except TimeoutError:
            return await self._lookup_after_timeout(client, payment_hash)
        except Exception as exc:  # noqa: BLE001
            # nostr_sdk raises a generic NostrSdkError for NIP-47 failures.
            # The Rust binding exposes `code` and `message` when the underlying
            # failure was a NIP-47 error, so duck-type for them before falling
            # through to a generic error proof.
            code_attr = getattr(exc, "code", None)
            message_attr = getattr(exc, "message", None)
            if code_attr is not None and message_attr is not None:
                code = getattr(code_attr, "name", None) or str(code_attr)
                return (
                    False,
                    payment_hash,
                    json.dumps({"error_code": code, "message": message_attr}),
                )
            return False, payment_hash, _err(str(exc))

        preimage = getattr(response, "preimage", None)
        if not preimage:
            return False, payment_hash, _err("nwc response missing preimage")
        return True, payment_hash, json.dumps({"preimage": preimage})

    async def _lookup_after_timeout(
        self, client: Any, payment_hash: str | None
    ) -> tuple[bool, str | None, str | None]:
        # Idempotency salvage: a timeout does not mean the payment failed.
        if payment_hash is None:
            return False, None, _err("timeout and no payment hash to reconcile")
        try:
            lookup = await client.lookup_invoice(
                LookupInvoiceRequest(payment_hash=payment_hash, invoice=None)
            )
        except Exception as exc:  # noqa: BLE001
            return (
                False,
                payment_hash,
                json.dumps({"error": "timeout", "lookup_error": str(exc)}),
            )

        state = getattr(lookup, "state", None)
        preimage = getattr(lookup, "preimage", None)
        if state == TransactionState.SETTLED and preimage:
            return True, payment_hash, json.dumps({"preimage": preimage})
        return False, payment_hash, json.dumps({"error": "timeout"})

    async def _resolve_invoice(self, counterparty: str, amount_msat: int) -> str:
        if _is_bolt11(counterparty):
            return counterparty

        if not self._config.allow_lnurl:
            raise ValueError("LNURL/Lightning Address resolution disabled")  # noqa: TRY003

        if _is_lightning_address(counterparty):
            local, _, domain = counterparty.partition("@")
            lnurlp_url = f"https://{domain}/.well-known/lnurlp/{local}"
            return await self._lnurl_pay(lnurlp_url, amount_msat)

        if _is_lnurl(counterparty):
            from lnurl import decode as lnurl_decode  # lazy import, heavy

            decoded_url = str(lnurl_decode(counterparty))
            return await self._lnurl_pay(decoded_url, amount_msat)

        raise ValueError("counterparty is not a bolt11, LNURL, or lightning address")  # noqa: TRY003

    async def _lnurl_pay(self, endpoint_url: str, amount_msat: int) -> str:
        async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as http:
            meta_resp = await http.get(endpoint_url)
            meta_resp.raise_for_status()
            meta = meta_resp.json()

            if meta.get("tag") and meta["tag"] != "payRequest":
                raise ValueError(f"unexpected LNURL tag: {meta.get('tag')!r}")  # noqa: TRY003

            callback = meta.get("callback")
            if not callback:
                raise ValueError("LNURL response missing callback")  # noqa: TRY003

            min_sendable = int(meta.get("minSendable", 0))
            max_sendable = int(meta.get("maxSendable", 0))
            if min_sendable and amount_msat < min_sendable:
                raise ValueError(  # noqa: TRY003
                    f"amount {amount_msat} below minSendable {min_sendable}"
                )
            if max_sendable and amount_msat > max_sendable:
                raise ValueError(  # noqa: TRY003
                    f"amount {amount_msat} above maxSendable {max_sendable}"
                )

            cb_resp = await http.get(callback, params={"amount": amount_msat})
            cb_resp.raise_for_status()
            cb = cb_resp.json()

            if cb.get("status") == "ERROR":
                raise ValueError(f"LNURL callback error: {cb.get('reason')!r}")  # noqa: TRY003

            invoice = cb.get("pr")
            if not invoice or not _is_bolt11(invoice):
                raise ValueError("LNURL callback missing bolt11 invoice")  # noqa: TRY003
            return str(invoice)
