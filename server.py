"""
抓包工具 - 网页版服务

架构：
  FastAPI 本地服务(默认 127.0.0.1:16824) 提供 Web 界面 + REST API + WebSocket 实时推送
  mitmproxy 引擎(默认 127.0.0.1:16823) 跑在后台线程
  启动方式: python server.py  或双击 启动网页版.bat

端口策略：默认端口若被占用会自动向后顺延寻找空闲端口。
"""
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import queue as pyqueue
import warnings
import webbrowser
from logging.handlers import RotatingFileHandler

__version__ = "2.1.1"

# mitmproxy 10.2.4 在 cryptography 42 下打证书时会输出无害的弃用警告，别吓用户
try:
    from cryptography.utils import CryptographyDeprecationWarning
    warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
except Exception:
    pass
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

BASE_DIR = Path(__file__).parent.absolute()
if getattr(sys, "frozen", False):
    # PyInstaller 打包后：资源(web/capture_addon)在解压目录，运行数据(config/logs)在 exe 旁
    DATA_DIR = Path(sys._MEIPASS)
    BASE_DIR = Path(sys.executable).parent
else:
    DATA_DIR = BASE_DIR
sys.path.insert(0, str(DATA_DIR))
sys.path.insert(0, str(BASE_DIR))

from capture_addon import CaptureAddon, MitmLogBridge  # noqa: E402

WEB_DIR = DATA_DIR / "web"
LOG_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.ini"

log = logging.getLogger("server")


