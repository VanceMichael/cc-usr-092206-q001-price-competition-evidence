"""办案服务：线索、调查、告知、申辩、决定、整改与核验的完整流程。

不变量：
- 立案时锁定调查范围（商品规格、活动范围、时段、当期成本标准），并写入证据链；
- 证据链只追加：平台补报、跨地区移送、口径修订、规则下线只能追加到原始记录之后；
- 处罚决定钉住所依据的证据版本（序号与哈希），复核人员可按版本重现；
- 商业秘密按案件授权开放，每次查阅均在链上留痕；
- 整改核验使用同一批商品规格与等长时段，确认风险是否真正消除。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from . import assessment
from .assessment import Assessment
from .ledger import GENESIS_HASH, Ledger, Record, record_to_dict
from .models import Case, CaseStatus, Grant, LinkageTask, RecordKind, Scope
from .store import InMemoryStore


class CaseError(Exception):
    """办案流程中的领域错误。"""


class NotFoundError(CaseError):
    """案件或联动任务不存在。"""


class StateError(CaseError):
    """当前案件状态不允许该操作。"""


_TX_FIELDS = ("product_spec", "channel", "price", "quantity", "sold_at")

# 各类记录的必填字段；平台补报、跨地区移送按 provides 指向的类别一并校验
_REQUIRED_FIELDS = {
    RecordKind.PRODUCT_SPEC.value: ("spec_id", "name"),
    RecordKind.COST_STANDARD.value: ("standard_id", "items", "valid_from", "valid_to"),
    RecordKind.ONLINE_TRANSACTION.value: _TX_FIELDS,
    RecordKind.OFFLINE_TRANSACTION.value: _TX_FIELDS,
    RecordKind.PLATFORM_SUPPLEMENT.value: ("provides",),
    RecordKind.CROSS_REGION_TRANSFER.value: ("from_region", "provides"),
    RecordKind.CALIBER_REVISION.value: ("revises",),
    RecordKind.RULE_OFFLINE.value: ("platform", "rule_id", "offline_at"),
    RecordKind.CLEARANCE_JUSTIFICATION.value: ("product_spec", "reason", "covers_from", "covers_to"),
    RecordKind.SAMPLING_RESULT.value: ("product_spec", "qualified", "sampled_at"),
}

_DATE_FIELDS = ("sold_at", "covers_from", "covers_to", "valid_from", "valid_to")

# 只能由专门办案动作写入、不允许直接追加的程序性记录
_PROCEDURAL_KINDS = {
    RecordKind.SCOPE_LOCK.value,
    RecordKind.APPEAL.value,
    RecordKind.PENALTY_NOTICE.value,
    RecordKind.PENALTY_DECISION.value,
    RecordKind.RECTIFICATION_REPORT.value,
    RecordKind.QUALITY_LINKAGE.value,
    RecordKind.VERIFICATION.value,
    RecordKind.DISMISSAL.value,
    RecordKind.SECRET_ACCESS.value,
}

# 结案前证据类记录均可追加（补报、移送等只会追加到原始记录之后）
_APPENDABLE_STATUSES = {
    CaseStatus.INVESTIGATING,
    CaseStatus.NOTIFIED,
    CaseStatus.DECIDED,
    CaseStatus.RECTIFYING,
}

_GAP_HINTS = {
    RecordKind.PRODUCT_SPEC.value: "需登记锁定商品的规格信息",
    RecordKind.COST_STANDARD.value: "需调取当期成本标准",
    RecordKind.ONLINE_TRANSACTION.value: "需平台报送锁定范围内的线上交易",
    RecordKind.OFFLINE_TRANSACTION.value: "需向门店或经销商调取线下交易凭证",
}


def _validate_payload(kind: str, payload: dict) -> None:
    missing = [f for f in _REQUIRED_FIELDS.get(kind, ()) if f not in payload]
    if missing:
        raise CaseError(f"{kind} 缺少必填字段：{'、'.join(missing)}")
    provided = payload.get("provides")
    if kind in (RecordKind.PLATFORM_SUPPLEMENT.value, RecordKind.CROSS_REGION_TRANSFER.value):
        if provided not in _REQUIRED_FIELDS:
            raise CaseError(f"provides 必须是可提供的证据类别：{provided!r}")
        missing = [f for f in _REQUIRED_FIELDS[provided] if f not in payload]
        if missing:
            raise CaseError(f"{kind}（{provided}）缺少必填字段：{'、'.join(missing)}")
    effective = provided if provided in _REQUIRED_FIELDS else kind
    if effective in (RecordKind.ONLINE_TRANSACTION.value, RecordKind.OFFLINE_TRANSACTION.value):
        if int(payload["price"]) < 0 or int(payload["quantity"]) <= 0:
            raise CaseError("交易价格不能为负、数量必须为正")
    for field in _DATE_FIELDS:
        if field in payload:
            try:
                date.fromisoformat(str(payload[field])[:10])
            except ValueError:
                raise CaseError(f"字段 {field} 不是有效日期：{payload[field]!r}") from None


class CaseService:
    """市场监管低价竞争案件的完整办案服务。"""

    def __init__(self, store: InMemoryStore | None = None, clock=datetime.now):
        self.store = store or InMemoryStore()
        self.clock = clock

    # ---- 内部工具 ----

    def _get_case(self, case_id: str) -> Case:
        try:
            return self.store.cases[case_id]
        except KeyError:
            raise NotFoundError(f"案件不存在：{case_id}") from None

    def _ledger(self, case_id: str) -> Ledger:
        return self.store.ledgers[case_id]

    @staticmethod
    def _require(case: Case, statuses: set[CaseStatus], action: str) -> None:
        if case.status not in statuses:
            names = "、".join(s.value for s in statuses)
            raise StateError(f"案件处于「{case.status.value}」，{action}要求案件处于：{names}")

    def _append(self, case: Case, kind: RecordKind, payload: dict, author: str, secret: bool = False) -> Record:
        return self._ledger(case.case_id).append(
            kind=kind.value, payload=payload, author=author, at=self.clock(), secret=secret
        )

    # ---- 线索与立案 ----

    def register_clue(self, case_id: str, title: str, source: str, handler: str) -> Case:
        """登记价格违法线索，建立案件台账与空证据链。"""
        if case_id in self.store.cases:
            raise CaseError(f"案件编号已存在：{case_id}")
        case = Case(case_id, title, source, handler, CaseStatus.CLUE, self.clock())
        self.store.cases[case_id] = case
        self.store.ledgers[case_id] = Ledger(case_id)
        return case

    def dismiss(self, case_id: str, author: str, reason: str) -> Case:
        """不予立案或撤案，理由写入证据链。"""
        case = self._get_case(case_id)
        self._require(case, {CaseStatus.CLUE, CaseStatus.INVESTIGATING}, "不予立案")
        self._append(case, RecordKind.DISMISSAL, {"reason": reason}, author)
        case.status = CaseStatus.DISMISSED
        return case

    def open_investigation(
        self,
        case_id: str,
        *,
        product_specs: list[str],
        channels: list[str],
        window_start: date | str,
        window_end: date | str,
        cost_standard_id: str,
        author: str,
    ) -> Case:
        """立案调查：锁定商品规格、活动范围、时段与当期成本标准并写入证据链。"""
        case = self._get_case(case_id)
        self._require(case, {CaseStatus.CLUE}, "立案调查")
        start = date.fromisoformat(str(window_start)) if not isinstance(window_start, date) else window_start
        end = date.fromisoformat(str(window_end)) if not isinstance(window_end, date) else window_end
        if not product_specs or not channels:
            raise CaseError("调查范围必须包含商品规格与活动范围")
        if start > end:
            raise CaseError("调查时段起止颠倒")
        scope = Scope(tuple(product_specs), tuple(channels), start, end, cost_standard_id)
        case.scope = scope
        self._append(case, RecordKind.SCOPE_LOCK, scope.to_payload(), author)
        case.status = CaseStatus.INVESTIGATING
        return case

    # ---- 证据链 ----

    def append_record(self, case_id: str, kind: str, payload: dict, author: str, secret: bool = False) -> Record:
        """向证据链追加记录；只追加，原始记录永不修改。"""
        case = self._get_case(case_id)
        if kind not in {k.value for k in RecordKind}:
            raise CaseError(f"未知记录类型：{kind!r}")
        if kind in _PROCEDURAL_KINDS:
            raise CaseError(f"{kind} 只能由对应办案动作写入")
        self._require(case, _APPENDABLE_STATUSES, "追加证据")
        _validate_payload(kind, payload)
        record = self._ledger(case_id).append(
            kind=kind, payload=payload, author=author, at=self.clock(), secret=secret
        )
        self._sync_quality_tasks(case)
        return record

    def read_records(self, case_id: str, user: str, version: int | None = None) -> list[dict]:
        """按用户授权读取记录；商业秘密未授权时遮蔽，授权查阅在链上留痕。"""
        case = self._get_case(case_id)
        return self._view_records(case, user, self._ledger(case_id).at(version))

    def _view_records(self, case: Case, user: str, records: list[Record]) -> list[dict]:
        granted = any(
            g.case_id == case.case_id and g.grantee == user and g.active(self.clock())
            for g in self.store.grants
        )
        view, accessed = [], []
        for record in records:
            item = record_to_dict(record)
            if record.secret and not granted:
                item["payload"] = {"已隐藏": True, "提示": "商业秘密，需案件授权后查阅"}
            elif record.secret:
                accessed.append(record.seq)
            view.append(item)
        if accessed:
            # 结案后复核仍可能查阅，留痕不受案件状态限制
            self._ledger(case.case_id).append(
                kind=RecordKind.SECRET_ACCESS.value,
                payload={"user": user, "record_seqs": accessed},
                author=user,
                at=self.clock(),
            )
        return view

    def grant_secret_access(self, case_id: str, grantee: str, granted_by: str, days: int = 7) -> Grant:
        """按案件授权开放商业秘密，到期自动失效。"""
        self._get_case(case_id)
        if days <= 0:
            raise CaseError("授权期限必须为正数天数")
        grant = Grant(case_id, grantee, granted_by, self.clock() + timedelta(days=days))
        self.store.grants.append(grant)
        return grant

    # ---- 调查范围与取证缺口 ----

    def gap_report(self, case_id: str) -> dict:
        """承办人说明调查范围与取证缺口。"""
        case = self._get_case(case_id)
        if case.scope is None:
            return {
                "case_id": case_id,
                "status": case.status.value,
                "scope": None,
                "present": [],
                "gaps": [],
                "explanation": "尚未立案，调查范围未锁定。",
            }
        records = self._ledger(case_id).at()
        gaps = assessment.evidence_gaps(records, case.scope)
        scope = case.scope
        head = (
            f"调查范围已锁定：商品规格 {'、'.join(scope.product_specs)}；"
            f"活动范围 {'、'.join(scope.channels)}；"
            f"时段 {scope.window_start.isoformat()} 至 {scope.window_end.isoformat()}；"
            f"成本标准 {scope.cost_standard_id}。"
        )
        if gaps:
            detail = "；".join(f"{g}（{_GAP_HINTS[g]}）" for g in gaps)
            explanation = head + f"取证缺口：{detail}。"
        else:
            explanation = head + "四类证据已补齐，无取证缺口。"
        present = [k for k in assessment.REQUIRED_EVIDENCE_KINDS if k not in gaps]
        return {
            "case_id": case_id,
            "status": case.status.value,
            "scope": scope.to_payload(),
            "present": present,
            "gaps": gaps,
            "explanation": explanation,
        }

    def assess(self, case_id: str, version: int | None = None) -> Assessment:
        """按当前或指定版本的证据判定正常清仓 / 恶意倾销 / 证据不足。"""
        case = self._get_case(case_id)
        if case.scope is None:
            raise StateError("尚未立案，无法判定")
        return assessment.assess(self._ledger(case_id).at(version), case.scope)

    # ---- 告知、申辩与决定 ----

    def notify(self, case_id: str, author: str, proposed: dict, appeal_days: int = 5) -> Record:
        """处罚告知：告知拟处罚内容并开启企业申辩期限。"""
        case = self._get_case(case_id)
        self._require(case, {CaseStatus.INVESTIGATING}, "处罚告知")
        if appeal_days <= 0:
            raise CaseError("申辩期限必须为正数天数")
        case.appeal_deadline = self.clock() + timedelta(days=appeal_days)
        payload = {"proposed": proposed, "appeal_deadline": case.appeal_deadline.isoformat()}
        record = self._append(case, RecordKind.PENALTY_NOTICE, payload, author)
        case.status = CaseStatus.NOTIFIED
        return record

    def appeal(self, case_id: str, author: str, content: str) -> Record:
        """企业在限期内申辩；逾期或不在告知阶段不予受理。"""
        case = self._get_case(case_id)
        self._require(case, {CaseStatus.NOTIFIED}, "企业申辩")
        if case.appeal_deadline and self.clock() > case.appeal_deadline:
            raise StateError(f"申辩期限已过（{case.appeal_deadline.isoformat()}）")
        return self._append(case, RecordKind.APPEAL, {"content": content}, author)

    def decide(self, case_id: str, author: str, penalty: dict) -> Record:
        """处罚决定：钉住所依据的证据版本，复核人员可按此版本重现。"""
        case = self._get_case(case_id)
        self._require(case, {CaseStatus.NOTIFIED}, "处罚决定")
        if case.appeal_deadline and self.clock() <= case.appeal_deadline:
            raise StateError("申辩期限未满，不能作出处罚决定")
        result = self.assess(case_id)
        if result.verdict == assessment.VERDICT_INSUFFICIENT:
            raise StateError("证据不足，不能作出处罚决定；请先补齐取证缺口")
        ledger = self._ledger(case_id)
        version, digest = ledger.head_seq, ledger.head_hash
        payload = {
            "penalty": penalty,
            "assessment": result.to_payload(),
            "evidence_version": version,
            "evidence_hash": digest,
        }
        record = self._append(case, RecordKind.PENALTY_DECISION, payload, author)
        case.decision_version, case.decision_hash = version, digest
        case.status = CaseStatus.DECIDED
        self._sync_quality_tasks(case)
        return record

    def reproduce(self, case_id: str, user: str, version: int | None = None) -> dict:
        """复核人员重现处罚所依据的版本：记录、判定与哈希链校验。"""
        case = self._get_case(case_id)
        if version is None:
            if case.decision_version is None:
                raise StateError("尚未作出处罚决定，无可重现版本")
            version = case.decision_version
        ledger = self._ledger(case_id)
        records = ledger.at(version)
        head_hash = records[-1].hash if records else GENESIS_HASH
        result = assessment.assess(records, case.scope) if case.scope else None
        return {
            "case_id": case_id,
            "version": version,
            "head_hash": head_hash,
            "chain_ok": ledger.verify(version),
            "matches_decision": case.decision_hash == head_hash,
            "assessment": result.to_payload() if result else None,
            "records": self._view_records(case, user, records),
        }

    # ---- 整改与核验 ----

    def submit_rectification(self, case_id: str, author: str, report: dict) -> Record:
        """企业提交整改报告，案件进入整改复核阶段。"""
        case = self._get_case(case_id)
        self._require(case, {CaseStatus.DECIDED}, "提交整改报告")
        record = self._append(case, RecordKind.RECTIFICATION_REPORT, report, author)
        case.status = CaseStatus.RECTIFYING
        return record

    def verify_rectification(self, case_id: str, author: str, window_end: date | str | None = None) -> dict:
        """用同一批商品规格与等长时段核验风险是否真正消除。

        核验范围沿用立案锁定的商品规格与活动范围，时段取与锁定时段等长、
        截至 window_end（默认当天）的区间；风险消除且质量联动任务办结方可结案。
        """
        case = self._get_case(case_id)
        self._require(case, {CaseStatus.RECTIFYING}, "整改核验")
        scope = case.scope
        end = date.fromisoformat(str(window_end)) if window_end and not isinstance(window_end, date) else window_end
        end = end or self.clock().date()
        duration = (scope.window_end - scope.window_start).days
        verify_scope = Scope(scope.product_specs, scope.channels, end - timedelta(days=duration), end, scope.cost_standard_id)
        result = assessment.assess(self._ledger(case_id).at(), verify_scope)
        open_tasks = [t for t in self.store.tasks.values() if t.case_id == case_id and not t.done]
        eliminated = result.verdict != assessment.VERDICT_DUMPING and not open_tasks
        reasons = list(result.reasons)
        if open_tasks:
            reasons.append(f"仍有 {len(open_tasks)} 项质量联动任务未办结")
        reasons.append("核验结论：风险已消除" if eliminated else "核验结论：风险未消除")
        payload = {
            "verification_scope": verify_scope.to_payload(),
            "assessment": result.to_payload(),
            "eliminated": eliminated,
            "reasons": reasons,
        }
        self._append(case, RecordKind.VERIFICATION, payload, author)
        if eliminated:
            case.status = CaseStatus.CLOSED
        return payload

    # ---- 质量联动 ----

    def _sync_quality_tasks(self, case: Case) -> None:
        """价格畸低且安全风险较大时发起抽查，抽检不合格时发起召回协作。"""
        if case.scope is None or case.status in (CaseStatus.CLOSED, CaseStatus.DISMISSED):
            return
        records = self._ledger(case.case_id).at()
        view = assessment.effective_records(records)
        result = assessment.assess(records, case.scope)
        wants = []
        if result.quality_risk:
            wants.append(("抽查", "价格畸低且安全风险较大，提高抽查力度"))
        if assessment.has_unqualified_sampling(view, case.scope):
            wants.append(("召回协作", "抽检不合格，平台需核验商品信息并配合召回"))
        for kind, reason in wants:
            if any(t.case_id == case.case_id and t.kind == kind and not t.done for t in self.store.tasks.values()):
                continue
            task_id = f"{case.case_id}-T{len(self.store.tasks) + 1:03d}"
            self.store.tasks[task_id] = LinkageTask(task_id, case.case_id, kind, reason, self.clock())
            self._append(case, RecordKind.QUALITY_LINKAGE, {"event": "创建", "task_id": task_id, "kind": kind, "reason": reason}, "系统")

    def list_tasks(self, case_id: str) -> list[dict]:
        self._get_case(case_id)
        return [
            {"task_id": t.task_id, "kind": t.kind, "reason": t.reason, "done": t.done,
             "created_at": t.created_at.isoformat()}
            for t in self.store.tasks.values()
            if t.case_id == case_id
        ]

    def complete_task(self, case_id: str, task_id: str, author: str) -> dict:
        case = self._get_case(case_id)
        task = self.store.tasks.get(task_id)
        if task is None or task.case_id != case_id:
            raise NotFoundError(f"联动任务不存在：{task_id}")
        if not task.done:
            task.done = True
            self._append(case, RecordKind.QUALITY_LINKAGE, {"event": "办结", "task_id": task_id}, author)
        return {"task_id": task.task_id, "kind": task.kind, "done": task.done}

    # ---- 总览与持久化 ----

    def overview(self, case_id: str) -> dict:
        case = self._get_case(case_id)
        ledger = self._ledger(case_id)
        verdict = self.assess(case_id).verdict if case.scope else None
        return {
            "case_id": case.case_id,
            "title": case.title,
            "source": case.source,
            "handler": case.handler,
            "status": case.status.value,
            "created_at": case.created_at.isoformat(),
            "scope": case.scope.to_payload() if case.scope else None,
            "appeal_deadline": case.appeal_deadline.isoformat() if case.appeal_deadline else None,
            "decision_version": case.decision_version,
            "decision_hash": case.decision_hash,
            "head_seq": ledger.head_seq,
            "head_hash": ledger.head_hash,
            "chain_ok": ledger.verify(),
            "latest_verdict": verdict,
            "open_tasks": [t["task_id"] for t in self.list_tasks(case_id) if not t["done"]],
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.store.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path, clock=datetime.now) -> "CaseService":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(store=InMemoryStore.restore(data), clock=clock)
