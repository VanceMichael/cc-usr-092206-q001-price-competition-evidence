"""服务命令面：阶段守卫、追加留痕、限期判定、授权开放与版本钉住。"""

import unittest
from datetime import date

from src.models import Confidence, PricePointType, QualityResult, RiskLevel
from src.service import ServiceError
from tests.casebuilders import (
    CASE_ID,
    add_basic_online_evidence,
    new_service,
    opened_case,
    price,
)


class ScopeLockTest(unittest.TestCase):
    def test_open_rejects_cost_period_not_covering_activity(self):
        from datetime import date as _date

        from src.models import CostStandard

        s = new_service()
        s.register_lead(CASE_ID, "线索", occurred_on="2026-03-02")
        from tests.casebuilders import products, scope

        bad_cost = CostStandard(
            "CX", "加权平均法", 100.0, True,
            _date(2026, 4, 1), _date(2026, 6, 30), "账册",
        )
        with self.assertRaises(ServiceError):
            s.open_case(CASE_ID, products(), scope(), bad_cost, occurred_on="2026-03-03")

    def test_cannot_open_twice(self):
        s, cid = opened_case()
        from tests.casebuilders import cost, products, scope

        with self.assertRaises(ServiceError):
            s.open_case(cid, products(), scope(), cost(), occurred_on="2026-03-04")

    def test_price_out_of_window_rejected(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-PRICE", "平台价格", "x", occurred_on="2026-03-06")
        with self.assertRaises(ServiceError):
            s.record_price(
                cid, price(on=date(2026, 4, 1)), occurred_on="2026-04-01"
            )

    def test_unknown_sku_rejected(self):
        s, cid = opened_case()
        with self.assertRaises(ServiceError):
            s.record_price(cid, price(sku="SKU-X"), occurred_on="2026-03-06")


class AppendOnlyFlowTest(unittest.TestCase):
    def test_supplement_never_overwrites_and_keeps_type(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-PRICE", "平台价格", "x", occurred_on="2026-03-06")
        s.record_price(cid, price(amount=80.0), occurred_on="2026-03-06")
        s.add_evidence(cid, "EV-SUP", "平台价格", "补报", occurred_on="2026-03-20")
        s.platform_report_price(
            cid, price(amount=70.0, kind=PricePointType.PLATFORM_REPORTED,
                       on=date(2026, 3, 10), ref="EV-SUP"),
            occurred_on="2026-03-20",
        )
        state = s.state(cid)
        types = [p.point_type for p in state.prices]
        self.assertIn(PricePointType.PROMO, types)
        self.assertIn(PricePointType.PLATFORM_REPORTED, types)
        # 原始 80 元仍在
        self.assertTrue(any(p.price == 80.0 for p in state.prices))

    def test_supplement_requires_reported_type(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-PRICE", "平台价格", "x", occurred_on="2026-03-06")
        with self.assertRaises(ServiceError):
            s.platform_report_price(
                cid, price(kind=PricePointType.PROMO), occurred_on="2026-03-20"
            )

    def test_evidence_ref_unique(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-1", "平台价格", "x", occurred_on="2026-03-06")
        with self.assertRaises(ServiceError):
            s.add_evidence(cid, "EV-1", "平台价格", "y", occurred_on="2026-03-07")

    def test_caliber_revision_keeps_history(self):
        s, cid = opened_case()
        s.revise_caliber(cid, "口径A", "v1", "首次调整", occurred_on="2026-03-10")
        s.revise_caliber(cid, "口径A", "v2", "再次调整", occurred_on="2026-03-11")
        state = s.state(cid)
        self.assertEqual(state.calibers["口径A"], "v2")
        self.assertEqual([h.old_value for h in state.caliber_history], ["（初次确立）", "v1"])

    def test_rule_takedown_idempotent_rejected(self):
        s, cid = opened_case()
        s.take_down_rule(cid, "R1", "平台", "违规", occurred_on="2026-03-15")
        with self.assertRaises(ServiceError):
            s.take_down_rule(cid, "R1", "平台", "违规", occurred_on="2026-03-16")

    def test_transfer_and_receipt(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-1", "平台价格", "x", occurred_on="2026-03-06")
        s.transfer_case(cid, "乙市", "跨区销售", material_refs=["EV-1"],
                        occurred_on="2026-03-10")
        state = s.state(cid)
        self.assertFalse(state.transfers[0].receipt)
        s.receipt_transfer(cid, "乙市", occurred_on="2026-03-12")
        self.assertTrue(s.state(cid).transfers[0].receipt)
        with self.assertRaises(ServiceError):
            s.receipt_transfer(cid, "乙市", occurred_on="2026-03-13")


class HearingAppealTest(unittest.TestCase):
    def _noticed(self):
        s, cid = opened_case()
        s.notice_hearing(cid, deadline="2026-03-20", occurred_on="2026-03-13")
        return s, cid

    def test_appeal_within_deadline_flag(self):
        s, cid = self._noticed()
        s.file_appeal(cid, "A1", "清仓", clearance_inventory=50,
                      submitted_on="2026-03-19")
        self.assertTrue(s.state(cid).appeals[0].within_deadline)

    def test_overdue_appeal_accepted_as_record_but_marked(self):
        s, cid = self._noticed()
        s.file_appeal(cid, "A1", "逾期清仓", clearance_inventory=50,
                      submitted_on="2026-03-22")
        self.assertFalse(s.state(cid).appeals[0].within_deadline)

    def test_appeal_before_notice_rejected(self):
        s, cid = opened_case()
        with self.assertRaises(ServiceError):
            s.file_appeal(cid, "A1", "清仓", submitted_on="2026-03-10")


class SecretAccessTest(unittest.TestCase):
    def test_grant_visibility_and_revoke(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-S", "企业成本", "涉密账册", confidential=True,
                       occurred_on="2026-03-06")
        s.grant_secret_access(cid, "G1", "复核员王某", "价费复核", ["EV-S"],
                              expires_on="2026-04-01", occurred_on="2026-03-08")
        self.assertTrue(s.visible_evidence(cid, "复核员王某", on="2026-03-20")["EV-S"])
        self.assertFalse(s.visible_evidence(cid, "路人赵某", on="2026-03-20")["EV-S"])
        self.assertFalse(s.visible_evidence(cid, "复核员王某", on="2026-04-02")["EV-S"])
        s.revoke_secret_access(cid, "G1", reason="案件结案", occurred_on="2026-03-25")
        self.assertFalse(s.visible_evidence(cid, "复核员王某", on="2026-03-25")["EV-S"])

    def test_grant_non_secret_rejected(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-N", "平台价格", "公开固证", occurred_on="2026-03-06")
        with self.assertRaises(ServiceError):
            s.grant_secret_access(cid, "G1", "王某", "用途", ["EV-N"],
                                  occurred_on="2026-03-08")


class PenaltyAndRectificationTest(unittest.TestCase):
    def _decided_case(self):
        """推进到处罚决定：价低、抽检不合格、申辩无库存。"""

        s, cid = opened_case(unit_cost=100.0)
        add_basic_online_evidence(s, cid, amount=80.0)
        # 线下交叉凭证，补齐渠道覆盖
        s.record_offline_txn(
            cid,
            {"txn_id": "O1", "txn_date": "2026-03-07", "sku": "SKU-1", "price": 82.0,
             "quantity": 3, "channel": "线下", "region": "甲市", "source_ref": "EV-PRICE"},
            occurred_on="2026-03-07",
        )
        s.record_offline_txn(
            cid,
            {"txn_id": "O2", "txn_date": "2026-03-07", "sku": "SKU-2", "price": 112.0,
             "quantity": 3, "channel": "线下", "region": "甲市", "source_ref": "EV-PRICE"},
            occurred_on="2026-03-07",
        )
        s.notice_hearing(cid, deadline="2026-03-20", occurred_on="2026-03-13")
        s.file_appeal(cid, "A1", "清仓但无库存清册", clearance_inventory=None,
                      submitted_on="2026-03-18")
        return s, cid

    def test_blocking_gap_prevents_decision(self):
        s, cid = self._decided_case()  # 重点品类价低但无抽检：阻断缺口
        with self.assertRaises(ServiceError):
            s.decide_penalty(cid, "P1", "认定", inputs=["EV-PRICE"],
                             occurred_on="2026-03-22")

    def test_decision_pins_head_and_post_events_excluded(self):
        s, cid = self._decided_case()
        s.add_evidence(cid, "EV-Q", "抽检结果", "不合格报告", occurred_on="2026-03-16")
        s.record_quality(
            cid,
            {"inspection_id": "Q1", "sku": "SKU-1", "sampled_on": "2026-03-10",
             "agency": "质检院", "result": QualityResult.FAIL.value,
             "items": ["稳定性"], "report_ref": "EV-Q", "risk": RiskLevel.HIGH.value},
            occurred_on="2026-03-16",
        )
        s.decide_penalty(
            cid, "P1", "低于成本倾销", inputs=["EV-PRICE", "EV-Q"],
            rules_known=["R1", "R2"], occurred_on="2026-03-22",
        )
        current = s.state(cid)
        pinned = s.state_at_head(cid)
        self.assertEqual(pinned.head_hash, current.decision.head_hash)
        # 处罚时刻尚无口径修订
        self.assertEqual(pinned.calibers, {})
        # 处罚后追加的规则下线不改变处罚决定中钉住的"有效规则"快照
        self.assertEqual(set(current.decision.rules_active), {"R1", "R2"})
        s.take_down_rule(cid, "R1", "平台", "违规", occurred_on="2026-03-25")
        self.assertEqual(set(s.state(cid).decision.rules_active), {"R1", "R2"})
        # 钉住时点的状态中价格证据数量与决定前一致，未被补报污染
        self.assertEqual(len(pinned.prices), len(s.state_at_head(cid).prices))

    def test_rectification_full_loop(self):
        s, cid = self._decided_case()
        s.add_evidence(cid, "EV-Q", "抽检结果", "不合格", occurred_on="2026-03-16")
        s.record_quality(
            cid,
            {"inspection_id": "Q1", "sku": "SKU-1", "sampled_on": "2026-03-10",
             "agency": "质检院", "result": QualityResult.FAIL.value,
             "items": ["稳定性"], "report_ref": "EV-Q", "risk": RiskLevel.HIGH.value},
            occurred_on="2026-03-16",
        )
        s.decide_penalty(cid, "P1", "倾销", inputs=["EV-PRICE", "EV-Q"],
                         occurred_on="2026-03-22")
        s.take_down_rule(cid, "R1", "平台", "违规", occurred_on="2026-03-25")
        s.require_rectification(cid, "PL1", due_on="2026-05-01",
                                skus=["SKU-1"], occurred_on="2026-03-23")
        # 未提交材料不能核验
        with self.assertRaises(ServiceError):
            s.verify_rectification(cid, occurred_on="2026-04-20")
        s.add_evidence(cid, "EV-R", "整改材料", "调价与复检", occurred_on="2026-04-18")
        s.submit_rectification(cid, ["EV-R"], occurred_on="2026-04-18")
        # 复核价必须落在原活动时段、且属于同批 SKU
        with self.assertRaises(ServiceError):
            s.record_rectification_price(
                cid, price(sku="SKU-1", amount=105.0, on=date(2026, 4, 1),
                           confidence=Confidence.A, ref="EV-R"),
                occurred_on="2026-04-19",
            )
        s.record_rectification_price(
            cid, price(sku="SKU-1", amount=105.0, on=date(2026, 3, 6),
                       confidence=Confidence.A, ref="EV-R"),
            occurred_on="2026-04-19",
        )
        # 无复检合格报告前核验不通过（但事件仍会被追加，结论 False）
        first = s.verify_rectification(cid, occurred_on="2026-04-20")
        self.assertFalse(first["passed"])
        s.add_evidence(cid, "EV-Q2", "抽检结果", "复检合格", occurred_on="2026-04-25")
        s.record_quality(
            cid,
            {"inspection_id": "Q2", "sku": "SKU-1", "sampled_on": "2026-04-22",
             "agency": "质检院", "result": QualityResult.PASS.value,
             "items": ["稳定性", "外观"], "report_ref": "EV-Q2",
             "risk": RiskLevel.LOW.value},
            occurred_on="2026-04-25",
        )
        second = s.verify_rectification(cid, occurred_on="2026-04-26")
        self.assertTrue(second["passed"])
        self.assertEqual(s.state(cid).phase.value, "已核验")


if __name__ == "__main__":
    unittest.main()