def setup_logging():
    """日志系统：
    - 控制台: INFO 级，简洁格式（流量行/生命周期事件/横幅）
    - 文件:   logs/service.log，DEBUG 级带完整时间戳，滚动保留 2MB x 3
    排障时把这个文件发回来即可定位大部分问题。
    """
    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fh = RotatingFileHandler(LOG_DIR / "service.log",
                             maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(ch)
    root.addHandler(fh)

    # mitmproxy 内部的 connect/disconnect 流水账在控制台太吵:
    # 文件里保留全量(DEBUG), 控制台只显示它的警告及以上
    logging.getLogger("mitmproxy").setLevel(logging.WARNING)

    # 未捕获异常全部落盘（主线程 + 各工作线程）
    sys.excepthook = lambda t, v, b: log.critical("未捕获异常", exc_info=(t, v, b))
    threading.excepthook = lambda a: log.critical(
        "线程未捕获异常: %s", a.exc_type, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
LOG_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.ini"

# 代理与界面的默认端口：避开 8080/8000/3000 等重灾区
DEFAULT_PROXY_PORT = 16823
DEFAULT_WEB_PORT = 16824
PORT_SCAN_TRIES = 20  # 被占用时向后顺延尝试的数量

INI_TEMPLATE = """[Settings]
# 目标URL配置：只抓取包含此关键字的请求，留空则抓取所有请求
TARGET_URL={url_kw}

# Header Key模式：填入Header名称（如 Cookie），只抓取包含该Header的请求
HEADER_KEY={header_kw}

# 内容关键字：在请求体/响应体里搜索的关键字，留空不启用
BODY_KEY={body_kw}

# 代理端口（被占用时自动顺延）
PROXY_PORT={proxy_port}

# 网页界面端口（被占用时自动顺延）
WEB_PORT={web_port}

# 开始抓包时自动设置系统代理：1=自动 0=手动（不改动系统设置）
ENABLE_SYSTEM_PROXY={esp}

# 启动时自动请求管理员权限(弹一次UAC)：1=开启 0=关闭
# 管理员模式下证书静默安装, 全程无弹窗
RUN_AS_ADMIN={run_as_admin}

# 关闭所有网页标签页后 10 秒自动退出服务并恢复网络：1=启用 0=后台常驻
AUTO_SHUTDOWN_WHEN_NO_BROWSER={auto_shutdown}

[HeaderModify]
# 上行请求头修改，每行一个 HeaderName=HeaderValue，留空则不修改
{header_modify}
"""


# ---------------- 共享配置 ----------------
class SharedConfig:
    def __init__(self):
        self.url_kw = ""
        self.header_kw = ""
        self.body_kw = ""
        self.proxy_port = DEFAULT_PROXY_PORT
        self.web_port = DEFAULT_WEB_PORT
        self.enable_system_proxy = True
        self.auto_shutdown = True
        self.run_as_admin = True
        self.header_modify = {}

    def load(self):
        import configparser
        cp = configparser.ConfigParser(comment_prefixes=("#",))
        if not CONFIG_FILE.exists():
            self.save()
            return
        cp.read(CONFIG_FILE, encoding="utf-8")
        s = cp["Settings"] if "Settings" in cp else cp["DEFAULT"]
        self.url_kw = s.get("TARGET_URL", "").strip()
        self.header_kw = s.get("HEADER_KEY", "").strip()
        self.body_kw = s.get("BODY_KEY", "").strip()
        try:
            self.proxy_port = int(s.get("PROXY_PORT", DEFAULT_PROXY_PORT))
        except ValueError:
            self.proxy_port = DEFAULT_PROXY_PORT
        try:
            self.web_port = int(s.get("WEB_PORT", DEFAULT_WEB_PORT))
        except ValueError:
            self.web_port = DEFAULT_WEB_PORT
        self.enable_system_proxy = s.get("ENABLE_SYSTEM_PROXY", "1").strip() != "0"
        self.auto_shutdown = s.get("AUTO_SHUTDOWN_WHEN_NO_BROWSER", "1").strip() != "0"
        self.run_as_admin = s.get("RUN_AS_ADMIN", "1").strip() != "0"
        if "HeaderModify" in cp:
            self.header_modify = dict(cp["HeaderModify"])

    def save(self):
        hm_lines = []
        for k, v in self.header_modify.items():
            hm_lines.append(f"{k}={v}")
        CONFIG_FILE.write_text(
            INI_TEMPLATE.format(
                url_kw=self.url_kw,
                header_kw=self.header_kw,
                body_kw=self.body_kw,
                proxy_port=self.proxy_port,
                web_port=self.web_port,
                esp=1 if self.enable_system_proxy else 0,
                run_as_admin=1 if self.run_as_admin else 0,
                auto_shutdown=1 if self.auto_shutdown else 0,
                header_modify="\n".join(hm_lines) if hm_lines else "",
            ),
            encoding="utf-8",
        )


CFG = SharedConfig()
CFG.load()


# ---------------- 捕获存储 ----------------
class FlowStore:
    def __init__(self):
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"capture_web_{ts}.jsonl"
        self._lock = threading.Lock()
        self._records = deque(maxlen=2000)
        self._next_id = 1

    def add(self, record: dict) -> dict:
        with self._lock:
            rec = dict(record)
            rec["id"] = self._next_id
            rec["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._next_id += 1
            self._records.append(rec)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return rec

    def after(self, last_id: int) -> list:
        with self._lock:
            return [r for r in self._records if r["id"] > last_id]

    def all(self) -> list:
        with self._lock:
            return list(self._records)

    def clear(self):
        with self._lock:
            self._records.clear()


STORE = FlowStore()
EVENT_Q: pyqueue.Queue = pyqueue.Queue()


def sink(record: dict):
    """CaptureAddon 命中回调（运行在 mitmproxy 线程）"""
    rec = STORE.add(record)
    EVENT_Q.put(rec)


# ---------------- mitmproxy 引擎（后台线程） ----------------
class Engine:
    def __init__(self):
        self.master = None
        self.loop = None
        self.thread = None
        self.last_error = ""
        self.port = None
        self._started = threading.Event()
        self._lifecycle = threading.Lock()  # 串行化启动/停止，避免停止未完成就响应启动

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, port: int, addon: CaptureAddon) -> tuple[bool, str]:
        with self._lifecycle:
            if self.running:
                return True, ""
            self.last_error = ""
            self.port = port
            log.info("引擎启动中, 代理端口 %s...", port)
            self._started.clear()
            self.thread = threading.Thread(target=self._run, args=(port, addon), daemon=True)
            self.thread.start()
            if not self._started.wait(timeout=15):
                return False, self.last_error or "引擎启动超时"
            time.sleep(0.3)  # 留给监听 socket 一点时间
            if self.last_error:
                return False, self.last_error
            return True, ""

    def _run(self, port: int, addon: CaptureAddon):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        # 记录 mitmproxy 通过 loop.create_server 创建的监听 Server，
        # 停止时显式 close——否则监听 socket 在进程内长期存活，
        # 表现为"已停止但端口一直被占用"
        servers = []
        orig_create_server = loop.create_server

        async def create_server_recorder(*args, **kwargs):
            srv = await orig_create_server(*args, **kwargs)
            servers.append(srv)
            return srv

        loop.create_server = create_server_recorder

        async def main():
            from mitmproxy import options
            from mitmproxy.tools.dump import DumpMaster

            opts = options.Options(listen_host="127.0.0.1", listen_port=port)
            master = DumpMaster(opts, with_termlog=False, with_dumper=False)
            master.addons.add(addon, MitmLogBridge())
            self.master = master
            self._started.set()
            log.info("引擎已启动, 监听 127.0.0.1:%s", port)
            await master.run()

        try:
            loop.run_until_complete(main())
        except Exception as e:
            self.last_error = str(e) or repr(e)
            log.error("引擎异常退出: %s", self.last_error)
        finally:
            self._started.set()
            log.info("引擎已停止")
            for srv in servers:
                try:
                    srv.close()
                except Exception:
                    pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self.loop = None

    def stop(self):
        with self._lifecycle:
            if not self.running:
                self.master = None
                self.loop = None
                return
            try:
                fut = asyncio.run_coroutine_threadsafe(self.master.shutdown(), self.loop)
                fut.result(timeout=10)
            except Exception:
                pass
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=10)
            self.master = None
            self.loop = None


ENGINE = Engine()


# ---------------- Windows 系统代理 / 证书 ----------------
_prev_proxy = None      # (ProxyEnable, ProxyServer) 记录开启前的状态，用于还原
_we_enabled_proxy = [False]  # 当前系统代理是否由本工具开启（退出时据此决定是否恢复）

# 代理读写全部走 winreg + wininet 原生 API（毫秒级），
# 早期版本用 PowerShell 子进程，冷启动一次要十几秒且偶发卡顿
import ctypes
import winreg

_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_wininet = None


def _refresh_wininet():
    """通知系统代理设置已变更（等同旧的 InternetSetOption PS 调用）"""
    global _wininet
    try:
        if _wininet is None:
            _wininet = ctypes.windll.wininet
        _wininet.InternetSetOptionW(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        _wininet.InternetSetOptionW(None, 37, None, 0)  # INTERNET_OPTION_REFRESH
    except Exception:
        pass


def read_proxy_reg() -> tuple[int, str]:
    """读取 (ProxyEnable, ProxyServer)"""
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS)
        try:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            try:
                server, _ = winreg.QueryValueEx(k, "ProxyServer")
            except OSError:
                server = ""
            return int(enable or 0), server or ""
        finally:
            winreg.CloseKey(k)
    except OSError:
        return 0, ""


def _set_proxy_reg(enable: int, server: str = ""):
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, enable)
        if server:
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
    finally:
        winreg.CloseKey(k)
    _refresh_wininet()


