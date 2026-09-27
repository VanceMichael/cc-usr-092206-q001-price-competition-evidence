"""低价倾销判定与质量联动风险识别。

判定只依赖证据链上截至某版本的记录：先应用口径修订得到生效视图，
再在立案锁定的范围内比对交易价格与当期成本，区分正常清仓与恶意倾销；
价格畸低且安全风险较大的产品标记为质量联动风险。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date

from .ledger import Record
from .models import REQUIRED_EVIDENCE_KINDS, TRANSACTION_KINDS, RecordKind, Scope

VERDICT_DUMPING = "恶意倾销"
VERDICT_CLEARANCE = "正常清仓"
VERDICT_INSUFFICIENT = "证据不足"

# 价格畸低触发质量联动的低于成本交易占比阈值
RISK_RATIO_THRESHOLD = 0.3

_CONTAINER_KINDS = (
    RecordKind.PLATFORM_SUPPLEMENT.value,
    RecordKind.CROSS_REGION_TRANSFER.value,
)


@dataclass(frozen=True)
class Assessment:
    """一次判定的结论与可解释依据。"""

    verdict: str
    transaction_count: int
    below_cost_count: int
    below_cost_ratio: float
    justified_count: int
    quality_risk: bool
    gaps: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_payload(self) -> dict:
        return asdict(self)


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def effective_kind(record: Record) -> str:
    """平台补报、跨地区移送按其实际提供的证据类别参与判定。"""
    if record.kind in _CONTAINER_KINDS:
        return str(record.payload.get("provides", record.kind))
    return record.kind


def effective_records(records: list[Record]) -> list[Record]:
    """应用口径修订：原始记录留在链上，判定采用最新口径。

    修订记录通过 payload["revises"] 指向被修订记录的序号；同一记录被多次
    修订时以序号靠后的修订为准。
    """
    by_seq = {r.seq: r for r in records}
    replaced: dict[int, Record] = {}
    superseded: set[int] = set()
    for record in records:
        if record.kind != RecordKind.CALIBER_REVISION.value:
            continue
        target = record.payload.get("revises")
        if target not in by_seq or by_seq[target].kind == RecordKind.CALIBER_REVISION.value:
            continue
        base = replaced.get(target, by_seq[target])
        merged = {**base.payload, **{k: v for k, v in record.payload.items() if k != "revises"}}
        replaced[target] = replace(base, payload=merged)
        superseded.add(target)
    return [replaced.get(r.seq, r) for r in records]


def _scoped_transactions(view: list[Record], scope: Scope) -> list[Record]:
    result = []
    for record in view:
        if effective_kind(record) not in TRANSACTION_KINDS:
            continue
        payload = record.payload
        try:
            on = _parse_date(payload.get("sold_at"))
        except (TypeError, ValueError):
            continue
        if scope.contains(str(payload.get("product_spec")), str(payload.get("channel")), on):
            result.append(record)
    return result


def _cost_table(view: list[Record], scope: Scope) -> dict[str, int]:
    """当期成本标准：按标准标识匹配，同标识的后续记录逐项覆盖。"""
    costs: dict[str, int] = {}
    for record in view:
        if effective_kind(record) != RecordKind.COST_STANDARD.value:
            continue
        if record.payload.get("standard_id") != scope.cost_standard_id:
            continue
        for item in record.payload.get("items", []):
            costs[str(item["product_spec"])] = int(item["unit_cost"])
    return costs


def _justifications(view: list[Record], scope: Scope) -> list[dict]:
    return [
        r.payload
        for r in view
        if effective_kind(r) == RecordKind.CLEARANCE_JUSTIFICATION.value
        and str(r.payload.get("product_spec")) in scope.product_specs
    ]


def _justified_by(payload: dict, justifications: list[dict]) -> str | None:
    try:
        on = _parse_date(payload.get("sold_at"))
    except (TypeError, ValueError):
        return None
    for item in justifications:
        if str(item.get("product_spec")) != str(payload.get("product_spec")):
            continue
        if _parse_date(item.get("covers_from")) <= on <= _parse_date(item.get("covers_to")):
            return str(item.get("reason"))
    return None


def _safety_risky(view: list[Record], scope: Scope) -> bool:
    return any(
        effective_kind(r) == RecordKind.PRODUCT_SPEC.value
        and str(r.payload.get("spec_id")) in scope.product_specs
        and bool(r.payload.get("safety_risk"))
        for r in view
    )


def has_unqualified_sampling(view: list[Record], scope: Scope) -> bool:
    """范围内是否有抽检不合格记录；sampled_at 落在范围时段内才计入。"""
    for record in view:
        if effective_kind(record) != RecordKind.SAMPLING_RESULT.value:
            continue
        if str(record.payload.get("product_spec")) not in scope.product_specs:
            continue
        if record.payload.get("qualified") is not False:
            continue
        try:
            sampled = _parse_date(record.payload.get("sampled_at"))
        except (TypeError, ValueError):
            return True  # 日期缺失时保守计入
        if scope.window_start <= sampled <= scope.window_end:
            return True
    return False


def evidence_gaps(records: list[Record], scope: Scope) -> list[str]:
    """立案锁定的四类证据中仍缺失的类别，即承办人需说明的取证缺口。"""
    view = effective_records(records)
    gaps = []
    if not any(
        effective_kind(r) == RecordKind.PRODUCT_SPEC.value
        and str(r.payload.get("spec_id")) in scope.product_specs
        for r in view
    ):
        gaps.append(RecordKind.PRODUCT_SPEC.value)
    if not _cost_table(view, scope):
        gaps.append(RecordKind.COST_STANDARD.value)
    transactions = _scoped_transactions(view, scope)
    if not any(effective_kind(r) == RecordKind.ONLINE_TRANSACTION.value for r in transactions):
        gaps.append(RecordKind.ONLINE_TRANSACTION.value)
    if not any(effective_kind(r) == RecordKind.OFFLINE_TRANSACTION.value for r in transactions):
        gaps.append(RecordKind.OFFLINE_TRANSACTION.value)
    return gaps


def assess(records: list[Record], scope: Scope) -> Assessment:
    """在锁定范围内比对交易与当期成本，给出可解释的判定结论。"""
    view = effective_records(records)
    gaps = evidence_gaps(records, scope)
    costs = _cost_table(view, scope)
    transactions = _scoped_transactions(view, scope)
    justifications = _justifications(view, scope)
    unqualified = has_unqualified_sampling(view, scope)

    reasons: list[str] = []
    comparable = [t for t in transactions if str(t.payload.get("product_spec")) in costs]
    if not costs:
        reasons.append("缺少当期成本标准，无法比对是否低于成本")
    if not transactions:
        reasons.append("锁定范围内没有交易证据")
    elif not comparable:
        reasons.append("成本标准未覆盖锁定商品，交易无法比对")
    if not comparable:
        quality_risk = unqualified or (_safety_risky(view, scope) and bool(transactions))
        return Assessment(
            VERDICT_INSUFFICIENT, len(transactions), 0, 0.0, 0,
            quality_risk, tuple(gaps), tuple(reasons),
        )

    below = [t for t in comparable if int(t.payload["price"]) < costs[str(t.payload["product_spec"])]]
    justified = [t for t in below if _justified_by(t.payload, justifications)]
    unjustified = [t for t in below if t not in justified]
    ratio = len(below) / len(comparable)

    reasons.append(f"锁定范围内共 {len(transactions)} 笔交易，其中 {len(comparable)} 笔可与当期成本比对")
    if below:
        reasons.append(f"低于成本销售 {len(below)} 笔，占比 {ratio:.1%}")
    else:
        reasons.append("未发现低于成本的销售记录")
    if justified:
        reasons_found = sorted({_justified_by(t.payload, justifications) for t in justified})
        reasons.append(f"{len(justified)} 笔低于成本交易有清仓事由（{'、'.join(reasons_found)}）覆盖")
    if unjustified:
        reasons.append(f"{len(unjustified)} 笔低于成本交易无正当理由，涉嫌为排挤竞争对手以低于成本销售")

    quality_risk = unqualified or (_safety_risky(view, scope) and ratio >= RISK_RATIO_THRESHOLD)
    if unqualified:
        reasons.append("抽检结果不合格，需启动质量联动")
    elif quality_risk:
        reasons.append("价格畸低且属安全风险较大产品，需提高抽查力度")

    verdict = VERDICT_DUMPING if unjustified else VERDICT_CLEARANCE
    return Assessment(
        verdict, len(transactions), len(below), round(ratio, 4), len(justified),
        quality_risk, tuple(gaps), tuple(reasons),
    )
