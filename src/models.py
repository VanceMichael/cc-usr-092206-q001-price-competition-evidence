"""办案服务的核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class CaseStatus(str, Enum):
    """案件生命周期状态。"""

    CLUE = "线索登记"
    INVESTIGATING = "立案调查"
    NOTIFIED = "处罚告知"
    DECIDED = "处罚决定"
    RECTIFYING = "整改中"
    CLOSED = "复核结案"
    DISMISSED = "不予立案"


class RecordKind(str, Enum):
    """可追加到案件证据链的记录类型。

    证据类记录（规格、成本、交易、补报、移送、修订等）在结案前均可追加；
    程序性记录（告知、决定、整改、核验等）只能由对应办案动作写入。
    """

    SCOPE_LOCK = "调查范围锁定"
    PRODUCT_SPEC = "商品规格"
    COST_STANDARD = "成本标准"
    ONLINE_TRANSACTION = "线上交易证据"
    OFFLINE_TRANSACTION = "线下交易证据"
    PLATFORM_SUPPLEMENT = "平台补报"
    CROSS_REGION_TRANSFER = "跨地区移送"
    CALIBER_REVISION = "口径修订"
    RULE_OFFLINE = "规则下线"
    CLEARANCE_JUSTIFICATION = "清仓事由证明"
    SAMPLING_RESULT = "抽检结果"
    APPEAL = "企业申辩"
    PENALTY_NOTICE = "处罚告知"
    PENALTY_DECISION = "处罚决定"
    RECTIFICATION_REPORT = "整改报告"
    QUALITY_LINKAGE = "质量联动"
    VERIFICATION = "核验结论"
    DISMISSAL = "不予立案"
    SECRET_ACCESS = "秘密查阅留痕"


TRANSACTION_KINDS = (
    RecordKind.ONLINE_TRANSACTION.value,
    RecordKind.OFFLINE_TRANSACTION.value,
)

# 立案时锁定、调查中必须补齐的证据类别
REQUIRED_EVIDENCE_KINDS = (
    RecordKind.PRODUCT_SPEC.value,
    RecordKind.COST_STANDARD.value,
    RecordKind.ONLINE_TRANSACTION.value,
    RecordKind.OFFLINE_TRANSACTION.value,
)


@dataclass(frozen=True)
class Scope:
    """立案时锁定的调查范围：商品规格、活动范围、时段与当期成本标准。"""

    product_specs: tuple[str, ...]
    channels: tuple[str, ...]
    window_start: date
    window_end: date
    cost_standard_id: str

    def contains(self, product_spec: str, channel: str, on: date) -> bool:
        return (
            product_spec in self.product_specs
            and channel in self.channels
            and self.window_start <= on <= self.window_end
        )

    def to_payload(self) -> dict:
        return {
            "product_specs": list(self.product_specs),
            "channels": list(self.channels),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "cost_standard_id": self.cost_standard_id,
        }


@dataclass
class Case:
    case_id: str
    title: str
    source: str
    handler: str
    status: CaseStatus
    created_at: datetime
    scope: Scope | None = None
    appeal_deadline: datetime | None = None
    decision_version: int | None = None
    decision_hash: str | None = None


@dataclass
class Grant:
    """商业秘密按案件授权的查阅许可。"""

    case_id: str
    grantee: str
    granted_by: str
    expires_at: datetime

    def active(self, now: datetime) -> bool:
        return now < self.expires_at


@dataclass
class LinkageTask:
    """质量风险触发的抽查或召回协作任务。"""

    task_id: str
    case_id: str
    kind: str
    reason: str
    created_at: datetime
    done: bool = False
