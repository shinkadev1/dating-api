"""Provider contract tests plus real PostgreSQL transactions with an in-memory PSP."""
import copy
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
import httpx
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

    def test_renewal_terms(self):
        self.assertEqual(billing.renewal_terms('trial'),('week',299,7))
        self.assertEqual(billing.renewal_terms('month'),('month',999,30))
        self.assertEqual(billing.renewal_terms('optimal'),('month',999,30))
        self.assertEqual(billing.CONSENT_VERSION,'2026-11-04')


class FakeResponse:
    def __init__(self,status,body=None): self.status_code=status;self.body=body
    def json(self):
        if self.body is None: raise ValueError('no json')
        return self.body
    def raise_for_status(self):
        if self.status_code>=400: raise httpx.HTTPStatusError('x',request=None,response=self)


class ProviderErrorTests(unittest.TestCase):
    def call(self,status,method='POST',body=None):
        with patch.dict(os.environ,{'YOOKASSA_SHOP_ID':'shop','YOOKASSA_SECRET_KEY':'secret-value'}),patch.object(billing.httpx,'request',return_value=FakeResponse(status,body)):
            return billing.Provider().request(method,'payments',{'amount':1},'key')

    def test_confirmed_refusals_are_rejected_without_leaking_details(self):
        for status in (400,401,403,404,422):
            with self.subTest(status=status),self.assertLogs(level='WARNING') as logs:
                with self.assertRaises(billing.ProviderRejected) as caught: self.call(status,body={'code':'invalid_request','description':'Bad amount','secret':'secret-value'})
                self.assertNotIn('Bad amount',str(caught.exception.detail))
                text=' '.join(logs.output)
                self.assertIn('invalid_request',text);self.assertIn('Bad amount',text);self.assertNotIn('secret-value',text)

    def test_unknown_outcomes_stay_503(self):
        for status in (429,500,502,503):
            with self.subTest(status=status),self.assertRaises(HTTPException) as caught:
                self.call(status)
            self.assertEqual(caught.exception.status_code,503);self.assertNotIsInstance(caught.exception,billing.ProviderRejected)
        with patch.dict(os.environ,{'YOOKASSA_SHOP_ID':'shop','YOOKASSA_SECRET_KEY':'k'}),patch.object(billing.httpx,'request',side_effect=httpx.ConnectError('down')):
            with self.assertRaises(HTTPException) as caught: billing.Provider().request('POST','payments',{},'key')
        self.assertEqual(caught.exception.status_code,503)
        with self.assertRaises(HTTPException) as caught: self.call(404,method='GET')
        self.assertEqual(caught.exception.status_code,503)


