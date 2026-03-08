'Transport layer for solvemcp.'

import contextlib, json, subprocess, threading, time
from typing import Any, Callable, Iterable, Iterator

import httpx


# ---------------------------------------------------------------------------
# Errors

class MCPError(Exception): "Base MCP client error."


class MCPRemoteError(MCPError):
    "JSON-RPC error returned by server."

    def __init__(self, code:int, message:str, data:Any=None, *, method:str|None=None, id:Any=None):
        super().__init__(f"{message} (code={code})")
        self.code, self.message, self.data, self.method, self.id = code, message, data, method, id


class MCPTimeout(MCPError): "Request timed out."


class MCPTransportError(MCPError): "Transport-level error."


# ---------------------------------------------------------------------------
# Helpers

def _now(): return time.time()


def _json_dumps(obj:Any)->str:
    "Compact JSON with no embedded newlines (required by stdio transport)."
    s = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    # Stdio transport is newline-delimited; JSON text must not contain *literal* newlines.
    if '\n' in s or '\r' in s: raise MCPTransportError('newline in JSON payload (stdio framing would break)')
    return s


def _as_list(payload:Any)->list[dict]:
    if payload is None: return []
    if isinstance(payload, list): return payload
    return [payload]


def _is_req(msg:dict)->bool: return isinstance(msg, dict) and 'method' in msg and 'id' in msg


def _is_notif(msg:dict)->bool: return isinstance(msg, dict) and 'method' in msg and 'id' not in msg


def _is_resp(msg:dict)->bool:
    return isinstance(msg, dict) and 'id' in msg and 'method' not in msg and ('result' in msg or 'error' in msg)


# ---------------------------------------------------------------------------
# SSE parsing

def sse_events(lines:Iterable[str])->Iterator[dict]:
    """Parse SSE events from an iterable of decoded lines.

    Yields dicts with keys like: event, data, id, retry (when present).
    Data lines are joined by '\n' per SSE spec.
    """
    ev, data_lines = {}, []

    def flush():
        nonlocal ev, data_lines
        if not ev and not data_lines: return None
        if data_lines: ev['data'] = '\n'.join(data_lines)
        out, ev, data_lines = ev, {}, []
        return out

    for raw in lines:
        if raw is None: continue
        line = raw.rstrip('\r')
        if line == '':
            out = flush()
            if out is not None: yield out
            continue
        if line.startswith(':'): continue
        if ':' in line:
            field, val = line.split(':', 1)
            if val.startswith(' '): val = val[1:]
        else: field, val = line, ''
        if field == 'data': data_lines.append(val)
        else: ev[field] = val

    out = flush()
    if out is not None: yield out


def sse_json_messages(lines:Iterable[str])->Iterator[Any]:
    "Yield parsed JSON payloads from SSE events with a `data` field. Preserves legacy `endpoint` events."
    for ev in sse_events(lines):
        if 'data' not in ev: continue
        if ev.get('event') == 'endpoint':
            yield ev
            continue
        try: yield json.loads(ev['data'])
        except json.JSONDecodeError: continue


# ---------------------------------------------------------------------------
# Transports

class MCPTransport:
    """Minimal transport interface.

    - `send(msg)` sends a JSON-RPC message (dict or list).
    - For synchronous-response transports, `send` may return an iterable of inbound messages.
    - For async-inbound transports, inbound messages arrive via `start(on_message)`.
    """

    def start(self, on_message:Callable[[dict], None]): ...

    def send(self, msg:dict|list, *, stream:bool=False): ...

    def close(self): ...


