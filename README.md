# AstrBot Codex CLI 插件

[![Tests](https://github.com/EisonMe/astrbot_plugin_codex_cli/actions/workflows/test.yml/badge.svg)](https://github.com/EisonMe/astrbot_plugin_codex_cli/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

让 AstrBot 调用系统中已安装的 Codex CLI 执行编程、排查和文件处理任务。支持后台执行、进度查询、继续会话、取消，以及任务结束后自动回报原聊天。可选接入 Codex 桌面，在应用中实时查看同一任务的进度、命令和文件修改。

默认使用独立的 `codex exec`，无需安装 Codex 桌面。Codex 使用自己的登录、模型和提供商配置，与 AstrBot 的聊天模型独立。

## 安装

要求 **Python 3.12+、AstrBot 4.23.1+**，以及可用的 Codex CLI。当前在 Linux 上验证；桌面联动依赖 Unix socket。

1. 在 **AstrBot 实际运行的环境和系统用户下** 安装、登录 Codex CLI：

   ```bash
   npm install -g @openai/codex
   codex login
   codex --version
   ```

   已有自定义提供商的用户可沿用自己的 Codex 配置。

2. 在 AstrBot WebUI → 插件管理 → 从链接安装，填入：

   ```text
   https://github.com/EisonMe/astrbot_plugin_codex_cli
   ```

   也可下载 [Releases](https://github.com/EisonMe/astrbot_plugin_codex_cli/releases/latest) 中的插件 ZIP，通过 WebUI 上传安装。

3. 在插件配置中填写 `default_workdir`。如果服务找不到 `codex`，将 `codex_path` 设置为可执行文件的绝对路径。
4. 用 AstrBot 管理员账号私聊发送 `/codex 查看当前目录并说明项目结构`。

**Docker 部署：** Codex 必须安装在 AstrBot 容器内，并为容器内运行用户配置登录信息和工作目录。宿主机安装的 CLI 不会自动出现在容器中。桌面联动还要求共享服务的 socket 对 AstrBot 可访问。

## 使用

| 操作 | 示例 |
| --- | --- |
| 自然语言委托 | `用 Codex 检查这个项目的错误，修改后运行验证。` |
| 直接发起任务 | `/codex 检查当前项目并说明结果` |
| 查询最新任务 | `/codex status` |
| 查询指定任务 | `/codex status 任务编号` |
| 取消任务 | `/codex cancel` 或 `/codex cancel 任务编号` |
| 继续原会话 | 告诉 AstrBot：`接着刚才的 Codex 任务继续修改` |

自然语言调用需要 AstrBot 聊天模型支持并启用工具调用；直接命令可独立使用。命令前缀以 AstrBot 的实际配置为准。

提供三个模型工具：`codex_cli_start`、`codex_cli_status`、`codex_cli_cancel`。继续任务时，模型在 `codex_cli_start` 中传入 `resume_job_id`。结果较长时，模型可以用 `offset` 分页读取。

任务默认先等待 10 秒，尚未结束就返回任务编号并转后台。默认同时执行一个任务，最长一小时，可在配置中调整。`running` 表示仍在执行，完成结果以最终状态为准。

## 完成后自动回报

后台任务完成、失败、超时或取消后，插件直接把状态和结果摘要发回原聊天，无需用户再次发消息，也不依赖聊天模型再次生成回答。完整结果可用 `/codex status` 或模型的分页查询读取。

通知通常在任务结束后的下一次轮询（约 2 秒）发送；平台暂时断线时自动重试。发送状态持久化，插件重载后继续处理待发送通知。初次等待期间已完成并直接返回的结果不会额外推送。

原聊天地址只从已授权的 AstrBot 事件获取，模型无法另指定收件人。旧版本任务如果没有保存原聊天地址，仍可查询，但不会猜测收件人补发。平台发送成功和本地状态写入之间若恰好崩溃，通知可能重复一次。

## 配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 启用插件 |
| `codex_path` | `codex` | CLI 名称或绝对路径 |
| `default_workdir` | 空 | 空值使用 AstrBot 工作目录 |
| `model` | 空 | 空值继承 Codex 配置 |
| `sandbox` | `inherit` | 继承 Codex 权限，或指定 `read-only`、`workspace-write`、`danger-full-access` |
| `allowed_users` | `[]` | 额外授权的平台身份，格式为 `平台ID:用户ID` |
| `allow_group` | `false` | 是否允许授权用户在群聊调用 |
| `allowed_workdirs` | `[]` | 允许的启动目录根路径；空列表不限制 |
| `environment_file` | 空 | 独立 CLI 模式可读取的本地环境变量 JSON 文件 |
| `timeout_seconds` | `3600` | 任务超时秒数 |
| `max_concurrent_jobs` | `1` | 同时执行任务数，范围 1–8 |
| `initial_wait_seconds` | `10` | 首次等待时间，范围 0–20 秒 |
| `desktop_link` | `false` | 启用 Codex 桌面联动 |
| `app_server_socket` | 空 | 桌面共享服务 socket；空值自动查找当前用户唯一的默认 socket |
| `task_title_prefix` | `AstrBot` | 桌面任务标题前缀，可设置为自己的机器人名字 |

默认仅限私聊中的 AstrBot 管理员使用；额外授权应填写实际平台 ID 和用户 ID，开启群聊仍需身份授权。任务查询和继续会话按平台、用户、聊天会话隔离。

插件会执行本机任务。`sandbox=inherit` 继承 Codex 的执行权限，固定以 `approval_policy=never` 运行，避免后台等待交互批准。`allowed_workdirs` 只校验启动目录，实际文件访问权限由 Codex sandbox 决定。

### 服务环境中的提供商凭据

systemd 或 Docker 服务可能没有终端中的环境变量。独立 CLI 模式可通过 `environment_file` 读取本地 JSON，例如：

```json
{"YOUR_PROVIDER_ENV_KEY": "YOUR_API_KEY"}
```

变量名必须与本机 Codex 提供商配置的 `env_key` 一致。将文件权限设置为 `600`，把绝对路径填写到插件配置；文件保存在插件源码目录之外。使用 Codex 已有登录信息时通常无需此文件。

## 可选：Codex 桌面实时联动

在同一台主机、同一系统用户下运行 Codex 桌面共享 app-server，再开启 `desktop_link`。插件接入现有服务，不另启动独立 app-server。当前联动测试版本为 Codex CLI **0.163.0**。

任务会以 `AstrBot · 任务内容` 显示，可通过 `task_title_prefix` 修改前缀。应用里可看到流式进度、命令和文件变动；`/codex status` 返回对应的 `session_id` 和 `app_thread_name`。继续任务使用同一个 Codex 会话。

`app_server_socket` 留空时自动发现当前用户唯一的默认 socket。存在多个服务时，需要明确填写目标 socket。找不到服务会直接报错。桌面模式使用共享服务已有的登录、提供商环境和配置；`environment_file` 无法向已经运行的共享服务注入环境变量。`model`、`sandbox` 和工作目录仍按插件配置应用。

取消和超时通过 `turn/interrupt` 停止所属任务，不终止共享服务。连接丢失后不会自动重发任务，避免重复执行。AstrBot 异常退出后，桌面任务可能仍在运行：请先在桌面确认状态；插件继续原会话前也会检查是否仍在执行。

## 数据与故障排查

任务数据位于 AstrBot 的 `data/plugin_data/astrbot_plugin_codex_cli/jobs`。插件保存任务状态、最多 2 MB 的事件日志和最多 50,000 字符的结果，并遮盖已知凭据；Codex 原生完整会话由 Codex 自己管理。可在没有运行任务时删除相应任务目录清理历史。

| 现象 | 检查项 |
| --- | --- |
| 找不到 Codex CLI | 在 AstrBot 运行环境检查 `codex --version`，或设置绝对 `codex_path` |
| 登录或提供商报错 | 确认服务运行用户的 Codex 配置和凭据；独立模式可设置 `environment_file` |
| 没有工具或提示无权限 | 检查管理员身份、`allowed_users`、是否私聊，以及聊天模型的工具调用配置 |
| 桌面看不到任务 | 确认开启 `desktop_link`，并连接同主机、同用户的共享服务 |
| 后台结果没有送达 | 用 `/codex status` 查询，检查 AstrBot 平台连接及插件发送重试日志 |

## 开发与测试

```bash
python -m pip install -r requirements-dev.txt "astrbot>=4.23.1,<5"
python -m pytest -q
ruff check .
ruff format --check .
python scripts/build_release.py
```

测试使用独立子进程和本地模拟 app-server，不调用真实模型、不发送真实平台消息。GitHub Actions 分别使用 AstrBot 4.23.1 和 4.29.4 验证。构建脚本只打包运行文件和文档，输出到 `dist/`。

## 许可证

[MIT](LICENSE)。欢迎通过 [Issues](https://github.com/EisonMe/astrbot_plugin_codex_cli/issues) 反馈问题或提交改进。
