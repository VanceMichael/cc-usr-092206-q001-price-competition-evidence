import unittest
from datetime import datetime, timedelta

from src.models import CaseStatus, RecordKind as K
from src.service import CaseError, CaseService, NotFoundError, StateError

NOW = datetime(2026, 7, 3, 9, 0, 0)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, days):
        self.now += timedelta(days=days)


def make_service(clock=None):
    clock = clock or Clock()
    service = CaseService(clock=clock)
    service.register_clue("A-001", "洗衣液低价线索", source="价格监测", handler="承办人甲")
    service.open_investigation(
        "A-001",
        product_specs=["SP-1"],
        channels=["线上-P", "线下-S"],
        window_start="2026-06-01",
        window_end="2026-06-30",
        cost_standard_id="CB-Q2",
        author="承办人甲",
    )
    return service, clock


def fill_evidence(service, case_id="A-001", online_price=500):
    service.append_record(case_id, K.PRODUCT_SPEC.value, {"spec_id": "SP-1", "name": "样品"}, "承办人甲")
    service.append_record(case_id, K.COST_STANDARD.value, {
        "standard_id": "CB-Q2",
        "items": [{"product_spec": "SP-1", "unit_cost": 800}],
        "valid_from": "2026-04-01", "valid_to": "2026-06-30",
    }, "承办人甲")
    service.append_record(case_id, K.ONLINE_TRANSACTION.value, {
        "product_spec": "SP-1", "channel": "线上-P", "price": online_price,
        "quantity": 100, "sold_at": "2026-06-18", "order_id": "E-1",
    }, "承办人甲")
    service.append_record(case_id, K.OFFLINE_TRANSACTION.value, {
        "product_spec": "SP-1", "channel": "线下-S", "price": 850,
        "quantity": 50, "sold_at": "2026-06-20", "order_id": "L-1",
    }, "承办人甲")


def reach_decided(service, clock):
    fill_evidence(service)
    service.notify("A-001", "承办人甲", {"拟处罚": "罚款"}, appeal_days=5)
    clock.advance(6)
    service.decide("A-001", "承办人甲", {"类型": "罚款", "金额": 1000000})


class LifecycleTest(unittest.TestCase):
    def test_full_lifecycle_to_closed(self):
        service, clock = make_service()
        reach_decided(service, clock)
        service.submit_rectification("A-001", "经营者", {"措施": "恢复合理定价"})
        clock.advance(30)
        service.append_record("A-001", K.ONLINE_TRANSACTION.value, {
            "product_spec": "SP-1", "channel": "线上-P", "price": 900,
            "quantity": 100, "sold_at": "2026-08-05", "order_id": "E-2",
        }, "平台企业")
        service.append_record("A-001", K.OFFLINE_TRANSACTION.value, {
            "product_spec": "SP-1", "channel": "线下-S", "price": 910,
            "quantity": 60, "sold_at": "2026-08-06", "order_id": "L-2",
        }, "承办人甲")
        result = service.verify_rectification("A-001", "复核人员")
        self.assertTrue(result["eliminated"])
        self.assertEqual(service.overview("A-001")["status"], CaseStatus.CLOSED.value)

    def test_invalid_transitions_rejected(self):
        service, clock = make_service()
        with self.assertRaises(StateError):
            service.open_investigation("A-001", product_specs=["SP-1"], channels=["线上-P"],
                                       window_start="2026-06-01", window_end="2026-06-30",
                                       cost_standard_id="CB-Q2", author="承办人甲")
        with self.assertRaises(StateError):
            service.decide("A-001", "承办人甲", {})  # 未告知不能决定
        with self.assertRaises(NotFoundError):
            service.overview("A-404")

    def test_no_append_after_closed(self):
        service, clock = make_service()
        reach_decided(service, clock)
        service.submit_rectification("A-001", "经营者", {"措施": "整改"})
        clock.advance(30)
        service.append_record("A-001", K.ONLINE_TRANSACTION.value, {
            "product_spec": "SP-1", "channel": "线上-P", "price": 900,
            "quantity": 1, "sold_at": "2026-08-05", "order_id": "E-2",
        }, "平台企业")
        service.append_record("A-001", K.OFFLINE_TRANSACTION.value, {
            "product_spec": "SP-1", "channel": "线下-S", "price": 900,
            "quantity": 1, "sold_at": "2026-08-05", "order_id": "L-2",
        }, "承办人甲")
        service.verify_rectification("A-001", "复核人员")
        with self.assertRaises(StateError):
            service.append_record("A-001", K.ONLINE_TRANSACTION.value, {
                "product_spec": "SP-1", "channel": "线上-P", "price": 900,
                "quantity": 1, "sold_at": "2026-08-07", "order_id": "E-3",
            }, "平台企业")

    def test_dismiss(self):
        service, _ = make_service()
        service.dismiss("A-001", "承办人甲", "线索不实")
        self.assertEqual(service.overview("A-001")["status"], CaseStatus.DISMISSED.value)

    def test_payload_validation(self):
        service, _ = make_service()
        with self.assertRaises(CaseError):
            service.append_record("A-001", K.ONLINE_TRANSACTION.value, {"price": 500}, "承办人甲")
        with self.assertRaises(CaseError):
            service.append_record("A-001", "不存在的类型", {}, "承办人甲")
        with self.assertRaises(CaseError):
            service.append_record("A-001", K.PENALTY_DECISION.value, {}, "承办人甲")


