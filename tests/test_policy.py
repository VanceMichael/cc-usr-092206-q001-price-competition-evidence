"""策略层：价费比对、清仓认定、取证缺口、质量联动、整改核验。"""

import unittest
from datetime import date

from src.models import (
    Confidence,
    PricePointType,
    QualityResult,
    RiskLevel,
)
from src.policy import (
    assess_price,
    find_evidence_gaps,
    quality_risk_triggers,
    verify_rectification,
)
from tests.casebuilders import CASE_ID, add_basic_online_evidence, opened_case, price


def _offline(s, cid, sku="SKU-1", amount=82.0):
    s.record_offline_txn(
        cid,
        {"txn_id": f"O-{sku}", "txn_date": "2026-03-07", "sku": sku,
         "price": amount, "quantity": 3, "channel": "线下", "region": "甲市",
         "source_ref": "EV-PRICE"},
        occurred_on="2026-03-07",
    )


class PriceAssessmentTest(unittest.TestCase):
    def test_below_cost_detected(self):
        s, cid = opened_case(unit_cost=100.0)
        add_basic_online_evidence(s, cid, amount=80.0)
        a = assess_price(s.state(cid), "SKU-1")
        self.assertTrue(a.below_cost)
        self.assertAlmostEqual(a.ratio, 0.8)
        self.assertFalse(a.clearance_justified)  # 无申辩

    def test_clearance_with_inventory_within_deadline_justifies(self):
        s, cid = opened_case()
        add_basic_online_evidence(s, cid)
        _offline(s, cid, "SKU-1"), _offline(s, cid, "SKU-2", 112.0)
        s.notice_hearing(cid, deadline="2026-03-20", occurred_on="2026-03-13")
        s.file_appeal(cid, "A1", "季节性清仓", clearance_inventory=300,
                      submitted_on="2026-03-18")
        a = assess_price(s.state(cid), "SKU-1")
        self.assertTrue(a.below_cost)
        self.assertTrue(a.clearance_justified)

    def test_overdue_clearance_not_justified(self):
        s, cid = opened_case()
        add_basic_online_evidence(s, cid)
        _offline(s, cid, "SKU-1"), _offline(s, cid, "SKU-2", 112.0)
        s.notice_hearing(cid, deadline="2026-03-20", occurred_on="2026-03-13")
        s.file_appeal(cid, "A1", "逾期清仓", clearance_inventory=300,
                      submitted_on="2026-03-22")
        self.assertFalse(assess_price(s.state(cid), "SKU-1").clearance_justified)

    def test_clearance_without_inventory_not_justified(self):
        s, cid = opened_case()
        add_basic_online_evidence(s, cid)
        _offline(s, cid, "SKU-1"), _offline(s, cid, "SKU-2", 112.0)
        s.notice_hearing(cid, deadline="2026-03-20", occurred_on="2026-03-13")
        s.file_appeal(cid, "A1", "口头主张清仓", clearance_inventory=None,
                      submitted_on="2026-03-18")
        self.assertFalse(assess_price(s.state(cid), "SKU-1").clearance_justified)

    def test_normal_promo_above_cost_not_flagged(self):
        s, cid = opened_case(unit_cost=100.0)
        add_basic_online_evidence(s, cid, amount=80.0)
        a = assess_price(s.state(cid), "SKU-2")  # 售价 110
        self.assertFalse(a.below_cost)


