"""Checkout page, consent enforcement and order/account pages through the real app on PostgreSQL."""
import copy
import os
import re
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import billing, identity
from app.main import app

FORBIDDEN = [r'(?<![\d.])(399|199|99)(?![\d])', 'urle.su', 'PCI DSS', '24/7', 'Гарантия возврата', 'Завершите за']


class FakeYooKassa:
    """Stands in for Provider.request: creates one payment per idempotence key."""
    def __init__(self): self.payments = {}; self.calls = []; self.mode = 'ok'
    def __call__(self, provider, method, path, body=None, key=None):
        self.calls.append((method, path, copy.deepcopy(body), str(key)))
        if method == 'GET': return copy.deepcopy(next(p for p in self.payments.values() if 'payments/' + p['id'] == path))
        if self.mode == 'reject': raise billing.ProviderRejected()
        if self.mode == 'unknown': raise HTTPException(503, 'Результат оплаты пока неизвестен. Повторите проверку, не создавая новый платёж.')
        key = str(key)
        self.payments.setdefault(key, {'id': str(uuid4()), 'recipient': {'account_id': 'shop'}, 'test': True, 'amount': body['amount'], 'metadata': body['metadata'], 'status': 'pending', 'paid': False,
                                       'confirmation': {'type': 'redirect', 'confirmation_url': 'https://yoomoney.example/checkout/' + key}})
        return copy.deepcopy(self.payments[key])


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'TEST_DATABASE_URL not configured')
class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.base = os.environ['TEST_DATABASE_URL']; self.schema = 'checkout_' + uuid4().hex
        with psycopg.connect(self.base, autocommit=True) as c: c.execute('CREATE SCHEMA ' + self.schema)
        self.env = patch.dict(os.environ, {'DATABASE_URL': make_conninfo(self.base, options='-c search_path=' + self.schema), 'PAY_AUTH_SECRET': 's' * 32, 'PAY_PUBLIC_URL': 'http://testserver', 'PAY_SECURE_COOKIE': 'false',
                                           'YOOKASSA_SHOP_ID': 'shop', 'YOOKASSA_SECRET_KEY': 'k', 'YOOKASSA_TEST_MODE': 'true', 'YOOKASSA_RECEIPT_MODE': 'exempt'})
        self.env.start(); billing.schema(); identity.schema()
        self.psp = FakeYooKassa()
        self.patch = patch.object(billing.Provider, 'request', lambda provider, *a, **k: self.psp(provider, *a, **k)); self.patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.patch.stop(); self.client.close(); self.env.stop()
        with psycopg.connect(self.base, autocommit=True) as c: c.execute('DROP SCHEMA ' + self.schema + ' CASCADE')

    def login(self, email='buyer@example.test'):
        with billing.connect() as c: account = c.execute('INSERT INTO premium_accounts(id,email) VALUES(%s,%s) RETURNING id', (uuid4(), email)).fetchone()['id']
        token, _ = identity.new_session(account)
        self.client.cookies.clear(); self.client.cookies.set(identity.COOKIE, token)
        return account

    def form(self, plan='week', **extra):
        csrf = re.search(r'name="csrf" value="([^"]+)"', self.client.get('/?plan=' + plan).text).group(1)
        data = {'csrf': csrf, 'plan': plan, 'request_key': str(uuid4()), 'consent_terms': 'yes', 'consent_recurring': billing.CONSENT_VERSION}
        data.update(extra)
        return {k: v for k, v in data.items() if v is not None}

    def post(self, **extra): return self.client.post('/checkout', data=self.form(**extra), follow_redirects=False)

    def row(self, query, args=()):
        with billing.connect() as c: return c.execute(query, args).fetchone()

    def test_referrer_policy_keeps_same_origin_form_posts_working(self):
        # no-referrer makes Chrome send "Origin: null" on same-site form POSTs, which csrf() rejects.
        response = self.client.get('/')
        self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')
        self.login(); data = self.form()
        self.assertEqual(self.client.post('/checkout', data=data, headers={'Origin': 'null'}, follow_redirects=False).status_code, 403)
        self.assertEqual(self.client.post('/checkout', data=self.form(), headers={'Origin': 'http://testserver'}, follow_redirects=False).status_code, 303)

    def test_trial_card_has_two_consents_disabled_button_and_real_terms(self):
        self.login()
        html = self.client.get('/?plan=trial').text
        for text in ['Заказ', 'К оплате', '1 ₽', 'Премиум начисляется сразу после подтверждения оплаты', 'Перейти к оплате →', 'Оплата через ЮKassa', 'Данные карты вводятся на стороне ЮKassa, мы их не храним', 'Visa, Mastercard, МИР',
                     'https://chatwithyou.site/policy/', 'https://chatwithyou.site/terms/', 'https://chatwithyou.site/#premium', 'support@chatwithyou.site', 'neon chat',
                     'после 3 дней — 299 ₽ каждые 7 дней автоматически до отключения', 'в любой момент, оплаченный срок сохраняется']:
            self.assertIn(text, html)
        self.assertEqual(html.count('type="checkbox"'), 2)
        self.assertIn('name="consent_terms"', html); self.assertIn('name="consent_recurring"', html)
        self.assertRegex(html, r'<button class="btn big" id="pay" disabled>Перейти к оплате →</button>')
        self.assertIn('<noscript>', html)
        for pattern in FORBIDDEN: self.assertIsNone(re.search(pattern, html), pattern)
        self.assertNotIn('<img', html); self.assertNotIn('//cdn', html)

    def test_other_plan_cards_use_own_renewal_terms(self):
        self.login()
        month = self.client.get('/?plan=month').text
        self.assertIn('999 ₽', month); self.assertIn('после 30 дней — 999 ₽ каждые 30 дней автоматически', month)
        self.assertEqual(self.client.get('/?plan=bogus').status_code, 422)

    def test_plan_list_links_to_selected_plan(self):
        self.login()
        html = self.client.get('/').text
        for plan in billing.PLANS: self.assertIn('href="/?plan=%s"' % plan, html)
        self.assertEqual(html.count('Выбрать'), len(billing.PLANS))
        self.assertNotIn('name="consent_terms"', html)
        for pattern in FORBIDDEN: self.assertIsNone(re.search(pattern, html), pattern)

    def test_login_comes_before_card(self):
        html = self.client.get('/?plan=trial').text
        self.assertIn('action="/auth/request"', html); self.assertNotIn('action="/checkout"', html)

    def test_missing_consent_is_422_and_creates_nothing(self):
        self.login()
        for field in ('consent_terms', 'consent_recurring'):
            with self.subTest(missing=field):
                response = self.post(**{field: None})
                self.assertEqual(response.status_code, 422); self.assertIn('Отметьте оба согласия', response.text)
        wrong = self.post(consent_recurring='2026-11-02')
        self.assertEqual(wrong.status_code, 422)
        self.assertIsNone(self.row('SELECT 1 FROM premium_orders'))
        self.assertEqual(self.psp.calls, [])

    def test_checkout_redirects_to_confirmation_and_saves_consent_version(self):
        account = self.login()
        response = self.post(plan='trial')
        self.assertEqual(response.status_code, 303)
        order = self.row('SELECT * FROM premium_orders')
        self.assertEqual(response.headers['location'], 'https://yoomoney.example/checkout/' + str(order['id']))
        self.assertEqual(self.row('SELECT consent_version FROM premium_subscriptions WHERE account_id=%s', (account,))['consent_version'], '2026-11-04')

    def test_without_confirmation_url_goes_to_order_page(self):
        self.login()
        with patch.object(billing, 'process', side_effect=lambda order_id, provider=None: self.row('SELECT * FROM premium_orders WHERE id=%s', (order_id,))):
            response = self.post()
        order = self.row('SELECT id FROM premium_orders')
        self.assertEqual(response.headers['location'], '/orders/' + str(order['id']))

    def test_rejection_shows_notice_and_allows_new_order(self):
        self.login(); self.psp.mode = 'reject'
        response = self.post()
        self.assertEqual(response.status_code, 409); self.assertIn('ЮKassa отклонила платёж', response.text); self.assertIn('support@chatwithyou.site', response.text)
        row = self.row('SELECT * FROM premium_orders'); self.assertEqual((row['status'], row['provider_status']), ('failed', 'rejected'))
        page = self.client.get('/orders/' + str(row['id'])).text
        self.assertIn('Отклонён ЮKassa', page); self.assertIn('Создать новый заказ', page); self.assertNotIn('Проверить оплату', page)
        self.psp.mode = 'ok'
        self.assertEqual(self.post().status_code, 303)

    def test_days_filter_declension(self):
        from app.main import plural_days
        for n, text in [(1, '1 день'), (2, '2 дня'), (3, '3 дня'), (5, '5 дней'), (11, '11 дней'), (14, '14 дней'), (21, '21 день'), (22, '22 дня'), (30, '30 дней'), (365, '365 дней')]: self.assertEqual(plural_days(n), text)

    def test_unknown_outcome_shows_notice_with_order_link_and_keeps_pending(self):
        self.login(); self.psp.mode = 'unknown'
        response = self.post()
        order = self.row('SELECT * FROM premium_orders')
        self.assertEqual(response.status_code, 503); self.assertIn('Результат оплаты пока неизвестен', response.text); self.assertIn('/orders/' + str(order['id']), response.text)
        self.assertEqual(order['status'], 'pending')
        page = self.client.get('/orders/' + str(order['id']))
        self.assertIn('Заказ №' + str(order['id'])[:8], page.text); self.assertIn('Ожидает оплаты', page.text); self.assertIn('Проверить оплату', page.text)

    def test_order_page_paid_and_manual_review(self):
        account = self.login()
        order = uuid4()
        with billing.connect() as c: c.execute("INSERT INTO premium_orders(id,account_id,amount_rub,period_hours,status,plan_id) VALUES(%s,%s,299,168,'paid','week')", (order, account))
        self.assertIn('Оплачен', self.client.get('/orders/' + str(order)).text); self.assertIn('7 дней', self.client.get('/orders/' + str(order)).text)
        with billing.connect() as c: c.execute("UPDATE premium_orders SET status='pending',provider_status='manual_review'")
        self.assertIn('Проверка поддержкой', self.client.get('/orders/' + str(order)).text)

    def test_account_shows_renewal_amount_card_status_and_no_link_codes(self):
        account = self.login()
        with billing.connect() as c:
            c.execute("INSERT INTO premium_subscriptions(account_id,plan_id,email,consent_version,method_id,period_end) VALUES(%s,'trial','buyer@example.test','2026-11-04','card',NOW()+INTERVAL '1 day')", (account,))
        html = self.client.get('/account').text
        self.assertIn('299 ₽ за 7 дней', html); self.assertIn('привязана', html)
        self.assertNotIn('link-codes', html); self.assertNotIn('/link', html)
        with billing.connect() as c: c.execute('UPDATE premium_subscriptions SET method_id=NULL')
        self.assertIn('отвязана', self.client.get('/account').text)
        self.assertIn('Отключить автопродление', self.client.get('/account').text)  # auto_renew still on
        with billing.connect() as c: c.execute('UPDATE premium_subscriptions SET auto_renew=FALSE')
        html = self.client.get('/account').text
        self.assertNotIn('Отключить автопродление', html); self.assertIn('Списаний больше не будет', html)


if __name__ == '__main__': unittest.main()
