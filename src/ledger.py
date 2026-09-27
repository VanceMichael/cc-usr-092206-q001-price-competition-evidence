"""仅追加（append-only）的案件事件账本。

每个案件一条独立哈希链：后一事件的哈希包含前一事件的哈希，
任何插入、删除、篡改都会在 :meth:`Ledger.verify` 中暴露。

- 原始记录一经写入不可修改；口径修订、规则下线等一律以新事件追加；
- 文件版按案件落为 ``<案件编号>.jsonl``，只以追加方式打开；
- 复核人员重放同一批事件即可重现处罚所依据的版本。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

GENESIS = "GENESIS"
ENCODING = "utf-8"


class LedgerError(Exception):
    """违反仅追加或哈希链规则。"""


def canonical(value: Any) -> str:
    """字段顺序无关的规范 JSON 串，作为哈希输入。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Event:
    seq: int
    case_id: str
    event_type: str
    actor: str
    occurred_on: str               # 业务发生日期 ISO-8601
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    recorded_at: str               # 入账时间（UTC，ISO-8601）

    def to_line(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "case_id": self.case_id,
                "event_type": self.event_type,
                "actor": self.actor,
                "occurred_on": self.occurred_on,
                "payload": self.payload,
                "prev_hash": self.prev_hash,
                "hash": self.hash,
                "recorded_at": self.recorded_at,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def from_line(line: str) -> "Event":
        d = json.loads(line)
        return Event(
            seq=int(d["seq"]),
            case_id=d["case_id"],
            event_type=d["event_type"],
            actor=d["actor"],
            occurred_on=d["occurred_on"],
            payload=d["payload"],
            prev_hash=d["prev_hash"],
            hash=d["hash"],
            recorded_at=d["recorded_at"],
        )


def digest(event: Event) -> str:
    """重算事件哈希；入账前不包含事件自身的 hash 字段。"""

    material = canonical(
        {
            "seq": event.seq,
            "case_id": event.case_id,
            "event_type": event.event_type,
            "actor": event.actor,
            "occurred_on": event.occurred_on,
            "payload": event.payload,
            "prev_hash": event.prev_hash,
            "recorded_at": event.recorded_at,
        }
    )
    return hashlib.sha256(material.encode(ENCODING)).hexdigest()


class Ledger(Protocol):
    def append(
        self,
        case_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        occurred_on: str,
    ) -> Event: ...

    def events(self, case_id: str) -> list[Event]: ...

    def cases(self) -> list[str]: ...

    def verify(self, case_id: str) -> None: ...


class _BaseLedger:
    """共用的序号、哈希链与入账时间逻辑。"""

    def _next_seq(self, case_id: str) -> int:
        return len(self.events(case_id)) + 1

    def _build(
        self,
        case_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        occurred_on: str,
    ) -> Event:
        existing = self.events(case_id)
        prev_hash = existing[-1].hash if existing else GENESIS
        event = Event(
            seq=len(existing) + 1,
            case_id=case_id,
            event_type=event_type,
            actor=actor,
            occurred_on=occurred_on,
            payload=payload,
            prev_hash=prev_hash,
            hash="",
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return Event(**{**event.__dict__, "hash": digest(event)})

    @staticmethod
    def _check_chain(events: Iterable[Event]) -> None:
        prev = GENESIS
        for i, event in enumerate(events, start=1):
            if event.seq != i:
                raise LedgerError(f"事件序号断裂：应为 {i}，实为 {event.seq}")
            if event.prev_hash != prev:
                raise LedgerError(f"事件 {i} 前序哈希不匹配，账本可能被截断或重排")
            if digest(event) != event.hash:
                raise LedgerError(f"事件 {i} 内容哈希不匹配，记录可能被篡改")
            prev = event.hash

    def verify(self, case_id: str) -> None:
        self._check_chain(self.events(case_id))


class InMemoryLedger(_BaseLedger):
    """进程内账本，供测试与一次性演示使用。"""

    def __init__(self) -> None:
        self._streams: dict[str, list[Event]] = {}

    def append(
        self,
        case_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        occurred_on: str,
    ) -> Event:
        event = self._build(case_id, event_type, actor, payload, occurred_on)
        self._streams.setdefault(case_id, []).append(event)
        return event

    def events(self, case_id: str) -> list[Event]:
        return list(self._streams.get(case_id, []))

    def cases(self) -> list[str]:
        return sorted(self._streams)


class FileLedger(_BaseLedger):
    """JSONL 文件账本：每个案件一个仅追加文件。"""

    def __init__(self, store_dir: str | Path) -> None:
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, case_id: str) -> Path:
        if not case_id or "/" in case_id or "\\" in case_id or ".." in case_id:
            raise LedgerError("非法案件编号")
        return self.store_dir / f"{case_id}.jsonl"

    def append(
        self,
        case_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        occurred_on: str,
    ) -> Event:
        path = self._path(case_id)
        if path.exists():
            # 追加前先自检，拒绝在被破坏的账本尾部继续写入。
            self.verify(case_id)
        event = self._build(case_id, event_type, actor, payload, occurred_on)
        with path.open("a", encoding=ENCODING) as fh:
            fh.write(event.to_line() + "\n")
            fh.flush()
        return event

    def events(self, case_id: str) -> list[Event]:
        path = self._path(case_id)
        if not path.exists():
            return []
        events: list[Event] = []
        with path.open("r", encoding=ENCODING) as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if line:
                    events.append(Event.from_line(line))
        return events

    def cases(self) -> list[str]:
        return sorted(p.stem for p in self.store_dir.glob("*.jsonl"))

    def verify_all(self) -> None:
        for case_id in self.cases():
            self.verify(case_id)
