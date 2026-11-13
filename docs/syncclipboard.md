# SyncClipboard：服务端放在哪台

**结论：服务端放在最常开着的那台电脑上（推荐 Mac），其它电脑都只当客户端连它。**
服务端那台关机或没登录时，其它电脑之间也同步不了，所以不要放在经常关机的电脑上。

## 推荐拓扑

| 角色 | 电脑 | 设置 |
|---|---|---|
| 服务端 + 客户端 | 常开的 Mac | 打开内置服务端，端口 5033，只绑定 Tailscale 地址；客户端连自己的 Tailscale 地址 |
| 客户端 | 其它 Mac / Windows | 服务器地址填 `http://<服务端的 Tailscale IP>:5033`，内置服务端保持关闭 |

用户名和密码自己定，只存在各台本机，不要写进本仓库。

## 服务端只让 Tailscale 访问

SyncClipboard 默认监听所有网卡。想只让 Tailscale 连，在服务端设置里打开「自定义配置文件」，
指向一个这样的文件（示例见 `examples/syncclipboard-server-kestrel.example.json`）：

```json
{ "Kestrel": { "Endpoints": { "Http": { "Url": "http://<服务端的 Tailscale IP>:5033" } } } }
```

因为只绑定 Tailscale 地址，开机自启时要**等 Tailscale 地址出现再启动 SyncClipboard**，
否则端口绑不上。Mac 上可以让 LaunchAgent 跑一个小脚本：循环检查 `ifconfig` 里出现该地址，再启动应用。

Mac 若开着系统代理（如 Shadowrocket），要让 SyncClipboard 绕过代理（启动前清掉 `HTTP(S)_PROXY`，
并把 `100.64.0.0/10` 加进代理的绕过名单），否则连 Tailscale 地址会 502。

## 验证

在每台客户端上：

- Mac：`curl -s -o /dev/null -w "%{http_code}\n" -u <用户名>:<密码> http://<服务端IP>:5033/SyncClipboard.json`，应为 `200`；不带账号应为 `401`。
- Windows：`Test-NetConnection <服务端IP> -Port 5033`，`TcpTestSucceeded` 应为 `True`。

## 迁移服务端（例如从 Windows 挪到 Mac）

1. 新服务端：打开内置服务端，沿用原来的用户名和密码，重启应用，确认端口在监听。
2. 旧服务端：关掉内置服务端，服务器地址改成新服务端地址；自启脚本里如有「等旧地址」的逻辑一起改。
3. 其它客户端：只改服务器地址，重启 SyncClipboard。
4. 改 Windows 上带中文的 `.ps1` 时保持 **UTF-8 带 BOM** 编码，否则 Windows PowerShell 5.1 会读错，脚本可能失效。
