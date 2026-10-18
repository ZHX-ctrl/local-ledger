"""Desktop startup checks against isolated, empty ledgers only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import desktop_launcher as launcher


class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        parent = launcher.PROJECT / 'private' / 'work'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent, prefix='desktop-test-')
        self.root = Path(self.temp.name)
        self.children = []
        real_popen = subprocess.Popen

        def capture(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            self.children.append(child)
            return child

        self.patcher = patch.object(launcher.subprocess, 'Popen', side_effect=capture)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        self.temp.cleanup()

    def start(self):
        return launcher.ensure_server(self.root, port=0, offline=True, timeout=15)

    def test_start_reopen_and_restart_without_duplicate_server(self):
        url, log = self.start()
        first = json.loads((self.root / 'runtime.json').read_text())
        self.assertIsNotNone(log)
        self.assertEqual(launcher.ready_url(self.root), url)
        self.assertEqual(self.start(), (url, None))
        self.assertEqual(len(self.children), 1)
        self.children[0].terminate()
        self.children[0].wait(timeout=10)
        self.assertIsNone(launcher.ready_url(self.root))
        url, _ = self.start()
        second = json.loads((self.root / 'runtime.json').read_text())
        self.assertNotEqual(first['instance_id'], second['instance_id'])
        self.assertEqual(launcher.ready_url(self.root), url)

    def test_concurrent_clicks_open_same_instance(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.start(), range(2)))
        self.assertEqual(results[0][0], results[1][0])
        info = json.loads((self.root / 'runtime.json').read_text())
        self.assertEqual(sum(p.pid == info['pid'] for p in self.children), 1)
        self.assertEqual(launcher.ready_url(self.root), results[0][0])

    def test_invalid_runtime_and_wrong_instance_are_not_reused(self):
        runtime = self.root / 'runtime.json'
        for content in ('bad json', '[]', '{"port":"8765"}',
                        '{"port":999999,"instance_id":"' + 'a' * 32 + '"}'):
            runtime.write_text(content, encoding='utf-8')
            self.assertIsNone(launcher.ready_url(self.root))
        self.start()
        info = json.loads(runtime.read_text())
        info['instance_id'] = '0' * 32
        runtime.write_text(json.dumps(info), encoding='utf-8')
        self.assertIsNone(launcher.ready_url(self.root))

    def test_conflicting_port_reports_failure_without_opening_browser(self):
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            with self.assertRaises(launcher.StartupError):
                launcher.ensure_server(self.root, port=occupied.getsockname()[1],
                                       offline=True, timeout=5)
            self.assertIsNone(launcher.ready_url(self.root))

    def test_outside_private_rejected_before_creation(self):
        target = launcher.PROJECT / 'work' / 'desktop-test-must-not-create'
        with self.assertRaises(launcher.StartupError):
            launcher.ensure_server(target, offline=True)
        self.assertFalse(target.exists())

    def test_running_legacy_server_gives_restart_instructions(self):
        with patch.object(launcher, 'owner_running', return_value=True), \
                patch.object(launcher, 'legacy_server_running', return_value=True):
            with self.assertRaisesRegex(launcher.StartupError, '旧版记账服务'):
                launcher.ensure_server(self.root, offline=True, timeout=15)
            self.assertEqual(self.children, [])

    def test_health_is_metadata_only_and_rejects_cross_site(self):
        self.start()
        port = json.loads((self.root / 'runtime.json').read_text())['port']
        with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=2)) as connection:
            connection.request('GET', '/api/health')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(set(json.loads(response.read())), {'ok', 'app', 'instance_id'})
            connection.request('GET', '/api/health', headers={'Origin': 'https://example.com'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop entry')
    def test_pythonw_cold_start_can_finish_without_console(self):
        # Run the real no-console interpreter; browser dispatch itself is
        # captured so this synthetic test never opens a fake ledger for users.
        result_path = self.root / 'result.json'
        script_path = self.root / 'smoke.py'
        script_path.write_text(textwrap.dedent(f'''
            import json, os, subprocess, sys
            from pathlib import Path
            sys.path.insert(0, {str(launcher.PROJECT)!r})
            import desktop_launcher as d
            children = []
            result = {{}}
            original_popen = subprocess.Popen
            original_ensure = d.ensure_server
            def capture(*args, **kwargs):
                child = original_popen(*args, **kwargs)
                children.append(child)
                return child
            d.subprocess.Popen = capture
            d.ensure_server = lambda: original_ensure(Path({str(self.root / 'data')!r}), port=0, offline=True)
            os.startfile = lambda url: result.update(opened=url)
            d.notify = lambda message, **kwargs: result.update(error=message)
            try:
                result['exit_code'] = d.main()
                result['ready'] = d.ready_url(Path({str(self.root / 'data')!r}))
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=10)
                Path({str(result_path)!r}).write_text(json.dumps(result), encoding='utf-8')
        '''), encoding='utf-8')
        pythonw = Path(launcher.sys.executable).with_name('pythonw.exe')
        completed = subprocess.run([str(pythonw), '-X', 'utf8', str(script_path)],
                                   cwd=launcher.PROJECT, timeout=25)
        self.assertEqual(completed.returncode, 0)
        result = json.loads(result_path.read_text(encoding='utf-8'))
        self.assertNotIn('error', result)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['opened'], result['ready'])

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop entry')
    def test_desktop_opens_browser_only_after_readiness(self):
        with patch.object(launcher, 'ensure_server', return_value=('http://127.0.0.1:1234/', None)), \
                patch.object(os, 'startfile') as opened:
            self.assertEqual(launcher.main(), 0)
            opened.assert_called_once_with('http://127.0.0.1:1234/')
        with patch.object(launcher, 'ensure_server', side_effect=launcher.StartupError('failed')), \
                patch.object(os, 'startfile') as opened, patch.object(launcher, 'notify') as notified:
            self.assertEqual(launcher.main(), 1)
            opened.assert_not_called()
            notified.assert_called_once_with('failed')


if __name__ == '__main__':
    unittest.main()
