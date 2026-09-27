"""测试用案件构造器：快速搭出一个进入"已立案"阶段的案件。"""

from __future__ import annotations

from datetime import date

from src.ledger import InMemoryLedger
from src.models import (
    ActivityScope,
    Confidence,
    CostStandard,
    PriceObservation,
    PricePointType,
    Product,
)
from src.service import CaseService

CASE_ID = "T-001"


def products():
    return [
        Product("SKU-1", "测试风扇", "FT-1", "台", "家用电器"),
        Product("SKU-2", "测试普通品", "PT-1", "件", "日用百货"),
    ]


def scope():
    return ActivityScope(
        activity_name="测试大促",
        channels=("线上", "线下"),
        regions=("甲市",),
        start=date(2026, 3, 5),
        end=date(2026, 3, 12),
    )


def cost(unit_cost: float = 100.0):
    return CostStandard(
        standard_id="C-1",
        method="加权平均法",
        unit_cost=unit_cost,
        includes_logistics=True,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 3, 31),
        source="企业账册",
    )


def price(sku="SKU-1", amount=80.0, kind=PricePointType.PROMO, on=date(2026, 3, 6),
          channel="线上", confidence=Confidence.A, ref="EV-PRICE"):
    return PriceObservation(sku, on, kind, amount, channel, "甲市", confidence, ref)


def new_service() -> CaseService:
    return CaseService(InMemoryLedger())


def opened_case(service: CaseService | None = None, *, unit_cost=100.0) -> tuple[CaseService, str]:
    s = service or new_service()
    s.register_lead(CASE_ID, "测试线索", occurred_on="2026-03-02")
    s.open_case(CASE_ID, products(), scope(), cost(unit_cost), occurred_on="2026-03-03")
    return s, CASE_ID


def add_basic_online_evidence(s: CaseService, case_id=CASE_ID, *, amount=80.0):
    """两 SKU 线上 A 级促销价固证（线下覆盖与线下交叉印证另行添加）。"""

    s.add_evidence(case_id, "EV-PRICE", "平台价格", "活动期固证", occurred_on="2026-03-06")
    for sku, amt in (("SKU-1", amount), ("SKU-2", 110.0)):
        s.record_price(case_id, price(sku=sku, amount=amt), occurred_on="2026-03-06")
