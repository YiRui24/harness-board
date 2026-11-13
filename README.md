# Harness 总控板

在自己的几台电脑之间，用一个网页查看和收发 Codex、WorkBuddy、千问办公里的对话。本机打开不用登录；从另一台电脑经 Tailscale 打开时，要用这台电脑上的本地口令。

本仓库是给公开托管准备的：不含真实电脑名、IP、口令、对话内容或个人路径。

## 需要什么

- Python 3（标准库即可跑总控板；若要用 Codex 的自动发送，另装 `websocket-client`）
- 每台要连起来的电脑都装 Tailscale，并登录到同一个尾巴网络
- 建议用 Syncthing 同步「交接盘」文件夹（提示词和附件）
- 可选：SyncClipboard，把剪贴板同步到其它电脑（千问办公目前是半自动发送，会用到剪贴板）。服务端放在最常开着的那台（推荐 Mac），见 [docs/syncclipboard.md](docs/syncclipboard.md)

## 一步安装

### Mac

```bash
git clone <本仓库地址> harness && cd harness
bash scripts/install-mac.sh
# 若连环境一起装（Tailscale / Syncthing / SyncClipboard，登录仍要你自己做）：
# bash scripts/install-mac.sh --setup-env --id macbook --label "我的 Mac"
```

默认装到 `~/harness`。登录后会由 LaunchAgent 隐藏启动。本机打开 http://127.0.0.1:8765/

### Windows

在 PowerShell 里（当前用户即可）：

```powershell
git clone <本仓库地址> harness; cd harness
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
# 连环境一起装：
# powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -SetupEnv -Id my-pc -Label "我的电脑"
```

默认装到 `%USERPROFILE%\Harness`。登录后由计划任务通过 `hidden-run.vbs` 隐藏启动，不弹黑窗口。本机打开 http://127.0.0.1:8765/

## 第一台装好以后

1. 打开 `config.json`（第一次安装会从 `config.example.json` 拷出来，并填上本机名）。把其它电脑的 Tailscale IPv4（`tailscale ip -4` 得到的 `100.x.x.x`）写进 `peers`。
2. 在其它每台电脑上同样安装一次，peers 互相写上。
3. 本机口令在安装目录的 `board-token.txt`。从 Tailscale 打开时要输入这个口令。不要把口令提交到 git，也不要发给无关的人。
4. 用 Syncthing 把各台的交接盘文件夹共享起来。默认会尝试 `~/交接盘`（Mac）或用户目录下的「交接盘」（Windows），也可以在 `config.json` 的 `handoverRoot` 里指定。
5. 可选：在 `config.json` 里填 `ownerName`，总览问候会用这个称呼。

改完 `config.json` 后重启总控板：Mac 用 `launchctl kickstart -k gui/$(id -u)/com.harness.board`；Windows 在「任务计划程序」里启动 `\Harness\Board`，或重新跑一次 `scripts/autostart-windows.ps1`。

## 单独装环境

不跑一键安装、只补环境时：

- Mac：`scripts/setup-tailscale-mac.sh`、`setup-syncthing-mac.sh`、`setup-syncclipboard-mac.sh`
- Windows：`scripts/setup-tailscale-windows.ps1`、`setup-syncthing-windows.ps1`、`setup-syncclipboard-windows.ps1`

这些脚本只负责安装和打开目录，不会代你登录，也不会把账号、设备 ID 或密钥写进文件。

## 开发

```bash
bash dev/start-mock.sh
# 打开 http://127.0.0.1:18765/ ，三台假设备互为同伴
```

消息形态的约定见 `board/normalize.py`（`kind`: text / question / approval / publish / queue / tool / diff / plan / error）。维护说明见 [AGENTS.md](AGENTS.md)。

## 许可

MIT，见 [LICENSE](LICENSE)。