class StdioTransport(MCPTransport):
    "Subprocess stdio transport (newline-delimited JSON)."

    def __init__(self, cmd:list[str], *, cwd:str|None=None, env:dict|None=None, stderr:bool=True, text:bool=True,
                 debug:Callable[[str], None]|None=None):
        self.cmd, self.cwd, self.env = cmd, cwd, env
        self._proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE if stderr else subprocess.DEVNULL, text=text, bufsize=1)
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._debug = debug

    def _log(self, msg:str):
        if self._debug: self._debug(msg)

    @property
    def proc(self): return self._proc

    def start(self, on_message:Callable[[dict], None]):
        def _read_stdout():
            try:
                for line in iter(self._proc.stdout.readline, ''):
                    if self._stop.is_set(): break
                    line = line.strip()
                    if not line: continue
                    try: payload = json.loads(line)
                    except json.JSONDecodeError: continue
                    for msg in _as_list(payload):
                        if isinstance(msg, dict): on_message(msg)
            finally: self._stop.set()

        threading.Thread(target=_read_stdout, name='mcp-stdio-stdout', daemon=True).start()

        # Drain stderr to avoid deadlocks; user can also read proc.stderr if desired.
        if self._proc.stderr is not None:
            def _drain_stderr():
                for _ in iter(self._proc.stderr.readline, ''):
                    if self._stop.is_set(): break

            threading.Thread(target=_drain_stderr, name='mcp-stdio-stderr', daemon=True).start()

    def send(self, msg:dict|list, *, stream:bool=False):
        if stream: raise MCPTransportError('stdio transport does not support per-request HTTP streaming')
        if self._proc.stdin is None: raise MCPTransportError('stdin is closed')
        s = _json_dumps(msg)
        with self._send_lock:
            try:
                self._proc.stdin.write(s + '\n')
                self._proc.stdin.flush()
            except BrokenPipeError as e: raise MCPTransportError('Server stdin closed') from e
        return None

    def close(self, *, timeout:float=2.0, kill_timeout:float=1.0):
        self._stop.set()
        with contextlib.suppress(Exception):
            if self._proc.stdin: self._proc.stdin.close()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=timeout)
            return
        with contextlib.suppress(Exception):
            self._proc.terminate()
            self._proc.wait(timeout=kill_timeout)
            return
        with contextlib.suppress(Exception): self._proc.kill()


class StreamableHTTPTransport(MCPTransport):
    """Streamable HTTP transport (protocol >= 2025-03-26).

    One endpoint supports:
    - POST: send a JSON-RPC message. Requests may return JSON or SSE stream.
    - GET: optionally open an SSE stream for server->client notifications/requests.
    """

    def __init__(self, endpoint:str, *, headers:dict|None=None, timeout:float=30.0, debug:Callable[[str], None]|None=None):
        self.endpoint = endpoint
        self._base_headers = headers or {}
        self._c = httpx.Client(timeout=timeout, headers={'Accept': 'application/json, text/event-stream'} | self._base_headers)
        self.session_id = None
        self.protocol_version = None
        self._on_message = None
        self._listen_stop = threading.Event()
        self._listen_thread = None
        self._last_event_id = None
        self._debug = debug

    def _log(self, msg:str):
        if self._debug: self._debug(msg)

    def _headers(self, *, accept:str|None=None, include_version:bool=True)->dict:
        h = dict(self._base_headers)
        if accept is not None: h['Accept'] = accept
        if self.session_id: h['Mcp-Session-Id'] = self.session_id
        if include_version and self.protocol_version: h['MCP-Protocol-Version'] = self.protocol_version
        return h

    def _remember_session(self, resp:httpx.Response):
        sid = resp.headers.get('Mcp-Session-Id') or resp.headers.get('mcp-session-id')
        if sid: self.session_id = sid

    def _iter_sse_msgs(self, lines:Iterable[str])->Iterator[dict]:
        for obj in sse_json_messages(lines):
            if isinstance(obj, dict) and obj.get('event') == 'endpoint': continue
            for m in _as_list(obj):
                if isinstance(m, dict): yield m

    def _iter_response_msgs(self, resp:httpx.Response, *, stream:bool)->Iterator[dict]:
        ct = resp.headers.get('content-type', '')
        if ct.startswith('application/json'):
            for m in _as_list(resp.json()):
                if isinstance(m, dict): yield m
            return
        if ct.startswith('text/event-stream'):
            lines = resp.iter_lines() if stream else resp.text.splitlines()
            yield from self._iter_sse_msgs(lines)
            return
        raise MCPTransportError(f'Unknown content-type: {ct}')

    def start(self, on_message:Callable[[dict], None]): self._on_message = on_message

    def listen(self):
        "Open a background GET SSE stream (if supported)."
        if self._on_message is None: raise MCPTransportError('start(on_message) must be called before listen()')
        if self._listen_thread and self._listen_thread.is_alive(): return

        def _run():
            hdrs = self._headers(accept='text/event-stream')
            if self._last_event_id: hdrs['Last-Event-ID'] = self._last_event_id
            try:
                with self._c.stream('GET', self.endpoint, headers=hdrs) as resp:
                    if resp.status_code == 405: return
                    resp.raise_for_status()
                    if not resp.headers.get('content-type', '').startswith('text/event-stream'): return
                    for ev in sse_events(resp.iter_lines()):
                        if self._listen_stop.is_set(): break
                        if 'id' in ev: self._last_event_id = ev['id']
                        if 'data' not in ev: continue
                        try: payload = json.loads(ev['data'])
                        except json.JSONDecodeError: continue
                        for msg in _as_list(payload):
                            if isinstance(msg, dict): self._on_message(msg)
            except (httpx.HTTPError, MCPTransportError, OSError, RuntimeError, ValueError) as e:
                self._log(f'HTTP listen loop stopped: {e!r}')
                return

        self._listen_stop.clear()
        self._listen_thread = threading.Thread(target=_run, name='mcp-http-listen', daemon=True)
        self._listen_thread.start()

    def send(self, msg:dict|list, *, stream:bool=False):
        payload = msg
        method = payload.get('method') if isinstance(payload, dict) else None

        # Spec: do not require version/session on initialize; server may set them.
        hdrs = self._headers(include_version=method != 'initialize')
        if method == 'initialize': hdrs.pop('Mcp-Session-Id', None)
        if stream: return self._send_stream(payload, headers=hdrs)

        r = self._c.post(self.endpoint, json=payload, headers=hdrs)
        self._remember_session(r)
        if r.status_code == 202: return []
        r.raise_for_status()
        return list(self._iter_response_msgs(r, stream=False))

    def _send_stream(self, payload:dict|list, *, headers:dict)->Iterator[dict]:
        def _gen():
            with self._c.stream('POST', self.endpoint, json=payload, headers=headers) as resp:
                self._remember_session(resp)
                if resp.status_code == 202: return
                resp.raise_for_status()
                yield from self._iter_response_msgs(resp, stream=True)

        return _gen()

    def close(self):
        self._listen_stop.set()
        with contextlib.suppress(Exception): self._c.close()


