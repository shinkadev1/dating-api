"""Provider contract tests plus real PostgreSQL transactions with an in-memory PSP."""
import copy
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row
from app import billing


class ProviderTests(unittest.TestCase):
    def test_rejects_cross_shop_mode_amount_and_order(self):
        order={'id':uuid4(),'amount_rub':1,'payment_id':'payment'}
        original={'id':'payment','recipient':{'account_id':'shop'},'test':True,'amount':{'value':'1.00','currency':'RUB'},'metadata':{'order_id':str(order['id'])}}
        with patch.dict(os.environ,{'YOOKASSA_SHOP_ID':'shop','YOOKASSA_TEST_MODE':'true'}):
            billing.Provider().validate(original,order)
            for key,value in [('id','other'),('recipient',{'account_id':'other'}),('test',False),('amount',{'value':'0.01','currency':'RUB'}),('metadata',{'order_id':str(uuid4())})]:
                with self.subTest(key=key),self.assertRaises(HTTPException): billing.Provider().validate(original|{key:value},order)


class FakeProvider(billing.Provider):
    def __init__(self): self.payments={};self.calls=[];self.lose=False;self.status='succeeded';self.saved=True
    def request(self,method,path,body=None,key=None):
        self.calls.append((method,path,copy.deepcopy(body),str(key)))
        if method=='GET': return copy.deepcopy(next(p for p in self.payments.values() if 'payments/'+p['id']==path))
        key=str(key)
        if key not in self.payments:
            self.payments[key]={'id':str(uuid4()),'recipient':{'account_id':'shop'},'test':True,'amount':body['amount'],'metadata':body['metadata'],'status':self.status,'paid':self.status=='succeeded','payment_method':{'id':'saved','saved':self.saved}}
        if self.lose:
            self.lose=False
            raise HTTPException(503,'Lost response after payment creation')
        return copy.deepcopy(self.payments[key])


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'),'TEST_DATABASE_URL required for PostgreSQL tests')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema='premium_test_'+uuid4().hex
        self.dsn=os.environ['TEST_DATABASE_URL']
        with psycopg.connect(self.dsn,autocommit=True) as c: c.execute('CREATE SCHEMA '+self.schema)
        self.connection_patch=patch.object(billing,'connect',lambda:psycopg.connect(self.dsn,autocommit=True,row_factory=dict_row,options='-c search_path='+self.schema))
        self.connection_patch.start()
        self.env=patch.dict(os.environ,{'YOOKASSA_SHOP_ID':'shop','YOOKASSA_TEST_MODE':'true','YOOKASSA_RECEIPT_MODE':'exempt'})
        self.env.start()
        billing.schema();billing.schema()
        self.account=uuid4();self.provider=FakeProvider()
        with billing.connect() as c: c.execute('INSERT INTO premium_accounts(id,email) VALUES(%s,%s)',(self.account,'payer@example.test'))

    def tearDown(self):
        self.env.stop();self.connection_patch.stop()
        with psycopg.connect(self.dsn,autocommit=True) as c: c.execute('DROP SCHEMA '+self.schema+' CASCADE')

    def row(self,query,args=()):
        with billing.connect() as c: return c.execute(query,args).fetchone()

    def execute(self,query,args=()):
        with billing.connect() as c: c.execute(query,args)

    def create(self,plan='trial',key=None):
        return billing.create_order(self.account,plan,key or uuid4(),'ignored@example.test')

    def test_concurrent_checkout_and_status_grant_once(self):
        key=uuid4()
        with ThreadPoolExecutor(2) as pool: orders=list(pool.map(lambda _:self.create(key=key),range(2)))
        self.assertEqual(orders[0]['id'],orders[1]['id'])
        with ThreadPoolExecutor(2) as pool: rows=list(pool.map(lambda _:billing.process(orders[0]['id'],self.provider),range(2)))
        self.assertTrue(all(row['status']=='paid' for row in rows))
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='POST']),1)
        owner=self.row('SELECT * FROM premium_accounts')
        self.assertTrue(owner['trial_used'])
        self.assertTrue(2.99<(owner['premium_until']-datetime.now(timezone.utc)).total_seconds()/86400<=3)

    def test_lost_response_retries_immutable_payload_and_key(self):
        order=self.create();self.provider.lose=True
        with self.assertRaises(HTTPException): billing.process(order['id'],self.provider)
        self.execute("UPDATE premium_accounts SET email='changed@example.test'")
        billing.process(order['id'],self.provider)
        self.assertEqual(self.provider.calls[0],self.provider.calls[1])
        self.assertEqual(len(self.provider.payments),1)

    def test_unknown_window_never_reposts_late_webhook_restores(self):
        order=self.create();self.provider.lose=True
        with self.assertRaises(HTTPException): billing.process(order['id'],self.provider)
        self.execute("UPDATE premium_orders SET created_at=NOW()-INTERVAL '25 hours'")
        row=billing.process(order['id'],self.provider)
        self.assertEqual(row['provider_status'],'manual_review')
        self.assertEqual(len(self.provider.calls),1)
        payment=next(iter(self.provider.payments.values()))
        billing.webhook(payment['id'],self.provider);billing.webhook(payment['id'],self.provider)
        self.assertEqual(self.row('SELECT status FROM premium_orders')['status'],'paid')
        self.assertFalse(self.row('SELECT auto_renew FROM premium_subscriptions')['auto_renew'])

    def test_pending_failed_and_unsaved_method(self):
        self.provider.status='pending';order=self.create()
        billing.process(order['id'],self.provider)
        self.assertIsNone(self.row('SELECT premium_until FROM premium_accounts')['premium_until'])
        payment=next(iter(self.provider.payments.values()))
        payment.update(status='succeeded',paid=True,payment_method={'id':'unsaved','saved':False})
        billing.process(order['id'],self.provider)
        self.assertFalse(self.row('SELECT auto_renew FROM premium_subscriptions')['auto_renew'])

    def test_trial_reuse_and_active_checkout_rejected(self):
        order=self.create();billing.process(order['id'],self.provider)
        with self.assertRaises(HTTPException):self.create('week')
        self.execute("UPDATE premium_accounts SET premium_until=NOW()-INTERVAL '1 hour'")
        with self.assertRaises(HTTPException):self.create()

    def test_cancel_and_concurrent_renewal(self):
        order=self.create();billing.process(order['id'],self.provider)
        self.execute("UPDATE premium_accounts SET premium_until=NOW()-INTERVAL '1 hour'")
        self.execute("UPDATE premium_subscriptions SET period_end=NOW()-INTERVAL '1 hour'")
        with patch.object(billing,'Provider',return_value=self.provider):
            with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:billing.tick(),range(2)))
        self.assertEqual(len(self.provider.payments),2)
        posts=[c for c in self.provider.calls if c[0]=='POST']
        self.assertEqual(posts[1][2]['amount'],{'value':'299.00','currency':'RUB'})
        until=self.row('SELECT premium_until FROM premium_accounts')['premium_until']
        billing.cancel(self.account)
        self.execute("UPDATE premium_subscriptions SET period_end=NOW()-INTERVAL '1 hour'")
        with patch.object(billing,'Provider',return_value=self.provider):billing.tick()
        self.assertEqual(len(self.provider.payments),2)
        self.assertEqual(until,self.row('SELECT premium_until FROM premium_accounts')['premium_until'])


if __name__=='__main__':unittest.main()
