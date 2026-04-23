from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lobster_ledger.adapters_cdp import CdpBaseUsdcAdapter, CdpConfig
from lobster_ledger.types import PaymentRequest

_VALID_ADDRESS = "0x9F663335Cd6Ad02a37B633602E98866CF944124d"
_TX_HASH = "0xabc0000000000000000000000000000000000000000000000000000000000001"


def _make_request(
    counterparty: str | None = _VALID_ADDRESS,
    amount_usdc_micro: int | None = 10_000,
) -> PaymentRequest:
    return PaymentRequest(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=1,
        amount_usdc_micro=amount_usdc_micro,
        counterparty=counterparty,
    )


def _testnet_config(paymaster_url: str | None = None) -> CdpConfig:
    return CdpConfig(
        network="base-sepolia",
        account_name="lobster-ledger-hot",
        paymaster_url=paymaster_url,
        receipt_timeout_seconds=5.0,
    )


def _install_fake_cdp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transfer: AsyncMock | None = None,
    receipt: Any | None = None,
    wait_exc: BaseException | None = None,
    get_account: AsyncMock | None = None,
) -> dict[str, Any]:
    # Stand in for `from cdp import CdpClient`. Returning a pre-built module lets us assert
    # the adapter's call shape without importing the real SDK inside tests.
    fake_module = types.ModuleType("cdp")

    wait_mock = AsyncMock()
    if wait_exc is not None:
        wait_mock.side_effect = wait_exc
    else:
        wait_mock.return_value = receipt

    scoped = MagicMock()
    scoped.wait_for_transaction_receipt = wait_mock

    account = MagicMock()
    account.transfer = transfer if transfer is not None else AsyncMock(return_value=_TX_HASH)
    account.use_network = MagicMock(return_value=scoped)

    evm = MagicMock()
    evm.get_account = get_account if get_account is not None else AsyncMock(return_value=account)

    client = MagicMock()
    client.evm = evm
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    cdp_client_cls = MagicMock(return_value=client)
    fake_module.CdpClient = cdp_client_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cdp", fake_module)
    return {
        "module": fake_module,
        "client": client,
        "evm": evm,
        "account": account,
        "scoped": scoped,
        "wait": wait_mock,
        "cdp_client_cls": cdp_client_cls,
    }


def test_config_mainnet_blocked_without_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LL_CDP_NETWORK", "base")
    monkeypatch.setenv("LL_CDP_ACCOUNT_NAME", "lobster-hot")
    monkeypatch.delenv("LL_ALLOW_MAINNET", raising=False)
    with pytest.raises(RuntimeError, match="mainnet blocked"):
        CdpConfig.from_env()


def test_config_mainnet_allowed_with_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LL_CDP_NETWORK", "base")
    monkeypatch.setenv("LL_CDP_ACCOUNT_NAME", "lobster-hot")
    monkeypatch.setenv("LL_ALLOW_MAINNET", "1")
    cfg = CdpConfig.from_env()
    assert cfg.network == "base"
    assert cfg.account_name == "lobster-hot"


def test_config_rejects_invalid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LL_CDP_NETWORK", "ethereum")
    monkeypatch.setenv("LL_CDP_ACCOUNT_NAME", "lobster-hot")
    with pytest.raises(ValueError, match="invalid network"):
        CdpConfig.from_env()


def test_config_testnet_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LL_CDP_NETWORK", raising=False)
    monkeypatch.delenv("LL_ALLOW_MAINNET", raising=False)
    monkeypatch.setenv("LL_CDP_ACCOUNT_NAME", "lobster-hot")
    cfg = CdpConfig.from_env()
    assert cfg.network == "base-sepolia"
    assert cfg.paymaster_url is None
    assert cfg.receipt_timeout_seconds == 60.0


def test_pay_rejects_invalid_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_cdp(monkeypatch)
    adapter = CdpBaseUsdcAdapter(config=_testnet_config())
    ok, tx_hash, proof = adapter.pay(_make_request(counterparty="notahex"))
    assert ok is False
    assert tx_hash is None
    assert proof is not None
    assert "error" in json.loads(proof)


