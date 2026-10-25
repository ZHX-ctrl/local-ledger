"""Process lifecycle, single-owner lock, and restart persistence."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from ledger_app.cli import Client

PROJECT=Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_single_owner_and_restart(self):
        test_parent=PROJECT/'private'/'work'
        test_parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_parent,prefix='launcher-test-') as folder:
            root=Path(folder)
            env=dict(os.environ,PYTHONIOENCODING='utf-8')
            command=[sys.executable,'-X','utf8',str(PROJECT/'run_ledger.py'),
                     '--no-browser','--offline','--port','0','--data-dir',folder]
            process=None
            def start():
                proc=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if proc.poll() is not None: raise AssertionError('server exited before startup')
                    try:
                        info=json.loads((root/'runtime.json').read_text(encoding='utf-8'))
                        if info['pid']==proc.pid:
                            client=Client(info['port'])
                            return proc,client
                    except (OSError,ValueError,KeyError):pass
                    time.sleep(.1)
                proc.terminate();proc.wait(timeout=5)
                raise AssertionError('server startup timed out')
            try:
                process,client=start()
                result=client.command('snapshot.save',{'currency':'CNY','balance':'123.45','cutoff':'2026-07-31'},uuid.uuid4().hex)
                self.assertTrue(result['ok'],result)
                second=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=10)
                self.assertEqual(second.returncode,1)
                process.terminate();process.wait(timeout=5);process=None
                process,client=start()
                state=client.state()['result']
                self.assertEqual(next(w['balance'] for w in state['wallets'] if w['currency']=='CNY'),'123.45')
            finally:
                if process is not None:
                    process.terminate();process.wait(timeout=5)

    def test_rejects_data_directory_outside_private_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'must-not-create'
            result=subprocess.run([sys.executable,'-X','utf8',str(PROJECT/'run_ledger.py'),
                '--no-browser','--offline','--data-dir',str(target)],capture_output=True,timeout=10)
            self.assertEqual(result.returncode,2)
            self.assertFalse(target.exists())


class NightlyRefreshTests(unittest.TestCase):
    def test_next_refresh_is_23_local(self):
        from run_ledger import next_nightly_refresh
        tz=dt.timezone(dt.timedelta(hours=8))
        self.assertEqual(next_nightly_refresh(dt.datetime(2026,10,22,9,41,tzinfo=tz)),
                         dt.datetime(2026,10,22,23,0,tzinfo=tz))
        self.assertEqual(next_nightly_refresh(dt.datetime(2026,10,22,23,0,tzinfo=tz)),
                         dt.datetime(2026,10,23,23,0,tzinfo=tz))

    def test_refreshes_once_per_night_and_after_wake(self):
        from run_ledger import run_nightly_refresh
        tz=dt.timezone(dt.timedelta(hours=8))
        clock=[dt.datetime(2026,10,22,22,58,tzinfo=tz)]
        fired=[]
        def sleep(seconds):
            clock[0]+=dt.timedelta(seconds=seconds)
            if clock[0].date()==dt.date(2026,10,23) and clock[0].hour==12:
                clock[0]=dt.datetime(2026,10,24,8,0,tzinfo=tz)  # slept through 23:00
        run_nightly_refresh(lambda: fired.append(clock[0]), lambda: clock[0], sleep,
                            stop=lambda: len(fired)>=2)
        self.assertEqual(fired[0],dt.datetime(2026,10,22,23,0,tzinfo=tz))
        self.assertEqual(fired[1],dt.datetime(2026,10,24,8,0,tzinfo=tz))


if __name__=='__main__':unittest.main()
