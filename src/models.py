"""办案服务的领域模型与取值字典。

这些 ``@dataclass`` 只承载数据与序列化逻辑，不包含业务流程；
业务流程见 :mod:`src.service`，事实如何追加见 :mod:`src.ledger`。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from enum import Enum
from typing import Any


class CasePhase(str, Enum):
    """案件所处阶段，只能按登记顺序前移。"""

    LEAD = "线索"          # 已登记线索，尚未立案
    OPENED = "已立案"      # 调查范围已锁定
    HEARING = "申辩期"     # 已告知当事人，可在限期内申辩
    CLOSED = "已结案"      # 处罚决定版本已钉住
    RECTIFIED = "已核验"  # 整改复核通过


class EvidenceKind(str, Enum):
    """证据类别：对应题目要求锁定的四类事实。"""

    PLATFORM_PRICE = "平台价格"
    COST = "企业成本"
    QUALITY = "抽检结果"
    APPEAL = "商家申诉"
    OFFLINE_TXN = "线下交易"
    SCOPE = "调查范围"
    RECTIFICATION = "整改材料"


class PricePointType(str, Enum):
    """价格观测的时间口径。"""

    DAILY = "日常售价"
    PROMO = "促销成交价"
    PRE_PROMO = "促前标价"
    PLATFORM_REPORTED = "平台补报"  # 调查开始后补报，只能追加
    RECTIFICATION = "整改复核价"    # 整改后同批产品同时段的重新定价


class Confidence(str, Enum):
    """证据可信程度，由取证方式决定。"""

    A = "A-固定提取"   # 公证、区块链固证、执法记录仪同步
    B = "B-后台调取"   # 平台后台接口/企业账册调取
    C = "C-截图自述"   # 截图、当事人自述等易变证据


class QualityResult(str, Enum):
    PENDING = "待检"
    PASS = "合格"
    FAIL = "不合格"


class RiskLevel(str, Enum):
    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"


class SecretAccessResult(str, Enum):
    GRANTED = "授权开放"
    DENIED = "未授权"


@dataclass(frozen=True)
class Product:
    """商品规格：立案时锁定，后续不可修改。"""

    sku: str                       # 商品编码
    name: str                      # 商品名称
    spec: str                      # 规格型号（容量/尺寸/等级等）
    unit: str                      # 计价单位
    category: str                  # 品类（决定重点监管目录匹配）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Product":
        return Product(**d)


@dataclass(frozen=True)
class ActivityScope:
    """活动范围：促销活动的时空与渠道边界，立案时锁定。"""

    activity_name: str
    channels: tuple[str, ...]      # 线上平台/门店等渠道
    regions: tuple[str, ...]       # 覆盖地区
    start: date                    # 活动开始日
    end: date                      # 活动结束日

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["channels"] = list(self.channels)
        d["regions"] = list(self.regions)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ActivityScope":
        return ActivityScope(
            activity_name=d["activity_name"],
            channels=tuple(d["channels"]),
            regions=tuple(d["regions"]),
            start=date.fromisoformat(d["start"]),
            end=date.fromisoformat(d["end"]),
        )


@dataclass(frozen=True)
class CostStandard:
    """当期成本标准：适用期间与调查时段必须一致，成本口径与活动同期。"""

    standard_id: str
    method: str                    # 成本核算方法（如：加权平均法）
    unit_cost: float               # 单位成本（元/计价单位）
    includes_logistics: bool       # 是否含物流费用
    valid_from: date
    valid_to: date
    source: str                    # 来源（企业账册/第三方审计）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["valid_from"] = self.valid_from.isoformat()
        d["valid_to"] = self.valid_to.isoformat()
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "CostStandard":
        return CostStandard(
            standard_id=d["standard_id"],
            method=d["method"],
            unit_cost=float(d["unit_cost"]),
            includes_logistics=bool(d["includes_logistics"]),
            valid_from=date.fromisoformat(d["valid_from"]),
            valid_to=date.fromisoformat(d["valid_to"]),
            source=d["source"],
        )


@dataclass(frozen=True)
class PriceObservation:
    """一次价格观测；不同时间口径各自留存，不能相互覆盖。"""

    sku: str
    observed_on: date
    point_type: PricePointType
    price: float
    channel: str
    region: str
    confidence: Confidence
    source_ref: str                # 证据编号
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "observed_on": self.observed_on.isoformat(),
            "point_type": self.point_type.value,
            "price": self.price,
            "channel": self.channel,
            "region": self.region,
            "confidence": self.confidence.value,
            "source_ref": self.source_ref,
            "note": self.note,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PriceObservation":
        return PriceObservation(
            sku=d["sku"],
            observed_on=date.fromisoformat(d["observed_on"]),
            point_type=PricePointType(d["point_type"]),
            price=float(d["price"]),
            channel=d["channel"],
            region=d["region"],
            confidence=Confidence(d["confidence"]),
            source_ref=d["source_ref"],
            note=d.get("note", ""),
        )


@dataclass(frozen=True)
class QualityInspection:
    """一次抽检结果。"""

    inspection_id: str
    sku: str
    sampled_on: date
    agency: str
    result: QualityResult
    items: tuple[str, ...]         # 检验项目
    report_ref: str
    risk: RiskLevel = RiskLevel.LOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "inspection_id": self.inspection_id,
            "sku": self.sku,
            "sampled_on": self.sampled_on.isoformat(),
            "agency": self.agency,
            "result": self.result.value,
            "items": list(self.items),
            "report_ref": self.report_ref,
            "risk": self.risk.value,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "QualityInspection":
        return QualityInspection(
            inspection_id=d["inspection_id"],
            sku=d["sku"],
            sampled_on=date.fromisoformat(d["sampled_on"]),
            agency=d["agency"],
            result=QualityResult(d["result"]),
            items=tuple(d["items"]),
            report_ref=d["report_ref"],
            risk=RiskLevel(d.get("risk", "低")),
        )


@dataclass(frozen=True)
class MerchantAppeal:
    """商家申辩材料；清仓等正当理由需有库存与期限佐证。"""

    appeal_id: str
    submitted_on: date
    grounds: str                   # 申辩理由（如：季节性清仓）
    clearance_inventory: int | None  # 清仓库存数量，None 表示未提供
    evidence_refs: tuple[str, ...]
    within_deadline: bool          # 是否在限期内提交

    def to_dict(self) -> dict[str, Any]:
        return {
            "appeal_id": self.appeal_id,
            "submitted_on": self.submitted_on.isoformat(),
            "grounds": self.grounds,
            "clearance_inventory": self.clearance_inventory,
            "evidence_refs": list(self.evidence_refs),
            "within_deadline": self.within_deadline,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MerchantAppeal":
        return MerchantAppeal(
            appeal_id=d["appeal_id"],
            submitted_on=date.fromisoformat(d["submitted_on"]),
            grounds=d["grounds"],
            clearance_inventory=d.get("clearance_inventory"),
            evidence_refs=tuple(d.get("evidence_refs", [])),
            within_deadline=bool(d["within_deadline"]),
        )


@dataclass(frozen=True)
class OfflineTransaction:
    """线下交易凭证，用于与线上价格交叉印证。"""

    txn_id: str
    txn_date: date
    sku: str
    price: float
    quantity: int
    channel: str
    region: str
    source_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "txn_date": self.txn_date.isoformat(),
            "sku": self.sku,
            "price": self.price,
            "quantity": self.quantity,
            "channel": self.channel,
            "region": self.region,
            "source_ref": self.source_ref,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "OfflineTransaction":
        return OfflineTransaction(
            txn_id=d["txn_id"],
            txn_date=date.fromisoformat(d["txn_date"]),
            sku=d["sku"],
            price=float(d["price"]),
            quantity=int(d["quantity"]),
            channel=d["channel"],
            region=d["region"],
            source_ref=d["source_ref"],
        )