class AppealTest(unittest.TestCase):
    def test_appeal_within_deadline(self):
        service, clock = make_service()
        fill_evidence(service)
        service.notify("A-001", "承办人甲", {"拟处罚": "罚款"}, appeal_days=5)
        record = service.appeal("A-001", "经营者", "系清理积压库存")
        self.assertEqual(record.kind, K.APPEAL.value)

    def test_appeal_after_deadline_rejected(self):
        service, clock = make_service()
        fill_evidence(service)
        service.notify("A-001", "承办人甲", {"拟处罚": "罚款"}, appeal_days=5)
        clock.advance(6)
        with self.assertRaises(StateError):
            service.appeal("A-001", "经营者", "逾期申辩")

    def test_decision_blocked_before_appeal_deadline(self):
        service, clock = make_service()
        fill_evidence(service)
        service.notify("A-001", "承办人甲", {"拟处罚": "罚款"}, appeal_days=5)
        with self.assertRaises(StateError):
            service.decide("A-001", "承办人甲", {})

    def test_decision_blocked_when_evidence_insufficient(self):
        service, clock = make_service()
        service.notify("A-001", "承办人甲", {"拟处罚": "罚款"}, appeal_days=5)
        clock.advance(6)
        with self.assertRaises(StateError):
            service.decide("A-001", "承办人甲", {})


class ReproduceTest(unittest.TestCase):
    def test_decision_pins_version_and_reproduce_matches(self):
        service, clock = make_service()
        reach_decided(service, clock)
        overview = service.overview("A-001")
        version = overview["decision_version"]
        # 决定之后追加的证据不影响处罚所依据的版本
        service.append_record("A-001", K.CROSS_REGION_TRANSFER.value, {
            "from_region": "邻市", "provides": K.ONLINE_TRANSACTION.value,
            "product_spec": "SP-1", "channel": "线上-P", "price": 400,
            "quantity": 10, "sold_at": "2026-06-19", "order_id": "E-9",
        }, "邻市执法部门")
        reproduced = service.reproduce("A-001", user="复核人员")
        self.assertEqual(reproduced["version"], version)
        self.assertTrue(reproduced["chain_ok"])
        self.assertTrue(reproduced["matches_decision"])
        self.assertEqual(reproduced["assessment"]["transaction_count"], 2)
        latest = service.assess("A-001")
        self.assertEqual(latest.transaction_count, 3)

    def test_reproduce_requires_decision(self):
        service, _ = make_service()
        with self.assertRaises(StateError):
            service.reproduce("A-001", user="复核人员")


class GapReportTest(unittest.TestCase):
    def test_gaps_shrink_as_evidence_arrives(self):
        service, _ = make_service()
        report = service.gap_report("A-001")
        self.assertEqual(len(report["gaps"]), 4)
        self.assertIn("取证缺口", report["explanation"])
        fill_evidence(service)
        report = service.gap_report("A-001")
        self.assertEqual(report["gaps"], [])
        self.assertIn("无取证缺口", report["explanation"])


