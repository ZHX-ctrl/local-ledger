import datetime as dt
import unittest
from unittest.mock import patch
from ledger_app.fx import resolve_rate, convert, expected_quote_date, _fetch, ENDPOINT


class FXTests(unittest.TestCase):
    def quote(self, requested, actual, rate='5.271'):
        return resolve_rate(requested, today=dt.date(2026,9,12), fetcher=lambda d:
            {'date':actual,'base':'SGD','quote':'CNY','rate':rate})

    def test_dates_and_weekend(self):
        self.assertEqual(self.quote('2026-08-10','2026-08-10')['rate'],'5.271')
        self.assertEqual(self.quote('2026-08-15','2026-08-14','5.2744')['quote_date'],'2026-08-14')
        self.assertEqual(self.quote('2026-08-10','2026-08-07')['status'],'pending')
        self.assertEqual(self.quote('2026-08-15','2026-08-13')['status'],'pending')

    def test_closing_dates(self):
        for requested,actual in [('2026-04-03','2026-04-02'),('2026-04-06','2026-04-02'),
                                 ('2026-05-01','2026-04-30'),('2026-12-26','2026-12-24')]:
            self.assertEqual(expected_quote_date(dt.date.fromisoformat(requested)).isoformat(),actual)

    def test_future_and_bad_schema(self):
        self.assertEqual(self.quote('2026-08-10','2026-08-11')['status'],'pending')
        self.assertEqual(self.quote('2027-01-01','2026-12-31')['status'],'pending')
        for rate in ['NaN','Infinity','0','-1',True,'oops']:
            self.assertEqual(self.quote('2026-08-10','2026-08-10',rate)['status'],'pending')
        self.assertEqual(resolve_rate('2026-08-10',fetcher=lambda d:[])['status'],'pending')

    def test_offline_and_today_unpublished(self):
        def offline(d): raise OSError('offline')
        self.assertEqual(resolve_rate('2026-08-10',fetcher=offline)['status'],'pending')
        result=resolve_rate('2026-09-11',today=dt.date(2026,9,11),fetcher=lambda d:
            {'date':'2026-09-10','base':'SGD','quote':'CNY','rate':'5.2'})
        self.assertEqual(result['status'],'pending')

    def test_rounding(self):
        self.assertEqual(convert(1,'5.5'),6)
        self.assertEqual(convert(550,'5.271'),2899)

    def test_outbound_only_date_currency(self):
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,n): return b'{"date":"2026-08-10","base":"SGD","quote":"CNY","rate":5.271}'
        with patch('ledger_app.fx.build_opener') as opener:
            opener.return_value.open.return_value=Response()
            _fetch('2026-08-10')
            req=opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, ENDPOINT+'?date=2026-08-10')
            self.assertIsNone(req.data)


if __name__=='__main__': unittest.main()