class EvidenceGapTest(unittest.TestCase):
    def _codes(self, state):
        return {g.code for g in find_evidence_gaps(state)}

    def test_missing_offline_cross_check_for_below_cost_online(self):
        s, cid = opened_case()
        add_basic_online_evidence(s, cid)  # 两 SKU 线上价，无线下凭证
        codes = self._codes(s.state(cid))
        self.assertIn("OFFLINE-SKU-1", codes)

    def test_weak_confidence_is_blocking(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-PRICE", "平台价格", "截图", occurred_on="2026-03-06")
        s.record_price(
            cid, price(amount=80.0, confidence=Confidence.C), occurred_on="2026-03-06"
        )
        s.record_price(
            cid, price(sku="SKU-2", amount=110.0, confidence=Confidence.C),
            occurred_on="2026-03-06",
        )
        codes = self._codes(s.state(cid))
        self.assertTrue(any(c.startswith("PRICE-FIXITY") for c in codes))

    def test_key_category_below_cost_requires_quality(self):
        s, cid = opened_case()
        add_basic_online_evidence(s, cid)
        _offline(s, cid, "SKU-1"), _offline(s, cid, "SKU-2", 112.0)
        codes = self._codes(s.state(cid))
        self.assertIn("QUALITY-SKU-1", codes)  # 家用电器 + 价低，无抽检

    def test_transfer_without_receipt_flagged(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-1", "平台价格", "x", occurred_on="2026-03-06")
        s.transfer_case(cid, "乙市", "跨区", material_refs=["EV-1"],
                        occurred_on="2026-03-10")
        self.assertTrue(
            any(g.code == "TRANSFER-乙市" for g in find_evidence_gaps(s.state(cid)))
        )

    def test_expired_secret_grant_flagged(self):
        s, cid = opened_case()
        s.add_evidence(cid, "EV-S", "企业成本", "涉密", confidential=True,
                       occurred_on="2026-03-06")
        s.grant_secret_access(cid, "G1", "王某", "用途", ["EV-S"],
                              expires_on="2026-03-04", occurred_on="2026-03-03")
        codes = self._codes(s.state(cid))
        self.assertIn("SECRET-G1", codes)

    def test_complete_chain_has_no_blocking_gaps(self):
        s, cid = opened_case()
        add_basic_online_evidence(s, cid)
        _offline(s, cid, "SKU-1"), _offline(s, cid, "SKU-2", 112.0)
        s.add_evidence(cid, "EV-Q", "抽检结果", "合格", occurred_on="2026-03-15")
        s.record_quality(
            cid,
            {"inspection_id": "Q1", "sku": "SKU-1", "sampled_on": "2026-03-10",
             "agency": "质检院", "result": QualityResult.PASS.value,
             "items": ["稳定性"], "report_ref": "EV-Q", "risk": RiskLevel.LOW.value},
            occurred_on="2026-03-15",
        )
        blocking = [g for g in find_evidence_gaps(s.state(cid)) if g.severity == "阻断"]
        self.assertEqual(blocking, [])


class QualityTriggerTest(unittest.TestCase):
    def test_fail_triggers_recall_and_delisting(self):
        s, cid = opened_case()
        add_basic_online_evidence(s, cid)
        _offline(s, cid, "SKU-1"), _offline(s, cid, "SKU-2", 112.0)
        s.add_evidence(cid, "EV-Q", "抽检结果", "不合格", occurred_on="2026-03-15")
        s.record_quality(
            cid,
            {"inspection_id": "Q1", "sku": "SKU-1", "sampled_on": "2026-03-10",
             "agency": "质检院", "result": QualityResult.FAIL.value,
             "items": ["稳定性"], "report_ref": "EV-Q", "risk": RiskLevel.HIGH.value},
            occurred_on="2026-03-15",
        )
        triggers = quality_risk_triggers(s.state(cid))
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["risk"], "高")
        joined = "；".join(triggers[0]["actions"])
        self.assertIn("召回协作", joined)
        self.assertIn("下架", joined)


class RectificationVerifyTest(unittest.TestCase):
    def _closed(self):
        s, cid = opened_case()
        add_basic_online_evidence(s, cid)
        _offline(s, cid, "SKU-1"), _offline(s, cid, "SKU-2", 112.0)
        s.add_evidence(cid, "EV-Q", "抽检结果", "不合格", occurred_on="2026-03-15")
        s.record_quality(
            cid,
            {"inspection_id": "Q1", "sku": "SKU-1", "sampled_on": "2026-03-10",
             "agency": "质检院", "result": QualityResult.FAIL.value,
             "items": ["稳定性"], "report_ref": "EV-Q", "risk": RiskLevel.HIGH.value},
            occurred_on="2026-03-15",
        )
        s.notice_hearing(cid, deadline="2026-03-20", occurred_on="2026-03-13")
        s.file_appeal(cid, "A1", "无库存主张", submitted_on="2026-03-18")
        s.decide_penalty(cid, "P1", "倾销", inputs=["EV-PRICE", "EV-Q"],
                         occurred_on="2026-03-22")
        s.take_down_rule(cid, "R1", "平台", "违规", occurred_on="2026-03-25")
        s.require_rectification(cid, "PL1", due_on="2026-05-01", skus=["SKU-1"],
                                occurred_on="2026-03-23")
        s.add_evidence(cid, "EV-R", "整改材料", "调价复检", occurred_on="2026-04-18")
        s.submit_rectification(cid, ["EV-R"], occurred_on="2026-04-18")
        return s, cid

    def test_fail_without_rectification_price(self):
        s, cid = self._closed()
        result = verify_rectification(s.state(cid), today=date(2026, 4, 20))
        self.assertFalse(result["passed"])
        items = {c["item"] for c in result["checks"]}
        self.assertTrue(any("重新定价固证" in i for i in items))

    def test_fail_when_reinspection_misses_failed_item(self):
        s, cid = self._closed()
        s.record_rectification_price(
            cid, price(sku="SKU-1", amount=105.0, on=date(2026, 3, 6),
                       ref="EV-R"),
            occurred_on="2026-04-19",
        )
        # 复检合格但未覆盖原不合格项目"稳定性"
        s.add_evidence(cid, "EV-Q2", "抽检结果", "复检", occurred_on="2026-04-25")
        s.record_quality(
            cid,
            {"inspection_id": "Q2", "sku": "SKU-1", "sampled_on": "2026-04-22",
             "agency": "质检院", "result": QualityResult.PASS.value,
             "items": ["外观"], "report_ref": "EV-Q2", "risk": RiskLevel.LOW.value},
            occurred_on="2026-04-25",
        )
        result = verify_rectification(s.state(cid), today=date(2026, 4, 26))
        quality_check = next(c for c in result["checks"] if "抽检" in c["item"])
        self.assertFalse(quality_check["passed"])

    def test_pass_full_rectification(self):
        s, cid = self._closed()
        s.record_rectification_price(
            cid, price(sku="SKU-1", amount=105.0, on=date(2026, 3, 6),
                       ref="EV-R"),
            occurred_on="2026-04-19",
        )
        s.add_evidence(cid, "EV-Q2", "抽检结果", "复检", occurred_on="2026-04-25")
        s.record_quality(
            cid,
            {"inspection_id": "Q2", "sku": "SKU-1", "sampled_on": "2026-04-22",
             "agency": "质检院", "result": QualityResult.PASS.value,
             "items": ["稳定性", "外观"], "report_ref": "EV-Q2",
             "risk": RiskLevel.LOW.value},
            occurred_on="2026-04-25",
        )
        result = verify_rectification(s.state(cid), today=date(2026, 4, 26))
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
