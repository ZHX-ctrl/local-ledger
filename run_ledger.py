"""Double-click launcher entry point; one server owns each local database."""
from __future__ import annotations
import argparse
import atexit
import json
import os
from pathlib import Path
import sys
import threading
import uuid
import webbrowser

PROJECT = Path(__file__).resolve().parent


def take_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / 'server.lock').open('a+b')
    if handle.tell() == 0:
        handle.write(b'0'); handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main():
    parser=argparse.ArgumentParser(description='本地双币账本，仅监听本机')
    parser.add_argument('--data-dir', type=Path, default=PROJECT/'private')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--offline', action='store_true', help='不请求汇率（新币折算保持待更新）')
    args=parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('端口范围为 0–65535')
    root=args.data_dir.resolve()
    private_root = PROJECT / 'private'
    if private_root.is_symlink() or private_root.resolve() != private_root:
        parser.error('private 目录不能是链接或重定向目录')
    if not root.is_relative_to(private_root):
        parser.error('数据目录必须位于当前项目的 private/ 内')
    lock=take_lock(root)
    if lock is None:
        print('这个账本已经在运行。请使用已打开的窗口。')
        return 1
    atexit.register(lock.close)
    from ledger_app.service import Ledger, LedgerError
    from ledger_app.web import make_server
    resolver=(lambda d: {'status':'pending','reason':'离线模式'}) if args.offline else None
    ledger=Ledger(root, fx_resolver=resolver)
    try:
        server=make_server(ledger,port=args.port)
    except OSError:
        ledger.close()
        print('启动失败：本地端口已被占用。可用 --port 8766 指定另一个端口。')
        return 1
    try:
        ledger.prepare_daily_backup()
        print('今日自动备份检查通过。', flush=True)
    except LedgerError as exc:
        print(f'启动检查未通过：{exc.message}。可以查看账本，但保存前必须解决备份问题。', flush=True)
    port=server.server_address[1]
    runtime_tmp = root/'runtime.json.tmp'
    runtime_tmp.write_text(json.dumps({
        'port':port,'pid':os.getpid(),'instance_id':server.instance_id,
    }),encoding='utf-8')
    os.replace(runtime_tmp, root/'runtime.json')
    print(f'本地账本已启动：http://127.0.0.1:{port}', flush=True)
    print('数据仅保存在本地。关闭此窗口或按 Ctrl+C 停止服务。',flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda:webbrowser.open(f'http://127.0.0.1:{port}')).start()
    if not args.offline:
        def refresh():
            try:
                ledger.dispatch('fx.refresh',{},'startup-'+uuid.uuid4().hex)
            except Exception:
                pass  # Startup must work offline; no financial details in logs.
        threading.Thread(target=refresh,daemon=True).start()
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ledger.close()
        lock.close()
    return 0


if __name__=='__main__': sys.exit(main())
