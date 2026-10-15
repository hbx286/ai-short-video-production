# 阿里云百炼万相 3.0 视频调用

本说明对应万相 3.0 异步视频生成接口，脚本使用 Python 标准库，无需安装第三方依赖。官方文档可能更新，使用前仍需复核模型、素材限制、地域和价格。

## 配置镜头计划

从 `assets/shot-plan.template.json` 复制一份项目计划。填写 `bailian` 中的模型、地域、Workspace ID、分辨率、画幅和当前单价；价格来源与核对日期必填。不要把 API Key 写入 JSON。

单镜必须包含：

- `id`：唯一镜头 ID；
- `duration_seconds`：整数，2–30 秒；
- `prompt` 或 `prompt_file`：本镜提示词；
- `output_file`：项目目录内的相对路径；
- `media`：可选数组，元素为百炼支持的 `type` 与 `url`；
- `reference_video_duration_seconds`：有参考视频时填写输入总时长，预算会计入输入和输出秒数；
- `planned_attempts`：整片预算使用的计划尝试次数。

素材 URL 可用 HTTPS 公网地址或百炼支持的 OSS 临时 URL。不要把带签名的素材 URL 放入日志或任务记录；客户端只把必要的素材 URL 发送给百炼。`first_frame`/`last_frame` 不能与参考素材模式混用；`file` 与 `link` 不能同次传入。调用素材前需按官方文档核对类型、数量、时长和文件大小限制。

## 设置凭据

Linux/macOS：

```bash
export DASHSCOPE_API_KEY='你的百炼 API Key'
export DASHSCOPE_WORKSPACE_ID='你的业务空间 ID'
```

PowerShell：

```powershell
$env:DASHSCOPE_API_KEY = '你的百炼 API Key'
$env:DASHSCOPE_WORKSPACE_ID = '你的业务空间 ID'
```

Workspace ID 也可写入本地计划文件；API Key 仅从环境变量读取。终端历史记录、提示词、代码、任务 JSON 和聊天内容都不要保存 API Key。

## 预览、提交和下载

```bash
python scripts/estimate_budget.py assets/my-shot-plan.json
python scripts/bailian_video.py preview assets/my-shot-plan.json --shot-id shot-01
python scripts/bailian_video.py submit assets/my-shot-plan.json --shot-id shot-01 --max-cost 3.00 --confirm
python scripts/bailian_video.py poll tasks/shot-01-attempt-01.json --watch
```

上述命令假设模板已复制为 `assets/my-shot-plan.json`，且已填写当前正数单价、核价日期、价格来源和镜头提示词。

`preview` 不发起网络请求，列出请求参数、当前估价和价格来源。`submit` 每次只创建一个镜头的一次任务，必须同时提供明确的 `--max-cost` 与 `--confirm`；超过上限时拒绝提交。需要重新生成时使用新的 `--attempt-label`，先核对旧任务状态和预算，并给新尝试设置独立的 `output_file`，避免覆盖原片。

成功提交后，脚本把任务 ID、提交时间、模型、地域、估价和输出路径写入 `tasks/<镜头>-<attempt-label>.json`，不保存 API Key、提示词或素材 URL。百炼可能在服务端已经创建任务后发生网络超时，因此客户端不会自动重试；先在控制台查询，再决定下一步。

`poll` 默认只查询一次。添加 `--watch` 后每 15 秒轮询，直到成功或任务结束。成功时会立即把 MP4 下载至计划中的 `output_file`，并记录百炼返回的 usage。万相 3.0 的任务 ID 与视频链接有效期为 24 小时，需尽快下载。下载完成后仍需用媒体探测工具检查可解码性、分辨率、帧率、时长和音轨；脚本只确认下载非空，不代替完整质检。

## 地域、计费和边界

当前客户端支持北京、新加坡、东京、法兰克福、弗吉尼亚和中国香港的万相 3.0 接口域名。`region` 必须与 API Key、Workspace 和开通地域相匹配。

费用估算使用计划文件里的 `price_per_second`，不内置永久单价。提交前到[百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)核对地域、模型和分辨率。万相 3.0 的参考视频输入与输出都计费；图片、音频参考不计输入秒数，输出按成功生成视频秒数计费。实际费用以控制台账单和任务 usage 为准，免费额度、折扣及活动可能变化。

万相 3.0 当前参数、素材组合、异步任务状态和有效期见[官方 API 参考](https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference)与[调用指南](https://help.aliyun.com/zh/model-studio/wan3-video-generation-guide)。本客户端不处理上传本地素材到 OSS、不自动购买模型、不自动重试、不批量提交，也不执行配音、剪辑或字幕。