class FakeProvider(billing.Provider):
    def __init__(self): self.payments={};self.calls=[];self.lose=False;self.reject=False;self.status='succeeded';self.saved=True
    def request(self,method,path,body=None,key=None):
        self.calls.append((method,path,copy.deepcopy(body),str(key)))
        if method=='GET': return copy.deepcopy(next(p for p in self.payments.values() if 'payments/'+p['id']==path))
        key=str(key)
        if self.reject: raise billing.ProviderRejected()
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

    def test_paid_order_syncs_bot_users_for_linked_identities(self):
        self.execute("CREATE TABLE bot_users(platform TEXT NOT NULL,user_id BIGINT NOT NULL,premium_until TIMESTAMPTZ,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(platform,user_id))")
        self.execute("CREATE TABLE premium_identities(platform TEXT NOT NULL,user_id BIGINT NOT NULL,account_id UUID NOT NULL,PRIMARY KEY(platform,user_id))")
        self.execute("INSERT INTO premium_identities VALUES('vk',1,%s),('tg',2,%s)",(self.account,self.account))
        self.execute("INSERT INTO bot_users(platform,user_id,premium_until) VALUES('tg',2,NOW()+INTERVAL '100 days')")
        order=self.create();billing.process(order['id'],self.provider)
        until=self.row('SELECT premium_until FROM premium_accounts')['premium_until']
        self.assertEqual(self.row("SELECT premium_until FROM bot_users WHERE platform='vk'")['premium_until'],until)
        self.assertGreater(self.row("SELECT premium_until FROM bot_users WHERE platform='tg'")['premium_until'],until)

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

    def test_rejected_creation_fails_order_and_allows_new_one_at_once(self):
        self.provider.reject=True;order=self.create('week')
        with self.assertRaises(billing.ProviderRejected): billing.process(order['id'],self.provider)
        row=self.row('SELECT * FROM premium_orders')
        self.assertEqual((row['status'],row['provider_status']),('failed','rejected'))
        self.assertIsNone(row['payment_id'])
        self.provider.reject=False;second=self.create('week')
        self.assertNotEqual(second['id'],order['id'])
        self.assertEqual(billing.process(second['id'],self.provider)['status'],'paid')

    def test_unknown_outcome_keeps_pending_and_retries_same_idempotence_key(self):
        order=self.create('week');self.provider.lose=True
        with self.assertRaises(HTTPException) as caught: billing.process(order['id'],self.provider)
        self.assertEqual(caught.exception.status_code,503)
        self.assertEqual(self.row('SELECT status FROM premium_orders')['status'],'pending')
        billing.process(order['id'],self.provider)
        posts=[c for c in self.provider.calls if c[0]=='POST']
        self.assertEqual(len(posts),2);self.assertEqual(posts[0][3],posts[1][3]);self.assertEqual(posts[0][3],str(order['id']))

    def test_rejected_renewal_disables_auto_renew(self):
        order=self.create();billing.process(order['id'],self.provider)
        self.execute("UPDATE premium_subscriptions SET period_end=NOW()-INTERVAL '1 hour'")
        self.provider.reject=True
        with patch.object(billing,'Provider',return_value=self.provider):billing.tick()
        sub=self.row('SELECT * FROM premium_subscriptions')
        self.assertFalse(sub['auto_renew'])
        renewal=self.row("SELECT * FROM premium_orders WHERE plan_id='week'")
        self.assertEqual((renewal['status'],renewal['provider_status']),('failed','rejected'))

    def test_cancel_clears_saved_card_and_tick_does_not_renew(self):
        order=self.create();billing.process(order['id'],self.provider)
        self.assertEqual(self.row('SELECT method_id FROM premium_subscriptions')['method_id'],'saved')
        billing.cancel(self.account)
        sub=self.row('SELECT * FROM premium_subscriptions')
        self.assertFalse(sub['auto_renew']);self.assertIsNone(sub['method_id'])
        self.execute("UPDATE premium_subscriptions SET period_end=NOW()-INTERVAL '1 hour'")
        with patch.object(billing,'Provider',return_value=self.provider):billing.tick()
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='POST']),1)

    def test_payment_for_closed_order_goes_to_manual_review(self):
        self.provider.reject=True;order=self.create('week')
        with self.assertRaises(billing.ProviderRejected): billing.process(order['id'],self.provider)
        self.provider.reject=False
        # YooKassa nevertheless created and captured the payment for the order we closed.
        self.provider.payments['late']={'id':str(uuid4()),'recipient':{'account_id':'shop'},'test':True,'amount':{'value':'299.00','currency':'RUB'},'metadata':{'order_id':str(order['id'])},'status':'succeeded','paid':True,'payment_method':{'id':'saved','saved':True}}
        with self.assertLogs(level='ERROR') as logs: billing.webhook(self.provider.payments['late']['id'],self.provider)
        row=self.row('SELECT * FROM premium_orders')
        self.assertEqual((row['status'],row['provider_status'],row['payment_id']),('failed','manual_review',self.provider.payments['late']['id']))
        self.assertIsNone(self.row('SELECT premium_until FROM premium_accounts')['premium_until'])
        self.assertNotIn('payer@example.test',''.join(logs.output))

    def test_late_payment_after_cancel_does_not_resave_card(self):
        order=self.create('week');billing.cancel(self.account)
        self.assertEqual(billing.process(order['id'],self.provider)['status'],'paid')
        sub=self.row('SELECT * FROM premium_subscriptions')
        self.assertFalse(sub['auto_renew']);self.assertIsNone(sub['method_id'])
        self.assertIsNotNone(self.row('SELECT premium_until FROM premium_accounts')['premium_until'])

    def test_consent_version_saved_and_trial_renews_as_week(self):
        order=billing.create_order(self.account,'trial',uuid4(),'x@example.test')
        self.assertEqual(self.row('SELECT consent_version FROM premium_subscriptions')['consent_version'],'2026-11-04')
        billing.process(order['id'],self.provider)
        self.assertEqual(self.row('SELECT plan_id FROM premium_subscriptions')['plan_id'],'week')
        self.execute('DELETE FROM premium_orders');self.execute('DELETE FROM premium_subscriptions');self.execute('UPDATE premium_accounts SET premium_until=NULL')
        billing.create_order(self.account,'month',uuid4(),'x@example.test',consent_version='custom')
        self.assertEqual(self.row('SELECT consent_version FROM premium_subscriptions')['consent_version'],'custom')


