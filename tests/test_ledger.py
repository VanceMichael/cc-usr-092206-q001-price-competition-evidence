"""哈希链账本：仅追加、篡改可检出、文件持久化可重放。"""

import tempfile
import unittest
from pathlib import Path

from src.ledger import Event, FileLedger, InMemoryLedger, LedgerError, digest


class LedgerChainTest(unittest.TestCase):
    def setUp(self):
        self.ledger = InMemoryLedger()

    def test_seq_and_chain(self):
        e1 = self.ledger.append("C1", "A", "u", {"x": 1}, "2026-03-01")
        e2 = self.ledger.append("C1", "B", "u", {"x": 2}, "2026-03-02")
        self.assertEqual((e1.seq, e2.seq), (1, 2))
        self.assertEqual(e1.prev_hash, "GENESIS")
        self.assertEqual(e2.prev_hash, e1.hash)
        self.assertEqual(digest(e1), e1.hash)
        self.ledger.verify("C1")

    def test_cases_are_independent_chains(self):
        self.ledger.append("C1", "A", "u", {}, "2026-03-01")
        self.ledger.append("C2", "A", "u", {}, "2026-03-01")
        self.assertEqual(self.ledger.events("C1")[0].prev_hash, "GENESIS")
        self.assertEqual(self.ledger.events("C2")[0].prev_hash, "GENESIS")
        self.assertEqual(self.ledger.cases(), ["C1", "C2"])

    def test_tamper_payload_detected(self):
        self.ledger.append("C1", "A", "u", {"price": 80}, "2026-03-01")
        events = self.ledger._streams["C1"]
        events[0] = Event(**{**events[0].__dict__, "payload": {"price": 1}})
        with self.assertRaises(LedgerError):
            self.ledger.verify("C1")

    def test_reorder_detected(self):
        self.ledger.append("C1", "A", "u", {"n": 1}, "2026-03-01")
        self.ledger.append("C1", "B", "u", {"n": 2}, "2026-03-02")
        events = self.ledger._streams["C1"]
        events[0], events[1] = events[1], events[0]
        with self.assertRaises(LedgerError):
            self.ledger.verify("C1")


class FileLedgerTest(unittest.TestCase):
    def test_persist_and_replay(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = FileLedger(d)
            ledger.append("C9", "A", "u", {"v": 1}, "2026-03-01")
            ledger.append("C9", "B", "u", {"v": 2}, "2026-03-02")
            ledger.verify("C9")

            reloaded = FileLedger(d)
            self.assertEqual(reloaded.cases(), ["C9"])
            events = reloaded.events("C9")
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1].prev_hash, events[0].hash)
            reloaded.verify_all()

    def test_refuse_append_onto_tampered_file(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = FileLedger(d)
            ledger.append("C9", "A", "u", {"v": 1}, "2026-03-01")
            path = Path(d) / "C9.jsonl"
            line = path.read_text(encoding="utf-8")
            import json as _json

            obj = _json.loads(line)
            obj["payload"] = {"v": 999}
            path.write_text(_json.dumps(obj, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(LedgerError):
                ledger.append("C9", "B", "u", {"v": 2}, "2026-03-02")

    def test_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(LedgerError):
                FileLedger(d).events("../evil")


if __name__ == "__main__":
    unittest.main()
