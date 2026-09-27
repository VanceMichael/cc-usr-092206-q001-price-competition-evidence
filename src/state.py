"""把仅追加事件流折叠为案件当前（或历史任一序号的）状态。

复核人员把事件重放到处罚决定钉住的 ``head_seq``，即可还原当时所依据的
全部价格、成本、抽检、口径版本与规则状态——这正是"重现处罚版本"的含义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .ledger import Event
from .models import (
    ActivityScope,
    CasePhase,
    CostStandard,
    MerchantAppeal,
    OfflineTransaction,
    PriceObservation,
    Product,
    QualityInspection,
)


@dataclass
class TransferRecord:
    seq: int
    occurred_on: date
    from_region: str
    to_region: str
    reason: str
    material_refs: tuple[str, ...]
    receipt: bool = False          # 移送回执，后续以追加事件补登


@dataclass(frozen=True)
class CaliberRevision:
    """口径修订：老版本不删除，只保留在历史里。"""

    seq: int
    occurred_on: date
    caliber: str                   # 口径名称，如"券后成交价口径"
    old_value: str
    new_value: str
    reason: str


@dataclass(frozen=True)
class RuleTakedown:
    seq: int
    occurred_on: date
    rule_id: str
    platform: str
    reason: str


@dataclass
class SecretGrant:
    """授权可被后续撤销事件置为失效，因此不是 frozen。"""

    seq: int
    grant_id: str
    requester: str
    purpose: str
    material_refs: tuple[str, ...]
    granted_on: date
    expires_on: date | None
    active: bool = True
    revoke_seq: int | None = None
    revoke_reason: str = ""


@dataclass
class EvidenceItem:
    ref: str
    kind: str
    summary: str
    confidential: bool
    recorded_seq: int


@dataclass
class PenaltyDecision:
    decision_id: str
    decided_on: date
    findings: str
    head_seq: int                  # 钉住：处罚依据的事件序号
    head_hash: str                 # 钉住：处罚依据的链头哈希
    inputs: tuple[str, ...]        # 引用的证据编号
    calibers: dict[str, str]       # 钉住时刻各口径取值
    rules_active: tuple[str, ...]  # 钉住时刻仍有效的平台规则


@dataclass
class RectificationPlan:
    plan_id: str
    required_on: date
    due_on: date
    skus: tuple[str, ...]
    window_start: date             # 核验须复用的同一时段
    window_end: date
    materials_refs: tuple[str, ...] = ()
    submitted_on: date | None = None
    verified_on: date | None = None
    passed: bool | None = None
    verify_note: str = ""


@dataclass
class CaseState:
    case_id: str
    phase: CasePhase = CasePhase.LEAD
    summary: str = ""
    lead_source: str = ""
    opened_on: date | None = None
    products: dict[str, Product] = field(default_factory=dict)
    scope: ActivityScope | None = None
    cost_standard: CostStandard | None = None
    prices: list[PriceObservation] = field(default_factory=list)
    rect_prices: list[PriceObservation] = field(default_factory=list)
    quality: list[QualityInspection] = field(default_factory=list)
    appeals: list[MerchantAppeal] = field(default_factory=list)
    offline_txns: list[OfflineTransaction] = field(default_factory=list)
    evidence: dict[str, EvidenceItem] = field(default_factory=dict)
    transfers: list[TransferRecord] = field(default_factory=list)
    caliber_history: list[CaliberRevision] = field(default_factory=list)
    calibers: dict[str, str] = field(default_factory=dict)
    rules_takedown: list[RuleTakedown] = field(default_factory=list)
    hearing_deadline: date | None = None
    hearing_noticed_on: date | None = None
    secret_grants: list[SecretGrant] = field(default_factory=list)
    decision: PenaltyDecision | None = None
    rectification: RectificationPlan | None = None
    head_seq: int = 0
    head_hash: str = ""
    as_of: date | None = None      # 账本中最新的业务日期，供期限类检查取"今天"

    @property
    def window(self) -> tuple[date, date]:
        """立案锁定的调查时段（活动期间）。"""

        if self.scope is None:
            raise ValueError("调查范围尚未锁定")
        return self.scope.start, self.scope.end

    def prices_in_window(self, sku: str) -> list[PriceObservation]:
        start, end = self.window
        return [
            p
            for p in self.prices
            if p.sku == sku and start <= p.observed_on <= end
        ]

    def latest_quality(self, sku: str) -> QualityInspection | None:
        items = [q for q in self.quality if q.sku == sku]
        return items[-1] if items else None


def _d(value: str) -> date:
    return date.fromisoformat(value)


def fold(events: list[Event], *, upto_seq: int | None = None) -> CaseState:
    """按序重放事件；``upto_seq`` 可还原历史版本状态。"""

    if not events:
        raise ValueError("案件事件流为空")
    case_id = events[0].case_id
    state = CaseState(case_id=case_id)
    bound = upto_seq if upto_seq is not None else len(events)

    for event in events[:bound]:
        state.head_seq = event.seq
        state.head_hash = event.hash
        event_date = _d(event.occurred_on)
        if state.as_of is None or event_date > state.as_of:
            state.as_of = event_date
        p = event.payload
        et = event.event_type

        if et == "CASE_LEAD_REGISTERED":
            state.summary = p["summary"]
            state.lead_source = p.get("source", "")

        elif et == "CASE_OPENED":
            state.phase = CasePhase.OPENED
            state.opened_on = _d(event.occurred_on)
            for item in p["products"]:
                prod = Product.from_dict(item)
                state.products[prod.sku] = prod
            state.scope = ActivityScope.from_dict(p["scope"])
            state.cost_standard = CostStandard.from_dict(p["cost_standard"])

        elif et == "EVIDENCE_ADDED":
            state.evidence[p["ref"]] = EvidenceItem(
                ref=p["ref"],
                kind=p["kind"],
                summary=p["summary"],
                confidential=bool(p.get("confidential", False)),
                recorded_seq=event.seq,
            )

        elif et == "PRICE_RECORDED":
            obs = PriceObservation.from_dict(p["observation"])
            # 整改复核价是处罚后、对同一时段的重新定价固证，单列以免污染
            # 处罚所依据的促销价集合；两类价格永不相互平均。
            if obs.point_type.value == "整改复核价":
                state.rect_prices.append(obs)
            else:
                state.prices.append(obs)

        elif et == "PLATFORM_REPORTED":
            # 平台补报与原始观测同样保存，但类型自带"补报"口径，永不覆盖原值。
            state.prices.append(PriceObservation.from_dict(p["observation"]))

        elif et == "QUALITY_RECORDED":
            state.quality.append(QualityInspection.from_dict(p["inspection"]))

        elif et == "OFFLINE_TXN_RECORDED":
            state.offline_txns.append(OfflineTransaction.from_dict(p["txn"]))

        elif et == "CASE_TRANSFERRED":
            state.transfers.append(
                TransferRecord(
                    seq=event.seq,
                    occurred_on=_d(event.occurred_on),
                    from_region=p["from_region"],
                    to_region=p["to_region"],
                    reason=p["reason"],
                    material_refs=tuple(p.get("material_refs", [])),
                )
            )

        elif et == "TRANSFER_RECEIPTED":
            for t in state.transfers:
                if t.to_region == p["to_region"] and not t.receipt:
                    t.receipt = True
                    break

        elif et == "CALIBER_REVISED":
            state.caliber_history.append(
                CaliberRevision(
                    seq=event.seq,
                    occurred_on=_d(event.occurred_on),
                    caliber=p["caliber"],
                    old_value=p["old_value"],
                    new_value=p["new_value"],
                    reason=p["reason"],
                )
            )
            state.calibers[p["caliber"]] = p["new_value"]

        elif et == "RULE_TAKEN_DOWN":
            state.rules_takedown.append(
                RuleTakedown(
                    seq=event.seq,
                    occurred_on=_d(event.occurred_on),
                    rule_id=p["rule_id"],
                    platform=p["platform"],
                    reason=p["reason"],
                )
            )

        elif et == "HEARING_NOTICED":
            state.phase = CasePhase.HEARING
            state.hearing_noticed_on = _d(event.occurred_on)
            state.hearing_deadline = _d(p["deadline"])

        elif et == "APPEAL_FILED":
            state.appeals.append(MerchantAppeal.from_dict(p["appeal"]))

        elif et == "SECRET_ACCESS_GRANTED":
            state.secret_grants.append(
                SecretGrant(
                    seq=event.seq,
                    grant_id=p["grant_id"],
                    requester=p["requester"],
                    purpose=p["purpose"],
                    material_refs=tuple(p.get("material_refs", [])),
                    granted_on=_d(event.occurred_on),
                    expires_on=_d(p["expires_on"]) if p.get("expires_on") else None,
                )
            )

        elif et == "SECRET_ACCESS_REVOKED":
            for g in reversed(state.secret_grants):
                if g.grant_id == p["grant_id"] and g.active:
                    g.active = False
                    g.revoke_seq = event.seq
                    g.revoke_reason = p.get("reason", "")
                    break

        elif et == "PENALTY_DECIDED":
            state.phase = CasePhase.CLOSED
            # 钉住时刻仍有效的平台规则：登记的规则全集减去已下线规则。
            active_rules = tuple(
                sorted(
                    set(p.get("rules_known", ()))
                    - {r.rule_id for r in state.rules_takedown}
                )
            )
            state.decision = PenaltyDecision(
                decision_id=p["decision_id"],
                decided_on=_d(event.occurred_on),
                findings=p["findings"],
                head_seq=event.seq - 1,          # 决定本身之前的链头
                head_hash=event.prev_hash,
                inputs=tuple(p["inputs"]),
                calibers=dict(p.get("calibers_snapshot", {})),
                rules_active=active_rules,
            )

        elif et == "RECTIFICATION_REQUIRED":
            state.rectification = RectificationPlan(
                plan_id=p["plan_id"],
                required_on=_d(event.occurred_on),
                due_on=_d(p["due_on"]),
                skus=tuple(p["skus"]),
                window_start=_d(p["window_start"]),
                window_end=_d(p["window_end"]),
            )

        elif et == "RECTIFICATION_SUBMITTED":
            if state.rectification is None:
                raise ValueError("不存在整改计划，无法登记整改材料")
            state.rectification.materials_refs = tuple(p["material_refs"])
            state.rectification.submitted_on = _d(event.occurred_on)

        elif et == "RECTIFICATION_VERIFIED":
            if state.rectification is None:
                raise ValueError("不存在整改计划，无法登记核验结果")
            state.rectification.verified_on = _d(event.occurred_on)
            state.rectification.passed = bool(p["passed"])
            state.rectification.verify_note = p.get("note", "")
            if p["passed"]:
                state.phase = CasePhase.RECTIFIED

        else:
            raise ValueError(f"未知事件类型：{et}")

    return state
