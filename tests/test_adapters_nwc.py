from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lobster_ledger.adapters_nwc import NWCConfig, NWCLightningAdapter
from lobster_ledger.types import PaymentRequest


# A deterministic bolt11 fixture via a lightweight stand-in. Generating a real
# signed invoice in-process is impractical, and the adapter only reads
# amount_msat, payment_hash, date, expiry, and has_expired(). We therefore
# fake bolt11.decode to return this dataclass.
@dataclass
class _FakeBolt11:
    amount_msat: int
    payment_hash: str = "a" * 64
    date: int = 1_700_000_000
    expiry: int = 3600
    _expired: bool = False

    def has_expired(self) -> bool:
        return self._expired


_INVOICE = "lnbc10u1pfake"
_LN_ADDRESS = "alice@example.com"


def _make_adapter(**overrides: Any) -> NWCLightningAdapter:
    cfg = NWCConfig(
        connection_uri="nostr+walletconnect://pubkey?relay=wss://r&secret=deadbeef",
        timeout_seconds=overrides.pop("timeout_seconds", 1.0),
        max_fee_msat_floor=overrides.pop("max_fee_msat_floor", 10_000),
        allow_lnurl=overrides.pop("allow_lnurl", True),
    )
    return NWCLightningAdapter(config=cfg)


def _req(amount_sats: int = 1000, counterparty: str = _INVOICE) -> PaymentRequest:
    return PaymentRequest(
        rail="lightning",
        amount_usd_fmv_cents=100,
        amount_sats=amount_sats,
        counterparty=counterparty,
    )


def test_config_from_env_requires_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NWC_CONNECTION_URI", raising=False)
    with pytest.raises(ValueError, match="NWC_CONNECTION_URI"):
        NWCConfig.from_env()