class SecretAccessTest(unittest.TestCase):
    def test_secret_masked_until_granted_and_access_logged(self):
        service, _ = make_service()
        fill_evidence(service)
        service.append_record("A-001", K.COST_STANDARD.value, {
            "standard_id": "CB-Q2",
            "items": [{"product_spec": "SP-1", "unit_cost": 800}],
            "valid_from": "2026-04-01", "valid_to": "2026-06-30", "note": "保密条款",
        }, "承办人甲", secret=True)
        masked = service.read_records("A-001", user="复核人员")[-1]
        self.assertTrue(masked["payload"]["已隐藏"])
        service.grant_secret_access("A-001", grantee="复核人员", granted_by="负责人", days=3)
        opened = service.read_records("A-001", user="复核人员")[-1]
        self.assertEqual(opened["payload"]["note"], "保密条款")
        kinds = [r["kind"] for r in service.read_records("A-001", user="承办人甲")]
        self.assertIn(K.SECRET_ACCESS.value, kinds)

    def test_expired_grant_masks_again(self):
        clock = Clock()
        service, _ = make_service(clock)
        service.append_record("A-001", K.PRODUCT_SPEC.value, {"spec_id": "SP-1", "name": "样品"}, "承办人甲", secret=True)
        service.grant_secret_access("A-001", grantee="复核人员", granted_by="负责人", days=2)
        clock.advance(3)
        masked = service.read_records("A-001", user="复核人员")[-1]
        self.assertTrue(masked["payload"]["已隐藏"])


class QualityLinkageTest(unittest.TestCase):
    def test_unqualified_sampling_creates_tasks_and_blocks_closure(self):
        service, clock = make_service()
        fill_evidence(service)
        service.append_record("A-001", K.SAMPLING_RESULT.value, {
            "product_spec": "SP-1", "qualified": False, "sampled_at": "2026-06-25",
        }, "质量监管人员")
        tasks = service.list_tasks("A-001")
        self.assertEqual({t["kind"] for t in tasks}, {"抽查", "召回协作"})
        service.notify("A-001", "承办人甲", {"拟处罚": "罚款"}, appeal_days=5)
        clock.advance(6)
        service.decide("A-001", "承办人甲", {"类型": "罚款"})
        service.submit_rectification("A-001", "经营者", {"措施": "整改"})
        clock.advance(30)
        service.append_record("A-001", K.ONLINE_TRANSACTION.value, {
            "product_spec": "SP-1", "channel": "线上-P", "price": 900,
            "quantity": 1, "sold_at": "2026-08-05", "order_id": "E-2",
        }, "平台企业")
        service.append_record("A-001", K.OFFLINE_TRANSACTION.value, {
            "product_spec": "SP-1", "channel": "线下-S", "price": 900,
            "quantity": 1, "sold_at": "2026-08-05", "order_id": "L-2",
        }, "承办人甲")
        result = service.verify_rectification("A-001", "复核人员")
        self.assertFalse(result["eliminated"])  # 联动任务未办结不能结案
        for task in service.list_tasks("A-001"):
            service.complete_task("A-001", task["task_id"], "质量监管人员")
        result = service.verify_rectification("A-001", "复核人员")
        self.assertTrue(result["eliminated"])


class RectificationVerifyTest(unittest.TestCase):
    def test_continued_dumping_fails_verification(self):
        service, clock = make_service()
        reach_decided(service, clock)
        service.submit_rectification("A-001", "经营者", {"措施": "整改"})
        clock.advance(30)
        # 核验时段内仍低于成本：风险未消除
        service.append_record("A-001", K.ONLINE_TRANSACTION.value, {
            "product_spec": "SP-1", "channel": "线上-P", "price": 500,
            "quantity": 100, "sold_at": "2026-08-05", "order_id": "E-2",
        }, "平台企业")
        result = service.verify_rectification("A-001", "复核人员")
        self.assertFalse(result["eliminated"])
        self.assertEqual(service.overview("A-001")["status"], CaseStatus.RECTIFYING.value)
        scope = result["verification_scope"]
        self.assertEqual(scope["product_specs"], ["SP-1"])  # 同一批商品
        self.assertEqual(scope["window_start"], "2026-07-10")  # 等长时段（29 天）
        self.assertEqual(scope["window_end"], "2026-08-08")


class PersistenceTest(unittest.TestCase):
    def test_snapshot_roundtrip_preserves_chain(self):
        service, clock = make_service()
        reach_decided(service, clock)
        path = "/tmp/case_service_snapshot.json"
        service.save(path)
        loaded = CaseService.load(path, clock=clock)
        before = service.overview("A-001")
        after = loaded.overview("A-001")
        self.assertEqual(before["head_hash"], after["head_hash"])
        self.assertEqual(before["decision_hash"], after["decision_hash"])
        self.assertTrue(after["chain_ok"])
        self.assertTrue(loaded.reproduce("A-001", user="复核人员")["matches_decision"])


if __name__ == "__main__":
    unittest.main()