def enable_proxy(port: int):
    global _prev_proxy
    _prev_proxy = read_proxy_reg()
    _set_proxy_reg(1, f"127.0.0.1:{port}")
    _we_enabled_proxy[0] = True
    log.info("系统代理已开启: 127.0.0.1:%s (之前: %s)", port, _prev_proxy)


def disable_proxy(force: bool = False):
    global _prev_proxy
    if not _we_enabled_proxy[0] and not force:
        return  # 不是我们开的代理(或已恢复过), 不动它
    _we_enabled_proxy[0] = False
    prev = _prev_proxy
    _prev_proxy = None
    if prev and prev[0]:
        _set_proxy_reg(1, prev[1])  # 还原用户原本的代理设置
        log.info("系统代理已还原为用户原设置: %s", prev[1])
    else:
        _set_proxy_reg(0)
        log.info("系统代理已关闭")


CERT_DIR = Path.home() / ".mitmproxy"
CERT_FILE = CERT_DIR / "mitmproxy-ca-cert.cer"
_cert_status = {"file": False, "installed": False}


def ensure_cert_file() -> bool:
    """确保 CA 证书文件存在。首次运行时引擎还没跑过, 这里用 mitmproxy API
    直接生成, 让"启动即安装"成为可能(不用先点一次抓包)。"""
    if CERT_FILE.exists():
        return True
    try:
        CERT_DIR.mkdir(exist_ok=True)
        from mitmproxy.certs import CertStore
        CertStore.from_store(str(CERT_DIR), "mitmproxy", 2048)
        ok = CERT_FILE.exists()
        log.info("CA 证书文件已生成: %s", CERT_FILE)
        return ok
    except Exception:
        log.exception("生成 CA 证书文件失败")
        return False


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def try_elevate() -> bool:
    """弹 UAC 以管理员身份重启自身。

    返回 True = 已发起提权重启(当前进程应立即退出);
    返回 False = 用户拒绝了 UAC 或提权失败, 可以普通权限继续。
    """
    try:
        import ctypes
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe, params = sys.executable, f'"{BASE_DIR / "server.py"}"'
        # ShellExecuteW 的 "runas" 动词会触发 UAC, 阻塞到用户选择为止
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, str(BASE_DIR), 1)  # SW_SHOWNORMAL
        return ret > 32
    except Exception:
        log.exception("提权重启失败")
        return False


