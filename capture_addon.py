"""
mitmproxy 抓包插件（网页版）

职责：
  1. 控制台实时流量行（时间/方法/状态/耗时/地址），用于确认服务在正常工作
  2. 按 SharedConfig 过滤（URL / Header / 内容关键字，AND 组合，可运行中实时修改）
  3. 命中后生成结构化记录交给 sink 回调（服务端再推给网页和落盘）
  4. 上行请求头修改 [HeaderModify]
"""
import json
import logging
import re
from datetime import datetime
from urllib.parse import parse_qsl

from mitmproxy import http

# 推送给界面的正文最大长度，防止超大响应拖垮 WebSocket
MAX_BODY_TEXT = 20000

BINARY_MARKERS = ("image/", "video/", "audio/", "font/", "octet-stream")

# 表单体形态: 键=值&键=值（至少两组，键为常规字符，不含空格/换行）
_FORM_RE = re.compile(r"^[\w\-.%\[\]]+=[^&]*(&[\w\-.%\[\]]+=[^&]*)+$")

log = logging.getLogger("capture")
traffic = logging.getLogger("traffic")


def _console_line(flow: http.HTTPFlow) -> str:
    """一行流量摘要: [时间] 方法 状态 耗时ms 主机+路径"""
    req = flow.request
    t = datetime.now().strftime("%H:%M:%S")
    host_path = f"{req.pretty_host}{req.path}"
    if len(host_path) > 110:
        host_path = host_path[:107] + "..."
    if flow.response is not None:
        try:
            ms = int((flow.response.timestamp_end - req.timestamp_start) * 1000)
        except Exception:
            ms = 0
        return f"[{t}] {req.method:<7s} {flow.response.status_code:<4d} {ms:>5d}ms  {host_path}"
    reason = str(flow.error) if flow.error else "error"
    return f"[{t}] {req.method:<7s} ERR    ----  {host_path}  ({reason[:40]})"


def decode_message(msg) -> tuple[str, object]:
    """返回 (纯文本, 解析后的结构化数据或None)。二进制内容返回占位文本。

    智能识别两类常见格式（不依赖 Content-Type，很多客户端头是错的）：
      - JSON: 以 { 或 [ 开头
      - 表单体: 键=值&键=值（token/签名类接口常用这种）
    解析成功则前端获得结构化数据用于排版。
    """
    if msg is None or not msg.content:
        return "", None
    ctype = msg.headers.get("content-type", "")
    raw = msg.content
    if any(t in ctype for t in BINARY_MARKERS):
        return f"<binary data: {len(raw)} bytes>", None
    text = raw.decode("utf-8", errors="ignore")

    parsed = None
    stripped = text.lstrip()
    if stripped[:1] in ("{", "[") and len(raw) <= 200_000:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
    if parsed is None and "urlencoded" in ctype and len(raw) <= 200_000:
        parsed = _parse_form(text)
    if parsed is None and len(raw) <= 200_000 and _FORM_RE.match(stripped):
        parsed = _parse_form(stripped)

    if len(text) > MAX_BODY_TEXT:
        text = text[:MAX_BODY_TEXT] + f"...(已截断，总长度 {len(text)})"
    return text, parsed


def _parse_form(text: str):
    """解析表单体为字典，失败返回 None"""
    try:
        pairs = parse_qsl(text.strip(), keep_blank_values=True)
        return dict(pairs) if pairs else None
    except Exception:
        return None


class CaptureAddon:
    def __init__(self, cfg, sink):
        self.cfg = cfg    # SharedConfig，属性可被网页接口实时修改
        self.sink = sink  # callable(record: dict)

    def _gate(self, flow: http.HTTPFlow):
        """URL / Header 门控。通过返回 hits 列表，未通过返回 None。"""
        hits = []
        if self.cfg.url_kw:
            if self.cfg.url_kw in flow.request.pretty_url:
                hits.append("URL")
            else:
                return None
        if self.cfg.header_kw:
            if self.cfg.header_kw in flow.request.headers:
                hits.append(self.cfg.header_kw)
            else:
                return None
        return hits

    def request(self, flow: http.HTTPFlow) -> None:
        # 上行请求头修改：在所有过滤之前应用
        for key, value in (self.cfg.header_modify or {}).items():
            flow.request.headers[key] = value

    def response(self, flow: http.HTTPFlow) -> None:
        # 控制台流量行：不受过滤条件影响，所有请求都显示，方便确认服务在跑
        traffic.info(_console_line(flow))

        try:
            hits = self._gate(flow)
            if hits is None:
                return

            req_text, req_json = decode_message(flow.request)
            res_text, res_json = decode_message(flow.response)

            if self.cfg.body_kw:
                if self.cfg.body_kw in req_text or self.cfg.body_kw in res_text:
                    hits.append(f"内容「{self.cfg.body_kw}」")
                else:
                    return

            req, res = flow.request, flow.response
            self.sink({
                "method": req.method,
                "url": req.pretty_url,
                "host": req.pretty_host,
                "path": req.path,
                "status_code": res.status_code,
                "reason": res.reason,
                "req_headers": dict(req.headers),
                "res_headers": dict(res.headers),
                "query": dict(req.query) if req.query else {},
                "req_body": req_json if req_json is not None else req_text,
                "res_body": res_json if res_json is not None else res_text,
                "hits": hits,
            })
        except Exception:
            log.exception("处理响应时出错: %s %s", flow.request.method, flow.request.pretty_url)

    def error(self, flow: http.HTTPFlow) -> None:
        # 连接失败/超时等场景，让用户在界面上看到失败原因而不是'抓不到'
        traffic.info(_console_line(flow))

        try:
            hits = self._gate(flow)
            if hits is None:
                return
            req = flow.request
            self.sink({
                "method": req.method,
                "url": req.pretty_url,
                "host": req.pretty_host,
                "path": req.path,
                "status_code": 0,
                "reason": str(flow.error) if flow.error else "connection error",
                "req_headers": dict(req.headers),
                "res_headers": {},
                "query": dict(req.query) if req.query else {},
                "req_body": "",
                "res_body": "",
                "hits": hits,
            })
        except Exception:
            log.exception("处理连接错误时出错: %s %s", flow.request.method, flow.request.pretty_url)


class MitmLogBridge:
    """把 mitmproxy 内部日志(TLS握手失败、上游连接错误等)桥接到我们的日志系统，
    远程排障时这些是关键线索。"""

    LEVELS = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
        "alert": logging.ERROR,
    }

    def log(self, entry):
        mitm_log = logging.getLogger("mitmproxy")
        mitm_log.log(self.LEVELS.get(getattr(entry, "level", "info"), logging.INFO),
                     getattr(entry, "msg", str(entry)))
