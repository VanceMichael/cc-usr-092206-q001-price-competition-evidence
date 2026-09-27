"""面向三类使用场景的可读报告。

- 承办人：调查范围说明 + 取证缺口清单（解释"查到哪、还差什么"）；
- 复核人员：处罚版本重现报告（按钉住的序号与哈希重放）；
- 整改核验：同一批产品、同一时段的核验结论。
"""

from __future__ import annotations

from .policy import (
    assess_price,
    find_evidence_gaps,
    quality_risk_triggers,
    verify_rectification,
)
from .service import CaseService

SEVERITY_ORDER = {"阻断": 0, "重要": 1, "提示": 2}


def scope_statement(service: CaseService, case_id: str) -> dict:
    """承办人用于说明调查范围与取证缺口的结构化报告。"""

    state = service.state(case_id)
    if state.scope is None:
        return {
            "case_id": case_id,
            "phase": state.phase.value,
            "summary": state.summary,
            "locked": False,
            "message": "案件尚未立案，调查范围、商品规格与当期成本标准待锁定",
        }

    start, end = state.window
    gaps = sorted(find_evidence_gaps(state), key=lambda g: (SEVERITY_ORDER[g.severity], g.code))
    return {
        "case_id": case_id,
        "phase": state.phase.value,
        "summary": state.summary,
        "locked": True,
        "opened_on": state.opened_on.isoformat() if state.opened_on else None,
        "scope": state.scope.to_dict(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "products": [p.to_dict() for p in state.products.values()],
        "cost_standard": state.cost_standard.to_dict() if state.cost_standard else None,
        "counts": {
            "price_observations": len(state.prices),
            "quality_inspections": len(state.quality),
            "offline_txns": len(state.offline_txns),
            "appeals": len(state.appeals),
            "evidence": len(state.evidence),
            "transfers": len(state.transfers),
            "caliber_revisions": len(state.caliber_history),
            "rule_takedowns": len(state.rules_takedown),
        },
        "price_assessments": [assess_price(state, sku).to_dict() for sku in sorted(state.products)],
        "risk_triggers": quality_risk_triggers(state),
        "gaps": [g.to_dict() for g in gaps],
        "chain": {"head_seq": state.head_seq, "head_hash": state.head_hash},
    }


def reproduction_report(service: CaseService, case_id: str) -> dict:
    """复核人员重现处罚所依据的版本。"""

    current = service.state(case_id)
    if current.decision is None:
        return {
            "case_id": case_id,
            "decided": False,
            "message": "尚未作出处罚决定",
        }
    decision = current.decision
    # 按决定钉住的序号重放：之后追加的补报/修订/下线均不进入处罚依据
    pinned = service.state(case_id, upto_seq=decision.head_seq)

    replayed_head_ok = pinned.head_hash == decision.head_hash
    calibers_ok = pinned.calibers == decision.calibers
    inputs_present = all(ref in pinned.evidence for ref in decision.inputs)

    return {
        "case_id": case_id,
        "decided": True,
        "decision": {
            "decision_id": decision.decision_id,
            "decided_on": decision.decided_on.isoformat(),
            "findings": decision.findings,
            "pinned_head_seq": decision.head_seq,
            "pinned_head_hash": decision.head_hash,
        },
        "integrity": {
            "chain_verified": True,  # state() 读取时已执行 verify，失败会抛错
            "replayed_head_matches": replayed_head_ok,
            "caliber_snapshot_matches": calibers_ok,
            "inputs_all_present_at_head": inputs_present,
            "reproducible": replayed_head_ok and calibers_ok and inputs_present,
        },
        "pinned_state": {
            "phase": pinned.phase.value,
            "calibers": pinned.calibers,
            "rules_active": list(decision.rules_active),
            "price_assessments": [
                assess_price(pinned, sku).to_dict() for sku in sorted(pinned.products)
            ],
            "evidence_inputs": [
                {"ref": ref, "kind": pinned.evidence[ref].kind, "summary": pinned.evidence[ref].summary}
                for ref in decision.inputs
            ],
            "gaps_at_head": [g.to_dict() for g in find_evidence_gaps(pinned)],
            "counts_at_head": {
                "price_observations": len(pinned.prices),
                "quality_inspections": len(pinned.quality),
                "appeals": len(pinned.appeals),
                "caliber_revisions": len(pinned.caliber_history),
                "rule_takedowns": len(pinned.rules_takedown),
            },
        },
        "appended_after_decision": {
            "caliber_revisions": [
                {
                    "caliber": c.caliber,
                    "old_value": c.old_value,
                    "new_value": c.new_value,
                    "seq": c.seq,
                }
                for c in current.caliber_history
                if c.seq > decision.head_seq
            ],
            "rule_takedowns": [
                {"rule_id": r.rule_id, "platform": r.platform, "seq": r.seq}
                for r in current.rules_takedown
                if r.seq > decision.head_seq
            ],
        },
    }


def rectification_report(service: CaseService, case_id: str) -> dict:
    """整改闭环报告：复用同批 SKU 与同一时段的核验结果。"""

    state = service.state(case_id)
    rect = state.rectification
    if rect is None:
        return {"case_id": case_id, "rectified": False, "message": "未下达整改计划"}
    result = verify_rectification(state)
    return {
        "case_id": case_id,
        "phase": state.phase.value,
        "rectified": state.phase.value == "已核验",
        "plan": {
            "plan_id": rect.plan_id,
            "skus": list(rect.skus),
            "window": {
                "start": rect.window_start.isoformat(),
                "end": rect.window_end.isoformat(),
            },
            "same_window_as_case": state.scope is not None
            and (rect.window_start, rect.window_end) == state.window,
            "due_on": rect.due_on.isoformat(),
            "submitted_on": rect.submitted_on.isoformat() if rect.submitted_on else None,
            "verified_on": rect.verified_on.isoformat() if rect.verified_on else None,
            "materials_refs": list(rect.materials_refs),
        },
        "verification": result,
    }


# --------------------------------------------------------------------- 文本渲染

def _render_scopes(data: dict) -> str:
    if not data.get("locked"):
        return f"[{data['case_id']}] {data['message']}"
    lines = [
        f"案件 {data['case_id']}（阶段：{data['phase']}）——调查范围说明",
        f"事由：{data['summary']}",
        f"活动：{data['scope']['activity_name']}｜渠道：{'、'.join(data['scope']['channels'])}"
        f"｜地区：{'、'.join(data['scope']['regions'])}",
        f"锁定时段：{data['window']['start']} ~ {data['window']['end']}",
        "锁定商品：",
    ]
    for p in data["products"]:
        lines.append(f"  - {p['sku']}｜{p['name']}｜{p['spec']}｜{p['category']}/{p['unit']}")
    cs = data["cost_standard"]
    lines.append(
        f"当期成本标准：{cs['standard_id']}｜{cs['method']}｜单位成本 {cs['unit_cost']}"
        f"｜适用 {cs['valid_from']}~{cs['valid_to']}｜来源：{cs['source']}"
    )
    c = data["counts"]
    lines.append(
        "证据存量：价格观测 {price_observations}、抽检 {quality_inspections}、"
        "线下交易 {offline_txns}、申辩 {appeals}、证据登记 {evidence}、"
        "移送 {transfers}、口径修订 {caliber_revisions}、规则下线 {rule_takedowns}".format(**c)
    )
    lines.append("价费比对：")
    for a in data["price_assessments"]:
        ratio = "—" if a["ratio"] is None else f"{a['ratio']:.2%}"
        lines.append(
            f"  - {a['sku']}：促销均价 {a['promo_avg']} / 成本 {a['cost']}（{ratio}）；{a['note']}"
        )
    if data["risk_triggers"]:
        lines.append("质量风险联动：")
        for t in data["risk_triggers"]:
            lines.append(f"  - {t['sku']}（风险 {t['risk']}）：" + "；".join(t["actions"]))
    lines.append(f"取证缺口（{len(data['gaps'])} 项）：")
    if not data["gaps"]:
        lines.append("  - 无，证据链完整")
    for g in data["gaps"]:
        lines.append(f"  - [{g['severity']}] {g['code']} {g['title']}：{g['detail']}")
    lines.append(f"链头：序号 {data['chain']['head_seq']}｜{data['chain']['head_hash'][:16]}…")
    return "\n".join(lines)


def _render_reproduction(data: dict) -> str:
    if not data.get("decided"):
        return f"[{data['case_id']}] {data['message']}"
    d = data["decision"]
    integ = data["integrity"]
    pinned = data["pinned_state"]
    lines = [
        f"案件 {data['case_id']}——处罚版本重现报告",
        f"决定：{d['decision_id']}（{d['decided_on']}）",
        f"认定：{d['findings']}",
        f"钉住位置：序号 {d['pinned_head_seq']}｜哈希 {d['pinned_head_hash'][:24]}…",
        "完整性校验："
        + ("通过" if all(
            integ[k]
            for k in (
                "chain_verified",
                "replayed_head_matches",
                "caliber_snapshot_matches",
                "inputs_all_present_at_head",
            )
        ) else "未通过"),
        f"  - 哈希链连续：{integ['chain_verified']}",
        f"  - 重放链头一致：{integ['replayed_head_matches']}",
        f"  - 口径快照一致：{integ['caliber_snapshot_matches']}",
        f"  - 引用证据在钉住时点均存在：{integ['inputs_all_present_at_head']}",
        f"钉住时刻口径：{pinned['calibers'] or '（无修订）'}",
        f"钉住时刻仍有效平台规则：{'、'.join(pinned['rules_active']) or '（无）'}",
        "处罚引用证据：",
    ]
    for ref in pinned["evidence_inputs"]:
        lines.append(f"  - {ref['ref']}（{ref['kind']}）：{ref['summary']}")
    lines.append("钉住时点的价费比对：")
    for a in pinned["price_assessments"]:
        ratio = "—" if a["ratio"] is None else f"{a['ratio']:.2%}"
        lines.append(f"  - {a['sku']}：{a['promo_avg']} / {a['cost']}（{ratio}）；{a['note']}")
    after = data["appended_after_decision"]
    if after["caliber_revisions"] or after["rule_takedowns"]:
        lines.append("处罚后追加（不影响已钉住版本，仅作追溯）：")
        for c in after["caliber_revisions"]:
            lines.append(f"  - 口径修订 #{c['seq']} {c['caliber']}：{c['old_value']} → {c['new_value']}")
        for r in after["rule_takedowns"]:
            lines.append(f"  - 规则下线 #{r['seq']} {r['rule_id']}（{r['platform']}）")
    return "\n".join(lines)


def _render_rectification(data: dict) -> str:
    if not data.get("plan"):
        return f"[{data['case_id']}] {data['message']}"
    p = data["plan"]
    v = data["verification"]
    lines = [
        f"案件 {data['case_id']}——整改核验报告（阶段：{data['phase']}）",
        f"计划：{p['plan_id']}｜同批产品：{'、'.join(p['skus'])}",
        f"核验时段：{p['window']['start']} ~ {p['window']['end']}"
        f"（{'与立案时段一致' if p['same_window_as_case'] else '与立案时段不一致！'}）",
        f"期限 {p['due_on']}｜提交 {p['submitted_on']}｜核验 {p['verified_on']}",
        f"核验结论：{'通过，风险已消除' if v['passed'] else '未通过，风险尚未消除'}",
        "逐项核验：",
    ]
    for c in v["checks"]:
        lines.append(f"  - [{'√' if c['passed'] else '×'}] {c['item']}：{c['detail']}")
    return "\n".join(lines)


RENDERERS = {
    "scope": _render_scopes,
    "reproduce": _render_reproduction,
    "rectify": _render_rectification,
}
