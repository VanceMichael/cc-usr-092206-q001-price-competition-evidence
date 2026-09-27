"""命令行入口（零依赖，标准库）。

日常用法：

    python -m src.cli demo --store data/cases            # 端到端演示
    python -m src.cli list --store data/cases            # 案件清单
    python -m src.cli verify --store data/cases [案件]   # 哈希链自检
    python -m src.cli scope 案件编号 --store data/cases   # 调查范围与取证缺口
    python -m src.cli reproduce 案件编号 --store ...      # 处罚版本重现
    python -m src.cli rectify 案件编号 --store ...        # 整改核验
    python -m src.cli apply actions.json --store ...      # 按动作文件批量写入

动作文件为 ``{"case_id": "...", "actions": [{"op": "...", "args": {...}}]}``，
字段名与 :class:`~src.service.CaseService` 的方法参数一致；动作文件建议随案卷归档。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import demo as demo_module
from .ledger import FileLedger, LedgerError
from .models import (
    ActivityScope,
    CostStandard,
    PriceObservation,
    Product,
)
from .reports import RENDERERS, rectification_report, reproduction_report, scope_statement
from .service import CaseService, ServiceError


def _build_models(args: dict[str, Any]) -> dict[str, Any]:
    """把动作参数中的嵌套结构还原为领域模型。"""

    out = dict(args)
    if "products" in out:
        out["products"] = [Product(**p) for p in out["products"]]
    if "scope" in out:
        out["scope"] = ActivityScope.from_dict(out["scope"])
    if "cost_standard" in out:
        out["cost_standard"] = CostStandard.from_dict(out["cost_standard"])
    if "observation" in out:
        out["observation"] = PriceObservation.from_dict(out["observation"])
    return out


def apply_actions(service: CaseService, plan: dict) -> list[str]:
    case_id = plan["case_id"]
    applied: list[str] = []
    for step in plan.get("actions", []):
        op = step["op"]
        method = getattr(service, op, None)
        if method is None or not callable(method):
            raise ServiceError(f"未知操作：{op}")
        args = _build_models(step.get("args", {}))
        event = method(case_id, **args)
        applied.append(f"#{event.seq} {event.event_type}")
    return applied


def _print_report(kind: str, service: CaseService, case_id: str, as_json: bool) -> None:
    builders = {
        "scope": scope_statement,
        "reproduce": reproduction_report,
        "rectify": rectification_report,
    }
    data = builders[kind](service, case_id)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(RENDERERS[kind](data))


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default="data/cases", help="JSONL 账本目录")
    parser = argparse.ArgumentParser(prog="src.cli", description="低价竞争办案服务", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", parents=[common], help="写入并演示一个完整案件")
    sub.add_parser("list", parents=[common], help="列出案件")

    p_verify = sub.add_parser("verify", parents=[common], help="校验哈希链完整性")
    p_verify.add_argument("case_id", nargs="?", help="省略则校验全部案件")

    for name, help_text in (
        ("scope", "调查范围与取证缺口"),
        ("reproduce", "处罚版本重现"),
        ("rectify", "整改核验"),
    ):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("case_id")
        p.add_argument("--json", action="store_true", dest="as_json")

    p_apply = sub.add_parser("apply", parents=[common], help="按动作文件批量写入")
    p_apply.add_argument("plan_file")

    args = parser.parse_args(argv)
    ledger = FileLedger(args.store)
    service = CaseService(ledger)

    try:
        if args.command == "demo":
            case_id = demo_module.run_demo(service)
            print(f"演示案件已写入：{case_id}（账本目录 {args.store}）\n")
            _print_report("scope", service, case_id, False)
            print("\n" + "=" * 72 + "\n")
            _print_report("reproduce", service, case_id, False)
            print("\n" + "=" * 72 + "\n")
            _print_report("rectify", service, case_id, False)
            return 0

        if args.command == "list":
            cases = ledger.cases()
            for case_id in cases:
                state = service.state(case_id)
                print(f"{case_id}\t{state.phase.value}\t{state.summary}")
            if not cases:
                print("（暂无案件）")
            return 0

        if args.command == "verify":
            targets = [args.case_id] if args.case_id else ledger.cases()
            if not targets:
                print("（暂无案件）")
                return 0
            for case_id in targets:
                ledger.verify(case_id)
                print(f"[OK] {case_id} 哈希链完整")
            return 0

        if args.command in ("scope", "reproduce", "rectify"):
            _print_report(args.command, service, args.case_id, args.as_json)
            return 0

        if args.command == "apply":
            plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
            for line in apply_actions(service, plan):
                print(f"[追加] {line}")
            ledger.verify(plan["case_id"])
            print(f"[OK] {plan['case_id']} 哈希链校验通过")
            return 0

    except (ServiceError, LedgerError, ValueError, KeyError) as exc:
        print(f"操作被拒绝：{exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
