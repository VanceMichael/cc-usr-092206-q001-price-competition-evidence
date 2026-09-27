import unittest
from datetime import date, datetime

from src import assessment
from src.ledger import Ledger
from src.models import RecordKind as K
from src.models import Scope

AT = datetime(2026, 7, 1, 9, 0, 0)
SCOPE = Scope(("SP-1",), ("线上-P", "线下-S"), date(2026, 6, 1), date(2026, 6, 30), "CB-Q2")


def build_ledger(entries):
    ledger = Ledger("A-001")
    for kind, payload in entries:
        ledger.append(kind=kind, payload=payload, author="测试", at=AT)
    return ledger


def base_entries():
    return [
        (K.PRODUCT_SPEC.value, {"spec_id": "SP-1", "name": "样品", "safety_risk": False}),
        (K.COST_STANDARD.value, {
            "standard_id": "CB-Q2",
            "items": [{"product_spec": "SP-1", "unit_cost": 800}],
            "valid_from": "2026-04-01", "valid_to": "2026-06-30",
        }),
    ]


def tx(price, channel="线上-P", sold_at="2026-06-18", spec="SP-1"):
    return {"product_spec": spec, "channel": channel, "price": price, "quantity": 10, "sold_at": sold_at}


class AssessTest(unittest.TestCase):
    def test_insufficient_without_cost_standard(self):
        ledger = build_ledger([(K.ONLINE_TRANSACTION.value, tx(500))])
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertEqual(result.verdict, assessment.VERDICT_INSUFFICIENT)
        self.assertIn("成本标准", result.gaps)

    def test_clearance_when_no_below_cost_sales(self):
        ledger = build_ledger(base_entries() + [(K.ONLINE_TRANSACTION.value, tx(900))])
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertEqual(result.verdict, assessment.VERDICT_CLEARANCE)
        self.assertEqual(result.below_cost_count, 0)

    def test_dumping_when_below_cost_without_justification(self):
        ledger = build_ledger(base_entries() + [(K.ONLINE_TRANSACTION.value, tx(500))])
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertEqual(result.verdict, assessment.VERDICT_DUMPING)
        self.assertEqual(result.below_cost_count, 1)

    def test_clearance_when_below_cost_covered_by_justification(self):
        entries = base_entries() + [
            (K.ONLINE_TRANSACTION.value, tx(500)),
            (K.CLEARANCE_JUSTIFICATION.value, {
                "product_spec": "SP-1", "reason": "积压商品",
                "covers_from": "2026-06-01", "covers_to": "2026-06-30",
            }),
        ]
        ledger = build_ledger(entries)
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertEqual(result.verdict, assessment.VERDICT_CLEARANCE)
        self.assertEqual(result.justified_count, 1)

    def test_caliber_revision_applies_latest_caliber(self):
        entries = base_entries()
        ledger = build_ledger(entries)
        record = ledger.append(kind=K.ONLINE_TRANSACTION.value, payload=tx(900), author="测试", at=AT)
        ledger.append(kind=K.CALIBER_REVISION.value, payload={"revises": record.seq, "price": 500}, author="平台", at=AT)
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertEqual(result.verdict, assessment.VERDICT_DUMPING)
        # 原始记录仍在链上，只是判定采用修订后口径
        self.assertEqual(ledger.at(record.seq)[-1].payload["price"], 900)

    def test_supplement_provides_evidence_category(self):
        entries = base_entries() + [
            (K.PLATFORM_SUPPLEMENT.value, {"provides": K.ONLINE_TRANSACTION.value, **tx(900)}),
            (K.CROSS_REGION_TRANSFER.value, {"from_region": "邻市", "provides": K.OFFLINE_TRANSACTION.value, **tx(880, channel="线下-S")}),
        ]
        ledger = build_ledger(entries)
        self.assertEqual(assessment.evidence_gaps(ledger.at(), SCOPE), [])

    def test_out_of_scope_transactions_ignored(self):
        entries = base_entries() + [
            (K.ONLINE_TRANSACTION.value, tx(500, sold_at="2026-07-15")),  # 时段外
            (K.ONLINE_TRANSACTION.value, tx(500, channel="线上-其他")),     # 渠道外
            (K.ONLINE_TRANSACTION.value, tx(500, spec="SP-2")),             # 商品外
        ]
        ledger = build_ledger(entries)
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertEqual(result.verdict, assessment.VERDICT_INSUFFICIENT)

    def test_quality_risk_from_unqualified_sampling(self):
        entries = base_entries() + [
            (K.ONLINE_TRANSACTION.value, tx(900)),
            (K.SAMPLING_RESULT.value, {"product_spec": "SP-1", "qualified": False, "sampled_at": "2026-06-20"}),
        ]
        ledger = build_ledger(entries)
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertTrue(result.quality_risk)

    def test_quality_risk_from_extreme_low_price_on_risky_product(self):
        entries = [
            (K.PRODUCT_SPEC.value, {"spec_id": "SP-1", "name": "样品", "safety_risk": True}),
            (K.COST_STANDARD.value, {
                "standard_id": "CB-Q2",
                "items": [{"product_spec": "SP-1", "unit_cost": 800}],
                "valid_from": "2026-04-01", "valid_to": "2026-06-30",
            }),
            (K.ONLINE_TRANSACTION.value, tx(500)),
        ]
        ledger = build_ledger(entries)
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertTrue(result.quality_risk)

    def test_sampling_outside_window_not_counted(self):
        entries = base_entries() + [
            (K.ONLINE_TRANSACTION.value, tx(900)),
            (K.SAMPLING_RESULT.value, {"product_spec": "SP-1", "qualified": False, "sampled_at": "2026-08-01"}),
        ]
        ledger = build_ledger(entries)
        result = assessment.assess(ledger.at(), SCOPE)
        self.assertFalse(result.quality_risk)


if __name__ == "__main__":
    unittest.main()
