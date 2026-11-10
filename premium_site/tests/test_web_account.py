"""Site cabinet: /internal/web/account[/cancel] on real PostgreSQL with an in-memory PSP."""
import copy
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import billing
from app.main import app

APP_TOKEN = 'a' * 16
WEB_TOKEN = 'w' * 32


class CardProvider(billing.Provider):
    def __init__(self, card=None, saved=True):
        self.payments = {}
        self.card = card
        self.saved = saved

    def request(self, method, path, body=None, key=None):
        if method == 'GET':
            return copy.deepcopy(next(p for p in self.payments.values() if 'payments/' + p['id'] == path))
        key = str(key)
        if key not in self.payments:
            method_info = {'id': 'saved', 'saved': self.saved}
            if self.card is not None:
                method_info['card'] = self.card
            self.payments[key] = {'id': str(uuid4()), 'recipient': {'account_id': 'shop'}, 'test': True, 'amount': body['amount'], 'metadata': body['metadata'], 'status': 'succeeded', 'paid': True, 'payment_method': method_info}
        return copy.deepcopy(self.payments[key])


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'TEST_DATABASE_URL required for PostgreSQL tests')
class WebAccountTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'premium_test_' + uuid4().hex
        self.dsn = os.environ['TEST_DATABASE_URL']
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute('CREATE SCHEMA ' + self.schema)
        self.connection_patch = patch.object(billing, 'connect', lambda: psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row, options='-c search_path=' + self.schema))
        self.connection_patch.start()
        self.env = patch.dict(os.environ, {'YOOKASSA_SHOP_ID': 'shop', 'YOOKASSA_TEST_MODE': 'true', 'YOOKASSA_RECEIPT_MODE': 'exempt',
                                           'CHAT_API_APP_TOKEN': APP_TOKEN, 'PREM_SERVICE_WEB_TOKEN': WEB_TOKEN, 'PREM_SERVICE_VK_TOKEN': 'v' * 32})
        self.env.start()
        billing.schema()
        self.account = uuid4()
        self.client = TestClient(app)
        self.auth = {'Authorization': 'Bearer ' + APP_TOKEN}
        with billing.connect() as c:
            c.execute('INSERT INTO premium_accounts(id,email) VALUES(%s,%s)', (self.account, 'payer@example.test'))

    def tearDown(self):
        self.env.stop()
        self.connection_patch.stop()
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute('DROP SCHEMA ' + self.schema + ' CASCADE')

    def row(self, query, args=()):
        with billing.connect() as c:
            return c.execute(query, args).fetchone()

    def execute(self, query, args=()):
        with billing.connect() as c:
            c.execute(query, args)

    def pay(self, plan='month', provider=None):
        order = billing.create_order(self.account, plan, uuid4(), 'x@example.test')
        return billing.process(order['id'], provider or CardProvider({'last4': '4242', 'card_type': 'Visa'}))

    def get(self, email='payer@example.test', headers=None):
        return self.client.post('/internal/web/account', json={'email': email}, headers=self.auth if headers is None else headers)

    def test_auth(self):
        for headers in ({}, {'Authorization': 'Bearer wrong-token-value-1234'}, {'Authorization': 'Bearer ' + 'v' * 32}, {'Authorization': APP_TOKEN + 'x'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.get(headers=headers).status_code, 403)
                self.assertEqual(self.client.post('/internal/web/account/cancel', json={'email': 'payer@example.test'}, headers=headers).status_code, 403)
        self.assertEqual(self.get(headers={'Authorization': 'Bearer ' + APP_TOKEN}).status_code, 200)
        self.assertEqual(self.get(headers={'Authorization': 'Bearer ' + WEB_TOKEN}).status_code, 200)
        with patch.dict(os.environ, {'CHAT_API_APP_TOKEN': 'short', 'PREM_SERVICE_WEB_TOKEN': ''}):
            self.assertEqual(self.get(headers={'Authorization': 'Bearer short'}).status_code, 403)
            self.assertEqual(self.get(headers={'Authorization': 'Bearer '}).status_code, 403)

    def test_unknown_email_is_empty_and_creates_nothing(self):
        answer = self.get('nobody@example.test')
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json(), {'email': None, 'premium_until': None, 'premium_active': False, 'subscription': None, 'payments': []})
        self.assertEqual(self.row('SELECT count(*) n FROM premium_accounts')['n'], 1)

    def test_known_account_without_subscription(self):
        body = self.get().json()
        self.assertEqual((body['email'], body['subscription'], body['payments'], body['premium_active']), ('payer@example.test', None, [], False))

    def test_email_case_insensitive(self):
        self.assertEqual(self.get('PAYER@Example.Test').json()['email'], 'payer@example.test')

    def test_active_subscription_with_card_and_next_charge(self):
        self.pay('month')
        body = self.get().json()
        sub = body['subscription']
        self.assertTrue(body['premium_active'])
        self.assertEqual((sub['plan_id'], sub['plan_name'], sub['price_rub'], sub['days']), ('month', 'Месяц', 999, 30))
        self.assertEqual((sub['auto_renew'], sub['card_attached'], sub['card_last4'], sub['card_brand']), (True, True, '4242', 'Visa'))
        self.assertEqual(sub['next_charge']['amount_rub'], 999)
        self.assertEqual(sub['next_charge']['plan_name'], 'Месяц')
        self.assertEqual(sub['next_charge']['at'], sub['period_end'])
        self.assertIsNotNone(sub['started_at'])

    def test_trial_renews_as_week(self):
        self.pay('trial')
        sub = self.get().json()['subscription']
        self.assertEqual(sub['plan_id'], 'trial')
        self.assertEqual(sub['next_charge']['amount_rub'], 299)
        self.assertEqual(sub['next_charge']['plan_name'], 'Неделя')

    def test_card_details_only_for_saved_valid_card(self):
        self.pay('month', CardProvider({'last4': '4242', 'card_type': 'M' * 50}))
        sub = self.row('SELECT * FROM premium_subscriptions')
        self.assertEqual((sub['card_last4'], sub['card_brand']), ('4242', 'M' * 32))
        for card, saved in (({'last4': '4242', 'card_type': 'Visa'}, False), ({'last4': 'abcd', 'card_type': 5}, True), ({'last4': '12345'}, True), (None, True)):
            with self.subTest(card=card, saved=saved):
                self.execute('DELETE FROM premium_orders')
                self.execute('DELETE FROM premium_subscriptions')
                self.execute('UPDATE premium_accounts SET premium_until=NULL')
                self.pay('month', CardProvider(card, saved))
                sub = self.row('SELECT * FROM premium_subscriptions')
                self.assertIsNone(sub['card_brand'])
                self.assertIsNone(sub['card_last4'])
                self.assertEqual(sub['method_id'] is not None, saved)

    def test_payments_exclude_pending_and_are_newest_first(self):
        self.pay('week')
        self.execute("UPDATE premium_orders SET created_at=NOW()-INTERVAL '3 days'")
        self.execute("UPDATE premium_accounts SET premium_until=NOW()-INTERVAL '1 hour'")
        failed = billing.create_order(self.account, 'month', uuid4(), 'x@example.test')
        self.execute("UPDATE premium_orders SET status='failed',created_at=NOW()-INTERVAL '2 days' WHERE id=%s", (failed['id'],))
        billing.create_order(self.account, 'year', uuid4(), 'x@example.test')  # stays pending
        payments = self.get().json()['payments']
        self.assertEqual([(p['plan_id'], p['status'], p['amount_rub']) for p in payments], [('month', 'failed', 999), ('week', 'paid', 299)])

    def test_cancel_keeps_access_and_is_idempotent(self):
        self.pay('month')
        until = self.row('SELECT premium_until FROM premium_accounts')['premium_until']
        for _ in range(2):
            answer = self.client.post('/internal/web/account/cancel', json={'email': 'payer@example.test'}, headers=self.auth)
            self.assertEqual(answer.status_code, 200)
            sub = answer.json()['subscription']
            self.assertEqual((sub['auto_renew'], sub['card_attached'], sub['card_last4'], sub['next_charge']), (False, False, None, None))
            self.assertTrue(answer.json()['premium_active'])
        row = self.row('SELECT * FROM premium_subscriptions')
        self.assertEqual((row['auto_renew'], row['method_id'], row['card_last4'], row['card_brand']), (False, None, None, None))
        self.assertEqual(self.row('SELECT premium_until FROM premium_accounts')['premium_until'], until)

    def test_cancel_without_subscription_is_409(self):
        for email in ('payer@example.test', 'nobody@example.test'):
            answer = self.client.post('/internal/web/account/cancel', json={'email': email}, headers=self.auth)
            self.assertEqual(answer.status_code, 409)
        self.assertEqual(self.row('SELECT count(*) n FROM premium_accounts')['n'], 1)

    def test_invalid_email_is_422(self):
        self.assertEqual(self.get('not-an-email').status_code, 422)


if __name__ == '__main__':
    unittest.main()
