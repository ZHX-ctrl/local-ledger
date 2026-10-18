"""Local conversational adapter. Shares the running server's business service."""
from __future__ import annotations
import argparse
import http.cookiejar
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPCookieProcessor, Request


class Client:
    def __init__(self,port=8765):
        if not 1 <= int(port) <= 65535: raise ValueError('无效端口')
        self.base=f'http://127.0.0.1:{int(port)}'
        self.opener=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf=self._request('/api/session')['csrf']

    def _request(self,path,data=None):
        headers={'Origin':self.base,'Accept':'application/json'}
        encoded=None
        if data is not None:
            encoded=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            headers.update({'Content-Type':'application/json','X-CSRF-Token':self.csrf})
        req=Request(self.base+path,data=encoded,headers=headers)
        try:
            with self.opener.open(req,timeout=90) as response:
                return json.load(response)
        except HTTPError as exc:
            with exc:
                try: return json.load(exc)
                except Exception: return {'ok':False,'error':{'code':'http_error','message':f'HTTP {exc.code}'}}

    def state(self,filters=None):
        return self._request('/api/state?'+urlencode(filters or {}))

    def command(self,action,payload,request_id):
        return self._request('/api/command',{'action':action,'payload':payload,'request_id':request_id})


def main():
    p=argparse.ArgumentParser(description='向本机账本读写结构化记录；先启动网站')
    p.add_argument('--port',type=int,default=8765)
    sub=p.add_subparsers(dest='cmd',required=True)
    state=sub.add_parser('state'); state.add_argument('--month'); state.add_argument('--currency');state.add_argument('--amount');state.add_argument('--q')
    command=sub.add_parser('command')
    command.add_argument('--action',required=True)
    command.add_argument('--request-id',required=True,help='同一逻辑操作重试时保持不变')
    command.add_argument('--file',type=Path,required=True,help='私有目录中的 UTF-8 JSON payload 文件')
    args=p.parse_args()
    try:
        client=Client(args.port)
        if args.cmd=='state':
            result=client.state({k:getattr(args,k) for k in ['month','currency','amount','q'] if getattr(args,k)})
        else:
            payload=json.loads(args.file.read_text(encoding='utf-8-sig'))
            if not isinstance(payload,dict): raise ValueError('payload 必须为 JSON 对象')
            result=client.command(args.action,payload,args.request_id)
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
        return 0 if result.get('ok') else 1
    except (OSError, ValueError, URLError, KeyError):
        print(json.dumps({'ok':False,'error':{'code':'client_error','message':'无法完成操作。请确认网站已启动、端口正确、输入文件是有效 JSON。'}},ensure_ascii=False))
        return 1


if __name__=='__main__':sys.exit(main())
