"""端到端演示：一次"低价促销"线索从立案到整改核验的完整流转。

运行：``python -m src.cli demo --store <目录>``。
全程展示：范围锁定 → 取证 → 平台补报 → 口径修订 → 限期申辩 →
商业秘密授权 → 跨地区移送 → 抽查不合格触发召回协作 → 处罚版本钉住 →
规则下线 → 同批产品同时段整改核验。
"""

from __future__ import annotations

from datetime import date

from .models import (
    ActivityScope,
    Confidence,
    CostStandard,
    PriceObservation,
    PricePointType,
    Product,
    QualityResult,
    RiskLevel,
)
from .service import CaseService

CASE_ID = "demo-2026-001"


def run_demo(service: CaseService) -> str:
    s = service

    # 1) 线索登记 ----------------------------------------------------------
    s.register_lead(
        CASE_ID,
        "某平台「冰点焕新节」部分家电促销价显著低于成本，疑低价倾销",
        source="平台监测推送",
        occurred_on="2026-03-02",
    )

    # 2) 立案：锁定商品规格、活动范围、当期成本标准（此后不可改写） ----------
    products = [
        Product("SKU-FAN-01", "清风台扇", "FT-300/220V", "台", "家用电器"),
        Product("SKU-POT-02", "乐厨电压力锅", "YL-50/5L", "台", "家用电器"),
    ]
    scope = ActivityScope(
        activity_name="冰点焕新节",
        channels=("平台旗舰店", "线下专卖店"),
        regions=("江东市",),
        start=date.fromisoformat("2026-03-05"),
        end=date.fromisoformat("2026-03-12"),
    )
    cost = CostStandard(
        standard_id="COST-2026Q1",
        method="加权平均法（含物流）",
        unit_cost=120.0,
        includes_logistics=True,
        valid_from=date.fromisoformat("2026-01-01"),
        valid_to=date.fromisoformat("2026-03-31"),
        source="企业账册及第三方审计报告",
    )
    s.open_case(CASE_ID, products, scope, cost, occurred_on="2026-03-03")

    # 3) 原始取证：活动期价格（A/B 级固证）、促前标价、线下交叉凭证 --------
    s.add_evidence(CASE_ID, "EV-001", "平台价格", "活动期页面固证及成交明细（公证）",
                   occurred_on="2026-03-06")
    s.record_price(
        CASE_ID,
        PriceObservation("SKU-FAN-01", date(2026, 3, 6), PricePointType.PROMO, 89.0,
                         "平台旗舰店", "江东市", Confidence.A, "EV-001"),
        occurred_on="2026-03-06",
    )
    s.record_price(
        CASE_ID,
        PriceObservation("SKU-FAN-01", date(2026, 3, 8), PricePointType.PROMO, 89.0,
                         "平台旗舰店", "江东市", Confidence.B, "EV-001"),
        occurred_on="2026-03-08",
    )
    s.record_price(
        CASE_ID,
        PriceObservation("SKU-FAN-01", date(2026, 3, 1), PricePointType.PRE_PROMO, 159.0,
                         "平台旗舰店", "江东市", Confidence.B, "EV-001",
                         note="促前30日内日常标价"),
        occurred_on="2026-03-05",
    )
    # 另一规格促销价高于成本：演示"正常促销不被误伤"
    s.record_price(
        CASE_ID,
        PriceObservation("SKU-POT-02", date(2026, 3, 7), PricePointType.PROMO, 129.0,
                         "平台旗舰店", "江东市", Confidence.B, "EV-001"),
        occurred_on="2026-03-07",
    )
    s.add_evidence(CASE_ID, "EV-002", "线下交易", "专卖店同期销售小票与进货台账",
                   occurred_on="2026-03-09")
    s.record_offline_txn(
        CASE_ID,
        {
            "txn_id": "OFF-1001", "txn_date": "2026-03-07", "sku": "SKU-FAN-01",
            "price": 95.0, "quantity": 12, "channel": "线下专卖店",
            "region": "江东市", "source_ref": "EV-002",
        },
        occurred_on="2026-03-09",
    )
    s.record_offline_txn(
        CASE_ID,
        {
            "txn_id": "OFF-1002", "txn_date": "2026-03-08", "sku": "SKU-POT-02",
            "price": 135.0, "quantity": 6, "channel": "线下专卖店",
            "region": "江东市", "source_ref": "EV-002",
        },
        occurred_on="2026-03-09",
    )

    # 4) 平台事后补报：先登记证据再补报；只能追加，且自带补报口径 ------------
    s.add_evidence(CASE_ID, "EV-003", "平台价格", "平台后台补报：3月10日券后成交数据",
                   occurred_on="2026-03-20")
    s.platform_report_price(
        CASE_ID,
        PriceObservation("SKU-FAN-01", date(2026, 3, 10), PricePointType.PLATFORM_REPORTED, 86.0,
                         "平台旗舰店", "江东市", Confidence.B, "EV-003",
                         note="平台调查开始后补报的券后成交价"),
        occurred_on="2026-03-20",  # 取得日在调查开始后
    )

    # 5) 口径修订：老口径保留在历史中 --------------------------------------
    s.revise_caliber(
        CASE_ID, "券后成交价口径", "以页面标价为准",
        "明确以剔除平台券后实际成交价为准", occurred_on="2026-03-21",
    )
    s.revise_caliber(
        CASE_ID, "券后成交价口径", "以剔除平台券和店铺满减后的实际支付价为准",
        "店铺满减同属商家让价，明确一并剔除", occurred_on="2026-03-22",
    )

    # 6) 质量风险联动：重点品类价低 → 抽查；不合格 → 下架与召回协作 ---------
    s.add_evidence(CASE_ID, "EV-004", "抽检结果", "市质检院抽检报告（稳定性不合格）",
                   occurred_on="2026-03-18")
    s.record_quality(
        CASE_ID,
        {
            "inspection_id": "QC-2026-031", "sku": "SKU-FAN-01", "sampled_on": "2026-03-10",
            "agency": "江东市质检院", "result": QualityResult.FAIL.value,
            "items": ["稳定性", "电源连接"], "report_ref": "EV-004",
            "risk": RiskLevel.HIGH.value,
        },
        occurred_on="2026-03-18",
    )

    # 7) 商业秘密按案件授权开放（成本账册涉密） ----------------------------
    s.add_evidence(CASE_ID, "EV-005", "企业成本", "成本核算底稿（商业秘密）",
                   confidential=True, occurred_on="2026-03-10")
    s.grant_secret_access(
        CASE_ID, "GRANT-01", "承办人李某", "核算价费比并制作处罚告知",
        ["EV-005"], expires_on="2026-06-30", occurred_on="2026-03-11",
    )

    # 8) 跨地区移送并补登回执 ---------------------------------------------
    s.transfer_case(
        CASE_ID, "海州市", "同批次商品在海州市有销售记录",
        material_refs=["EV-001"], occurred_on="2026-03-19",
    )
    s.receipt_transfer(CASE_ID, "海州市", occurred_on="2026-03-25")

    # 9) 告知并限期申辩：无库存佐证的清仓理由不予采纳 -----------------------
    s.notice_hearing(CASE_ID, deadline="2026-03-30", occurred_on="2026-03-23")
    s.file_appeal(
        CASE_ID, "APPEAL-01", "主张系季节性清仓，但未提供库存清册",
        clearance_inventory=None, evidence_refs=["EV-001"],
        submitted_on="2026-03-28",
    )

    # 10) 处罚决定：钉住决定前的链头、口径与规则状态 -----------------------
    s.decide_penalty(
        CASE_ID, "PEN-2026-017",
        "促销成交均价低于当期成本，清仓申辩无库存佐证，构成低价倾销；抽检不合格另案处置",
        inputs=["EV-001", "EV-002", "EV-003", "EV-005"],
        rules_known=["RULE-PLAT-A-COUPON", "RULE-PLAT-A-SEARCH"],
        occurred_on="2026-04-02",
    )

    # 11) 平台规则下线（处罚后追加，不影响已钉住版本） ---------------------
    s.take_down_rule(
        CASE_ID, "RULE-PLAT-A-COUPON", "某平台",
        "违规大额券规则诱导低于成本成交，责令下线", occurred_on="2026-04-08",
    )

    # 12) 整改：同批 SKU、同一活动时段；复检合格、价格回归 -----------------
    s.require_rectification(
        CASE_ID, "PLAN-01", due_on="2026-05-15", occurred_on="2026-04-03",
        skus=["SKU-FAN-01"],
    )
    s.add_evidence(CASE_ID, "EV-006", "整改材料", "调价记录、规则下线截图、复检申请",
                   occurred_on="2026-05-06")
    s.submit_rectification(CASE_ID, ["EV-006"], occurred_on="2026-05-06")
    # 复核窗口复用 3月5日~12日：同一批产品在同一时段重新固证（独立复核价口径）
    s.add_evidence(CASE_ID, "EV-007", "平台价格", "整改后同一时段复核固证",
                   occurred_on="2026-05-08")
    s.record_rectification_price(
        CASE_ID,
        PriceObservation("SKU-FAN-01", date(2026, 3, 6), PricePointType.RECTIFICATION, 125.0,
                         "平台旗舰店", "江东市", Confidence.A, "EV-007",
                         note="整改复核：同批产品同时段重新定价"),
        occurred_on="2026-05-08",
    )
    s.add_evidence(CASE_ID, "EV-008", "抽检结果", "整改后复检报告（原不合格项目覆盖）",
                   occurred_on="2026-05-09")
    s.record_quality(
        CASE_ID,
        {
            "inspection_id": "QC-2026-058", "sku": "SKU-FAN-01", "sampled_on": "2026-05-05",
            "agency": "江东市质检院", "result": QualityResult.PASS.value,
            "items": ["稳定性", "电源连接", "噪声"], "report_ref": "EV-008",
            "risk": RiskLevel.LOW.value,
        },
        occurred_on="2026-05-09",
    )
    s.verify_rectification(CASE_ID, occurred_on="2026-05-12")
    return CASE_ID