class LegacySSETransport(MCPTransport):
    """Legacy HTTP+SSE transport (protocol 2024-11-05).

    - GET to server URL opens SSE.
    - First event: `endpoint` with POST URL for client->server messages.
    - Subsequent events contain JSON-RPC messages in data.
    """

    def __init__(self, sse_url:str, *, headers:dict|None=None, timeout:float=30.0, debug:Callable[[str], None]|None=None):
        self.sse_url = sse_url
        self._base_headers = headers or {}
        self._c = httpx.Client(timeout=timeout, headers=self._base_headers)
        self._post_url = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._debug = debug

    def _log(self, msg:str):
        if self._debug: self._debug(msg)

    def start(self, on_message:Callable[[dict], None]):
        def _run():
            try:
                with self._c.stream('GET', self.sse_url, headers={'Accept': 'text/event-stream'} | self._base_headers) as resp:
                    resp.raise_for_status()
                    if not resp.headers.get('content-type', '').startswith('text/event-stream'):
                        raise MCPTransportError('Legacy transport requires text/event-stream response')
                    for ev in sse_events(resp.iter_lines()):
                        if self._stop.is_set(): break
                        if ev.get('event') == 'endpoint' and 'data' in ev:
                            self._post_url = ev['data'].strip()
                            continue
                        if 'data' not in ev: continue
                        try: payload = json.loads(ev['data'])
                        except json.JSONDecodeError: continue
                        for msg in _as_list(payload):
                            if isinstance(msg, dict): on_message(msg)
            except (httpx.HTTPError, MCPTransportError, OSError, RuntimeError, ValueError) as e:
                self._stop.set()
                self._log(f'Legacy SSE loop stopped: {e!r}')
                return

        self._stop.clear()
        threading.Thread(target=_run, name='mcp-legacy-sse', daemon=True).start()

    def send(self, msg:dict|list, *, stream:bool=False):
        if stream: raise MCPTransportError('Legacy SSE does not support per-request HTTP streaming')
        if not self._post_url: raise MCPTransportError("Cannot send before receiving legacy 'endpoint' event")
        with self._send_lock: r = self._c.post(self._post_url, json=msg, headers=self._base_headers)
        if r.status_code >= 400: raise MCPTransportError(f'HTTP {r.status_code}: {r.text[:200]}')
        return None

    def close(self):
        self._stop.set()
        with contextlib.suppress(Exception): self._c.close()


__all__ = ['MCPError', 'MCPRemoteError', 'MCPTimeout', 'MCPTransportError', 'sse_events', 'sse_json_messages', 'MCPTransport', 'StdioTransport', 'StreamableHTTPTransport', 'LegacySSETransport']
