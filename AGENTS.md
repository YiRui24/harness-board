# 给 Codex / 编程助手的维护说明

这是一个跑在用户自己电脑上的本地总控板：Python HTTP 服务 + 单文件前端。不要把真实密钥、口令、Tailscale 账号、电脑名、个人路径、真实对话 ID 或消息内容写进仓库。

## 结构

- `board/server.py`：本地 HTTP 服务（默认 8765）。读 `config.json`（兼容旧的 `peers.json`），用 `board-token.txt` 作同伴互调口令。第一次运行若没有配置，会从 `config.example.json` 生成一份。
- `board/index.html`：总览、对话流、特殊消息卡片。不要引入外部 CDN。
- `board/normalize.py`：把各源原始消息收成统一 `kind`。只映射已核对过的协议字段；对不上的走通用卡片并标明 `verified=False`。
- `board/codex_adapter.py`、`workbuddy_adapter.py`、`qwen_adapter.py`：只读本地数据库 / 官方接口。Codex 的 sqlite 一律 `mode=ro`。不要读 `auth.json` 或把 WorkBuddy 的本地访问密码写进文件或日志。
- `scripts/install-*.sh|ps1`：一键安装；`autostart-*` 写 LaunchAgent 或计划任务（隐藏启动）；`setup-*` 只装环境，不代登。
- `dev/start-mock.sh`：本机假数据，设备名用「电脑 A/B/C」，不要用真实电脑名。

## 改代码时请遵守

1. 新字段先对官方协议或已有适配器，再画卡片。猜的要在卡片上说明，并在说明文档里分开写「已核对」和「兜底」。
2. 不要在适配器里自动拒绝 Codex 的审批请求（会抢走桌面端的处理权）。没有未决 RPC 时，界面应提示去原窗口处理。
3. 同伴请求走 `/api/*` 并带口令；不要把口令放进 URL 日志。EventSource 带不上自定义头，这是已知限制。
4. 提交前用 `bash dev/start-mock.sh` 看总览和对话流；不要对用户的真机下发远程命令，除非用户明确要求并且人不用那台电脑。
5. `config.json`、`board-token.txt`、`board/data/`、日志、交接盘内容已在 `.gitignore`。不要用 `--force` 把它们加进去。
6. 用户称呼来自 `config.json` 的 `ownerName`，不要把某个人的名字写死在界面里。

## 协议备忘

- Codex app-server v2：`ThreadItem`、`item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`item/tool/requestUserInput`、`thread/queue/*`、`queue_1.sqlite`。
- WorkBuddy / CodeBuddy：jsonl 的 `message` / `function_call` / `function_call_result`；官方工具 `AskUserQuestion`。权限请求主要走钩子，不一定入库。
- 千问办公：`agents.db` 的 messages.parts；发布结果识别官方域名 `*.qwenwork.host` 和工具名 `qwenwork_pages_publish`。parts 的其它键若没有样本，不要编造。
