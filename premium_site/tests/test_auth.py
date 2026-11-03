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
from app import billing, identity
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

if __name__=='__main__':unittest.main()