def wait_port_free(port: int, timeout: float) -> bool:
    """等待端口变为可用(自提权重启时旧实例退出有短暂竞态), 超时返回 False"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.15)
    return False


def _bring_cert_dialog_front():
    """Windows 的证书确认框经常被压在浏览器后面看不见,
    这里在 8 秒内轮询找到「安全警告」窗口并强制置顶+抢前台。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    targets = ("安全警告", "security warning")
    found_hwnds = []

    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(128)
            user32.GetWindowTextW(hwnd, buf, 128)
            title = buf.value.strip().lower()
            if any(t in title for t in targets):
                found_hwnds.append(hwnd)
        return True

    deadline = time.time() + 8
    while time.time() < deadline and not found_hwnds:
        user32.EnumWindows(EnumProc(_cb), 0)
        time.sleep(0.3)

    for hwnd in found_hwnds:
        SW_RESTORE, SWP_FLAGS = 9, 0x0057  # SWP_NOMOVE|NOSIZE|SHOWWINDOW
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP_FLAGS)   # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, SWP_FLAGS)   # HWND_NOTOPMOST
        user32.SetForegroundWindow(hwnd)
    if found_hwnds:
        log.info("证书确认框已置顶 (%s 个窗口)", len(found_hwnds))

# 本工具使用的端口段：启动自愈只清理指向这个段的残留代理，
# 避免误伤用户手动配置的其他代理（如公司 VPN）
OUR_PORT_RANGE = (16800, 16999)


