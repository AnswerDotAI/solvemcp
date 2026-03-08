import json, sys
from fake_payloads import *

def dumps(o): return json.dumps(o, ensure_ascii=False, separators=(',', ':'))
def p(s): print(s, flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line: continue
    msg = json.loads(line)
    method = msg.get('method') if isinstance(msg, dict) else None
    rid = msg.get('id')
    if method=='initialize': p(dumps(rpc_result(rid, initialize_result(msg['params']['protocolVersion']))))
    elif method=='notifications/initialized': pass
    elif method=='tools/list': p(dumps(tools_list_result(rid)))
    elif method=='tools/call':
        args = msg['params']['arguments']
        out = args.get('text', '')
        p(dumps(tool_call_result(rid, out)))
    elif rid is not None: p(dumps(rpc_error(rid)))

