"""Checkout page, consent enforcement and order/account pages through the real app on PostgreSQL."""
import copy
from types import SimpleNamespace
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

    def test_guest_order_form_asks_only_email(self):
        html = self.client.get('/?plan=trial').text
        self.assertIn('action="/checkout"', html); self.assertIn('name="email"', html)
        self.assertNotIn('action="/auth/request"', html); self.assertNotIn('Получить код', html)

    def test_guest_pays_by_typed_email_without_code(self):
        response = self.post(plan='trial', email=' Guest@Example.test ')
        self.assertEqual(response.status_code, 303)
        order = self.row('SELECT * FROM premium_orders')
        account = self.row('SELECT * FROM premium_accounts WHERE id=%s', (order['account_id'],))
        self.assertEqual(account['email'], 'guest@example.test'); self.assertFalse(account['email_verified'])
        self.assertEqual(response.headers['location'], 'https://yoomoney.example/checkout/' + str(order['id']))
        # The browser is not signed in to the account, but sees the order it created.
        self.assertIsNone(identity.session(SimpleNamespace(cookies=self.client.cookies))['account_id'])
        self.assertEqual(self.client.get('/orders/' + str(order['id'])).status_code, 200)
        self.assertEqual(self.client.get('/account').status_code, 401)
        # A second press while the payment is unfinished reopens the same order.
        again = self.post(plan='trial', email='guest@example.test')
        self.assertEqual(again.headers['location'], response.headers['location'])
        # Another browser with the same email is refused with the generic text.
        with TestClient(app) as other:
            other_csrf = re.search(r'name="csrf" value="([^"]+)"', other.get('/?plan=trial').text).group(1)
            refused = other.post('/checkout', data={'csrf': other_csrf, 'plan': 'trial', 'request_key': str(uuid4()), 'consent_terms': 'yes', 'consent_recurring': billing.CONSENT_VERSION, 'email': 'guest@example.test'})
        self.assertEqual(refused.status_code, 409); self.assertIn(billing.GUEST_REFUSED, refused.text)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM premium_orders')['n'], 1)

    def test_guest_requires_valid_email(self):
        self.assertEqual(self.post(plan='trial', email='').status_code, 422)
        self.assertEqual(self.post(plan='trial', email='not-an-email').status_code, 422)
        self.assertIsNone(self.row('SELECT * FROM premium_orders'))

    def test_guest_cannot_pay_into_confirmed_email_account(self):
        self.login('verified@example.test'); self.client.cookies.clear()
        response = self.post(plan='trial', email='verified@example.test')
        self.assertEqual(response.status_code, 409); self.assertIn(billing.GUEST_REFUSED, response.text)
        self.assertIsNone(self.row('SELECT * FROM premium_orders'))
        self.assertIsNone(self.row('SELECT * FROM premium_subscriptions'))

    def test_refused_guest_order_leaves_no_account(self):
        with patch.dict(os.environ, {'YOOKASSA_RECEIPT_MODE': ''}):
            self.assertEqual(self.post(plan='trial', email='nobody@example.test').status_code, 503)
        self.assertIsNone(self.row("SELECT * FROM premium_accounts WHERE email='nobody@example.test'"))

    def test_guest_checkout_is_rate_limited_per_email(self):
        with patch.dict(os.environ, {'YOOKASSA_RECEIPT_MODE': ''}):
            codes = [self.post(plan='trial', email='spam@example.test').status_code for _ in range(6)]
        self.assertEqual(codes[:5], [503] * 5); self.assertEqual(codes[5], 429)

    def test_signing_in_by_code_confirms_a_guest_email_account(self):
        self.post(plan='week', email='later@example.test')
        account = self.row('SELECT * FROM premium_accounts')['id']
        owner = identity.session(SimpleNamespace(cookies=self.client.cookies))
        with billing.connect() as c: c.execute("INSERT INTO premium_email_codes(session_digest,email,code_hash,expires_at) VALUES(%s,'later@example.test',%s,NOW()+INTERVAL '10 minutes')", (owner['digest'], identity.code_hash(owner['digest'], 'later@example.test', '123456')))
        token = identity.verify_code(owner, '123456')
        current = identity.session(SimpleNamespace(cookies={identity.COOKIE: token}))
        self.assertEqual(current['account_id'], account)
        self.assertTrue(self.row('SELECT email_verified FROM premium_accounts')['email_verified'])

    def test_guest_cannot_replace_saved_card_or_see_foreign_orders(self):
        account = self.login('owner@example.test')
        with billing.connect() as c:
            c.execute("INSERT INTO premium_subscriptions(account_id,plan_id,email,consent_version,method_id) VALUES(%s,'week','owner@example.test','v','card-1')", (account,))
            c.execute("INSERT INTO premium_orders(id,account_id,amount_rub,period_hours,status) VALUES(%s,%s,299,168,'paid')", (uuid4(), account))
        foreign = self.row('SELECT id FROM premium_orders')['id']
        self.client.cookies.clear()
        response = self.post(plan='week', email='owner@example.test')
        self.assertEqual(response.status_code, 409); self.assertIn(billing.GUEST_REFUSED, response.text)
        self.assertEqual(self.row('SELECT method_id FROM premium_subscriptions')['method_id'], 'card-1')
        self.assertEqual(self.client.get('/orders/' + str(foreign)).status_code, 404)

    def test_guest_buyer_can_stop_renewal_from_order_page(self):
        self.post(plan='week', email='guest@example.test')
        order = self.row('SELECT * FROM premium_orders')
        with billing.connect() as c: c.execute("UPDATE premium_subscriptions SET method_id='card-9'")
        html = self.client.get('/orders/' + str(order['id'])).text
        self.assertIn('stop-renewal', html)
        csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        with TestClient(app) as other:
            other_csrf = re.search(r'name="csrf" value="([^"]+)"', other.get('/?plan=week').text).group(1)
            self.assertEqual(other.post('/orders/%s/stop-renewal' % order['id'], data={'csrf': other_csrf}).status_code, 404)
        self.assertEqual(self.client.post('/orders/%s/stop-renewal' % order['id'], data={'csrf': csrf}, follow_redirects=False).status_code, 303)
        sub = self.row('SELECT * FROM premium_subscriptions')
        self.assertFalse(sub['auto_renew']); self.assertIsNone(sub['method_id'])
        self.assertNotIn('stop-renewal', self.client.get('/orders/' + str(order['id'])).text)

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
