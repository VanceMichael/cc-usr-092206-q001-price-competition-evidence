"""端到端演示：一个低价竞争案件从线索到复核结案的完整流程。

运行：python scripts/demo.py
"""

from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import RecordKind as K
from src.service import CaseService


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, days):
        self.now += timedelta(days=days)


def show(title, value):
    print(f"\n== {title} ==")
    if isinstance(value, str):
        print(value)
    else:
        import json

        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main():
    clock = Clock(datetime(2026, 7, 3, 9, 0, 0))
    service = CaseService(clock=clock)

    # 1. 线索登记与立案调查：锁定商品规格、活动范围、时段与当期成本标准
    service.register_clue("A-2026-001", "某平台洗衣液促销涉嫌低价倾销", source="价格监测预警", handler="承办人甲")
    service.open_investigation(
        "A-2026-001",
        product_specs=["SP-洗衣液-500g"],
        channels=["线上-某平台", "线下-城东店"],
        window_start="2026-06-01",
        window_end="2026-06-30",
        cost_standard_id="CB-2026Q2",
        author="承办人甲",
    )

    # 2. 取证：商品规格、当期成本标准、线上线下交易（金额单位：分）
    service.append_record("A-2026-001", K.PRODUCT_SPEC.value, {
        "spec_id": "SP-洗衣液-500g", "name": "洗衣液 500g 装", "safety_risk": True,
    }, "承办人甲")
    service.append_record("A-2026-001", K.COST_STANDARD.value, {
        "standard_id": "CB-2026Q2",
        "items": [{"product_spec": "SP-洗衣液-500g", "unit_cost": 800}],
        "valid_from": "2026-04-01", "valid_to": "2026-06-30",
    }, "承办人甲")
    service.append_record("A-2026-001", K.ONLINE_TRANSACTION.value, {
        "product_spec": "SP-洗衣液-500g", "channel": "线上-某平台",
        "price": 500, "quantity": 2000, "sold_at": "2026-06-18", "order_id": "E-1001",
    }, "承办人甲")
    service.append_record("A-2026-001", K.OFFLINE_TRANSACTION.value, {
        "product_spec": "SP-洗衣液-500g", "channel": "线下-城东店",
        "price": 850, "quantity": 300, "sold_at": "2026-06-20", "order_id": "L-2031",
    }, "承办人甲")

    # 3. 平台补报与口径修订：只能追加到原始记录之后
    supplement = service.append_record("A-2026-001", K.PLATFORM_SUPPLEMENT.value, {
        "provides": K.ONLINE_TRANSACTION.value,
        "product_spec": "SP-洗衣液-500g", "channel": "线上-某平台",
        "price": 500, "quantity": 1500, "sold_at": "2026-06-19", "order_id": "E-1002",
    }, "平台企业")
    service.append_record("A-2026-001", K.CALIBER_REVISION.value, {
        "revises": supplement.seq, "quantity": 1480, "note": "平台修订统计口径，剔除测试单",
    }, "平台企业")
    service.append_record("A-2026-001", K.RULE_OFFLINE.value, {
        "platform": "某平台", "rule_id": "PROMO-618", "offline_at": "2026-06-21",
        "content": "满 199 减 100 叠加券规则",
    }, "承办人甲")

    show("调查范围与取证缺口", service.gap_report("A-2026-001")["explanation"])
    show("倾销判定", service.assess("A-2026-001").to_payload())

    # 4. 抽检不合格触发质量联动（抽查 + 召回协作）
    service.append_record("A-2026-001", K.SAMPLING_RESULT.value, {
        "product_spec": "SP-洗衣液-500g", "qualified": False,
        "item": "总活性物含量", "sampled_at": "2026-06-25",
    }, "质量监管人员")
    show("质量联动任务", service.list_tasks("A-2026-001"))

    # 5. 告知、限期申辩、处罚决定（钉住证据版本）
    service.notify("A-2026-001", "承办人甲", {"拟处罚": "责令改正并罚款", "拟罚款": 5000000}, appeal_days=5)
    service.appeal("A-2026-001", "被调查经营者", "促销系清理积压库存，请求核实清仓事由")
    service.append_record("A-2026-001", K.CLEARANCE_JUSTIFICATION.value, {
        "product_spec": "SP-洗衣液-500g", "reason": "积压商品",
        "covers_from": "2026-06-01", "covers_to": "2026-06-10", "note": "仅覆盖上旬",
    }, "被调查经营者")
    clock.advance(6)  # 申辩期届满
    decision = service.decide("A-2026-001", "承办人甲", {"类型": "罚款", "金额": 5000000, "责令": "停止低价倾销并整改"})
    show("处罚决定（钉住版本）", decision.payload)

    # 6. 复核人员重现处罚所依据的版本
    clock.advance(2)
    service.append_record("A-2026-001", K.CROSS_REGION_TRANSFER.value, {
        "from_region": "邻省某市", "provides": K.ONLINE_TRANSACTION.value,
        "product_spec": "SP-洗衣液-500g", "channel": "线上-某平台",
        "price": 480, "quantity": 500, "sold_at": "2026-06-15", "order_id": "E-0901",
    }, "邻省执法部门")
    reproduced = service.reproduce("A-2026-001", user="复核人员乙")
    show("复核重现（决定后新证据不影响历史版本）", {
        "version": reproduced["version"],
        "chain_ok": reproduced["chain_ok"],
        "matches_decision": reproduced["matches_decision"],
        "verdict": reproduced["assessment"]["verdict"],
        "transaction_count": reproduced["assessment"]["transaction_count"],
    })

    # 7. 商业秘密按案件授权开放
    service.append_record("A-2026-001", K.COST_STANDARD.value, {
        "standard_id": "CB-2026Q2",
        "items": [{"product_spec": "SP-洗衣液-500g", "unit_cost": 800}],
        "valid_from": "2026-04-01", "valid_to": "2026-06-30",
        "note": "含供应商保密条款",
    }, "承办人甲", secret=True)
    masked = service.read_records("A-2026-001", user="复核人员乙")[-1]
    print(f"\n未授权查阅商业秘密：{masked['payload']}")
    service.grant_secret_access("A-2026-001", grantee="复核人员乙", granted_by="办案负责人", days=3)
    opened = service.read_records("A-2026-001", user="复核人员乙")[-1]
    print(f"授权后查阅：{opened['payload']['note']}")
    print(f"查阅留痕：{service.read_records('A-2026-001', user='承办人甲')[-1]['kind']}")

    # 8. 整改与核验：同一批商品、等长时段
    service.submit_rectification("A-2026-001", "被调查经营者", {"措施": "下架低价链接、恢复合理定价"})
    clock.advance(30)
    service.append_record("A-2026-001", K.PLATFORM_SUPPLEMENT.value, {
        "provides": K.ONLINE_TRANSACTION.value,
        "product_spec": "SP-洗衣液-500g", "channel": "线上-某平台",
        "price": 890, "quantity": 1200, "sold_at": "2026-08-02", "order_id": "E-2001",
    }, "平台企业")
    service.append_record("A-2026-001", K.OFFLINE_TRANSACTION.value, {
        "product_spec": "SP-洗衣液-500g", "channel": "线下-城东店",
        "price": 899, "quantity": 260, "sold_at": "2026-08-03", "order_id": "L-3102",
    }, "承办人甲")
    for task in service.list_tasks("A-2026-001"):
        service.complete_task("A-2026-001", task["task_id"], "质量监管人员")
    result = service.verify_rectification("A-2026-001", "复核人员乙")
    show("整改核验", {"eliminated": result["eliminated"], "reasons": result["reasons"]})
    show("案件总览", service.overview("A-2026-001"))


if __name__ == "__main__":
    main()
