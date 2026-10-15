from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REGIONS = {
    "cn-beijing": "cn-beijing",
    "ap-southeast-1": "ap-southeast-1",
    "ap-northeast-1": "ap-northeast-1",
    "eu-central-1": "eu-central-1",
    "us-east-1": "us-east-1",
    "cn-hongkong": "cn-hongkong",
}
MODELS = {"wan3.0-video", "wan3.0-video-prime"}
MEDIA_TYPES = {
    "first_frame", "last_frame", "reference_image", "reference_video",
    "reference_audio", "file", "link",
}


class ClientError(Exception):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClientError(f"找不到文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ClientError(f"JSON 格式错误：{path}（第 {exc.lineno} 行）") from exc
    if not isinstance(value, dict):
        raise ClientError("JSON 顶层必须是对象。")
    return value


def decimal_value(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ClientError(f"{field} 必须是数字。") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ClientError(f"{field} 必须是{'正数' if positive else '非负数'}。")
    return result


def project_root(plan_path: Path) -> Path:
    return plan_path.resolve().parent.parent


def safe_output_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ClientError("镜头必须填写 output_file 相对路径。")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ClientError("output_file 必须位于项目目录内。") from exc
    return candidate


def load_plan(plan_path: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    plan = read_json(plan_path)
    settings = plan.get("bailian")
    if not isinstance(settings, dict):
        raise ClientError("计划缺少 bailian 配置对象。")
    model = settings.get("model")
    if model not in MODELS:
        raise ClientError("bailian.model 仅支持 wan3.0-video 或 wan3.0-video-prime。")
    if settings.get("region") not in REGIONS:
        raise ClientError("bailian.region 必须是受支持的百炼地域标识。")
    workspace = settings.get("workspace_id") or os.environ.get("DASHSCOPE_WORKSPACE_ID") or ""
    if not isinstance(workspace, str):
        raise ClientError("workspace_id 格式无效。")
    if workspace and not re.fullmatch(r"[A-Za-z0-9_-]+", workspace):
        raise ClientError("workspace_id 格式无效。")
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ClientError("shots 必须是非空数组。")
    return plan, settings, workspace


def find_shot(plan: dict[str, Any], shot_id: str) -> dict[str, Any]:
    matches = [shot for shot in plan["shots"] if isinstance(shot, dict) and shot.get("id") == shot_id]
    if len(matches) != 1:
        raise ClientError(f"镜头 ID 不存在或重复：{shot_id}")
    return matches[0]


def resolve_prompt(shot: dict[str, Any], root: Path) -> str:
    prompt = shot.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    prompt_file = shot.get("prompt_file")
    if isinstance(prompt_file, str) and prompt_file.strip():
        path = (root / prompt_file).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise ClientError("prompt_file 必须位于项目目录内。") from exc
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise ClientError(f"找不到提示词文件：{prompt_file}") from exc
        if value:
            return value
    raise ClientError(f"镜头 {shot.get('id')} 缺少 prompt 或有效的 prompt_file。")


def build_payload(plan: dict[str, Any], settings: dict[str, Any], shot: dict[str, Any], root: Path) -> dict[str, Any]:
    prompt = resolve_prompt(shot, root)
    media = shot.get("media", [])
    if not isinstance(media, list):
        raise ClientError(f"镜头 {shot['id']} 的 media 必须是数组。")
    if len(prompt) > 20000:
        raise ClientError("提示词超过百炼接口的 20000 字符限制。")
    validated_media: list[dict[str, str]] = []
    for index, item in enumerate(media):
        if not isinstance(item, dict):
            raise ClientError(f"media[{index}] 必须是对象。")
        media_type, url = item.get("type"), item.get("url")
        if media_type not in MEDIA_TYPES or not isinstance(url, str) or not url.strip():
            raise ClientError(f"media[{index}] 必须包含有效的 type 和 url。")
        validated_media.append({"type": media_type, "url": url.strip()})
    frame_types = {item["type"] for item in validated_media} & {"first_frame", "last_frame"}
    reference_types = {item["type"] for item in validated_media} & {
        "reference_image", "reference_video", "reference_audio", "file", "link"
    }
    if frame_types and reference_types:
        raise ClientError("首尾帧素材不能与参考图、参考视频、参考音频、文件或网页混用。")
    media_types = [item["type"] for item in validated_media]
    if media_types.count("first_frame") > 1 or media_types.count("last_frame") > 1:
        raise ClientError("first_frame 和 last_frame 各最多传入一张。")
    if "last_frame" in media_types and "first_frame" not in media_types:
        raise ClientError("last_frame 必须与 first_frame 同时传入。")
    for media_type, maximum in (("reference_image", 10), ("reference_video", 5), ("reference_audio", 5)):
        if media_types.count(media_type) > maximum:
            raise ClientError(f"{media_type} 最多传入 {maximum} 个。")
    if "file" in {item["type"] for item in validated_media} and "link" in {item["type"] for item in validated_media}:
        raise ClientError("file 和 link 不能在同一请求中同时传入。")

    parameters = {
        "resolution": settings.get("resolution", "720P"),
        "ratio": settings.get("ratio", "9:16"),
        "duration": shot.get("duration_seconds"),
        "prompt_extend": settings.get("prompt_extend", True),
        "watermark": settings.get("watermark", False),
    }
    if settings.get("audio") is not None:
        parameters["audio"] = settings["audio"]
    if shot.get("seed") is not None:
        parameters["seed"] = shot["seed"]
    if not isinstance(parameters["duration"], int) or not 2 <= parameters["duration"] <= 30:
        raise ClientError(f"镜头 {shot['id']} 的 duration_seconds 必须是 2 到 30 的整数。")
    if parameters["resolution"] not in {"480P", "720P", "1080P"}:
        raise ClientError("resolution 必须是 480P、720P 或 1080P。")
    if parameters["ratio"] not in {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
        raise ClientError("ratio 不是万相 3.0 支持的画幅。")
    if "audio" in parameters and not isinstance(parameters["audio"], bool):
        raise ClientError("audio 必须是 true 或 false。")
    if not isinstance(parameters["prompt_extend"], bool) or not isinstance(parameters["watermark"], bool):
        raise ClientError("prompt_extend 和 watermark 必须是 true 或 false。")

    input_data: dict[str, Any] = {"prompt": prompt}
    if validated_media:
        input_data["media"] = validated_media
    return {"model": settings["model"], "input": input_data, "parameters": parameters}


def estimated_cost(settings: dict[str, Any], shot: dict[str, Any]) -> Decimal:
    price = decimal_value(settings.get("price_per_second"), "bailian.price_per_second", positive=True)
    verified_at = settings.get("price_verified_at")
    source = settings.get("price_source")
    if not verified_at or str(verified_at).startswith("填写") or not source or str(source).startswith("填写"):
        raise ClientError("提交前必须填写 price_verified_at 和 price_source，确认地域、模型和分辨率单价。")
    input_seconds = decimal_value(shot.get("reference_video_duration_seconds", 0), "reference_video_duration_seconds")
    output_seconds = decimal_value(shot.get("duration_seconds"), "duration_seconds", positive=True)
    media = shot.get("media", [])
    has_reference_video = isinstance(media, list) and any(
        isinstance(item, dict) and item.get("type") == "reference_video" for item in media
    )
    if has_reference_video and input_seconds <= 0:
        raise ClientError("包含 reference_video 时必须填写 reference_video_duration_seconds，避免低估费用。")
    if not has_reference_video and input_seconds > 0:
        raise ClientError("填写了参考视频输入时长，但 media 中没有 reference_video。")
    if has_reference_video and (input_seconds > 15 or input_seconds + output_seconds > 30):
        raise ClientError("万相 3.0 参考视频总输入时长不得超过 15 秒，输入与输出合计不得超过 30 秒。")
    attempts = decimal_value(shot.get("planned_attempts", 1), "planned_attempts", positive=True)
    if attempts != attempts.to_integral_value():
        raise ClientError("planned_attempts 必须是整数。")
    return (input_seconds + output_seconds) * price


def host(settings: dict[str, Any], workspace: str) -> str:
    if not workspace:
        raise ClientError("提交或查询需要 workspace_id，或设置 DASHSCOPE_WORKSPACE_ID。")
    region = REGIONS[settings["region"]]
    return f"https://{workspace}.{region}.maas.aliyuncs.com"


def request_json(url: str, api_key: str, *, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if method == "POST":
        headers["X-DashScope-Async"] = "enable"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        request_id = ""
        try:
            error_data = json.loads(exc.read().decode("utf-8"))
            request_id = str(error_data.get("request_id", ""))
            code = str(error_data.get("code", ""))
        except (UnicodeDecodeError, json.JSONDecodeError):
            code = ""
        detail = f"，错误码 {code}" if code else ""
        detail += f"，request_id {request_id}" if request_id else ""
        raise ClientError(f"百炼请求失败，HTTP {exc.code}{detail}。不会自动重试。") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ClientError("请求未能确认是否已被百炼接收；请先在控制台核对任务，勿直接重复提交。") from exc
    try:
        result = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError("百炼返回内容不是有效 JSON。") from exc
    if not isinstance(result, dict):
        raise ClientError("百炼返回内容格式无效。")
    return result


def task_record_path(root: Path, shot_id: str, label: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", shot_id) or not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        raise ClientError("镜头 ID 和 attempt-label 仅能包含字母、数字、下划线和连字符。")
    return root / "tasks" / f"{shot_id}-{label}.json"


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def do_preview(args: argparse.Namespace) -> None:
    plan, settings, _ = load_plan(args.plan)
    shot = find_shot(plan, args.shot_id)
    safe_output_path(project_root(args.plan), shot.get("output_file"))
    payload = build_payload(plan, settings, shot, project_root(args.plan))
    preview_payload = json.loads(json.dumps(payload))
    for item in preview_payload["input"].get("media", []):
        item["url"] = "[素材地址已隐藏]"
    try:
        cost = str(estimated_cost(settings, shot).quantize(Decimal("0.01")))
        cost_note = ""
    except ClientError as exc:
        if "price_per_second" not in str(exc) and "price_verified_at" not in str(exc):
            raise
        cost = None
        cost_note = "请先填写已核实的正数单价、核价日期和价格来源。"
    print(json.dumps({
        "shot_id": shot["id"],
        "model": settings["model"],
        "region": settings["region"],
        "resolution": payload["parameters"]["resolution"],
        "ratio": payload["parameters"]["ratio"],
        "duration_seconds": payload["parameters"]["duration"],
        "reference_video_duration_seconds": shot.get("reference_video_duration_seconds", 0),
        "single_attempt_estimated_cost": cost,
        "cost_note": cost_note,
        "planned_attempts": shot.get("planned_attempts", 1),
        "price_verified_at": settings.get("price_verified_at"),
        "price_source": settings.get("price_source"),
        "request_preview": preview_payload,
        "note": "预览不会调用百炼。提交时只创建这一个镜头的一次任务。",
    }, ensure_ascii=False, indent=2))


def do_submit(args: argparse.Namespace) -> None:
    plan, settings, workspace = load_plan(args.plan)
    shot = find_shot(plan, args.shot_id)
    root = project_root(args.plan)
    safe_output_path(root, shot.get("output_file"))
    payload = build_payload(plan, settings, shot, root)
    cost = estimated_cost(settings, shot)
    limit = decimal_value(args.max_cost, "--max-cost")
    if cost > limit:
        raise ClientError(f"单次估算费用 {cost:.2f} 元高于 --max-cost {limit:.2f} 元，未提交。")
    if not args.confirm:
        raise ClientError("这是付费请求。检查预览后，提交命令必须显式添加 --confirm。")
    record_path = task_record_path(root, shot["id"], args.attempt_label)
    if record_path.exists():
        raise ClientError(f"任务记录已存在，拒绝重复提交：{record_path.relative_to(root)}。需要新一轮时使用新的 --attempt-label。")
    destination = safe_output_path(root, shot["output_file"])
    if destination.exists():
        raise ClientError(f"输出文件已存在，为避免覆盖原片请先修改 output_file：{destination.relative_to(root)}")
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise ClientError("请先设置 DASHSCOPE_API_KEY 环境变量。")
    endpoint = host(settings, workspace) + "/api/v1/services/aigc/video-generation/video-synthesis"
    result = request_json(endpoint, api_key, method="POST", body=payload)
    output = result.get("output", {})
    task_id = output.get("task_id") if isinstance(output, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise ClientError("响应中没有 task_id。请先到百炼控制台核对任务，勿直接重复提交。")
    record = {
        "shot_id": shot["id"],
        "attempt_label": args.attempt_label,
        "task_id": task_id,
        "model": settings["model"],
        "region": settings["region"],
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "status": output.get("task_status", "PENDING"),
        "estimated_cost_cny": str(cost.quantize(Decimal("0.01"))),
        "request_id": result.get("request_id"),
        "output_file": shot["output_file"],
        "plan_file": str(args.plan.resolve()),
        "workspace_id": str(workspace),
    }
    save_json(record_path, record)
    print(f"任务已提交：{task_id}")
    print(f"任务记录：{record_path.relative_to(root)}")
    print("后续查询该记录并下载视频；本脚本不会自动重试或提交其他镜头。")


def download_video(url: str, destination: Path) -> int:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ClientError("百炼返回的视频地址不是有效 HTTPS 地址。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 2 * 1024 * 1024 * 1024:
                    raise ClientError("下载文件超过 2 GiB 限制，已中止。")
                target.write(chunk)
        if total == 0:
            raise ClientError("下载到的文件为空。")
        temporary.replace(destination)
    except ClientError:
        temporary.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise ClientError("视频下载失败。请在任务有效期内重新查询并下载。") from exc
    return total


def do_poll(args: argparse.Namespace) -> None:
    record_path = args.task_record.resolve()
    record = read_json(record_path)
    root = project_root(Path(record["plan_file"]))
    plan_path = Path(record["plan_file"])
    plan, settings, workspace = load_plan(plan_path)
    if settings["region"] != record.get("region") or str(workspace) != record.get("workspace_id"):
        raise ClientError("任务记录与当前计划的地域或 Workspace ID 不匹配。")
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise ClientError("请先设置 DASHSCOPE_API_KEY 环境变量。")
    shot = find_shot(plan, record["shot_id"])
    destination = safe_output_path(root, record["output_file"])
    if destination.exists():
        raise ClientError(f"输出文件已存在，为避免覆盖原片请先移走文件或修改计划中的 output_file：{destination.relative_to(root)}")
    endpoint = host(settings, workspace) + f"/api/v1/tasks/{record['task_id']}"
    while True:
        result = request_json(endpoint, api_key, method="GET")
        output = result.get("output", {})
        status = output.get("task_status", "UNKNOWN") if isinstance(output, dict) else "UNKNOWN"
        record["status"] = status
        record["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        save_json(record_path, record)
        print(f"任务状态：{status}")
        if status == "SUCCEEDED":
            video_url = output.get("video_url")
            if not isinstance(video_url, str) or not video_url:
                raise ClientError("任务成功但响应中没有 video_url。")
            size = download_video(video_url, destination)
            record["downloaded_file"] = str(destination.relative_to(root))
            record["downloaded_bytes"] = size
            record["usage"] = result.get("usage", {})
            record["downloaded_at"] = datetime.now(timezone.utc).isoformat()
            save_json(record_path, record)
            print(f"视频已下载：{destination.relative_to(root)}（{size} 字节）")
            return
        if status in {"FAILED", "CANCELED", "UNKNOWN"}:
            code = output.get("code", "") if isinstance(output, dict) else ""
            request_id = result.get("request_id", "")
            print(f"任务结束状态：{status}；错误码：{code or '无'}；request_id：{request_id or '无'}")
            return
        if not args.watch:
            return
        time.sleep(args.interval)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全调用阿里云百炼万相 3.0 视频生成接口。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preview", "submit"):
        sub = subparsers.add_parser(command, help="预览调用" if command == "preview" else "提交单镜生成任务")
        sub.add_argument("plan", type=Path, help="分镜计划 JSON")
        sub.add_argument("--shot-id", required=True, help="要处理的镜头 ID")
        if command == "submit":
            sub.add_argument("--max-cost", required=True, help="本次调用可接受的最高估算费用（元）")
            sub.add_argument("--confirm", action="store_true", help="确认创建一次可能产生费用的任务")
            sub.add_argument("--attempt-label", default="attempt-01", help="本次调用的唯一名称")
    poll = subparsers.add_parser("poll", help="查询任务并在成功后下载视频")
    poll.add_argument("task_record", type=Path, help="submit 生成的任务记录 JSON")
    poll.add_argument("--watch", action="store_true", help="持续轮询至任务结束")
    poll.add_argument("--interval", type=int, default=15, help="轮询间隔秒数，默认 15 秒")
    return parser


def main() -> None:
    args = make_parser().parse_args()
    try:
        if args.command == "preview":
            do_preview(args)
        elif args.command == "submit":
            do_submit(args)
        elif args.command == "poll":
            if args.interval < 5 or args.interval > 300:
                raise ClientError("轮询间隔必须在 5 到 300 秒之间。")
            do_poll(args)
    except ClientError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
