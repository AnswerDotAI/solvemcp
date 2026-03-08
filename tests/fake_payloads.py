'Shared fake MCP payload builders for test fixtures.'

TOOL_SCHEMA = dict(name='echo', description='Echo text',
    inputSchema=dict(type='object', properties={'text': dict(type='string', description='text')}, required=['text']))
TOOLS = [TOOL_SCHEMA]

def initialize_result(protocol_version:str, *, server_name:str='fake', server_version:str='0'):
    return dict(protocolVersion=protocol_version, capabilities=dict(tools={}), serverInfo=dict(name=server_name, version=server_version))

def rpc_result(rid, result): return dict(jsonrpc='2.0', id=rid, result=result)
def rpc_error(rid, *, code:int=-32601, message:str='Method not found'): return dict(jsonrpc='2.0', id=rid, error=dict(code=code, message=message))
def tools_list_result(rid): return rpc_result(rid, dict(tools=TOOLS))

def tool_call_result(rid, text:str): return rpc_result(rid, dict(content=[dict(type='text', text=text)]))

def listing_notification(*, logger:str='fake', data:str='listing'):
    return dict(jsonrpc='2.0', method='notifications/message', params=dict(level='info', logger=logger, data=data))

