from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def number(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SystemExit(f"{field} 必须是数字。") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        comparator = "正数" if positive else "非负数"
        raise SystemExit(f"{field} 必须是{comparator}。")
    return result


def load_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"找不到计划文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 格式错误：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("计划文件顶层必须是 JSON 对象。")
    return value


def estimate(plan: dict[str, Any]) -> dict[str, Any]:
    pricing = plan.get("bailian", plan.get("pricing"))
    if not isinstance(pricing, dict):
        raise SystemExit("缺少 bailian 或 pricing 对象。")
    price = number(pricing.get("price_per_second"), "price_per_second", positive=True)
    currency = str(pricing.get("currency", "CNY"))

    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise SystemExit("shots 必须是非空数组。")

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    final_seconds = Decimal("0")
    candidate_seconds = Decimal("0")

    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            raise SystemExit(f"shots[{index}] 必须是对象。")
        shot_id = str(shot.get("id", "")).strip()
        if not shot_id or shot_id in seen:
            raise SystemExit(f"shots[{index}].id 缺失或重复：{shot_id!r}")
        seen.add(shot_id)
        duration = number(shot.get("duration_seconds"), f"{shot_id}.duration_seconds", positive=True)
        attempts = number(shot.get("planned_attempts", 1), f"{shot_id}.planned_attempts", positive=True)
        if attempts != attempts.to_integral_value():
            raise SystemExit(f"{shot_id}.planned_attempts 必须是整数。")
        reference_video_duration = number(
            shot.get("reference_video_duration_seconds", 0),
            f"{shot_id}.reference_video_duration_seconds",
        )
        media = shot.get("media", [])
        if not isinstance(media, list):
            raise SystemExit(f"{shot_id}.media 必须是数组。")
        has_reference_video = any(
            isinstance(item, dict) and item.get("type") == "reference_video" for item in media
        )
        if has_reference_video and reference_video_duration <= 0:
            raise SystemExit(f"{shot_id} 包含参考视频，必须填写 reference_video_duration_seconds。")
        if not has_reference_video and reference_video_duration > 0:
            raise SystemExit(f"{shot_id} 填写了参考视频时长，但 media 中没有 reference_video。")
        if has_reference_video and (
            reference_video_duration > 15 or reference_video_duration + duration > 30
        ):
            raise SystemExit(f"{shot_id} 的参考视频时长或输入输出总时长超过万相 3.0 限制。")
        # 万相 3.0 参考视频模式会同时计算输入视频和输出视频时长。
        seconds = (duration + reference_video_duration) * attempts
        rows.append(
            {
                "id": shot_id,
                "duration_seconds": float(duration),
                "planned_attempts": int(attempts),
                "reference_video_duration_seconds": float(reference_video_duration),
                "candidate_seconds": float(seconds),
                "estimated_generation_cost": float((seconds * price).quantize(Decimal("0.01"))),
            }
        )
        final_seconds += duration
        candidate_seconds += seconds

    extras = plan.get("additional_costs", [])
    if not isinstance(extras, list):
        raise SystemExit("additional_costs 必须是数组。")
    extra_total = Decimal("0")
    for index, item in enumerate(extras):
        if not isinstance(item, dict):
            raise SystemExit(f"additional_costs[{index}] 必须是对象。")
        extra_total += number(item.get("amount", 0), f"additional_costs[{index}].amount")

    generation = candidate_seconds * price
    total = generation + extra_total
    return {
        "project_title": plan.get("project_title", ""),
        "currency": currency,
        "price_per_second": float(price),
        "final_timeline_seconds": float(final_seconds),
        "candidate_generation_seconds": float(candidate_seconds),
        "generation_cost": float(generation.quantize(Decimal("0.01"))),
        "additional_costs": float(extra_total.quantize(Decimal("0.01"))),
        "estimated_total": float(total.quantize(Decimal("0.01"))),
        "shots": rows,
        "note": "估算包含参考视频输入时长和输出时长，并按计划尝试次数计算；需核对地域、模型、分辨率、当前价格、免费额度和实际账单。",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="按候选素材秒数估算 AI 视频生成预算。")
    parser.add_argument("plan", type=Path, help="镜头计划 JSON 文件")
    parser.add_argument("--output", type=Path, help="可选：把结果保存为 JSON")
    args = parser.parse_args()

    result = estimate(load_plan(args.plan))
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