def test_pay_rejects_non_usdc_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    # PaymentRequest validator forces amount_usdc_micro for base rail at construction time,
    # so we bypass via model_construct to exercise the adapter's own guard.
    _install_fake_cdp(monkeypatch)
    req = PaymentRequest.model_construct(
        rail="base",
        direction="out",
        amount_usd_fmv_cents=1,
        amount_usdc_micro=None,
        counterparty=_VALID_ADDRESS,
    )
    adapter = CdpBaseUsdcAdapter(config=_testnet_config())
    ok, tx_hash, proof = adapter.pay(req)
    assert ok is False
    assert tx_hash is None
    assert proof is not None
    assert "amount_usdc_micro" in json.loads(proof)["error"]


def test_pay_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = {"status": 1, "transactionHash": _TX_HASH, "blockNumber": 42}
    fakes = _install_fake_cdp(monkeypatch, receipt=receipt)
    adapter = CdpBaseUsdcAdapter(config=_testnet_config())
    ok, tx_hash, proof = adapter.pay(_make_request())
    assert ok is True
    assert tx_hash == _TX_HASH
    assert proof is not None
    parsed = json.loads(proof)
    assert parsed["status"] == 1
    fakes["account"].transfer.assert_awaited_once()
    kwargs = fakes["account"].transfer.await_args.kwargs
    assert kwargs["to"] == _VALID_ADDRESS
    assert kwargs["amount"] == 10_000
    assert kwargs["token"] == "usdc"
    assert kwargs["network"] == "base-sepolia"
    assert "paymaster_url" not in kwargs


def test_pay_receipt_status_zero_means_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = {"status": 0, "transactionHash": _TX_HASH}
    _install_fake_cdp(monkeypatch, receipt=receipt)
    adapter = CdpBaseUsdcAdapter(config=_testnet_config())
    ok, tx_hash, proof = adapter.pay(_make_request())
    assert ok is False
    assert tx_hash == _TX_HASH
    assert proof is not None
    assert json.loads(proof)["status"] == 0


def test_pay_handles_transfer_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_transfer = AsyncMock(side_effect=RuntimeError("rpc down"))
    _install_fake_cdp(monkeypatch, transfer=bad_transfer)
    adapter = CdpBaseUsdcAdapter(config=_testnet_config())
    ok, tx_hash, proof = adapter.pay(_make_request())
    assert ok is False
    assert tx_hash is None
    assert proof is not None
    assert "rpc down" in json.loads(proof)["error"]


def test_pay_receipt_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_cdp(monkeypatch, wait_exc=TimeoutError("receipt deadline"))
    adapter = CdpBaseUsdcAdapter(config=_testnet_config())
    ok, tx_hash, proof = adapter.pay(_make_request())
    assert ok is False
    # Transfer already submitted before the wait timed out, so we report the hash for reconcile.
    assert tx_hash == _TX_HASH
    assert proof is not None
    assert "receipt deadline" in json.loads(proof)["error"]


def test_pay_passes_paymaster_url_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = {"status": 1, "transactionHash": _TX_HASH}
    fakes = _install_fake_cdp(monkeypatch, receipt=receipt)
    adapter = CdpBaseUsdcAdapter(
        config=_testnet_config(paymaster_url="https://paymaster.example/rpc")
    )
    ok, _, _ = adapter.pay(_make_request())
    assert ok is True
    kwargs = fakes["account"].transfer.await_args.kwargs
    assert kwargs["paymaster_url"] == "https://paymaster.example/rpc"


def test_pay_serializes_pydantic_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    class Receipt:
        status = 1

        def model_dump(self) -> dict[str, Any]:
            return {"status": 1, "transactionHash": _TX_HASH, "gasUsed": 21000}

    _install_fake_cdp(monkeypatch, receipt=Receipt())
    adapter = CdpBaseUsdcAdapter(config=_testnet_config())
    ok, tx_hash, proof = adapter.pay(_make_request())
    assert ok is True
    assert tx_hash == _TX_HASH
    assert proof is not None
    parsed = json.loads(proof)
    assert parsed["gasUsed"] == 21000


def test_adapter_rail_matches_protocol() -> None:
    # Static attribute so the payments selector can route by rail without constructing.
    assert CdpBaseUsdcAdapter.rail == "base"


def test_adapter_init_loads_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LL_CDP_NETWORK", "base-sepolia")
    monkeypatch.setenv("LL_CDP_ACCOUNT_NAME", "env-account")
    monkeypatch.delenv("LL_ALLOW_MAINNET", raising=False)
    with patch.object(CdpConfig, "from_env", wraps=CdpConfig.from_env) as spy:
        adapter = CdpBaseUsdcAdapter()
    spy.assert_called_once()
    assert adapter._config.account_name == "env-account"
