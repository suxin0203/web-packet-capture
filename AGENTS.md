# AGENTS.md — 项目记忆

> 本文件是项目知识沉淀，供 AI 助手和开发者快速恢复上下文。改动核心逻辑前先读「关键机制」和「编码陷阱」。

## 项目

**抓包助手**（web-packet-capture）v2.1.1：mitmproxy 引擎 + FastAPI + 单文件网页界面的 Windows 抓包工具。发布形态：PyInstaller 单文件 EXE（约 28MB）+ config.ini + 操作手册。

- 仓库：github.com/suxin0203/web-packet-capture（SSH 推送可用；gh CLI 已授权 suxin0203）
- 用户机器环境注意：v2rayN 常驻（会写系统代理 10808）；HTTPS 到 github.com 时通时断，SSH 22 稳定；管理员自提权已验证可用

## 常用命令

| 操作 | 命令 |
|---|---|
| 源码运行（无头调试） | `NO_BROWSER=1 python -u server.py`（stdout 加 -u 防缓冲丢日志） |
| 打包 EXE | `cmd //c 打包网页版.bat`（自动清 build/dist 并回填 config+手册） |
| 版本发布 | 改 `server.py` 的 `__version__` → 打包 → `gh release create vX.Y.Z <ASCII名zip> --notes-file <UTF-8 md>` |
| 网络问题排查 | 双击 `强制关闭代理-断网修复.bat`（分层诊断，见下） |

## 关键机制（改动前必读）

- **代理状态机**（server.py 的 enable/disable_proxy）：
  - enable：记录 `_prev_proxy`；若开启前已有**指向死端口**的代理残留（127.0.0.1:* 且连不上），先清理，不把死代理当"原设置"
  - disable：还原 `_prev_proxy` 前做**活性校验**，原代理已死则直接恢复直连，绝不还原死代理；`force=True` 无条件关闭（仅用于自愈/修复按钮）
  - 历史事故：防误还原门控误伤了自愈和修复按钮 + 死代理被忠实还原 = 用户断网循环。改这里必须跑全链路测试：写死代理残留 → 启动(自愈) → 开 → 停 → 修复，逐步断言注册表 ProxyEnable
- **自愈范围**：只清理 16800-16999 端口段的残留代理，不碰 v2rayN 等其他工具的设置
- **自提权**：`RUN_AS_ADMIN=1`（默认开）时非管理员启动 → ShellExecuteW runas 重启自身（UAC）；拒绝则普通模式继续。管理员模式证书静默装机器级存储
- **证书**：管理员 → certutil 静默装机器级；非管理员 → CurrentUser 弹确认框（`_bring_cert_dialog_front` 轮询置顶，解决被浏览器遮挡）。证书文件可 `ensure_cert_file()` 用 mitmproxy CertStore API 预生成（启动即装的前提）
- **心跳退出**：所有网页 WebSocket 断开 10 秒 → 自动退出恢复网络；`update_skip`/`last_update_check`/`guide_shown_v1` 存浏览器 localStorage

## 编码陷阱（全部踩过）

- **bat 必须 GBK + CRLF + 不加 chcp**：UTF-8+chcp 会让 cmd 按错位字节解析出乱命令（`'l' 不是内部或外部命令`）
- **命令行传中文参数给 gh/子进程会被 GBK 搞乱**：中文内容一律写 UTF-8 文件，用 `gh api --input x.json` / `--notes-file x.md` 传递
- **Release 附件名用 ASCII**：`PacketCapture-vX.Y.Z-win64.zip`；中文名上传会丢字变成 `-v2.1.0-win64.zip`
- **Python 不认 Git Bash 的 `/tmp` 路径**（映射关系不同），传给 python/gh 的路径写 Windows 形式
- **`reg query` 返回 DWORD 是 `0x1`**：字符串比较前先 `set /a` 数值化，否则 bat 分支永远不进
- **Git Bash 会把 `/IM` 等参数转成路径**：杀进程用 `powershell Stop-Process`，不要用 taskkill
- **heredoc 里写 JS 转义（如 `'\\n'`）会变成真实换行**：写入后必须做断行字符串扫描或浏览器实测

## 测试要点

- EXE 冒烟流程：干净目录运行 → `/api/state` → api/start → `curl -x 127.0.0.1:16823 http://127.0.0.1:9999/...`（本地 target.py）→ HTTP+HTTPS 都抓 → api/shutdown → 确认端口释放 + ProxyEnable=0
- 测试期把 `ENABLE_SYSTEM_PROXY` 设 0（避免改动真实系统代理），测完恢复 1
- 测试后必清：残留端口（16823/16824/9999）、config.ini 恢复默认、logs 测试文件、临时目录
- 停止/重启有竞态：web 端口先 `wait_port_free` 等旧实例退出；提权重启同理

## 排障入口

- 服务日志：`logs/service.log`（滚动 2MB×3，DEBUG 全量，含 mitmproxy 内部错误桥接）
- 网络断网：`强制关闭代理-断网修复.bat`（分层：残留代理 → 抓包证书 → DNS/WinHTTP → winsock）
- 用户反馈断网：先看 service.log 里「系统代理」开/关/还原的时间线 + 「(之前: ...)」的值
- 与 v2rayN 共存：两者都写系统代理，后写生效；建议抓包时退出 v2rayN 的系统代理模式

## 版本历史要点

- v2.1.0 首发于 GitHub Releases；v2.1.1 起附件命名 `PacketCapture-vX.Y.Z-win64.zip`
- 旧命令行版（main.py/抓包脚本.py）已移除，网页版是唯一版本
- EXE 体积优化已完成（去 collect-all + 排除 numpy/PIL，47.7→27.9MB），勿回退 collect-all
