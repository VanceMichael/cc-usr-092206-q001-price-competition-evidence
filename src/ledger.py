"""只追加、可校验的案件证据链。

每个案件一条链：记录只能追加，不能修改或删除；平台补报、跨地区移送、
口径修订、规则下线一律追加到原始记录之后。每条记录携带前一条记录的哈希，
任何事后改动都会破坏链条并被 verify 检出。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

GENESIS_HASH = "0" * 64


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Record:
    """证据链上的一条不可变记录。"""

    seq: int
    case_id: str
    kind: str
    payload: dict
    author: str
    secret: bool
    created_at: datetime
    prev_hash: str
    hash: str


def record_hash(
    seq: int,
    case_id: str,
    kind: str,
    payload: dict,
    author: str,
    secret: bool,
    created_at: datetime,
    prev_hash: str,
) -> str:
    body = {
        "seq": seq,
        "case_id": case_id,
        "kind": kind,
        "payload": payload,
        "author": author,
        "secret": secret,
        "created_at": created_at.isoformat(),
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def record_to_dict(record: Record) -> dict:
    return {
        "seq": record.seq,
        "case_id": record.case_id,
        "kind": record.kind,
        "payload": record.payload,
        "author": record.author,
        "secret": record.secret,
        "created_at": record.created_at.isoformat(),
        "prev_hash": record.prev_hash,
        "hash": record.hash,
    }


def record_from_dict(data: dict) -> Record:
    return Record(
        seq=int(data["seq"]),
        case_id=data["case_id"],
        kind=data["kind"],
        payload=dict(data["payload"]),
        author=data["author"],
        secret=bool(data["secret"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        prev_hash=data["prev_hash"],
        hash=data["hash"],
    )


class Ledger:
    """单案件只追加证据链。"""

    def __init__(self, case_id: str, records: list[Record] | None = None):
        self._case_id = case_id
        self._records: list[Record] = sorted(records or [], key=lambda r: r.seq)

    @property
    def case_id(self) -> str:
        return self._case_id

    @property
    def head_seq(self) -> int:
        return self._records[-1].seq if self._records else 0

    @property
    def head_hash(self) -> str:
        return self._records[-1].hash if self._records else GENESIS_HASH

    def append(self, *, kind: str, payload: dict, author: str, at: datetime, secret: bool = False) -> Record:
        seq = self.head_seq + 1
        digest = record_hash(seq, self._case_id, kind, payload, author, secret, at, self.head_hash)
        record = Record(seq, self._case_id, kind, dict(payload), author, secret, at, self.head_hash, digest)
        self._records.append(record)
        return record

    def at(self, version: int | None = None) -> list[Record]:
        """返回截至指定版本（含）的记录，用于重现处罚所依据的历史口径。"""
        if version is None:
            return list(self._records)
        if version < 0 or version > self.head_seq:
            raise ValueError(f"版本 {version} 超出范围（当前链头为 {self.head_seq}）")
        return [r for r in self._records if r.seq <= version]

    def verify(self, version: int | None = None) -> bool:
        """重放并校验哈希链；version 给定则只校验到该版本。"""
        prev = GENESIS_HASH
        for record in self.at(version):
            if record.prev_hash != prev:
                return False
            expected = record_hash(
                record.seq,
                record.case_id,
                record.kind,
                record.payload,
                record.author,
                record.secret,
                record.created_at,
                record.prev_hash,
            )
            if record.hash != expected:
                return False
            prev = record.hash
        return True
