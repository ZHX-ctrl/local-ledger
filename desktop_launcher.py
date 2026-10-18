"""Open the local ledger from a desktop shortcut, starting it only if needed."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import webbrowser

from run_ledger import PROJECT, take_lock


class StartupError(Exception):
    pass


def ready_url(root: Path) -> str | None:
    """Match the live server against this data directory's runtime metadata.

    A port being open, or a stale runtime.json alone, is not enough. Direct
    loopback HTTP bypasses system proxy settings and reads no financial data.
    """
    connection = None
    try:
        info = json.loads((root / 'runtime.json').read_text(encoding='utf-8'))
        port = info['port']
        instance = info['instance_id']
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        if not isinstance(instance, str) or len(instance) != 32:
            return None
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.8)
        connection.request('GET', '/api/health')
        response = connection.getresponse()
        if response.status != 200:
            return None
        payload = json.loads(response.read(4096))
        if (payload.get('ok') is True and payload.get('app') == 'local-ledger'
                and payload.get('instance_id') == instance):
            return f'http://127.0.0.1:{port}/'
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            http.client.HTTPException):
        pass
    finally:
        if connection is not None:
            connection.close()
    return None


def owner_running(root: Path) -> bool:
    lock = take_lock(root)
    if lock is None:
        return True
    lock.close()
    return False


def legacy_server_running(root: Path) -> bool:
    """Recognize a pre-shortcut server using its static page, never its ledger."""
    connection = None
    try:
        info = json.loads((root / 'runtime.json').read_text(encoding='utf-8'))
        port = info['port']
        if info.get('instance_id') or type(port) is not int or not 1 <= port <= 65535:
            return False
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.8)
        connection.request('GET', '/api/health')
        response = connection.getresponse()
        response.read(4096)
        if response.status != 404:
            return False
        connection.request('GET', '/')
        response = connection.getresponse()
        return response.status == 200 and '<title>双币账本 · 本机</title>'.encode() in response.read(32768)
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def ensure_server(root: Path = PROJECT / 'private', *, port: int = 8765,
                  offline: bool = False, timeout: float = 60) -> tuple[str, Path | None]:
    root = root.resolve()
    private = PROJECT / 'private'
    if private.is_symlink() or private.resolve() != private or not root.is_relative_to(private):
        raise StartupError('数据目录必须位于当前项目的 private/ 内，且不能是重定向目录。')
    if url := ready_url(root):
        return url, None

    # Only the server process owns the database lock. Concurrent clicks may
    # briefly launch contenders; all wait for the one successful owner.
    process = None
    log_path = None
    running = owner_running(root)
    if running and legacy_server_running(root):
        raise StartupError('旧版记账服务还在运行。\n请先关闭原来的“启动记账”命令窗口，'
                           '再双击桌面“记账”。这次重启后即可直接使用快捷方式。')
    if not running:
        work = root / 'work'
        work.mkdir(parents=True, exist_ok=True)
        log_path = work / f'desktop-startup-{uuid.uuid4().hex}.log'
        python = Path(sys.executable).with_name('python.exe') if os.name == 'nt' else Path(sys.executable)
        command = [str(python), '-X', 'utf8', str(PROJECT / 'run_ledger.py'),
                   '--no-browser', '--data-dir', str(root), '--port', str(port)]
        if offline:
            command.append('--offline')
        with log_path.open('wb') as output:
            process = subprocess.Popen(
                command, cwd=PROJECT, stdin=subprocess.DEVNULL,
                stdout=output, stderr=output,
                env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if url := ready_url(root):
            return url, log_path
        if process is not None and process.poll() is not None and not owner_running(root):
            raise StartupError(f'记账服务未能启动，可能是端口占用或文件权限问题。\n\n启动说明保存在：\n{log_path}')
        time.sleep(.2)
    raise StartupError('记账服务尚未就绪，请稍后再双击桌面“记账”。\n'
                       '如果仍然打不开，请运行项目文件夹里的“启动记账.bat”查看具体提示。')


def notify(message: str, *, error: bool = True) -> None:
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, '记账', 0x10 if error else 0x30)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def main() -> int:
    try:
        url, log_path = ensure_server()
        if os.name == 'nt':
            os.startfile(url)
        elif not webbrowser.open(url):
            raise StartupError(f'服务已启动，请在浏览器打开：{url}')
        if log_path and '启动检查未通过' in log_path.read_text(encoding='utf-8', errors='replace'):
            notify('记账页面已打开，但自动备份检查未通过，暂时无法保存新账目。\n'
                   f'请查看启动说明：\n{log_path}', error=False)
        return 0
    except (StartupError, OSError) as exc:
        notify(str(exc))
        return 1
    except Exception:
        notify('启动遇到问题。请打开项目文件夹里的“启动记账.bat”查看具体提示。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
