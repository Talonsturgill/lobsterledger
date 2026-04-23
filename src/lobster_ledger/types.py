from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

Rail = Literal["lightning", "base", "manual"]
Direction = Literal["in", "out"]
TxStatus = Literal["pending", "settled", "denied", "failed"]
ApprovalStatus = Literal["pending", "approved", "denied"]
RuleKind = Literal[
    "spend_cap",
    "velocity",
    "allowlist",
    "blocklist",
    "category_budget",
    "require_approval_above",
]
PolicyOutcome = Literal["allow", "deny", "require_approval"]


class SpendCapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["spend_cap"]
    max_amount_usd_cents: int
    rail: Rail | None = None


class VelocityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["velocity"]
    window_seconds: int
    max_total_usd_cents: int
    rail: Rail | None = None


class AllowlistConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["allowlist"]
    entries: list[str]


class BlocklistConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["blocklist"]
    entries: list[str]


class CategoryBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["category_budget"]
    category: str
    window_seconds: int
    max_total_usd_cents: int


class RequireApprovalAboveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["require_approval_above"]
    threshold_usd_cents: int
    rail: Rail | None = None


RuleConfig = Annotated[
    SpendCapConfig
    | VelocityConfig
    | AllowlistConfig
    | BlocklistConfig
    | CategoryBudgetConfig
    | RequireApprovalAboveConfig,
    Field(discriminator="kind"),
]

RULE_CONFIG_ADAPTER: TypeAdapter[RuleConfig] = TypeAdapter(RuleConfig)


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rail: Rail
    direction: Direction = "out"
    amount_usd_fmv_cents: int
    amount_sats: int | None = None
    amount_usdc_micro: int | None = None
    counterparty: str | None = None
    category: str | None = None
    memo: str | None = None
    agent_id: str | None = None
    external_id: str | None = None
    wallet_id: int | None = None

    @model_validator(mode="after")
    def _validate_native_amounts(self) -> PaymentRequest:
        # Native amounts anchor lot accounting on both sides of the trade, so
        # outbound and inbound must each carry the correct native unit for rail.
        if self.rail == "lightning" and self.amount_sats is None:
            raise ValueError("amount_sats is required when rail is 'lightning'")
        if self.rail == "base" and self.amount_usdc_micro is None:
            raise ValueError("amount_usdc_micro is required when rail is 'base'")
        return self


class TriggeredRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: int
    name: str
    kind: RuleKind
    reason: str


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: PolicyOutcome
    reasons: list[str]
    triggered_rules: list[TriggeredRule]


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    direction: Direction
    rail: Rail
    status: TxStatus
    wallet_id: int | None = None
    amount_sats: int | None = None
    amount_usdc_micro: int | None = None
    amount_usd_fmv_cents: int
    counterparty: str | None = None
    category: str | None = None
    memo: str | None = None
    agent_id: str | None = None
    external_id: str | None = None
    raw_proof: str | None = None
    policy_json: str | None = None
    created_at: int
    settled_at: int | None = None
