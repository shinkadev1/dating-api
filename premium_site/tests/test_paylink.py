"""Signed chat links and bot_users sync without a database (fake connections / patched identity)."""
import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app import billing, identity, paylink
from app.main import app

SECRET = 'k' * 32
CHAT_API_MODULE = Path(__file__).resolve().parents[2] / 'chat_api' / 'app' / 'premium_link.py'


class PaylinkTests(unittest.TestCase):
    def test_roundtrip_and_rejections(self):
        token = paylink.sign('tg', 123456789, SECRET, now=10_000)
        self.assertNotIn('123456789', token)
        self.assertEqual(paylink.verify(token, SECRET, now=10_001), ('tg', 123456789))
        for bad, secret, now in [(token, 'z' * 32, 10_001), (token, SECRET, 10_000 + paylink.TTL_SECONDS), (token[:-1], SECRET, 10_001), ('garbage', SECRET, 10_001)]:
            with self.subTest(bad=bad[:10], now=now), self.assertRaises(paylink.InvalidLinkToken):
                paylink.verify(bad, secret, now=now)

    @unittest.skipUnless(CHAT_API_MODULE.is_file(), 'chat_api not in this checkout')
    def test_interoperates_with_chat_api_signer(self):
        spec = importlib.util.spec_from_file_location('chat_api_premium_link', CHAT_API_MODULE)
        other = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(other)
        nonce = b'\x01' * 12
        self.assertEqual(other.sign('vk', 7, SECRET, now=50, nonce=nonce), paylink.sign('vk', 7, SECRET, now=50, nonce=nonce))
        self.assertEqual(paylink.verify(other.sign('vk', 7, SECRET), SECRET), ('vk', 7))

    def test_verify_link_token_needs_long_secret(self):
        token = paylink.sign('vk', 5, SECRET)
        with patch.dict(os.environ, {'PREMIUM_LINK_SECRET': SECRET}):
            self.assertEqual(identity.verify_link_token(token), ('vk', 5))
            self.assertIsNone(identity.verify_link_token(token + 'x'))
        with patch.dict(os.environ, {'PREMIUM_LINK_SECRET': 'short'}):
            self.assertIsNone(identity.verify_link_token(token))


class FakeCursor:
    def __init__(self, row=None, rowcount=0): self.row = row; self.rowcount = rowcount
    def fetchone(self): return self.row


class FakeConnection:
    def __init__(self, has_table=True): self.has_table = has_table; self.calls = []
    def execute(self, query, args=None):
        self.calls.append((query, args))
        if 'to_regclass' in query: return FakeCursor({'ok': self.has_table})
        return FakeCursor(rowcount=2)


class SyncTests(unittest.TestCase):
    def test_sync_one_account_upserts_with_greatest(self):
        c = FakeConnection(); account = uuid4()
        self.assertEqual(billing.sync_bot_users(c, account), 2)
        query, args = c.calls[-1]
        self.assertIn('INSERT INTO bot_users(platform,user_id,premium_until)', query)
        self.assertIn('ON CONFLICT(platform,user_id) DO UPDATE SET premium_until=GREATEST(', query)
        self.assertIn('a.id=%s', query)
        self.assertEqual(args, (account,))

    def test_sync_all_active_and_missing_table(self):
        c = FakeConnection()
        billing.sync_bot_users(c)
        self.assertIn('a.premium_until>NOW()', c.calls[-1][0])
        self.assertIsNone(c.calls[-1][1])
        missing = FakeConnection(has_table=False)
        with self.assertLogs(level='WARNING'):
            self.assertEqual(billing.sync_bot_users(missing), 0)
        self.assertEqual(len(missing.calls), 1)


class LinkLoginTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'PREMIUM_LINK_SECRET': SECRET, 'PAY_SECURE_COOKIE': 'false'})
        self.env.start(); self.client = TestClient(app)
    def tearDown(self):
        self.client.close(); self.env.stop()

    def test_valid_link_sets_session_and_strips_token(self):
        account = uuid4()
        with patch.object(identity, 'platform_account', return_value=account) as found, \
             patch.object(identity, 'session', return_value=None), \
             patch.object(identity, 'new_session', return_value=('session-token', {})) as created:
            response = self.client.get('/', params={'utm_source': 'vk', 'utm_campaign': 'premium', 'utm_content': paylink.sign('vk', 9, SECRET), 'plan': 'week'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/?plan=week')
        self.assertIn(identity.COOKIE + '=session-token', response.headers['set-cookie'])
        found.assert_called_once_with('vk', 9)
        created.assert_called_once_with(account, via_link=True)

    def test_same_account_keeps_session(self):
        account = uuid4()
        with patch.object(identity, 'identity_account', return_value=account), \
             patch.object(identity, 'platform_account') as found, \
             patch.object(identity, 'session', return_value={'account_id': account}), \
             patch.object(identity, 'new_session') as created:
            response = self.client.get('/', params={'utm_content': paylink.sign('tg', 9, SECRET)}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertNotIn('set-cookie', response.headers)
        created.assert_not_called()
        found.assert_not_called()

    def test_other_signed_in_account_needs_csrf_confirmation(self):
        current = {'account_id': uuid4(), 'email': 'a@example.test', 'via_link': False, 'csrf': 'good', 'premium_until': None}
        token = paylink.sign('vk', 9, SECRET)
        with patch.object(identity, 'identity_account', return_value=uuid4()), \
             patch.object(identity, 'session', return_value=current), \
             patch.object(identity, 'platform_account') as found, \
             patch.object(identity, 'new_session') as created:
            page = self.client.get('/', params={'utm_content': token}, follow_redirects=False)
            self.assertEqual(page.status_code, 200)
            self.assertIn('Войти по ссылке из чата ВКонтакте?', page.text)
            self.assertIn('action="/auth/link"', page.text)
            found.assert_not_called(); created.assert_not_called()
            forged = self.client.post('/auth/link', data={'csrf': 'bad', 'token': token}, follow_redirects=False)
            self.assertEqual(forged.status_code, 403)
            found.assert_not_called()
            account = uuid4(); found.return_value = account; created.return_value = ('switched', {})
            ok = self.client.post('/auth/link', data={'csrf': 'good', 'token': token, 'plan': 'month'}, follow_redirects=False)
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(ok.headers['location'], '/?plan=month')
        self.assertIn(identity.COOKIE + '=switched', ok.headers['set-cookie'])
        created.assert_called_once_with(account, via_link=True)

    def test_invalid_link_falls_back_without_account(self):
        with patch.object(identity, 'platform_account') as found:
            response = self.client.get('/', params={'utm_content': paylink.sign('vk', 9, 'other' * 8)}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/')
        self.assertNotIn('set-cookie', response.headers)
        found.assert_not_called()

    def test_link_session_cannot_issue_link_codes(self):
        owner = {'account_id': uuid4(), 'email': None, 'via_link': True, 'csrf': 'c'}
        with patch.object(identity, 'csrf', return_value=owner), patch.object(identity, 'link_code') as code:
            response = self.client.post('/account/link-codes', data={'csrf': 'c', 'platform': 'tg'})
        self.assertEqual(response.status_code, 403)
        code.assert_not_called()

    def test_link_session_cannot_cancel_email_subscription(self):
        from app.main import can_cancel
        link = {'via_link': True}; email = {'via_link': False}
        self.assertFalse(can_cancel(link, {'created_via_link': False}))
        self.assertTrue(can_cancel(link, {'created_via_link': True}))
        self.assertTrue(can_cancel(email, {'created_via_link': False}))
        self.assertFalse(can_cancel(email, None))


class WebhookAllowlistTests(unittest.TestCase):
    def test_off_when_empty_and_cidr_match(self):
        from app.main import webhook_allowed
        with patch.dict(os.environ, {'YOOKASSA_WEBHOOK_IPS': ''}):
            self.assertTrue(webhook_allowed('203.0.113.9'))
        with patch.dict(os.environ, {'YOOKASSA_WEBHOOK_IPS': '198.51.100.0/27, 2001:db8::/32'}):
            self.assertTrue(webhook_allowed('198.51.100.7'))
            self.assertTrue(webhook_allowed('2001:db8::1'))
            self.assertFalse(webhook_allowed('198.51.100.40'))
            self.assertFalse(webhook_allowed('testclient'))

    def test_rejected_before_provider_call(self):
        with patch.dict(os.environ, {'YOOKASSA_WEBHOOK_IPS': '198.51.100.0/27'}), patch.object(billing, 'webhook') as handler:
            with TestClient(app) as client:
                response = client.post('/webhooks/yookassa', json={'type': 'notification', 'event': 'payment.succeeded', 'object': {'id': str(uuid4())}})
        self.assertEqual(response.status_code, 403)
        handler.assert_not_called()


if __name__ == '__main__': unittest.main()
