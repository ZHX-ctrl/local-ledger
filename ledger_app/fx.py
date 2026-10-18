"""Fixed-source reference exchange rates. Never accepts or transmits ledger data."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.request import Request, build_opener, HTTPRedirectHandler

SOURCE = 'ECB via Frankfurter v2'
ENDPOINT = 'https://api.frankfurter.dev/v2/providers/ecb/rate/sgd/cny'
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


def local_today():
    return dt.datetime.now(LOCAL_TZ).date()


def easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19*a + b-d-g+15) % 30
    i, k = c // 4, c % 4
    l = (32+2*e+2*i-h-k) % 7
    m = (a+11*h+22*l) // 451
    n = h+l-7*m+114
    return dt.date(year, n // 31, n % 31+1)


def is_closing_day(day):
    """ECB TARGET calendar, not the Singapore/China holiday calendar."""
    return (day.weekday() >= 5 or (day.month, day.day) in
            {(1, 1), (5, 1), (12, 25), (12, 26)} or
            day in {easter(day.year)-dt.timedelta(days=2),
                    easter(day.year)+dt.timedelta(days=1)})


def expected_quote_date(day):
    while is_closing_day(day):
        day -= dt.timedelta(days=1)
    return day


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected exchange-rate redirect')


def _fetch(date_str):
    # Only the ISO consumption date and fixed currency pair leave this machine.
    req = Request(ENDPOINT + '?date=' + date_str,
                  headers={'Accept': 'application/json', 'User-Agent': 'LocalLedger/1.0'})
    with build_opener(_NoRedirect()).open(req, timeout=6) as response:
        if response.status != 200:
            raise ValueError('Unexpected rate response')
        raw = response.read(16385)
        if len(raw) > 16384:
            raise ValueError('Rate response too large')
    return json.loads(raw.decode('utf-8'), parse_float=Decimal)


def resolve_rate(date_str, *, fetcher=None, today=None):
    """Returns pending on missing/stale/future/malformed/unavailable quotations."""
    pending = lambda reason: {'status': 'pending', 'reason': reason}
    try:
        day = dt.date.fromisoformat(date_str)
        if day.isoformat() != date_str or day < dt.date(1999, 1, 4):
            return pending('日期不在参考汇率支持范围内')
        if day > (today or local_today()):
            return pending('未来日期尚无汇率')
        data = (fetcher or _fetch)(date_str)
        if not isinstance(data, dict) or data.get('base') != 'SGD' or data.get('quote') != 'CNY':
            return pending('汇率响应币种不符，等待重试')
        quote = dt.date.fromisoformat(data['date'])
        if quote != expected_quote_date(day):
            return pending('消费日应有的报价尚未取得')
        raw_rate = data.get('rate')
        if isinstance(raw_rate, bool):
            raise ValueError('boolean rate')
        rate = Decimal(str(raw_rate))
        if not rate.is_finite() or not Decimal('0') < rate < Decimal('1000'):
            raise ValueError('invalid rate')
        return {'status': 'resolved', 'rate': str(rate),
                'quote_date': quote.isoformat(), 'source': SOURCE}
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return pending('汇率数据未通过校验，等待重试')
    except Exception:
        # No exception/URL/proxy details in user-facing logs.
        return pending('暂时无法联网取得汇率，稍后重试')


def convert(amount_minor, rate):
    value = Decimal(str(rate))
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ValueError('Amount must be integer minor units')
    if not value.is_finite() or value <= 0:
        raise ValueError('Invalid rate')
    return int((Decimal(amount_minor)*value).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