def run_powershell(script: str) -> str:
    """仅剩证书操作在用：X509 证书库访问走 PowerShell，其余热点路径已全部原生 API 化"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=30,
        )
        return r.stdout or ""
    except Exception:
        return ""


def refresh_cert_status():
    _cert_status["file"] = CERT_FILE.exists()
    if not CERT_FILE.exists():
        _cert_status["installed"] = False
        return
    out = run_powershell(f"""
    try {{
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2('{CERT_FILE}')
        $found = $false
        foreach ($p in @('Cert:\CurrentUser\Root', 'Cert:\LocalMachine\Root')) {{
            if (Test-Path $p) {{
                if (Get-ChildItem $p -ErrorAction SilentlyContinue | Where-Object {{ $_.Thumbprint -eq $cert.Thumbprint }}) {{
                    $found = $true; break
                }}
            }}
        }}
        if ($found) {{ Write-Host 'INSTALLED' }} else {{ Write-Host 'MISSING' }}
    }} catch {{ Write-Host 'ERROR' }}
    """)
    _cert_status["installed"] = "INSTALLED" in out


def install_certificate() -> bool:
    """安装 mitmproxy CA 证书。

    - 管理员身份: certutil 静默装到机器级根存储, 全程无弹窗
    - 普通身份: 装到当前用户根存储, Windows 会弹一次确认框(自动置顶)
    """
    if not ensure_cert_file():
        refresh_cert_status()
        return _cert_status["installed"]

    if is_admin():
        try:
            r = subprocess.run(["certutil", "-addstore", "-f", "Root", str(CERT_FILE)],
                               capture_output=True, text=True, timeout=30)
            ok = (r.returncode == 0)
            log.info("证书静默安装到机器级存储(管理员): %s", "成功" if ok else f"失败 rc={r.returncode}")
        except Exception:
            log.exception("机器级证书安装异常")
            ok = False
        if ok:
            refresh_cert_status()
            return True
        log.info("机器级安装失败, 回退到用户级安装")

    log.info("以当前用户安装证书 (Windows 会弹一次确认框, 已自动置顶)")
    threading.Thread(target=_bring_cert_dialog_front, daemon=True).start()
    out = run_powershell(f"""
    try {{
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2('{CERT_FILE}')
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store('Root', 'CurrentUser')
        $store.Open('ReadWrite')
        $store.Add($cert)
        $store.Close()
        Write-Host 'OK'
    }} catch {{ Write-Host 'ERROR' }}
    """)
    refresh_cert_status()
    log.info("证书安装: %s", "成功" if _cert_status["installed"] else "失败")
    return _cert_status["installed"]


# ---------------- 端口工具 ----------------
def pick_free_port(start: int) -> int:
    for p in range(start, start + PORT_SCAN_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start  # 兜底：交由引擎自行报错


# ---------------- 启动自愈 / 权限自检 ----------------
_stale_repaired = [False]


def heal_stale_proxy() -> bool:
    """启动时自愈：若系统代理残留在本工具端口段(上次异常退出所致)，自动关闭恢复网络。

    正常退出路径（点停止/Ctrl+C/关窗口走 finally）不会留下残留；
    这里兜底覆盖任务管理器强杀、断电等硬杀场景。
    """
    global _prev_proxy
    enable, server = read_proxy_reg()
    if not enable or not server.startswith("127.0.0.1:"):
        return False
    try:
        port = int(server.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return False
    if not (OUR_PORT_RANGE[0] <= port <= OUR_PORT_RANGE[1]):
        return False
    _prev_proxy = None  # 残留状态不值得还原，直接关闭
    disable_proxy()
    _stale_repaired[0] = True
    log.warning("检测到残留系统代理(127.0.0.1:%s), 已自动清理恢复网络", port)
    return True


_perm_ok = [True]


def check_permission() -> bool:
    """权限自检：实测能否写代理注册表键（正常用户权限即可，无需管理员）。

    失败通常意味着组策略限制，此时界面会提供"以管理员重启"。
    """
    try:
        cur = read_proxy_reg()
        _set_proxy_reg(cur[0], cur[1])  # 写回原值，仅测试写权限
        _perm_ok[0] = True
    except OSError:
        _perm_ok[0] = False
    return _perm_ok[0]


def restart_as_admin():
    """以管理员身份重新启动本服务（触发 UAC），当前实例随后退出。"""
    script = ("Start-Process -Verb RunAs -FilePath 'cmd.exe' "
              f"-ArgumentList '/c cd /d \"{BASE_DIR}\" && python server.py'")
    run_powershell(script)

    def _exit():
        time.sleep(1.5)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()


# ---------------- FastAPI ----------------
WS_CLIENTS: set = set()
BROWSER_TIMEOUT_SECONDS = 10     # 所有网页断开后等待重连的宽限期
_browser_seen = [False]          # 是否有网页连上过（从未连上则不触发自动退出）
_zero_since = [None]             # 所有网页断开的时刻
_shutting_down = threading.Event()
_cleaned = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    consumer_task = asyncio.create_task(consumer())
    watchdog_task = asyncio.create_task(auto_shutdown_watchdog())
    yield
    consumer_task.cancel()
    watchdog_task.cancel()
    cleanup()


async def consumer():
    """把引擎线程推来的记录广播给所有网页（轮询队列，抓包量级下足够）"""
    while True:
        try:
            rec = EVENT_Q.get_nowait()
        except pyqueue.Empty:
            await asyncio.sleep(0.1)
            continue
        for ws in list(WS_CLIENTS):
            try:
                await ws.send_json(rec)
            except Exception:
                WS_CLIENTS.discard(ws)


async def auto_shutdown_watchdog():
    """心跳看门狗：所有网页关闭超过宽限期 -> 停引擎、恢复网络、退出进程。

    刷新页面只会断线一两秒并立即重连，不会误杀；配置
    AUTO_SHUTDOWN_WHEN_NO_BROWSER=0 可关闭（后台挂机抓包场景）。
    """
    while True:
        await asyncio.sleep(1)
        if _shutting_down.is_set():
            return
        if not CFG.auto_shutdown or not _browser_seen[0]:
            continue
        if WS_CLIENTS:
            _zero_since[0] = None
            continue
        if _zero_since[0] is None:
            _zero_since[0] = time.time()
            continue
        if time.time() - _zero_since[0] >= BROWSER_TIMEOUT_SECONDS:
            print(f"\n[自动退出] 所有网页已关闭 {BROWSER_TIMEOUT_SECONDS} 秒，停止服务并恢复网络…")
            _shutting_down.set()
            cleanup()
            os._exit(0)


def cleanup():
    """完整退出清理：停引擎 + 恢复系统代理。幂等，可安全多次调用。"""
    if _cleaned.is_set():
        return
    _cleaned.set()
    try:
        ENGINE.stop()
    finally:
        disable_proxy()


def _graceful_exit(delay: float = 0.8):
    time.sleep(delay)  # 给 HTTP 响应留出送达时间
    cleanup()
    os._exit(0)


app = FastAPI(lifespan=lifespan)


def state() -> dict:
    return {
        "running": ENGINE.running,
        "proxy_port": ENGINE.port if ENGINE.running else CFG.proxy_port,
        "web_port": ACTUAL_WEB_PORT[0],
        "version": __version__,
        "cert": dict(_cert_status),
        "perm_ok": _perm_ok[0],
        "stale_repaired": _stale_repaired[0],
        "enable_system_proxy": CFG.enable_system_proxy,
        "auto_shutdown": CFG.auto_shutdown,
        "header_modify_text": chr(10).join(f"{k}={v}" for k, v in CFG.header_modify.items()),
        "config": {
            "url_kw": CFG.url_kw,
            "header_kw": CFG.header_kw,
            "body_kw": CFG.body_kw,
            "proxy_port": CFG.proxy_port,
        },
        "total": len(STORE.all()),
        "last_error": ENGINE.last_error,
    }


ACTUAL_WEB_PORT = [DEFAULT_WEB_PORT]


@app.get("/")
async def index():
    # no-cache: 程序升级后避免浏览器拿旧缓存的界面（HTML 内容小, 每次取新的没有开销）
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/qrcode.jpg")
async def qrcode_jpg():
    return Response((WEB_DIR / "qrcode.jpg").read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/state")
async def api_state():
    return state()


@app.get("/api/flows")
async def api_flows(after: int = 0):
    return STORE.after(after)


@app.post("/api/config")
def api_config(
    url_kw: str = Body("", embed=True),
    header_kw: str = Body("", embed=True),
    body_kw: str = Body("", embed=True),
    proxy_port: int = Body(DEFAULT_PROXY_PORT, embed=True),
    enable_system_proxy: bool = Body(True, embed=True),
    auto_shutdown: bool = Body(True, embed=True),
    header_modify: str = Body("", embed=True),
):
    CFG.url_kw = url_kw.strip()
    CFG.header_kw = header_kw.strip()
    CFG.body_kw = body_kw.strip()
    CFG.enable_system_proxy = enable_system_proxy
    CFG.auto_shutdown = auto_shutdown
    hm = {}
    for line in (header_modify or "").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            hm[k.strip()] = v.strip()
    CFG.header_modify = hm
    log.info("配置更新: url=%r header=%r body=%r port=%s", CFG.url_kw, CFG.header_kw, CFG.body_kw, CFG.proxy_port)
    if not ENGINE.running and 1024 < proxy_port < 65536:
        CFG.proxy_port = proxy_port
    CFG.save()
    return {"ok": True, "state": state()}


@app.post("/api/start")
def api_start():
    if ENGINE.running:
        return {"ok": True, "proxy_port": ENGINE.port, "state": state()}
    port = pick_free_port(CFG.proxy_port)
    addon = CaptureAddon(CFG, sink)
    ok, err = ENGINE.start(port, addon)
    if not ok:
        log.error("引擎启动失败: %s", err)
        return {"ok": False, "error": err or "启动失败", "state": state()}
    if CFG.enable_system_proxy:
        install_certificate()
        enable_proxy(port)
    log.info("开始抓包: 代理端口 %s (端口顺延=%s)", port, port != CFG.proxy_port)
    return {"ok": True, "proxy_port": port, "port_moved": port != CFG.proxy_port, "state": state()}


@app.post("/api/stop")
def api_stop():
    log.info("停止抓包")
    ENGINE.stop()
    disable_proxy()
    return {"ok": True, "state": state()}


@app.post("/api/clear")
def api_clear():
    STORE.clear()
    return {"ok": True}


@app.get("/api/export")
async def api_export():
    data = {
        "capture_info": {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url_keyword": CFG.url_kw,
            "header_keyword": CFG.header_kw,
            "body_keyword": CFG.body_kw,
            "total_requests": len(STORE.all()),
        },
        "requests": STORE.all(),
    }
    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=capture_export.json"},
    )


@app.post("/api/repair")
def api_repair():
    disable_proxy()
    return {"ok": True}


@app.post("/api/restart_admin")
async def api_restart_admin():
    restart_as_admin()
    return {"ok": True}


@app.post("/api/cert")
def api_cert():
    ok = install_certificate()
    return {"ok": ok, "cert": dict(_cert_status)}


@app.post("/api/shutdown")
def api_shutdown():
    """退出程序：停引擎、恢复网络、进程退出。"""
    log.info("收到退出指令")
    _shutting_down.set()
    threading.Thread(target=_graceful_exit, daemon=True).start()
    return {"ok": True}


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    WS_CLIENTS.add(ws)
    _browser_seen[0] = True
    _zero_since[0] = None
    log.debug("网页已连接 (当前 %s 个)", len(WS_CLIENTS))
    try:
        while True:
            await ws.receive_text()  # 保持连接，忽略客户端消息
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        WS_CLIENTS.discard(ws)
        if not WS_CLIENTS and _zero_since[0] is None:
            _zero_since[0] = time.time()


# ---------------- 入口 ----------------
def register_console_close_cleanup():
    """黑窗口被点 X 时 Windows 会在约5秒后强杀进程。

    Python 的 signal 模块拦截不到 CTRL_CLOSE_EVENT，这里用 Win32 原生
    SetConsoleCtrlHandler 抢在强杀前恢复代理（仅处理 CLOSE/LOGOFF/SHUTDOWN，
    Ctrl+C 走 uvicorn 的优雅退出，不在这里抢）。
    """
    if not sys.platform.startswith("win"):
        return False
    import ctypes

    HANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
    CLOSE, LOGOFF, SHUTDOWN = 2, 5, 6

    @HANDLER_ROUTINE
    def _handler(event_type):
        if event_type in (CLOSE, LOGOFF, SHUTDOWN):
            try:
                cleanup()
            except Exception:
                pass
        return False  # 返回 False 让 Windows 继续默认的终止流程

    _console_close_handler_ref[0] = _handler  # 防止被 GC 回收
    return bool(ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True))


_console_close_handler_ref = [None]


def _startup_checks():
    try:
        healed = heal_stale_proxy()
        if healed:
            print("  [自愈] 检测到上次异常退出残留的系统代理，已自动恢复网络")
        ensure_cert_file()
        refresh_cert_status()
        check_permission()
        # 启动即装证书: 用户双击打开时确认框就在眼前, 而不是点抓包后才冒出来
        if CFG.enable_system_proxy and not _cert_status["installed"]:
            log.info("检测到证书未安装, 自动开始安装...")
            install_certificate()
        if not _perm_ok[0]:
            print("  [权限] 无法修改系统代理设置，请在网页界面点击'以管理员重启'")
    except Exception:
        log.exception("启动检查异常")


def main():
    if not sys.platform.startswith("win"):
        print("提示: 本工具的代理/证书功能仅支持 Windows，界面服务仍可启动用于开发调试")
    else:
        register_console_close_cleanup()

    setup_logging()
    import platform

    # 自动提权: 非管理员且开启 RUN_AS_ADMIN 时, 弹 UAC 以管理员重启
    # (管理员模式下证书静默安装, 全程无弹窗; UAC 被拒绝则普通权限继续)
    if sys.platform.startswith("win") and CFG.run_as_admin and not is_admin():
        log.info("正在请求管理员权限 (用于证书静默安装), 请在 UAC 中点「是」...")
        if try_elevate():
            print("  已以管理员身份重启, 本窗口可以关闭")
            time.sleep(0.8)
            return
        print("  未获得管理员权限, 以普通权限继续 (证书将用确认框方式安装)")

    log.info("=" * 46)
    log.info("抓包助手 v%s 启动", __version__)
    log.info("系统: %s | Python: %s | 管理员: %s", platform.platform(), platform.python_version(), is_admin())
    log.info("工作目录: %s", BASE_DIR)
    log.info("配置: url=%r header=%r body=%r | 代理端口=%s 界面端口=%s | 自动系统代理=%s | 网页关闭自动退出=%s",
             CFG.url_kw, CFG.header_kw, CFG.body_kw, CFG.proxy_port, CFG.web_port,
             CFG.enable_system_proxy, CFG.auto_shutdown)

    # 证书/权限/自愈检查涉及 PowerShell 调用（冷启动可能数秒），放后台线程，
    # 让网页界面先秒开；前端每 5 秒轮询状态会自动拿到结果
    threading.Thread(target=_startup_checks, daemon=True).start()
    if wait_port_free(CFG.web_port, timeout=6):
        web_port = CFG.web_port
    else:
        web_port = pick_free_port(CFG.web_port)
    ACTUAL_WEB_PORT[0] = web_port

    url = f"http://127.0.0.1:{web_port}"
    print("=" * 56)
    print(f"  抓包工具 - 网页版 v{__version__}")
    print(f"  界面: {url}")
    print(f"  代理端口: {CFG.proxy_port} (点'开始抓包'后生效, 被占用自动顺延)")
    print("-" * 56)
    print("  [!] 注意: 抓包期间请不要关闭本窗口！")
    print("      本窗口就是抓包服务, 关掉 = 停止抓包。")
    print("      抓完后在网页上点 [退出程序] 即可, 网络会自动恢复。")
    print("-" * 56)
    print("  GitHub : https://github.com/suxin0203")
    print("  公众号  : 打开网页, 点右下角二维码扫码关注")
    print("=" * 56)
    if is_admin():
        print("  [管理员模式] 证书将静默安装, 无任何弹窗")
    if not _perm_ok[0]:
        print("  [权限] 无法修改系统代理设置，请在网页界面点击'以管理员重启'")

    if os.environ.get("NO_BROWSER") != "1":
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()

    config = uvicorn.Config(app, host="127.0.0.1", port=web_port,
                            log_level="warning", access_log=False)
    try:
        uvicorn.Server(config).run()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print("\n已退出，网络设置已恢复")


if __name__ == "__main__":
    main()