def test_config_from_env_parses_optional_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NWC_CONNECTION_URI", "nostr+walletconnect://x")
    monkeypatch.setenv("NWC_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("NWC_MAX_FEE_MSAT", "50000")
    monkeypatch.setenv("NWC_ALLOW_LNURL", "false")
    cfg = NWCConfig.from_env()
    assert cfg.connection_uri == "nostr+walletconnect://x"
    assert cfg.timeout_seconds == 15.0
    assert cfg.max_fee_msat_floor == 50_000
    assert cfg.allow_lnurl is False


def test_pay_rejects_zero_amount_invoice() -> None:
    adapter = _make_adapter()
    with patch("lobster_ledger.adapters_nwc.bolt11.decode") as dec:
        dec.return_value = _FakeBolt11(amount_msat=0)
        ok, ext, proof = adapter.pay(_req(amount_sats=1000))
    assert ok is False
    assert ext is None
    assert proof is not None
    assert "zero" in proof or "ambiguous" in proof


def test_pay_rejects_amount_mismatch() -> None:
    adapter = _make_adapter()
    with patch("lobster_ledger.adapters_nwc.bolt11.decode") as dec:
        dec.return_value = _FakeBolt11(amount_msat=500_000)  # 500 sats != 1000 sats
        ok, _ext, proof = adapter.pay(_req(amount_sats=1000))
    assert ok is False
    assert proof is not None
    assert "mismatch" in proof


def test_pay_rejects_expired_invoice() -> None:
    adapter = _make_adapter()
    with patch("lobster_ledger.adapters_nwc.bolt11.decode") as dec:
        dec.return_value = _FakeBolt11(amount_msat=1_000_000, _expired=True)
        ok, _ext, proof = adapter.pay(_req())
    assert ok is False
    assert proof is not None
    assert "expired" in proof


def test_pay_success_path() -> None:
    adapter = _make_adapter()
    fake = _FakeBolt11(amount_msat=1_000_000, payment_hash="b" * 64)
    response = MagicMock()
    response.preimage = "c" * 64

    fake_client = MagicMock()
    fake_client.pay_invoice = AsyncMock(return_value=response)

    with (
        patch("lobster_ledger.adapters_nwc.bolt11.decode", return_value=fake),
        patch("lobster_ledger.adapters_nwc.NostrWalletConnectUri") as uri_cls,
        patch("lobster_ledger.adapters_nwc.Nwc", return_value=fake_client) as nwc_cls,
    ):
        uri_cls.parse.return_value = object()
        ok, ext, proof = adapter.pay(_req())

    assert ok is True
    assert ext == "b" * 64
    assert proof is not None
    assert json.loads(proof) == {"preimage": "c" * 64}
    nwc_cls.assert_called_once()


def test_pay_timeout_falls_back_to_lookup_invoice_settled() -> None:
    adapter = _make_adapter(timeout_seconds=0.05)
    fake = _FakeBolt11(amount_msat=1_000_000, payment_hash="d" * 64)

    async def _hang(_: Any) -> Any:
        import asyncio as _a

        await _a.sleep(10)
        raise AssertionError

    lookup_resp = MagicMock()
    lookup_resp.state = _make_settled_state()
    lookup_resp.preimage = "ee" * 32

    fake_client = MagicMock()
    fake_client.pay_invoice = _hang
    fake_client.lookup_invoice = AsyncMock(return_value=lookup_resp)

    with (
        patch("lobster_ledger.adapters_nwc.bolt11.decode", return_value=fake),
        patch("lobster_ledger.adapters_nwc.NostrWalletConnectUri"),
        patch("lobster_ledger.adapters_nwc.Nwc", return_value=fake_client),
    ):
        ok, ext, proof = adapter.pay(_req())

    assert ok is True
    assert ext == "d" * 64
    assert proof is not None
    assert json.loads(proof) == {"preimage": "ee" * 32}


def test_pay_timeout_lookup_invoice_unpaid() -> None:
    adapter = _make_adapter(timeout_seconds=0.05)
    fake = _FakeBolt11(amount_msat=1_000_000, payment_hash="f" * 64)

    async def _hang(_: Any) -> Any:
        import asyncio as _a

        await _a.sleep(10)
        raise AssertionError

    lookup_resp = MagicMock()
    lookup_resp.state = _make_pending_state()
    lookup_resp.preimage = None

    fake_client = MagicMock()
    fake_client.pay_invoice = _hang
    fake_client.lookup_invoice = AsyncMock(return_value=lookup_resp)

    with (
        patch("lobster_ledger.adapters_nwc.bolt11.decode", return_value=fake),
        patch("lobster_ledger.adapters_nwc.NostrWalletConnectUri"),
        patch("lobster_ledger.adapters_nwc.Nwc", return_value=fake_client),
    ):
        ok, ext, proof = adapter.pay(_req())

    assert ok is False
    assert ext == "f" * 64
    assert proof is not None
    assert json.loads(proof) == {"error": "timeout"}


def test_pay_lnurl_resolution_disabled() -> None:
    adapter = _make_adapter(allow_lnurl=False)
    ok, ext, proof = adapter.pay(_req(counterparty=_LN_ADDRESS))
    assert ok is False
    assert ext is None
    assert proof is not None
    assert "disabled" in proof or "resolution" in proof


def test_pay_handles_nip47_error_code() -> None:
    adapter = _make_adapter()
    fake = _FakeBolt11(amount_msat=1_000_000, payment_hash="9" * 64)

    from nostr_sdk import ErrorCode

    class _FakeNip47Exc(Exception):
        # nostr_sdk surfaces NIP-47 errors as exceptions carrying code+message
        # on the instance. Mimic that shape so the adapter can detect it.
        def __init__(self, code: Any, message: str) -> None:
            super().__init__(message)
            self.code = code
            self.message = message

    fake_client = MagicMock()

    async def _raise(_: Any) -> Any:
        raise _FakeNip47Exc(ErrorCode.INSUFFICIENT_BALANCE, "not enough sats")

    fake_client.pay_invoice = _raise

    with (
        patch("lobster_ledger.adapters_nwc.bolt11.decode", return_value=fake),
        patch("lobster_ledger.adapters_nwc.NostrWalletConnectUri"),
        patch("lobster_ledger.adapters_nwc.Nwc", return_value=fake_client),
    ):
        ok, ext, proof = adapter.pay(_req())

    assert ok is False
    assert ext == "9" * 64
    assert proof is not None
    parsed = json.loads(proof)
    assert parsed["error_code"] == "INSUFFICIENT_BALANCE"
    assert parsed["message"] == "not enough sats"


def test_pay_lnurl_address_resolves_to_bolt11(respx_mock: Any) -> None:
    # Exercises the hand-rolled LUD-16 flow end-to-end with httpx mocked.
    import httpx

    adapter = _make_adapter()
    fake = _FakeBolt11(amount_msat=1_000_000, payment_hash="1" * 64)
    response = MagicMock()
    response.preimage = "22" * 32

    respx_mock.get("https://example.com/.well-known/lnurlp/alice").mock(
        return_value=httpx.Response(
            200,
            json={
                "tag": "payRequest",
                "callback": "https://example.com/lnurl/cb",
                "minSendable": 1000,
                "maxSendable": 10_000_000,
            },
        )
    )
    respx_mock.get("https://example.com/lnurl/cb").mock(
        return_value=httpx.Response(200, json={"pr": "lnbc10u1phandled"})
    )

    fake_client = MagicMock()
    fake_client.pay_invoice = AsyncMock(return_value=response)

    with (
        patch("lobster_ledger.adapters_nwc.bolt11.decode", return_value=fake),
        patch("lobster_ledger.adapters_nwc.NostrWalletConnectUri"),
        patch("lobster_ledger.adapters_nwc.Nwc", return_value=fake_client),
    ):
        ok, ext, proof = adapter.pay(_req(counterparty=_LN_ADDRESS))

    assert ok is True
    assert ext == "1" * 64
    assert proof is not None


def _make_settled_state() -> Any:
    from nostr_sdk import TransactionState

    return TransactionState.SETTLED


def _make_pending_state() -> Any:
    from nostr_sdk import TransactionState

    return TransactionState.PENDING


def test_invoice_expired_fallback_on_broken_has_expired() -> None:
    # Safety: a malformed bolt11 that raises from has_expired() must not crash
    # the adapter; it should fall back to date + expiry.
    adapter = _make_adapter()

    class _Exploding:
        amount_msat = 1_000_000
        payment_hash = "0" * 64
        date = int(time.time()) - 10_000
        expiry = 100

        def has_expired(self) -> bool:
            raise RuntimeError("boom")

    with patch("lobster_ledger.adapters_nwc.bolt11.decode", return_value=_Exploding()):
        ok, _ext, proof = adapter.pay(_req())

    assert ok is False
    assert proof is not None
    assert "expired" in proof
