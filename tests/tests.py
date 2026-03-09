#!/usr/bin/env python
from fastcore.test import test_eq

import http.server, json, socketserver, threading
from pathlib import Path

from fake_payloads import initialize_result, listing_notification, rpc_error, rpc_result, tools_list_result, tool_call_result
from solvemcp import *


# --- SSE parsing
sample = 'data: {"a":1}\n\ndata: {"b":2}\n\n'
msgs = list(sse_json_messages(sample.splitlines()))
test_eq(msgs, [{'a': 1}, {'b': 2}])


# --- Local stdio + HTTP fake servers (copied from module's self-tests section)
def _run_stdio_fake_server():
    "Return a command that runs a tiny MCP server over stdio."
    import sys as _sys

    parent = Path(__file__).parent
    return [_sys.executable, '-u', str(parent / 'echoserver.py')]


def _run_http_fake_server():
    "Spin up a local Streamable HTTP MCP endpoint and return (url, shutdown_fn)."
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'FakeMCP/0'
        protocol_version = 'HTTP/1.1'

        def _read_json(self):
            n = int(self.headers.get('content-length', '0'))
            b = self.rfile.read(n) if n else b''
            return json.loads(b.decode('utf-8') or 'null')

        def _send_json(self, obj, *, status=200, headers=None):
            body = json.dumps(obj, separators=(',', ':')).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            if headers:
                for k, v in headers.items(): self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_sse(self, events:list[str], *, status=200, headers=None):
            body = ''.join(events).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            if headers:
                for k, v in headers.items(): self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_response(405)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self):
            msg = self._read_json()
            method = msg.get('method') if isinstance(msg, dict) else None

            if method == 'initialize':
                rid = msg['id']
                res = initialize_result(msg['params']['protocolVersion'], server_name='fake-http')
                self._send_json(rpc_result(rid, res), headers={'Mcp-Session-Id': 'sess-1'})
                return

            if method == 'notifications/initialized':
                self.send_response(202)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return

            if method == 'tools/list':
                rid = msg['id']
                notif = listing_notification()
                resp = tools_list_result(rid)
                events = [
                    'data: ' + json.dumps(notif, separators=(',', ':')) + '\n\n',
                    'data: ' + json.dumps(resp, separators=(',', ':')) + '\n\n',
                ]
                self._send_sse(events)
                return

            if method == 'tools/call':
                rid = msg['id']
                out = msg.get('params', {}).get('arguments', {}).get('text', '')
                self._send_json(tool_call_result(rid, out))
                return

            rid = msg.get('id')
            self._send_json(rpc_error(rid))

        def log_message(self, format, *args): return

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    host, port = srv.server_address
    url = f'http://{host}:{port}/mcp'
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def shutdown():
        srv.shutdown()
        srv.server_close()

    return url, shutdown


if __name__ == '__main__':
    # SSE parsing
    sample = 'data: {"a":1}\n\ndata: {"b":2}\n\n'
    msgs = list(sse_json_messages(sample.splitlines()))
    test_eq(msgs, [{'a': 1}, {'b': 2}])

    # stdio roundtrip
    cmd = _run_stdio_fake_server()
    with MCPClient.stdio(cmd, client_name='t', client_version='0') as c:
        test_eq('echo' in c.tools, True)
        r = c.echo(text='hi')
        test_eq(r.content[0].text, 'hi')

    # streamable http roundtrip (local)
    url, shutdown = _run_http_fake_server()
    try:
        with MCPClient.http(url, client_name='t', client_version='0') as c:
            test_eq('echo' in c.tools, True)
            r = c.echo(text='yo')
            test_eq(r.content[0].text, 'yo')
            stream = list(c.rpc_stream('tools/list', params={}))
            methods = [m.method for m in stream if 'method' in m]
            test_eq('notifications/message' in methods, True)
    finally: shutdown()
    print('ok')
