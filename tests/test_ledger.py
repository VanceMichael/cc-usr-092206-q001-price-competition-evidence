import unittest
from dataclasses import replace
from datetime import datetime

from src.ledger import GENESIS_HASH, Ledger


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 7, 1, 9, 0, 0)
        self.ledger = Ledger("A-001")

    def test_append_assigns_seq_and_hash(self):
        first = self.ledger.append(kind="商品规格", payload={"spec_id": "S1"}, author="甲", at=self.at)
        second = self.ledger.append(kind="成本标准", payload={"standard_id": "C1"}, author="甲", at=self.at)
        self.assertEqual((first.seq, second.seq), (1, 2))
        self.assertEqual(first.prev_hash, GENESIS_HASH)
        self.assertEqual(second.prev_hash, first.hash)
        self.assertEqual(self.ledger.head_hash, second.hash)

    def test_verify_detects_tampering(self):
        self.ledger.append(kind="商品规格", payload={"spec_id": "S1"}, author="甲", at=self.at)
        self.ledger.append(kind="线上交易证据", payload={"price": 500}, author="甲", at=self.at)
        self.assertTrue(self.ledger.verify())
        # 篡改历史记录内容（如把价格改掉）会破坏哈希链
        self.ledger._records[0] = replace(self.ledger._records[0], payload={"spec_id": "S2"})
        self.assertFalse(self.ledger.verify())

    def test_at_replays_prefix(self):
        for i in range(3):
            self.ledger.append(kind="商品规格", payload={"n": i}, author="甲", at=self.at)
        prefix = self.ledger.at(2)
        self.assertEqual([r.seq for r in prefix], [1, 2])
        self.assertTrue(self.ledger.verify(2))
        with self.assertRaises(ValueError):
            self.ledger.at(4)


if __name__ == "__main__":
    unittest.main()
