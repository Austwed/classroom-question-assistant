"""Temporary, read-only LAN view of answers from the current app run."""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import socket
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def lan_ipv4_addresses() -> list[str]:
    """Return usable IPv4 addresses so the user can pick the phone's network."""
    found: list[str] = []
    # A UDP connect chooses the main network adapter without sending a packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("1.1.1.1", 80))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found.extend(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    addresses = []
    for value in found:
        ip = ipaddress.ip_address(value)
        if ip.is_loopback or ip.is_link_local or value in addresses:
            continue
        addresses.append(value)
    return addresses


MOBILE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>语音问答 · 手机查看</title>
<style>
:root{--content-size:20px;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;color:#192436;background:#f4f6fa}
*{box-sizing:border-box}body{margin:0}main{max-width:760px;margin:auto;padding:18px 14px 48px}
header{position:sticky;top:0;background:#f4f6faed;padding:8px 0 14px;z-index:1}
.topline{display:flex;align-items:center;justify-content:space-between;gap:12px}
h1{font-size:22px;margin:4px 0 8px}.status{font-size:13px;color:#526173}
header.collapsed{padding:0 0 5px}header.collapsed h1,header.collapsed #headerDetails{display:none}
.font-tools{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:10px;font-size:13px;color:#526173}
.font-tools output{min-width:42px;text-align:center;font-variant-numeric:tabular-nums}
.font-tools button{min-width:38px;font-size:16px}.font-tools button:disabled{opacity:.4}
.card{background:white;border-radius:16px;padding:18px;margin:14px 0;box-shadow:0 2px 12px #1b2d4b12}
.meta{font-size:13px;color:#607086;margin-bottom:12px}.question{font-size:max(14px,calc(var(--content-size) * .75));color:#526173;white-space:pre-wrap;overflow-wrap:anywhere}
.answer{font-size:var(--content-size);line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:12px}
.old{opacity:.58}.badge{color:#9b5300}.empty{color:#607086;padding:24px 4px}
button{border:1px solid #c9d3e1;border-radius:8px;background:white;padding:7px 11px;color:#26354a}
</style></head><body><main><header id="pageHeader">
<div class="topline"><h1>本次运行的回答</h1><button id="toggleHeader" type="button" aria-controls="headerDetails" aria-expanded="true">收起标题</button></div>
<div id="headerDetails"><div id="status" class="status">正在连接电脑…</div>
<div class="font-tools" aria-label="手机内容字体大小"><span>内容字号</span>
<button id="fontSmaller" type="button" aria-label="缩小字体">A−</button>
<output id="fontSize" aria-live="polite">20 px</output>
<button id="fontLarger" type="button" aria-label="放大字体">A＋</button>
<button id="fontReset" type="button">默认</button></div></div></header>
<div id="answers" aria-live="polite"></div></main>
<script>
const answers = new Map(); let latestId = 0;
const list = document.getElementById('answers'), status = document.getElementById('status');
const header = document.getElementById('pageHeader'), toggleHeader = document.getElementById('toggleHeader');
const smaller = document.getElementById('fontSmaller'), larger = document.getElementById('fontLarger');
const fontOutput = document.getElementById('fontSize');
function stored(key){try{return localStorage.getItem(key);}catch(_){return null;}}
function remember(key,value){try{localStorage.setItem(key,value);}catch(_){}}
let fontSize = 20;
function setFontSize(size){
  fontSize = Math.max(16,Math.min(36,size));
  document.documentElement.style.setProperty('--content-size',fontSize+'px');
  fontOutput.textContent=fontSize+' px';
  smaller.disabled=fontSize===16;larger.disabled=fontSize===36;
  remember('mobile-content-size',String(fontSize));
}
function setHeaderCollapsed(collapsed){
  header.classList.toggle('collapsed',collapsed);
  toggleHeader.textContent=collapsed?'展开标题与字号':'收起标题';
  toggleHeader.setAttribute('aria-expanded',String(!collapsed));
  remember('mobile-header-collapsed',String(collapsed));
}
setFontSize(Number(stored('mobile-content-size'))||20);
setHeaderCollapsed(stored('mobile-header-collapsed')==='true');
smaller.onclick=()=>setFontSize(fontSize-2);
larger.onclick=()=>setFontSize(fontSize+2);
document.getElementById('fontReset').onclick=()=>setFontSize(20);
toggleHeader.onclick=()=>setHeaderCollapsed(!header.classList.contains('collapsed'));
function draw(){
  list.replaceChildren();
  const rows = [...answers.values()].sort((a,b)=>b.id-a.id);
  if(!rows.length){const p=document.createElement('p');p.className='empty';p.textContent='本次运行还没有回答。';list.append(p);return;}
  for(const row of rows){
    const card=document.createElement('article');card.className='card'+(row.superseded?' old':'');
    const meta=document.createElement('div');meta.className='meta';
    meta.textContent=`第 ${row.id} 条 · 片段 #${row.record_id} · ${row.time}`;
    if(row.superseded){const badge=document.createElement('span');badge.className='badge';badge.textContent=' · 已被修正替代';meta.append(badge);}
    const question=document.createElement('div');question.className='question';question.textContent='发言：'+row.question;
    const answer=document.createElement('div');answer.className='answer';answer.textContent=row.answer;
    const button=document.createElement('button');button.type='button';button.textContent='复制回答';
    button.onclick=async()=>{
      try{
        if(navigator.clipboard)await navigator.clipboard.writeText(row.answer);
        else{const input=document.createElement('textarea');input.value=row.answer;document.body.append(input);
          input.select();if(!document.execCommand('copy'))throw Error('copy');input.remove();}
        button.textContent='已复制';
      }catch(_){button.textContent='请长按回答文字复制';}
    };
    card.append(meta,question,answer,button);list.append(card);
  }
}
function add(row){for(const item of answers.values())if(row.replaced_record_ids.includes(item.record_id))item.superseded=true;
  answers.set(row.id,row);latestId=Math.max(latestId,row.id);draw();}
fetch('/api/answers',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error('访问已失效');return r.json();}).then(data=>{
  for(const row of data.answers){answers.set(row.id,row);latestId=Math.max(latestId,row.id);}draw();
  const stream=new EventSource('/events?after='+latestId);
  stream.onopen=()=>status.textContent='已连接 · 电脑保持运行时自动更新';
  stream.onmessage=event=>add(JSON.parse(event.data));
  stream.onerror=()=>status.textContent='连接中断，正在重连；恢复后会补齐回答';
}).catch(e=>{status.textContent=e.message;draw();});
</script></body></html>"""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        # Pairing URLs contain a secret and must not be written to a console log.
        pass

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        cookie = cookies.get("mobile_share")
        return cookie is not None and hmac.compare_digest(cookie.value, self.server.share.token)

    def do_GET(self) -> None:
        share = self.server.share
        path = urlsplit(self.path)
        if path.path == "/pair":
            supplied = parse_qs(path.query).get("key", [""])[0]
            if not hmac.compare_digest(supplied, share.token):
                self._send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain; charset=utf-8")
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"mobile_share={share.token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return
        if not self._authenticated():
            self._send(HTTPStatus.FORBIDDEN, b"Open the QR code shown on the computer.", "text/plain; charset=utf-8")
            return
        if path.path == "/":
            self._send(HTTPStatus.OK, MOBILE_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path.path == "/api/answers":
            body = json.dumps({"answers": share.snapshot()}, ensure_ascii=False).encode("utf-8")
            self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        elif path.path == "/events":
            try:
                after = max(0, int(parse_qs(path.query).get("after", ["0"])[0]))
                after = max(after, int(self.headers.get("Last-Event-ID", "0")))
            except ValueError:
                self._send(HTTPStatus.BAD_REQUEST, b"Invalid event ID", "text/plain; charset=utf-8")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                while True:
                    active, rows = share.wait_after(after)
                    if not active:
                        break
                    if not rows:
                        self.wfile.write(b": keepalive\n\n")
                    for row in rows:
                        payload = json.dumps(row, ensure_ascii=False)
                        self.wfile.write(f"id: {row['id']}\ndata: {payload}\n\n".encode("utf-8"))
                        after = row["id"]
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")


class MobileShare:
    """Own the temporary server and a synchronized copy of answer events."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self._condition = threading.Condition()
        self._answers: list[dict] = []
        self._active = False
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def port(self) -> int | None:
        return self._server.server_port if self._server else None

    def start(self, existing: list[dict], bind_address: str = "127.0.0.1") -> int:
        if self._active:
            raise RuntimeError("手机共享已经开启")
        server = _Server((bind_address, 0), _Handler)
        server.share = self
        with self._condition:
            self.token = secrets.token_urlsafe(32)
            self._answers = [dict(row) for row in existing]
            self._active = True
            self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return server.server_port

    def stop(self) -> None:
        with self._condition:
            if not self._active:
                return
            self._active = False
            self._condition.notify_all()
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._server = None
        self._thread = None
        self.token = secrets.token_urlsafe(32)

    def pair_url(self, address: str) -> str:
        if not self.port:
            raise RuntimeError("手机共享尚未开启")
        return f"http://{address}:{self.port}/pair?key={self.token}"

    def publish(self, row: dict) -> None:
        with self._condition:
            if not self._active:
                return
            entry = dict(row)
            replaced = set(entry.get("replaced_record_ids", []))
            for old in self._answers:
                if old["record_id"] in replaced:
                    old["superseded"] = True
            self._answers.append(entry)
            self._condition.notify_all()

    def snapshot(self) -> list[dict]:
        with self._condition:
            return [dict(row) for row in self._answers]

    def wait_after(self, last_id: int, timeout: float = 15) -> tuple[bool, list[dict]]:
        with self._condition:
            self._condition.wait_for(lambda: not self._active or bool(self._answers and self._answers[-1]["id"] > last_id), timeout)
            return self._active, [dict(row) for row in self._answers if row["id"] > last_id]
