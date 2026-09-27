"""办案服务命令面：案件全流程的唯一写入口。

所有状态变更都由此处校验后作为事件追加到仅追加账本，业务人员不能直接
改库。关键约束：

- 立案即锁定商品规格、活动范围与当期成本标准，此后不可改写；
- 平台补报、跨地区移送、口径修订、规则下线一律追加新事件，保留历史；
- 申辩是否限期由服务按告知期限判定，不采信当事人自填；
- 商业秘密凭案件授权开放，授权与撤销全程留痕；
- 处罚决定钉住决定前一事件的序号与哈希，复核按此重放；
- 整改必须用同一批 SKU、同一时段复核。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from .ledger import Event, Ledger
from .models import (
    ActivityScope,
    CostStandard,
    MerchantAppeal,
    PriceObservation,
    PricePointType,
    Product,
)
from .policy import find_evidence_gaps, verify_rectification
from .state import CaseState, fold


class ServiceError(Exception):
    """业务规则不允许该操作。"""


def _iso(value: date | str) -> str:
    if isinstance(value, str):
        date.fromisoformat(value)  # 校验格式
        return value
    return value.isoformat()


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


class CaseService:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    # ------------------------------------------------------------------ 读取

    def state(self, case_id: str, *, upto_seq: int | None = None) -> CaseState:
        events = self.ledger.events(case_id)
        if not events:
            raise ServiceError(f"案件不存在：{case_id}")
        self.ledger.verify(case_id)
        return fold(events, upto_seq=upto_seq)

    def state_at_head(self, case_id: str) -> CaseState:
        """还原处罚决定所钉住的历史版本。"""

        current = self.state(case_id)
        if current.decision is None:
            raise ServiceError("案件尚未作出处罚决定，无可重现的处罚版本")
        return self.state(case_id, upto_seq=current.decision.head_seq)

    # ------------------------------------------------------------------ 阶段守卫

    def _require_phase(self, state: CaseState, allowed: set[str], action: str) -> None:
        if state.phase.value not in allowed:
            raise ServiceError(
                f"当前阶段为「{state.phase.value}」，不能{action}（允许阶段：{'、'.join(sorted(allowed))}）"
            )

    # ------------------------------------------------------------------ 线索与立案

    def register_lead(
        self,
        case_id: str,
        summary: str,
        *,
        source: str = "",
        occurred_on: date | str,
        actor: str = "承办人",
    ) -> Event:
        if self.ledger.events(case_id):
            raise ServiceError("线索已登记，不能重复建案")
        return self.ledger.append(
            case_id,
            "CASE_LEAD_REGISTERED",
            actor,
            {"summary": summary, "source": source},
            _iso(occurred_on),
        )

    def open_case(
        self,
        case_id: str,
        products: Iterable[Product],
        scope: ActivityScope,
        cost_standard: CostStandard,
        *,
        occurred_on: date | str,
        actor: str = "承办人",
    ) -> Event:
        """立案：锁定商品规格、活动范围、当期成本标准。"""

        state = self.state(case_id)
        self._require_phase(state, {"线索"}, "立案")
        products = list(products)
        if not products:
            raise ServiceError("至少锁定一个商品规格")
        if len({p.sku for p in products}) != len(products):
            raise ServiceError("商品编码重复，规格必须唯一")
        if scope.start > scope.end:
            raise ServiceError("活动开始日晚于结束日")
        if not scope.channels or not scope.regions:
            raise ServiceError("活动范围须包含渠道与地区")
        # 当期成本标准必须覆盖整个活动时段，口径同期是价费比对的前提
        if cost_standard.valid_from > scope.start or cost_standard.valid_to < scope.end:
            raise ServiceError(
                f"成本标准适用期 {cost_standard.valid_from}~{cost_standard.valid_to} "
                f"未覆盖活动时段 {scope.start}~{scope.end}，不能用非当期成本立案"
            )
        return self.ledger.append(
            case_id,
            "CASE_OPENED",
            actor,
            {
                "products": [p.to_dict() for p in products],
                "scope": scope.to_dict(),
                "cost_standard": cost_standard.to_dict(),
            },
            _iso(occurred_on),
        )

    # ------------------------------------------------------------------ 证据

    def add_evidence(
        self,
        case_id: str,
        ref: str,
        kind: str,
        summary: str,
        *,
        confidential: bool = False,
        occurred_on: date | str,
        actor: str = "承办人",
    ) -> Event:
        state = self.state(case_id)
        self._require_phase(state, {"已立案", "申辩期", "已结案"}, "登记证据")
        if ref in state.evidence:
            raise ServiceError(f"证据编号 {ref} 已存在，证据只能追加不能覆盖")
        return self.ledger.append(
            case_id,
            "EVIDENCE_ADDED",
            actor,
            {
                "ref": ref,
                "kind": kind,
                "summary": summary,
                "confidential": confidential,
            },
            _iso(occurred_on),
        )

    def _check_observation(self, state: CaseState, obs: PriceObservation, *, supplement: bool) -> None:
        if state.scope is None:
            raise ServiceError("调查范围未锁定")
        if obs.sku not in state.products:
            raise ServiceError(f"价格观测的商品 {obs.sku} 不在立案锁定的规格内")
        if obs.channel not in state.scope.channels:
            raise ServiceError(f"渠道 {obs.channel} 不在立案活动范围内")
        if obs.region not in state.scope.regions:
            raise ServiceError(f"地区 {obs.region} 不在立案活动范围内")
        start, end = state.window
        if obs.point_type is PricePointType.PRE_PROMO:
            earliest = start - timedelta(days=30)
            if not (earliest <= obs.observed_on <= end):
                raise ServiceError(f"促前标价日期 {obs.observed_on} 超出可采证区间（{earliest} 起）")
        elif not (start <= obs.observed_on <= end):
            raise ServiceError(
                f"价格观测的成交日 {obs.observed_on} 不在锁定调查时段 {start}~{end} 内"
            )
        if supplement and obs.point_type is not PricePointType.PLATFORM_REPORTED:
            raise ServiceError("平台补报必须使用「平台补报」价格口径，避免与原始观测混淆")
        if not supplement and obs.point_type is PricePointType.PLATFORM_REPORTED:
            raise ServiceError("「平台补报」口径只能通过平台补报通道登记")

    def record_price(
        self,
        case_id: str,
        observation: PriceObservation,
        *,
        occurred_on: date | str,
        actor: str = "承办人",
    ) -> Event:
        state = self.state(case_id)
        self._require_phase(state, {"已立案", "申辩期"}, "登记价格证据")
        self._check_observation(state, observation, supplement=False)
        return self.ledger.append(
            case_id,
            "PRICE_RECORDED",
            actor,
            {"observation": observation.to_dict()},
            _iso(occurred_on),
        )

    def platform_report_price(
        self,
        case_id: str,
        observation: PriceObservation,
        *,
        occurred_on: date | str,
        actor: str = "平台企业",
    ) -> Event:
        """平台事后补报：只能追加在原始记录之后，永不覆盖原观测。"""

        state = self.state(case_id)
        self._require_phase(state, {"已立案", "申辩期"}, "接收平台补报")
        # 调用方必须显式使用「平台补报」口径，服务不得静默改写原始口径；
        # 校验通过后与原始观测并存，永不覆盖。
        self._check_observation(state, observation, supplement=True)
        return self.ledger.append(
            case_id,
            "PLATFORM_REPORTED",
            actor,
            {"observation": observation.to_dict()},
            _iso(occurred_on),
        )

    def record_rectification_price(
        self,
        case_id: str,
        observation: PriceObservation,
        *,
        occurred_on: date | str,
        actor: str = "复核人员",
    ) -> Event:
        """整改阶段对同批产品、原活动时段的重新定价固证。

        单列「整改复核价」口径，经同一时段校验，但不进入处罚所依据的价格池，
        因此既能用同一时段核验，又不会污染处罚版本的均价。
        """

        state = self.state(case_id)
        self._require_phase(state, {"已结案", "已核验"}, "登记整改复核价")
        if state.rectification is None:
            raise ServiceError("尚未下达整改计划")
        if observation.sku not in state.rectification.skus:
            raise ServiceError(f"复核商品 {observation.sku} 不在整改的同批产品内")
        start, end = state.window
        if not (start <= observation.observed_on <= end):
            raise ServiceError(
                f"复核价观测日 {observation.observed_on} 必须落在原活动时段 {start}~{end}"
            )
        if observation.channel not in state.scope.channels:
            raise ServiceError(f"渠道 {observation.channel} 不在立案活动范围内")
        if observation.region not in state.scope.regions:
            raise ServiceError(f"地区 {observation.region} 不在立案活动范围内")
        payload = PriceObservation(
            **{**observation.__dict__, "point_type": PricePointType.RECTIFICATION}
        )
        return self.ledger.append(
            case_id,
            "PRICE_RECORDED",
            actor,
            {"observation": payload.to_dict()},
            _iso(occurred_on),
        )

    def record_quality(
        self,
        case_id: str,
        inspection: dict,
        *,
        occurred_on: date | str,
        actor: str = "质量监管人员",
    ) -> Event:
        """登记抽检结果（dict 形式，模型由状态层还原）。"""

        state = self.state(case_id)
        self._require_phase(state, {"已立案", "申辩期", "已结案"}, "登记抽检结果")
        if inspection["sku"] not in state.products:
            raise ServiceError(f"抽检商品 {inspection['sku']} 不在立案规格内")
        return self.ledger.append(
            case_id,
            "QUALITY_RECORDED",
            actor,
            {"inspection": inspection},
            _iso(occurred_on),
        )

    def record_offline_txn(
        self,
        case_id: str,
        txn: dict,
        *,
        occurred_on: date | str,
        actor: str = "承办人",
    ) -> Event:
        state = self.state(case_id)
        self._require_phase(state, {"已立案", "申辩期"}, "登记线下交易凭证")
        if txn["sku"] not in state.products:
            raise ServiceError(f"线下交易商品 {txn['sku']} 不在立案规格内")
        return self.ledger.append(
            case_id,
            "OFFLINE_TXN_RECORDED",
            actor,
            {"txn": txn},
            _iso(occurred_on),
        )

    # ------------------------------------------------------------------ 移送

    def transfer_case(
        self,
        case_id: str,
        to_region: str,
        reason: str,
        *,
        material_refs: Iterable[str] = (),
        occurred_on: date | str,
        actor: str = "承办人",
    ) -> Event:
        state = self.state(case_id)
        self._require_phase(state, {"已立案", "申辩期"}, "跨地区移送")
        refs = list(material_refs)
        missing = [r for r in refs if r not in state.evidence]
        if missing:
            raise ServiceError(f"移送材料编号不存在：{'、'.join(missing)}")
        from_region = state.scope.regions[0] if state.scope else "本机辖区"
        return self.ledger.append(
            case_id,
            "CASE_TRANSFERRED",
            actor,
            {
                "from_region": from_region,
                "to_region": to_region,
                "reason": reason,
                "material_refs": refs,
            },
            _iso(occurred_on),
        )

    def receipt_transfer(
        self,
        case_id: str,
        to_region: str,
        *,
        occurred_on: date | str,
        actor: str = "协办地区",
    ) -> Event:
        state = self.state(case_id)
        if not any(t.to_region == to_region and not t.receipt for t in state.transfers):
            raise ServiceError(f"不存在向 {to_region} 的未回执移送")
        return self.ledger.append(
            case_id,
            "TRANSFER_RECEIPTED",
            actor,
            {"to_region": to_region},
            _iso(occurred_on),
        )

    # ------------------------------------------------------------------ 口径修订与规则下线

    def revise_caliber(
        self,
        case_id: str,
        caliber: str,
        new_value: str,
        reason: str,
        *,
        occurred_on: date | str,
        actor: str = "承办人",
    ) -> Event:
        state = self.state(case_id)
        self._require_phase(state, {"已立案", "申辩期"}, "修订口径")
        old_value = state.calibers.get(caliber, "（初次确立）")
        if old_value == new_value:
            raise ServiceError("新旧口径一致，无需修订")
        return self.ledger.append(
            case_id,
            "CALIBER_REVISED",
            actor,
            {
                "caliber": caliber,
                "old_value": old_value,
                "new_value": new_value,
                "reason": reason,
            },
            _iso(occurred_on),
        )

    def take_down_rule(
        self,
        case_id: str,
        rule_id: str,
        platform: str,
        reason: str,
        *,
        occurred_on: date | str,
        actor: str = "平台企业",
    ) -> Event:
        state = self.state(case_id)
        self._require_phase(state, {"已立案", "申辩期", "已结案"}, "规则下线")
        if any(r.rule_id == rule_id for r in state.rules_takedown):
            raise ServiceError(f"规则 {rule_id} 已下线，记录只能追加不能重复")
        return self.ledger.append(
            case_id,
            "RULE_TAKEN_DOWN",
            actor,
            {"rule_id": rule_id, "platform": platform, "reason": reason},
            _iso(occurred_on),
        )

    # ------------------------------------------------------------------ 申辩

    def notice_hearing(
        self,
        case_id: str,
        *,
        deadline: date | str,
        occurred_on: date | str,
        actor: str = "承办人",
    ) -> Event:
        state = self.state(case_id)
        self._require_phase(state, {"已立案"}, "告知当事人陈述申辩")
        deadline_d = _as_date(deadline)
        if deadline_d < _as_date(occurred_on):
            raise ServiceError("申辩截止日早于告知日")
        return self.ledger.append(
            case_id,
            "HEARING_NOTICED",
            actor,
            {"deadline": _iso(deadline)},
            _iso(occurred_on),
        )

    def file_appeal(
        self,
        case_id: str,
        appeal_id: str,
        grounds: str,
        *,
        clearance_inventory: int | None = None,
        evidence_refs: Iterable[str] = (),
        submitted_on: date | str,
        actor: str = "被调查经营者",
    ) -> Event:
        """登记商家申辩；是否在限期内由系统按告知期限判定。"""

        state = self.state(case_id)
        self._require_phase(state, {"申辩期"}, "提交申辩")
        if any(a.appeal_id == appeal_id for a in state.appeals):
            raise ServiceError(f"申辩 {appeal_id} 已登记，重复提交应另行编号追加")
        refs = list(evidence_refs)
        missing = [r for r in refs if r not in state.evidence]
        if missing:
            raise ServiceError(f"申辩引用的证据不存在：{'、'.join(missing)}")
        submitted_d = _as_date(submitted_on)
        within = submitted_d <= state.hearing_deadline
        appeal = MerchantAppeal(
            appeal_id=appeal_id,
            submitted_on=submitted_d,
            grounds=grounds,
            clearance_inventory=clearance_inventory,
            evidence_refs=tuple(refs),
            within_deadline=within,
        )
        return self.ledger.append(
            case_id,
            "APPEAL_FILED",
            actor,
            {"appeal": appeal.to_dict()},
            _iso(submitted_on),
        )

    # ------------------------------------------------------------------ 商业秘密授权

    def grant_secret_access(
        self,
        case_id: str,
        grant_id: str,
        requester: str,
        purpose: str,
        material_refs: Iterable[str],
        *,
        expires_on: date | str | None = None,
        occurred_on: date | str,
        actor: str = "部门负责人",
    ) -> Event:
        state = self.state(case_id)
        refs = list(material_refs)
        if not refs:
            raise ServiceError("授权必须明确开放的材料范围")
        missing = [r for r in refs if r not in state.evidence]
        if missing:
            raise ServiceError(f"授权材料不存在：{'、'.join(missing)}")
        not_secret = [r for r in refs if not state.evidence[r].confidential]
        if not_secret:
            raise ServiceError(f"以下材料非商业秘密，无需授权：{'、'.join(not_secret)}")
        if any(g.grant_id == grant_id for g in state.secret_grants):
            raise ServiceError("授权编号重复")
        return self.ledger.append(
            case_id,
            "SECRET_ACCESS_GRANTED",
            actor,
            {
                "grant_id": grant_id,
                "requester": requester,
                "purpose": purpose,
                "material_refs": refs,
                "expires_on": _iso(expires_on) if expires_on else None,
            },
            _iso(occurred_on),
        )

    def revoke_secret_access(
        self,
        case_id: str,
        grant_id: str,
        *,
        reason: str,
        occurred_on: date | str,
        actor: str = "部门负责人",
    ) -> Event:
        state = self.state(case_id)
        active = [g for g in state.secret_grants if g.grant_id == grant_id and g.active]
        if not active:
            raise ServiceError(f"授权 {grant_id} 不存在或已撤销")
        return self.ledger.append(
            case_id,
            "SECRET_ACCESS_REVOKED",
            actor,
            {"grant_id": grant_id, "reason": reason},
            _iso(occurred_on),
        )

    def visible_evidence(self, case_id: str, requester: str, *, on: date | str) -> dict[str, bool]:
        """返回某查阅人在指定日期对各证据（含商业秘密）的可见性。"""

        state = self.state(case_id)
        on_d = _as_date(on)
        result: dict[str, bool] = {}
        for ref, item in state.evidence.items():
            if not item.confidential:
                result[ref] = True
                continue
            result[ref] = any(
                g.active
                and g.requester == requester
                and ref in g.material_refs
                and (g.expires_on is None or g.expires_on >= on_d)
                for g in state.secret_grants
            )
        return result

    # ------------------------------------------------------------------ 处罚与整改

    def decide_penalty(
        self,
        case_id: str,
        decision_id: str,
        findings: str,
        *,
        inputs: Iterable[str],
        rules_known: Iterable[str] = (),
        occurred_on: date | str,
        allow_blocking_gaps: bool = False,
        actor: str = "部门负责人",
    ) -> Event:
        """作出处罚决定，同时钉住所依据的链头、口径与规则状态。"""

        state = self.state(case_id)
        self._require_phase(state, {"申辩期"}, "作出处罚决定")
        refs = list(inputs)
        missing = [r for r in refs if r not in state.evidence]
        if missing:
            raise ServiceError(f"处罚引用的证据不存在：{'、'.join(missing)}")
        blocking = [g.to_dict() for g in find_evidence_gaps(state) if g.severity == "阻断"]
        if blocking and not allow_blocking_gaps:
            raise ServiceError(
                "存在阻断性取证缺口，不能钉住处罚版本："
                + "；".join(f"{g['code']} {g['title']}" for g in blocking)
            )
        return self.ledger.append(
            case_id,
            "PENALTY_DECIDED",
            actor,
            {
                "decision_id": decision_id,
                "findings": findings,
                "inputs": refs,
                "calibers_snapshot": dict(state.calibers),
                "rules_known": sorted(set(rules_known)),
                "blocking_gaps_waived": blocking if allow_blocking_gaps else [],
            },
            _iso(occurred_on),
        )

    def require_rectification(
        self,
        case_id: str,
        plan_id: str,
        *,
        due_on: date | str,
        occurred_on: date | str,
        skus: Iterable[str] | None = None,
        actor: str = "承办人",
    ) -> Event:
        """下达整改计划：默认覆盖同批产品，核验窗口强制复用原活动时段。"""

        state = self.state(case_id)
        self._require_phase(state, {"已结案"}, "下达整改要求")
        target_skus = list(skus) if skus is not None else sorted(state.products)
        unknown = [s for s in target_skus if s not in state.products]
        if unknown:
            raise ServiceError(f"整改产品不在原案规格内：{'、'.join(unknown)}")
        start, end = state.window
        return self.ledger.append(
            case_id,
            "RECTIFICATION_REQUIRED",
            actor,
            {
                "plan_id": plan_id,
                "due_on": _iso(due_on),
                "skus": target_skus,
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
            },
            _iso(occurred_on),
        )

    def submit_rectification(
        self,
        case_id: str,
        material_refs: Iterable[str],
        *,
        occurred_on: date | str,
        actor: str = "被调查经营者",
    ) -> Event:
        state = self.state(case_id)
        self._require_phase(state, {"已结案"}, "提交整改材料")
        if state.rectification is None or state.rectification.submitted_on is not None:
            raise ServiceError("整改计划不存在或材料已提交（补充材料须另行登记证据并重新核验）")
        refs = list(material_refs)
        missing = [r for r in refs if r not in state.evidence]
        if missing:
            raise ServiceError(f"整改材料编号不存在：{'、'.join(missing)}")
        return self.ledger.append(
            case_id,
            "RECTIFICATION_SUBMITTED",
            actor,
            {"material_refs": refs},
            _iso(occurred_on),
        )

    def verify_rectification(
        self,
        case_id: str,
        *,
        occurred_on: date | str,
        actor: str = "复核人员",
    ) -> dict:
        """按同一批产品、同一时段执行核验，并把结论追加留痕。"""

        state = self.state(case_id)
        self._require_phase(state, {"已结案", "已核验"}, "整改核验")
        if state.rectification is None or state.rectification.submitted_on is None:
            raise ServiceError("整改材料尚未提交，不能核验")
        result = verify_rectification(state, today=_as_date(occurred_on))
        failed = [c["item"] for c in result["checks"] if not c["passed"]]
        self.ledger.append(
            case_id,
            "RECTIFICATION_VERIFIED",
            actor,
            {
                "passed": result["passed"],
                "note": ("全部核验项通过" if result["passed"] else "未通过：" + "；".join(failed)),
            },
            _iso(occurred_on),
        )
        return result
