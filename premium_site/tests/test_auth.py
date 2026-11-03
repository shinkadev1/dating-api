"""PostgreSQL auth and HTTP ownership checks, no emails/provider side effects."""
import os
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
import psycopg
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app import billing, identity, paylink
from app.main import app

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'),'TEST_DATABASE_URL not configured')
class AuthTests(unittest.TestCase):
    def setUp(self):
        self.base=os.environ['TEST_DATABASE_URL'];self.schema='auth_'+uuid4().hex
        with psycopg.connect(self.base,autocommit=True) as c:c.execute('CREATE SCHEMA '+self.schema)
        self.env=patch.dict(os.environ,{'DATABASE_URL':make_conninfo(self.base,options='-c search_path='+self.schema),'PAY_AUTH_SECRET':'s'*32,'PAY_PUBLIC_URL':'http://testserver','PAY_SECURE_COOKIE':'false','PREM_SERVICE_VK_TOKEN':'v'*32,'PREM_SERVICE_TG_TOKEN':'t'*32,'PREM_SERVICE_WEB_TOKEN':'w'*32})
        self.env.start();billing.schema();identity.schema();self.client=TestClient(app)
    def tearDown(self):
        self.client.close();self.env.stop()
        with psycopg.connect(self.base,autocommit=True) as c:c.execute('DROP SCHEMA '+self.schema+' CASCADE')
    def account(self,email):
        with billing.connect() as c:return c.execute('INSERT INTO premium_accounts(id,email) VALUES(%s,%s) RETURNING id',(uuid4(),email)).fetchone()['id']
    def test_http_csrf_and_foreign_order(self):
        self.assertEqual(self.client.get('/').status_code,200)
        current=identity.session(SimpleNamespace(cookies=self.client.cookies))
        self.assertEqual(self.client.post('/auth/request',data={'csrf':'wrong','email':'test@example.test'}).status_code,403)
        aid=self.account('one@example.test');other=self.account('other@example.test')
        token,_=identity.new_session(aid);self.client.cookies.clear();self.client.cookies.set(identity.COOKIE,token)
        order=uuid4()
        with billing.connect() as c:c.execute("INSERT INTO premium_orders(id,account_id,amount_rub,period_hours,status) VALUES(%s,%s,1,72,'pending')",(order,other))
        self.assertEqual(self.client.get('/orders/'+str(order)).status_code,404)
        self.assertEqual(self.client.get('/account').status_code,200)
        self.assertEqual(self.client.get('/internal/peers/vk/42/account',headers={'Authorization':'Bearer '+'t'*32}).status_code,403)
    def test_otp_wrong_attempts_commit_and_success_rotates(self):
        token,owner=identity.new_session();email='person@example.test';code='123456'
        with billing.connect() as c:c.execute("INSERT INTO premium_email_codes(session_digest,email,code_hash,expires_at) VALUES(%s,%s,%s,NOW()+INTERVAL '10 minutes')",(owner['digest'],email,identity.code_hash(owner['digest'],email,code)))
        with self.assertRaises(HTTPException):identity.verify_code(owner,'000000')
        with billing.connect() as c:self.assertEqual(c.execute('SELECT attempts FROM premium_email_codes').fetchone()['attempts'],1)
        newtoken=identity.verify_code(owner,code)
        self.assertNotEqual(token,newtoken)
        self.assertIsNone(identity.session(SimpleNamespace(cookies={identity.COOKIE:token})))
        self.assertEqual(identity.session(SimpleNamespace(cookies={identity.COOKIE:newtoken}))['email'],email)
        with self.assertRaises(HTTPException):identity.verify_code(owner,code)
    def test_link_one_use_platform_and_conflict(self):
        aid=self.account('one@example.test');other=self.account('two@example.test')
        code=identity.link_code(aid,'vk')
        with self.assertRaises(HTTPException):identity.claim('tg',42,code)
        self.assertTrue(identity.claim('vk',42,code)['linked'])
        with self.assertRaises(HTTPException):identity.claim('vk',43,code)
        second=identity.link_code(other,'vk')
        with self.assertRaises(HTTPException):identity.claim('vk',42,second)
        self.assertNotIn('email',identity.peer_status('vk',42))
    def test_otp_expiry_lockout_and_session_expiry(self):
        token,owner=identity.new_session();email='locked@example.test'
        with billing.connect() as c:c.execute("INSERT INTO premium_email_codes(session_digest,email,code_hash,expires_at) VALUES(%s,%s,%s,NOW()+INTERVAL '10 minutes')",(owner['digest'],email,identity.code_hash(owner['digest'],email,'123456')))
        for _ in range(5):
            with self.assertRaises(HTTPException):identity.verify_code(owner,'000000')
        with self.assertRaises(HTTPException):identity.verify_code(owner,'123456')
        with billing.connect() as c:c.execute("UPDATE premium_email_codes SET attempts=0,expires_at=NOW()-INTERVAL '1 second'")
        with self.assertRaises(HTTPException):identity.verify_code(owner,'123456')
        with billing.connect() as c:c.execute("UPDATE premium_sessions SET expires_at=NOW()-INTERVAL '1 second'")
        self.assertIsNone(identity.session(SimpleNamespace(cookies={identity.COOKIE:token})))
    def test_email_rate_limit_commits_and_never_logs_or_exposes_code(self):
        _,owner=identity.new_session()
        with patch.dict(os.environ,{'RESEND_API_KEY':'fake-mail-key'}):
            with patch.object(identity.httpx,'post') as sender:
                identity.request_code(owner,'payer@example.test','127.0.0.1')
                self.assertEqual(sender.call_count,1)
                with self.assertRaises(HTTPException) as caught:identity.request_code(owner,'payer@example.test','127.0.0.1')
                self.assertEqual(caught.exception.status_code,429)
                self.assertEqual(sender.call_count,1)

    def link_session(self,platform='vk',user_id=42):
        account=identity.platform_account(platform,user_id)
        token,_=identity.new_session(account,via_link=True,profile=(platform,user_id))
        return account,identity.session(SimpleNamespace(cookies={identity.COOKIE:token}))
    def give_code(self,owner,email,code='123456',attach=None):
        with billing.connect() as c:c.execute("INSERT INTO premium_email_codes(session_digest,email,code_hash,expires_at,attach_account) VALUES(%s,%s,%s,NOW()+INTERVAL '10 minutes',%s)",(owner['digest'],email,identity.code_hash(owner['digest'],email,code),attach))
        return code
    def test_link_signin_checkout_exempt_mode(self):
        account,owner=self.link_session()
        self.assertTrue(owner['via_link']);self.assertIsNone(owner['email']);self.assertEqual(identity.profile_label(owner),'VK ••••42')
        with patch.dict(os.environ,{'YOOKASSA_RECEIPT_MODE':'exempt'}):
            order=billing.create_order(account,'week',uuid4(),owner['email'],via_link=True)
        self.assertEqual(order['status'],'pending')
        with billing.connect() as c:sub=c.execute('SELECT * FROM premium_subscriptions WHERE account_id=%s',(account,)).fetchone()
        self.assertIsNone(sub['email']);self.assertTrue(sub['created_via_link'])
    def test_receipt_mode_attach_requires_confirmation(self):
        account,owner=self.link_session()
        receipt={'YOOKASSA_RECEIPT_MODE':'receipt','YOOKASSA_VAT_CODE':'1','YOOKASSA_PAYMENT_SUBJECT':'service','RESEND_API_KEY':'fake-mail-key'}
        with patch.dict(os.environ,receipt):
            with self.assertRaises(HTTPException) as caught:billing.create_order(account,'week',uuid4(),None)
            self.assertEqual(caught.exception.status_code,409)
            with patch.object(identity.httpx,'post') as sender:
                self.assertEqual(identity.request_code(owner,'new@example.test','127.0.0.1'),'confirm')
                sender.assert_not_called()
            # A code without the confirmed attach target is refused.
            with self.assertRaises(HTTPException):identity.verify_code(owner,self.give_code(owner,'new@example.test'))
            with billing.connect() as c:c.execute('DELETE FROM premium_email_codes')
            token=identity.verify_code(owner,self.give_code(owner,'new@example.test',attach=account))
            current=identity.session(SimpleNamespace(cookies={identity.COOKIE:token}))
            self.assertEqual(current['account_id'],account);self.assertEqual(current['email'],'new@example.test');self.assertFalse(current['via_link'])
            order=billing.create_order(account,'week',uuid4(),None)
        self.assertEqual(order['payload']['receipt']['customer']['email'],'new@example.test')
    def test_link_session_with_existing_email_is_refused_without_merge(self):
        target=self.account('payer@example.test')
        account,owner=self.link_session('tg',7)
        with billing.connect() as c:c.execute("UPDATE premium_accounts SET premium_until=NOW()+INTERVAL '5 days' WHERE id=%s",(account,))
        with self.assertRaises(HTTPException) as caught:identity.request_code(owner,'payer@example.test','127.0.0.1',attach=True)
        self.assertEqual(caught.exception.detail,identity.EMAIL_TAKEN)
        with self.assertRaises(HTTPException) as caught:identity.verify_code(owner,self.give_code(owner,'payer@example.test',attach=account))
        self.assertEqual(caught.exception.detail,identity.EMAIL_TAKEN)
        with billing.connect() as c:
            self.assertEqual(c.execute("SELECT account_id FROM premium_identities WHERE platform='tg' AND user_id=7").fetchone()['account_id'],account)
            self.assertIsNone(c.execute('SELECT premium_until FROM premium_accounts WHERE id=%s',(target,)).fetchone()['premium_until'])
    def test_link_token_signs_in_once(self):
        token=paylink.sign('vk',77,'k'*32)
        anonymous=SimpleNamespace(cookies={})
        with patch.dict(os.environ,{'PREMIUM_LINK_SECRET':'k'*32}):
            result,session_token=identity.link_login(anonymous,token)
            self.assertEqual(result,'ok');self.assertTrue(session_token)
            self.assertEqual(identity.link_login(anonymous,token),(identity.LINK_USED,None))
    def test_claim_merges_link_account_and_drops_saved_card(self):
        email=self.account('one@example.test')
        account,_=self.link_session('vk',51)
        with billing.connect() as c:
            c.execute("UPDATE premium_accounts SET premium_until=NOW()+INTERVAL '5 days' WHERE id=%s",(account,))
            c.execute("UPDATE premium_accounts SET premium_until=NOW()+INTERVAL '3 days' WHERE id=%s",(email,))
            c.execute("INSERT INTO premium_orders(id,account_id,amount_rub,period_hours,status) VALUES(%s,%s,1,72,'paid')",(uuid4(),account))
            c.execute("INSERT INTO premium_subscriptions(account_id,plan_id,email,consent_version,method_id,period_end) VALUES(%s,'week',NULL,'2026-11-02','card',NOW()+INTERVAL '5 days')",(account,))
        self.assertTrue(identity.claim('vk',51,identity.link_code(email,'vk'))['premium_active'])
        with billing.connect() as c:
            sub=c.execute('SELECT * FROM premium_subscriptions WHERE account_id=%s',(email,)).fetchone()
            self.assertIsNone(sub['method_id']);self.assertFalse(sub['auto_renew']);self.assertEqual(sub['email'],'one@example.test')
            days=c.execute('SELECT EXTRACT(EPOCH FROM premium_until-NOW())/86400 d FROM premium_accounts WHERE id=%s',(email,)).fetchone()['d']
            self.assertTrue(7.99<float(days)<=8.0)  # remaining terms are summed, not GREATEST
            self.assertIsNone(c.execute('SELECT 1 FROM premium_accounts WHERE id=%s',(account,)).fetchone())
    def test_claim_waits_for_pending_but_not_manual_review(self):
        email=self.account('two@example.test')
        account,_=self.link_session('vk',52)
        with billing.connect() as c:c.execute("INSERT INTO premium_orders(id,account_id,amount_rub,period_hours,status) VALUES(%s,%s,1,72,'pending')",(uuid4(),account))
        with self.assertRaises(HTTPException):identity.claim('vk',52,identity.link_code(email,'vk'))
        with billing.connect() as c:c.execute("UPDATE premium_orders SET provider_status='manual_review'")
        self.assertTrue(identity.claim('vk',52,identity.link_code(email,'vk'))['linked'])
    def test_manual_review_order_does_not_block_checkout(self):
        account,_=self.link_session('vk',60)
        with billing.connect() as c:c.execute("INSERT INTO premium_orders(id,account_id,amount_rub,period_hours,status,provider_status) VALUES(%s,%s,1,72,'pending','manual_review')",(uuid4(),account))
        with patch.dict(os.environ,{'YOOKASSA_RECEIPT_MODE':'exempt'}):
            self.assertEqual(billing.create_order(account,'week',uuid4(),None,via_link=True)['status'],'pending')
            with self.assertRaises(HTTPException):billing.create_order(account,'week',uuid4(),None,via_link=True)

if __name__=='__main__':unittest.main()