if __name__=='__main__':unittest.main()


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'),'TEST_DATABASE_URL required for PostgreSQL tests')
class NoRecurringTests(PostgresTests):
    """YOOKASSA_RECURRING=false: магазин без автоплатежей."""
    def setUp(self):
        super().setUp()
        self.off=patch.dict(os.environ,{'YOOKASSA_RECURRING':'false'});self.off.start()

    def tearDown(self):
        self.off.stop();super().tearDown()

    def test_one_off_payment_saves_no_card_and_extends_active_premium(self):
        order=self.create('week')
        self.assertFalse(order['payload']['save_payment_method'])
        billing.process(order['id'],self.provider)
        sub=self.row('SELECT * FROM premium_subscriptions')
        self.assertFalse(sub['auto_renew']);self.assertIsNone(sub['method_id']);self.assertIsNone(sub['card_last4'])
        first=self.row('SELECT premium_until FROM premium_accounts')['premium_until']
        again=self.create('week');billing.process(again['id'],self.provider)
        second=self.row('SELECT premium_until FROM premium_accounts')['premium_until']
        self.assertAlmostEqual((second-first).total_seconds()/86400,7,places=3)

    def test_trial_not_offered(self):
        self.assertFalse(billing.offered('trial'));self.assertTrue(billing.offered('week'))
        with self.assertRaises(HTTPException) as refused: self.create('trial')
        self.assertEqual(refused.exception.status_code,422)

    def test_tick_never_charges_previously_saved_cards(self):
        self.execute("INSERT INTO premium_subscriptions(account_id,plan_id,email,consent_version,auto_renew,method_id,period_end) VALUES(%s,'week','x@example.test','v',TRUE,'old-test-card',NOW()-INTERVAL '1 hour')",(self.account,))
        with patch.object(billing,'Provider',return_value=self.provider):billing.tick()
        self.assertEqual(self.provider.calls,[])
        self.assertEqual(self.row('SELECT count(*) AS n FROM premium_orders')['n'],0)

# Наследуем только setUp/хелперы: тесты родителя рассчитаны на включённые автоплатежи.
for _name in [n for n in vars(PostgresTests) if n.startswith('test_') and n not in vars(NoRecurringTests)]: setattr(NoRecurringTests,_name,None)


class NoRecurringPendingTests(NoRecurringTests):
    def test_pending_renewal_from_before_switch_is_closed_without_charge(self):
        self.off.stop()
        order=self.create('week');billing.process(order['id'],self.provider)
        self.execute("UPDATE premium_subscriptions SET period_end=NOW()-INTERVAL '1 hour'")
        renewal=billing.create_order(self.account,'week',uuid4(),'x@example.test',True)
        self.assertIn('payment_method_id',renewal['payload'])
        self.off.start();calls=len(self.provider.calls)
        row=billing.process(renewal['id'],self.provider)
        self.assertEqual((row['status'],row['provider_status']),('failed','rejected'))
        self.assertEqual(len(self.provider.calls),calls)
        self.assertFalse(self.row('SELECT auto_renew FROM premium_subscriptions')['auto_renew'])

for _name in [n for n in vars(NoRecurringTests) if n.startswith('test_')]: setattr(NoRecurringPendingTests,_name,None)
