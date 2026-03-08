'High-level MCP client interface for solvemcp.'

import contextlib, threading
from typing import Any, Callable, Iterator

import httpx
from fastcore.basics import AttrDict
from fastcore.meta import delegates
from fastcore.utils import *
from toolslm.funccall import mk_tool

from .transports import (LegacySSETransport, MCPError, MCPRemoteError, MCPTimeout, MCPTransport,
                         MCPTransportError, StdioTransport, StreamableHTTPTransport, _as_list,
                         _is_notif, _is_req, _is_resp, _now)


def _rpc_msg(method:str, *, id_:Any=None, params:dict|None=None):
    msg = dict(jsonrpc='2.0', method=method)
    if id_ is not None: msg['id'] = id_
    if params is not None: msg['params'] = params
    return msg


class MCPClient:
    """A pragmatic MCP client with stdio + HTTP transports.

    - `rpc(method, params)` sends a JSON-RPC request and waits for the result.
    - `rpc_stream(...)` yields streamed JSON-RPC messages (Streamable HTTP only).
    - `refresh_tools()` fetches tools and attaches callables as attributes.
    """

    supported_protocol_versions = ('2025-06-18', '2025-03-26', '2024-11-05')

    def __init__(self, transport:MCPTransport, *, client_name:str='solvemcp', client_version:str='0.1.0',
                 capabilities:dict|None=None, protocol_version:str|None=None, init_timeout:float=20.0,
                 request_timeout:float=30.0, auto_initialize:bool=True, auto_tools:bool=True):
        self.t = transport
        self.client_info = dict(name=client_name, version=client_version)
        self.capabilities = capabilities or {}
        self.request_timeout = request_timeout

        self.protocol_version = protocol_version or self.supported_protocol_versions[0]
        self.server_info = None
        self.server_capabilities = None
        self.instructions = None

        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending = {}  # id -> {event, resp}
        self._pending_lock = threading.Lock()
        self._handlers = {}
        self._server_request_handler = None

        self.t.start(self._on_message)
        if auto_initialize: self.initialize(timeout=init_timeout)
        if auto_tools: self.refresh_tools()

    # --- constructors

    @classmethod
    @delegates(StdioTransport.__init__)
    def stdio(cls, cmd:list[str], **kwargs):
        "Create an MCP client using stdio (subprocess)."
        t = StdioTransport(cmd, cwd=kwargs.pop('cwd', None), env=kwargs.pop('env', None), stderr=kwargs.pop('stderr', True), text=kwargs.pop('text', True), debug=kwargs.pop('debug', None))
        return cls(t, **kwargs)

    @classmethod
    @delegates(StreamableHTTPTransport.__init__)
    def http(cls, url:str, **kwargs):
        "Auto-detect Streamable HTTP vs legacy HTTP+SSE (per 2025-06-18 backwards compat guidance)."
        # Split transport kwargs from client kwargs (delegates gives us `headers`, `timeout`, `debug`).
        headers = kwargs.pop('headers', None)
        timeout = kwargs.pop('timeout', 30.0)
        debug = kwargs.pop('debug', None)
        t = StreamableHTTPTransport(url, headers=headers, timeout=timeout, debug=debug)
        c = cls(t, auto_initialize=False, auto_tools=False, **kwargs)
        try:
            c.initialize()
            c.refresh_tools()
            return c
        except MCPTransportError as e:
            # Only fall back on 4xx (404/405 etc). Anything else should surface.
            cause = getattr(e, '__cause__', None)
            if isinstance(cause, httpx.HTTPStatusError):
                status = cause.response.status_code if cause.response is not None else None
                if status and 400 <= status < 500: c.close()
                else: raise
            else: raise

        # fallback: legacy HTTP+SSE
        t2 = LegacySSETransport(url, headers=headers, timeout=timeout, debug=debug)
        c2 = cls(t2, auto_initialize=False, auto_tools=False, **kwargs)
        c2._wait_for_legacy_endpoint()
        c2.initialize()
        c2.refresh_tools()
        return c2

    def _wait_for_legacy_endpoint(self, timeout:float=5.0):
        if not isinstance(self.t, LegacySSETransport): return
        start = _now()
        while self.t._post_url is None and _now() - start < timeout: sleep(0.01)
        if self.t._post_url is None: raise MCPTransportError('Legacy SSE server did not send an endpoint event')

    # --- lifecycle

    def initialize(self, *, timeout:float|None=None, capabilities:dict|None=None, protocol_version:str|None=None):
        if protocol_version: self.protocol_version = protocol_version
        init_params = dict(protocolVersion=self.protocol_version, capabilities=capabilities or self.capabilities,
                           clientInfo=self.client_info)

        last_err = None
        versions = (self.protocol_version,) + tuple(v for v in self.supported_protocol_versions if v != self.protocol_version)
        for ver in versions:
            init_params['protocolVersion'] = ver
            try: resp = self.rpc('initialize', init_params, timeout=timeout or self.request_timeout)
            except MCPRemoteError as e:
                last_err = e
                if e.code == -32602 or 'Unsupported protocol version' in e.message: continue
                raise

            pv = resp.get('protocolVersion') or resp.get('protocol_version')
            if pv and pv not in self.supported_protocol_versions:
                raise MCPError(f'Server negotiated unsupported protocol version: {pv}')
            if pv: self.protocol_version = pv

            # Inform transport (HTTP header)
            if hasattr(self.t, 'protocol_version'):
                with contextlib.suppress(Exception): setattr(self.t, 'protocol_version', self.protocol_version)

            self.server_info = dict2obj(resp.get('serverInfo', {}))
            self.server_capabilities = dict2obj(resp.get('capabilities', {}))
            self.instructions = resp.get('instructions')
            self.notify('notifications/initialized', {})
            return dict2obj(resp)

        if last_err: raise last_err
        raise MCPError('Failed to initialize')

    def close(self):
        with contextlib.suppress(Exception): self.t.close()

    def listen(self):
        "Open a background server->client SSE stream (Streamable HTTP only; no-op if unsupported by server)."
        if hasattr(self.t, 'listen'): return self.t.listen()
        raise MCPTransportError('listen() requires Streamable HTTP transport')

    def __enter__(self): return self

    def __exit__(self, exc_type, exc, tb): self.close()

    # --- dispatch

    def _new_id(self):
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _on_message(self, msg:dict):
        if isinstance(msg, list):
            for m in msg: self._on_message(m)
            return
        if not isinstance(msg, dict): return

        if _is_resp(msg):
            rid = msg.get('id')
            with self._pending_lock:
                p = self._pending.get(rid)
                if p:
                    p['resp'] = msg
                    p['event'].set()
            return

        if _is_req(msg):
            if self._server_request_handler is None:
                self._send_resp_error(msg.get('id'), code=-32601, message='Method not found')
                return
            try: res = self._server_request_handler(dict2obj(msg))
            except Exception as e:
                self._send_resp_error(msg.get('id'), code=-32000, message=str(e))
                return
            self._send_resp_ok(msg.get('id'), res)
            return

        if _is_notif(msg):
            self._emit(msg.get('method', ''), dict2obj(msg))
            return

    def on(self, method:str, f:Callable[[AttrDict], Any]):
        self._handlers.setdefault(method, []).append(f)
        return f

    def on_server_request(self, f:Callable[[AttrDict], Any]):
        self._server_request_handler = f
        return f

    def _emit(self, method:str, msg:AttrDict):
        for f in self._handlers.get(method, []):
            with contextlib.suppress(Exception): f(msg)

    # --- sending

    def _send(self, payload:dict|list, *, stream:bool=False):
        try: return self.t.send(payload, stream=stream)
        except httpx.HTTPStatusError as e: raise MCPTransportError(str(e)) from e
        except httpx.HTTPError as e: raise MCPTransportError(str(e)) from e

    def notify(self, method:str, params:dict|None=None):
        msg = _rpc_msg(method, params=params)
        self._send(msg)
        return True

    def cancel(self, request_id:Any, *, reason:str|None=None):
        params = {'requestId': request_id}
        if reason: params['reason'] = reason
        self.notify('notifications/cancelled', params)

    def _send_resp_ok(self, id_:Any, result:Any): self._send(dict(jsonrpc='2.0', id=id_, result=result))

    def _send_resp_error(self, id_:Any, *, code:int, message:str, data:Any=None):
        err = dict(code=code, message=message)
        if data is not None: err['data'] = data
        self._send(dict(jsonrpc='2.0', id=id_, error=err))

    # --- requests

    def rpc(self, method:str, params:dict|None=None, *, id_:Any=None, timeout:float|None=None, stream:bool=False)->Any:
        if stream: return self.rpc_stream(method, params=params, id_=id_)

        rid = id_ if id_ is not None else self._new_id()
        msg = _rpc_msg(method, id_=rid, params=params)

        ev = threading.Event()
        with self._pending_lock: self._pending[rid] = dict(event=ev, resp=None)

        try:
            maybe_msgs = self._send(msg)
            for m in _as_list(maybe_msgs):
                if isinstance(m, dict): self._on_message(m)

            ok = ev.wait(timeout or self.request_timeout)
            if not ok:
                if method != 'initialize':
                    with contextlib.suppress(Exception): self.cancel(rid, reason='timeout')
                raise MCPTimeout(f'Timeout waiting for response to {method}')

            with self._pending_lock: resp = self._pending.get(rid, {}).get('resp')
            if resp is None: raise MCPError('Missing response')
            if 'error' in resp:
                err = resp['error']
                raise MCPRemoteError(err.get('code', -32000), err.get('message', 'Error'), err.get('data'), method=method, id=rid)
            return dict2obj(resp['result'])
        finally:
            with self._pending_lock: self._pending.pop(rid, None)

    def rpc_stream(self, method:str, params:dict|None=None, *, id_:Any=None)->Iterator[AttrDict]:
        if not isinstance(self.t, StreamableHTTPTransport):
            raise MCPTransportError('rpc_stream is supported only for Streamable HTTP responses (SSE)')

        rid = id_ if id_ is not None else self._new_id()
        msg = _rpc_msg(method, id_=rid, params=params)

        it = self._send(msg, stream=True)
        if it is None: raise MCPTransportError('Transport did not return a stream iterator')

        for raw in it:
            if not isinstance(raw, dict): continue
            self._on_message(raw)
            yield dict2obj(raw)

    # --- tools

    def refresh_tools(self):
        try: res = self.rpc('tools/list', {})
        except MCPRemoteError:
            self.tools = AttrDict()
            return self.tools

        tools = res.get('tools', [])
        self.tools = AttrDict({t['name']: dict2obj(t) for t in tools})

        for t in self.tools.values():
            name = t.name
            if not name or not name.isidentifier(): continue
            if hasattr(self, name): continue
            setattr(self, name, mk_tool(self.call_tool, t))
        return self.tools

    def call_tool(self, name:str, **kwargs)->AttrDict: return self.rpc('tools/call', dict(name=name, arguments=kwargs))

    def call_tool_stream(self, name:str, **kwargs)->Iterator[AttrDict]:
        return self.rpc_stream('tools/call', params=dict(name=name, arguments=kwargs))

    def __dir__(self):
        base = set(super().__dir__())
        tools = set(getattr(self, 'tools', {}).keys())
        return sorted(base | tools)


__all__ = ['MCPClient']
