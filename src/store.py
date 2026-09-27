"""案件、证据链、授权与联动任务的存储及快照持久化。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime

from .ledger import Ledger, record_from_dict, record_to_dict
from .models import Case, CaseStatus, Grant, LinkageTask, Scope


def _scope_to_dict(scope: Scope | None) -> dict | None:
    return scope.to_payload() if scope else None


def _scope_from_dict(data: dict | None) -> Scope | None:
    if not data:
        return None
    return Scope(
        product_specs=tuple(data["product_specs"]),
        channels=tuple(data["channels"]),
        window_start=date.fromisoformat(data["window_start"]),
        window_end=date.fromisoformat(data["window_end"]),
        cost_standard_id=data["cost_standard_id"],
    )


def _case_to_dict(case: Case) -> dict:
    return {
        "case_id": case.case_id,
        "title": case.title,
        "source": case.source,
        "handler": case.handler,
        "status": case.status.value,
        "created_at": case.created_at.isoformat(),
        "scope": _scope_to_dict(case.scope),
        "appeal_deadline": case.appeal_deadline.isoformat() if case.appeal_deadline else None,
        "decision_version": case.decision_version,
        "decision_hash": case.decision_hash,
    }


def _case_from_dict(data: dict) -> Case:
    return Case(
        case_id=data["case_id"],
        title=data["title"],
        source=data["source"],
        handler=data["handler"],
        status=CaseStatus(data["status"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        scope=_scope_from_dict(data["scope"]),
        appeal_deadline=datetime.fromisoformat(data["appeal_deadline"]) if data["appeal_deadline"] else None,
        decision_version=data["decision_version"],
        decision_hash=data["decision_hash"],
    )


def _grant_to_dict(grant: Grant) -> dict:
    return {**asdict(grant), "expires_at": grant.expires_at.isoformat()}


def _grant_from_dict(data: dict) -> Grant:
    return Grant(
        case_id=data["case_id"],
        grantee=data["grantee"],
        granted_by=data["granted_by"],
        expires_at=datetime.fromisoformat(data["expires_at"]),
    )


def _task_to_dict(task: LinkageTask) -> dict:
    return {**asdict(task), "created_at": task.created_at.isoformat()}


def _task_from_dict(data: dict) -> LinkageTask:
    return LinkageTask(
        task_id=data["task_id"],
        case_id=data["case_id"],
        kind=data["kind"],
        reason=data["reason"],
        created_at=datetime.fromisoformat(data["created_at"]),
        done=bool(data["done"]),
    )


class InMemoryStore:
    """内存存储：案件台账、逐案证据链、商业秘密授权、质量联动任务。"""

    def __init__(self):
        self.cases: dict[str, Case] = {}
        self.ledgers: dict[str, Ledger] = {}
        self.grants: list[Grant] = []
        self.tasks: dict[str, LinkageTask] = {}

    def snapshot(self) -> dict:
        return {
            "cases": [_case_to_dict(c) for c in self.cases.values()],
            "ledgers": {
                case_id: [record_to_dict(r) for r in ledger.at()]
                for case_id, ledger in self.ledgers.items()
            },
            "grants": [_grant_to_dict(g) for g in self.grants],
            "tasks": [_task_to_dict(t) for t in self.tasks.values()],
        }

    @classmethod
    def restore(cls, data: dict) -> "InMemoryStore":
        store = cls()
        for item in data.get("cases", []):
            case = _case_from_dict(item)
            store.cases[case.case_id] = case
        for case_id, records in data.get("ledgers", {}).items():
            store.ledgers[case_id] = Ledger(case_id, [record_from_dict(r) for r in records])
        store.grants = [_grant_from_dict(g) for g in data.get("grants", [])]
        for item in data.get("tasks", []):
            task = _task_from_dict(item)
            store.tasks[task.task_id] = task
        return store
