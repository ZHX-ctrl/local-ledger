"""Real HTTP -> shared service -> SQLite acceptance; synthetic data only."""
import base64
import contextlib
import csv
import datetime as dt
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import uuid
import zlib
from unittest.mock import patch

from ledger_app.cli import Client, main as cli_main
from ledger_app.service import Ledger
from ledger_app.web import make_server


def tiny_png():
    def chunk(kind,data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',1,1,8,2,0,0,0))+
            chunk(b'IDAT',zlib.compress(b'\0\x10\x80\x80'))+chunk(b'IEND',b''))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.calls=[]
        def resolver(date):
            self.calls.append(date)
            return {'status':'resolved','rate':'5.25','quote_date':date,'source':'ECB via Frankfurter v2'}
        self.ledger=Ledger(self.root,fx_resolver=resolver)
        self.server=make_server(self.ledger,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=Client(self.server.server_address[1])
        self.date='2026-08-10'
        for currency in ['CNY','SGD']:
            self.cmd('snapshot.save',{'currency':currency,'balance':'1000.00','cutoff':'2026-07-31','note':'虚构测试'})
        self.category=next(x['id'] for x in self.state()['categories'] if x['name']=='三餐')

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
        self.ledger.close();self.temp.cleanup()

    def cmd(self,action,payload,request_id=None):
        result=self.client.command(action,payload,request_id or uuid.uuid4().hex)
        self.assertTrue(result.get('ok'),result)
        return result['result']

    def state(self,month='2026-08'):
        result=self.client.state({'month':month})
        self.assertTrue(result['ok'],result)
        return result['result']

    def entry(self,**kw):
        payload={'date':self.date,'kind':'expense','currency':'CNY','amount':'10.00','name':'虚构午餐','category_id':self.category}
        payload.update(kw)
        return payload

    def test_http_cli_money_idempotency_and_snapshot_backfill(self):
        key=uuid.uuid4().hex
        one=self.cmd('entry.save',self.entry(),key)
        again=self.cmd('entry.save',self.entry(),key)
        self.assertEqual(one,again)
        self.assertEqual(self.calls,[])
        self.cmd('entry.save',self.entry(currency='SGD',amount='5.50',name='虚构新币餐'))
        state=self.state()
        self.assertEqual(state['summary']['expense_cny'],'38.88')
        self.assertEqual(state['summary']['expense_by_currency'],{'CNY':'10.00','SGD':'5.50'})
        self.assertEqual(state['summary']['income_by_currency'],{'CNY':'0.00','SGD':'0.00'})
        wallets={w['currency']:w['balance'] for w in state['wallets']}
        self.assertEqual(wallets,{'CNY':'990.00','SGD':'994.50'})
        self.cmd('snapshot.save',{'currency':'CNY','balance':'800.00','cutoff':'2026-08-31'})
        self.cmd('entry.save',self.entry(name='虚构补录',amount='20.00',date='2026-08-20'))
        self.assertEqual(next(w['balance'] for w in self.state()['wallets'] if w['currency']=='CNY'),'800.00')
        self.assertEqual(self.state()['summary']['expense_cny'],'58.88')

    def test_photos_backup_restore_and_export(self):
        saved=self.cmd('photo.save',{'content_base64':base64.b64encode(tiny_png()).decode()})
        self.cmd('entry.save',self.entry(name='=虚构公式',photo_id=saved['photo_id']))
        backup=self.cmd('backup.create',{})['name']
        self.cmd('entry.save',self.entry(name='虚构第二条'))
        self.cmd('backup.restore',{'name':backup,'confirm':True})
        self.assertEqual(len(self.state()['entries']),1)
        export=self.cmd('export.csv',{'month':'2026-08'})['name']
        body=(self.root/'exports'/export).read_text(encoding='utf-8-sig')
        self.assertIn("'=虚构公式",body)
        with self.client.opener.open(self.client.base+'/api/photos/'+saved['photo_id']) as response:
            self.assertEqual(response.read(),tiny_png())

    def test_jpeg_upload_removes_only_trailing_application_bytes(self):
        jpeg=b'\xff\xd8\xff\xe0\x00\x06ab\xff\xd9\xff\xda\x00\x02\x12\xff\x00\x34\xff\xd9'
        saved=self.cmd('photo.save',{'content_base64':base64.b64encode(jpeg+b'synthetic-export-trailer').decode()})
        with self.client.opener.open(self.client.base+'/api/photos/'+saved['photo_id']) as response:
            self.assertEqual(response.read(),jpeg)
            self.assertEqual(response.headers['Content-Type'],'image/jpeg')

    def test_multiple_photos_round_trip_without_changing_finances(self):
        photo_ids=[self.cmd('photo.save',{'content_base64':base64.b64encode(tiny_png()).decode()})['photo_id'] for _ in range(5)]
        entry=self.cmd('entry.save',self.entry(photo_id=photo_ids[0]))['entry']
        before=self.state()
        key=uuid.uuid4().hex
        payload={'id':entry['id'],'photo_ids':photo_ids}
        saved=self.cmd('entry.save',payload,key)
        self.assertEqual(saved,self.cmd('entry.save',payload,key))
        self.assertEqual(saved['entry']['photo_ids'],photo_ids)
        self.assertEqual(saved['entry']['photo_id'],photo_ids[0])
        self.client=Client(self.server.server_address[1])
        after=self.state()
        self.assertEqual(after['wallets'],before['wallets'])
        self.assertEqual(after['summary'],before['summary'])
        self.assertEqual(len(after['entries']),1)
        self.assertEqual(after['entries'][0]['photo_ids'],photo_ids)
        for photo_id in photo_ids:
            with self.client.opener.open(self.client.base+'/api/photos/'+photo_id) as response:
                self.assertEqual(response.read(),tiny_png())
        reduced=self.cmd('entry.save',{'id':entry['id'],'photo_ids':photo_ids[1:]})['entry']
        self.assertEqual(reduced['photo_ids'],photo_ids[1:])
        self.assertEqual(reduced['photo_id'],photo_ids[1])
        self.assertEqual(self.state()['wallets'],before['wallets'])

    def test_edit_delete_exchange_and_income(self):
        entry=self.cmd('entry.save',self.entry())['entry']
        self.cmd('entry.save',self.entry(id=entry['id'],currency='SGD',amount='2.00'))
        self.cmd('entry.save',self.entry(kind='income',amount='50.00',name='虚构收入'))
        self.cmd('exchange.save',{'date':self.date,'from_currency':'CNY','from_amount':'100.00','to_currency':'SGD','to_amount':'20.00'})
        self.cmd('entry.delete',{'id':entry['id']})
        state=self.state()
        self.assertEqual({w['currency']:w['balance'] for w in state['wallets']},{'CNY':'950.00','SGD':'1020.00'})
        self.assertEqual(state['summary']['expense_cny'],'0.00')
        self.assertEqual(state['summary']['income_cny'],'50.00')
        self.assertEqual(state['summary']['income_by_currency'],{'CNY':'50.00','SGD':'0.00'})

    def test_amount_filter_http_cli_validation_and_export(self):
        match=self.cmd('entry.save',self.entry(currency='SGD',amount='6.00',name='虚构精确金额'))['entry']
        self.cmd('entry.save',self.entry(currency='SGD',amount='16.00',name='虚构其他金额'))
        self.cmd('entry.save',self.entry(currency='CNY',amount='6.00',name='虚构其他币种'))
        filters={'month':'2026-08','currency':'SGD','amount':'6'}
        baseline=self.state()
        result=self.client.state(filters)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['result']['entries'],[match])
        self.assertEqual(result['result']['summary'],baseline['summary'])
        self.assertEqual(result['result']['wallets'],baseline['wallets'])
        cleared=self.client.state({'month':'2026-08','amount':''})
        self.assertEqual(cleared['result']['entries'],baseline['entries'])
        invalid=self.client.state({**filters,'amount':'6.001'})
        self.assertFalse(invalid['ok'])
        self.assertEqual(invalid['error']['code'],'invalid_amount')
        duplicate=self.client._request('/api/state?month=2026-08&amount=6&amount=16')
        self.assertFalse(duplicate['ok'])
        self.assertEqual(duplicate['error']['code'],'invalid_filter')
        args=['ledger_app.cli','--port',str(self.server.server_port),'state','--month','2026-08','--currency','SGD','--amount','6']
        output=io.StringIO()
        with patch('sys.argv',args), contextlib.redirect_stdout(output):
            self.assertEqual(cli_main(),0)
        self.assertEqual(json.loads(output.getvalue())['result']['entries'],[match])
        exported=self.cmd('export.csv',{'filters':filters})['name']
        with self.client.opener.open(self.client.base+'/api/exports/'+exported) as response:
            rows=list(csv.DictReader(io.StringIO(response.read().decode('utf-8-sig'))))
        self.assertEqual([(row['id'],row['currency'],row['amount']) for row in rows],[(str(match['id']),'SGD','6.00')])


if __name__=='__main__':unittest.main()
