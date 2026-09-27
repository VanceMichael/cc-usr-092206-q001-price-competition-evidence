"""调查策略：取证缺口、价费比对、风险联动与整改核验。

所有规则函数均为纯函数，输入案件状态，输出结构化结论，便于复核与测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import (
    Confidence,
    PricePointType,
    QualityResult,
    RiskLevel,
)
from .state import CaseState

# 价格畸低阈值：促销成交均价低于当期单位成本（价费比 < 1 即低于成本）
BELOW_COST_RATIO = 1.0
# 仅 A/B 级固定或后台证据可单独支撑认定；C 级截图需有旁证
STRONG_CONFIDENCE = {Confidence.A, Confidence.B}
# 列入重点监管、价低且抽检不合格的品类触发高风险联动
KEY_CATEGORIES = {"家用电器", "儿童用品", "食品相关产品", "装修材料"}


@dataclass(frozen=True)
class Gap:
    code: str
    title: str
    detail: str
    severity: str                   # 阻断 / 重要 / 提示

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class PriceAssessment:
    sku: str
    cost: float
    promo_avg: float | None
    daily_avg: float | None
    ratio: float | None             # promo_avg / cost
    below_cost: bool
    clearance_justified: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "cost": self.cost,
            "promo_avg": self.promo_avg,
            "daily_avg": self.daily_avg,
            "ratio": round(self.ratio, 4) if self.ratio is not None else None,
            "below_cost": self.below_cost,
            "clearance_justified": self.clearance_justified,
            "note": self.note,
        }


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def assess_price(state: CaseState, sku: str, *, use_rectification: bool = False) -> PriceAssessment:
    """对单个 SKU 做价费比对。

    默认比对调查取证池；``use_rectification=True`` 时比对整改复核价池——
    两池独立留存，处罚后的重新定价不会拉低（或抬高）处罚所依据的均价。
    清仓申辩需在限期内且附库存佐证才予采纳。
    """

    if state.cost_standard is None:
        raise ValueError("当期成本标准未锁定")
    cost = state.cost_standard.unit_cost
    pool = state.rect_prices if use_rectification else state.prices
    start, end = state.window
    in_window = [p for p in pool if p.sku == sku and start <= p.observed_on <= end]
    promo = [
        p.price
        for p in in_window
        if p.point_type in (
            PricePointType.PROMO,
            PricePointType.PLATFORM_REPORTED,
            PricePointType.RECTIFICATION,
        )
    ]
    daily = [p.price for p in in_window if p.point_type == PricePointType.DAILY]
    promo_avg, daily_avg = _avg(promo), _avg(daily)
    ratio = (promo_avg / cost) if promo_avg is not None else None
    below_cost = ratio is not None and ratio < BELOW_COST_RATIO - 1e-9

    clearance = False
    note_parts: list[str] = []
    if below_cost:
        valid_appeals = [
            a
            for a in state.appeals
            if a.within_deadline and a.clearance_inventory and a.clearance_inventory > 0
        ]
        if valid_appeals:
            clearance = True
            note_parts.append(
                f"限期内申辩 {len(valid_appeals)} 份并附清仓库存佐证，价格虽低于成本但具正当理由"
            )
        else:
            overdue = [a for a in state.appeals if not a.within_deadline]
            no_inventory = [
                a for a in state.appeals if a.within_deadline and not a.clearance_inventory
            ]
            if overdue:
                note_parts.append("存在逾期申辩，不予采纳")
            if no_inventory:
                note_parts.append("申辩未附清仓库存数量，正当理由不成立")
            if not state.appeals:
                note_parts.append("当事人未在限期内申辩")
    else:
        note_parts.append("整改复核价不低于当期成本" if use_rectification else "促销成交价不低于当期成本，未发现低价倾销")

    return PriceAssessment(
        sku=sku,
        cost=cost,
        promo_avg=promo_avg,
        daily_avg=daily_avg,
        ratio=ratio,
        below_cost=below_cost,
        clearance_justified=clearance,
        note="；".join(note_parts),
    )


def find_evidence_gaps(state: CaseState) -> list[Gap]:
    """对照立案锁定的范围逐项检查取证缺口。"""

    gaps: list[Gap] = []
    if state.scope is None or not state.products:
        gaps.append(Gap("SCOPE", "调查范围未锁定", "尚未立案，商品规格与活动范围缺失", "阻断"))
        return gaps

    start, end = state.window
    channels = set(state.scope.channels)
    regions = set(state.scope.regions)
    skus = set(state.products)

    # 1) 成本标准的适用期间必须覆盖调查时段
    cs = state.cost_standard
    if cs is None:
        gaps.append(Gap("COST", "当期成本标准缺失", "无成本标准，无法比价", "阻断"))
    elif cs.valid_from > start or cs.valid_to < end:
        gaps.append(
            Gap(
                "COST-PERIOD",
                "成本标准期间不匹配",
                f"成本标准适用 {cs.valid_from}~{cs.valid_to}，未覆盖调查时段 {start}~{end}",
                "阻断",
            )
        )

    # 2) 每个 SKU × 渠道 × 地区都要有活动期内的价格观测（线上固证或线下凭证），
    #    且至少一条 A/B 级
    offline_in_window = [
        o for o in state.offline_txns if start <= o.txn_date <= end
    ]
    for sku in sorted(skus):
        rows = state.prices_in_window(sku)
        offline_rows = [o for o in offline_in_window if o.sku == sku]
        covered = {(p.channel, p.region) for p in rows}
        covered |= {(o.channel, o.region) for o in offline_rows}
        missing_pairs = sorted(
            (c, r) for c in channels for r in regions if (c, r) not in covered
        )
        if not rows and not offline_rows:
            gaps.append(Gap(f"PRICE-{sku}", "价格证据缺失", f"{sku} 活动期内无任何价格观测", "阻断"))
        else:
            if missing_pairs:
                shown = "、".join(f"{c}/{r}" for c, r in missing_pairs[:5])
                gaps.append(
                    Gap(
                        f"PRICE-COVERAGE-{sku}",
                        "价格覆盖不全",
                        f"{sku} 缺少渠道×地区组合：{shown}",
                        "重要",
                    )
                )
            # 线上渠道的认定必须有 A/B 级固证；线下凭证本身来自台账调取，视为 B 级旁证
            online_rows = [p for p in rows if p.channel not in {o.channel for o in offline_rows}]
            strong = [p for p in online_rows if p.confidence in STRONG_CONFIDENCE]
            if rows and not strong:
                gaps.append(
                    Gap(
                        f"PRICE-FIXITY-{sku}",
                        "价格证据未固证",
                        f"{sku} 线上观测仅有截图/自述级（C）证据，缺 A/B 级固定提取或后台调取",
                        "阻断",
                    )
                )

    # 3) 线上畸低必须有线下交易交叉印证（同 SKU、同时段）
    offline_skus = {o.sku for o in offline_in_window}
    for sku in sorted(skus):
        assessment = assess_price(state, sku)
        if assessment.below_cost and sku not in offline_skus:
            gaps.append(
                Gap(
                    f"OFFLINE-{sku}",
                    "缺线下交叉印证",
                    f"{sku} 线上促销价低于成本，但无活动期内线下交易凭证交叉印证",
                    "重要",
                )
            )

    # 4) 重点品类价低需有抽检结论
    for sku in sorted(skus):
        product = state.products[sku]
        latest = state.latest_quality(sku)
        assessment = assess_price(state, sku)
        if product.category in KEY_CATEGORIES and assessment.below_cost:
            if latest is None:
                gaps.append(
                    Gap(
                        f"QUALITY-{sku}",
                        "缺抽检结果",
                        f"{sku}（{product.category}）价格畸低且属重点监管品类，尚未触发抽查",
                        "阻断",
                    )
                )
            elif latest.result is QualityResult.PENDING:
                gaps.append(
                    Gap(f"QUALITY-{sku}", "抽检结论未出", f"{sku} 样品在检，结论待出", "重要")
                )

    # 5) 跨地区移送须有回执
    for t in state.transfers:
        if not t.receipt:
            gaps.append(
                Gap(
                    f"TRANSFER-{t.to_region}",
                    "移送无回执",
                    f"{t.occurred_on} 移送至 {t.to_region} 尚未补登回执",
                    "提示",
                )
            )

    # 6) 商业秘密访问须有授权且未到期（以账本最新业务日期为基准）
    today = state.as_of or state.opened_on or start
    for g in state.secret_grants:
        if g.active and g.expires_on and g.expires_on < today:
            gaps.append(
                Gap(
                    f"SECRET-{g.grant_id}",
                    "授权已到期",
                    f"{g.grant_id} 对 {g.requester} 的授权已于 {g.expires_on} 到期，需重新授权或撤销",
                    "重要",
                )
            )

    return gaps


def quality_risk_triggers(state: CaseState) -> list[dict]:
    """质量风险联动：不合格 → 建议抽查扩面 / 发起召回协作。"""

    triggers: list[dict] = []
    start, end = state.window
    for sku in sorted(state.products):
        product = state.products[sku]
        latest = state.latest_quality(sku)
        assessment = assess_price(state, sku)
        if latest is None:
            continue
        actions: list[str] = []
        if latest.result is QualityResult.FAIL:
            actions.append("责令平台下架同规格商品并核验商品信息")
            actions.append(f"向 {latest.agency} 发起召回协作，按批次追溯")
            if product.category in KEY_CATEGORIES:
                actions.append("重点监管品类：抽查扩面至同品类在售链接")
            level = RiskLevel.HIGH
        elif latest.risk in (RiskLevel.HIGH, RiskLevel.MEDIUM) and assessment.below_cost:
            actions.append("价格畸低：加密抽查频次")
            level = RiskLevel.MEDIUM
        else:
            level = RiskLevel.LOW
        if actions:
            triggers.append(
                {
                    "sku": sku,
                    "risk": level.value,
                    "inspection_id": latest.inspection_id,
                    "actions": actions,
                }
            )
    return triggers


def verify_rectification(state: CaseState, *, today: date | None = None) -> dict:
    """用同一批产品、同一时段核验风险是否真正消除。

    判定要点：
    1. 同一批 SKU 的活动期（复核窗口）价格均不低于当期成本，或有正当理由；
    2. 最新抽检合格，且处罚时不合格的检验项目全部复检合格；
    3. 平台规则已整改：处罚时有效的规则中触发下线的已下线并留存记录；
    4. 整改材料按期提交。
    """

    rect = state.rectification
    if rect is None:
        return {"passed": False, "reason": "未下达整改计划", "checks": []}

    today = today or rect.verified_on or date.today()
    checks: list[dict] = []

    on_time = rect.submitted_on is not None and rect.submitted_on <= rect.due_on
    checks.append(
        {
            "item": "整改材料按期提交",
            "passed": on_time,
            "detail": f"提交日 {rect.submitted_on}，期限 {rect.due_on}",
        }
    )

    # 同一批 SKU：复核窗口须与立案时段一致（服务层在登记核验结果前强校验，这里报告）
    same_window = (rect.window_start, rect.window_end) == (state.window if state.scope else (None, None))
    checks.append(
        {
            "item": "复用同一时段核验",
            "passed": same_window,
            "detail": f"核验窗口 {rect.window_start}~{rect.window_end}",
        }
    )

    price_ok = True
    for sku in rect.skus:
        start, end = state.window
        rect_rows = [
            p
            for p in state.rect_prices
            if p.sku == sku and start <= p.observed_on <= end
        ]
        if not rect_rows:
            price_ok = False
            checks.append(
                {
                    "item": f"同批同时段重新定价固证：{sku}",
                    "passed": False,
                    "detail": "缺少整改复核价：须对同一批产品在原活动时段重新固证",
                }
            )
            continue
        a = assess_price(state, sku, use_rectification=True)
        ok = (not a.below_cost) or a.clearance_justified
        price_ok = price_ok and ok
        checks.append(
            {
                "item": f"同批同时段重新定价固证：{sku}",
                "passed": ok,
                "detail": f"复核均价 {a.promo_avg} / 当期成本 {a.cost}；{a.note}",
            }
        )

    quality_ok = True
    failed_items_before = {
        sku: set(q.items)
        for sku in rect.skus
        for q in state.quality
        if q.sku == sku and q.result is QualityResult.FAIL
    }
    for sku in rect.skus:
        latest = state.latest_quality(sku)
        items = failed_items_before.get(sku, set())
        ok = latest is not None and latest.result is QualityResult.PASS
        if ok and items:
            # 处罚时不合格项目须在最新合格报告的检验项目内（复检覆盖）
            ok = items.issubset(set(latest.items))
        quality_ok = quality_ok and ok
        checks.append(
            {
                "item": f"抽检合格且问题项目复检：{sku}",
                "passed": ok,
                "detail": (
                    f"最新报告 {latest.inspection_id}：{latest.result.value}"
                    if latest
                    else "无抽检报告"
                )
                + (f"，须覆盖项目：{'、'.join(sorted(items))}" if items else ""),
            }
        )

    rules_ok = bool(state.rules_takedown) or not state.decision
    checks.append(
        {
            "item": "违规平台规则下线留痕",
            "passed": rules_ok,
            "detail": f"已记录规则下线 {len(state.rules_takedown)} 条",
        }
    )

    passed = on_time and same_window and price_ok and quality_ok and rules_ok
    return {
        "passed": passed,
        "case_id": state.case_id,
        "plan_id": rect.plan_id,
        "verified_on": today.isoformat(),
        "checks": checks,
    }
