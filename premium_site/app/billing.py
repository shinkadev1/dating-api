"""Postgres-backed YooKassa orders. All methods are synchronous for FastAPI def handlers."""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import HTTPException

PLANS = {'trial': ('Пробный', 3, 1), 'week': ('Неделя', 7, 299), 'month': ('Месяц', 30, 999), 'year': ('Год', 365, 2999)}
ALIASES = {'start':'week','optimal':'month'}

def connect():
    dsn=os.getenv('DATABASE_URL','')
    if not dsn: raise HTTPException(503,'База оплаты не настроена')
    return psycopg.connect(dsn,autocommit=True,row_factory=dict_row)

def schema():
    with connect() as c:
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(8917123)')
            c.execute("CREATE TABLE IF NOT EXISTS premium_accounts(id UUID PRIMARY KEY,email TEXT UNIQUE NOT NULL,premium_until TIMESTAMPTZ,trial_used BOOLEAN NOT NULL DEFAULT FALSE)")
            c.execute("CREATE TABLE IF NOT EXISTS premium_orders(id UUID PRIMARY KEY,vk_user_id BIGINT,amount_rub INTEGER NOT NULL,period_hours INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','paid','failed')),created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            for name,kind in [('account_id','UUID'),('plan_id','TEXT'),('request_key','UUID'),('payment_id','TEXT'),('payload','JSONB'),('applied_at','TIMESTAMPTZ'),('provider_status',"TEXT NOT NULL DEFAULT 'pending'"),('confirmation_url','TEXT')]:
                c.execute(f'ALTER TABLE premium_orders ADD COLUMN IF NOT EXISTS {name} {kind}')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS premium_payment_id ON premium_orders(payment_id)')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS premium_request_key ON premium_orders(account_id,request_key)')
            c.execute("CREATE TABLE IF NOT EXISTS premium_subscriptions(account_id UUID PRIMARY KEY REFERENCES premium_accounts(id),plan_id TEXT NOT NULL,auto_renew BOOLEAN NOT NULL DEFAULT TRUE,trial_used BOOLEAN NOT NULL DEFAULT FALSE,method_id TEXT,period_end TIMESTAMPTZ,consent_version TEXT NOT NULL,email TEXT NOT NULL)")

class Provider:
    def request(self,method,path,body=None,key=None):
        shop=os.getenv('YOOKASSA_SHOP_ID',''); secret=os.getenv('YOOKASSA_SECRET_KEY','')
        if not shop or not secret: raise HTTPException(503,'ЮKassa не настроена')
        try:
            response=httpx.request(method,'https://api.yookassa.ru/v3/'+path,auth=(shop,secret),json=body,headers={'Idempotence-Key':str(key)} if key else {},timeout=20)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError,ValueError) as error:
            raise HTTPException(503,'Результат оплаты пока неизвестен. Повторите проверку, не создавая новый платёж.') from error

    def validate(self,payment,order):
        if (payment.get('recipient',{}).get('account_id')!=os.getenv('YOOKASSA_SHOP_ID') or
            payment.get('test') is not (os.getenv('YOOKASSA_TEST_MODE','true').lower()=='true') or
            payment.get('amount')!={'value':f"{order['amount_rub']}.00",'currency':'RUB'} or
            payment.get('metadata',{}).get('order_id')!=str(order['id']) or
            (order['payment_id'] and order['payment_id']!=payment.get('id'))):
            raise HTTPException(409,'Несоответствие данных платежа')

def payload(order,email,method=None):
    body={'amount':{'value':f"{order['amount_rub']}.00",'currency':'RUB'},'capture':True,'description':'Premium '+order['plan_id'],'metadata':{'order_id':str(order['id'])}}
    if method: body['payment_method_id']=method
    else: body.update(save_payment_method=True,confirmation={'type':'redirect','return_url':os.getenv('PAY_PUBLIC_URL','https://pay.chatwithyou.site').rstrip('/')+'/orders/'+str(order['id'])})
    mode=os.getenv('YOOKASSA_RECEIPT_MODE','')
    if mode not in ('receipt','exempt'): raise HTTPException(503,'Не настроена политика чеков магазина')
    if mode=='receipt':
        vat=os.getenv('YOOKASSA_VAT_CODE',''); subject=os.getenv('YOOKASSA_PAYMENT_SUBJECT','')
        if not vat.isdigit() or not subject: raise HTTPException(503,'Не настроены параметры чеков')
        body['receipt']={'customer':{'email':email},'items':[{'description':body['description'],'quantity':'1.00','amount':body['amount'],'vat_code':int(vat),'payment_subject':subject,'payment_mode':'full_payment'}]}
    return body

def create_order(uid,plan,key,email,renew=False):
    plan=ALIASES.get(plan,plan)
    if plan not in PLANS: raise HTTPException(422,'Неизвестный тариф')
    with connect() as c:
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(uid),))
            existing=c.execute('SELECT * FROM premium_orders WHERE account_id=%s AND request_key=%s',(uid,key)).fetchone()
            if existing:
                if existing['plan_id']!=plan: raise HTTPException(409,'Ключ использован с другим тарифом')
                return existing
            owner=c.execute('SELECT * FROM premium_accounts WHERE id=%s FOR UPDATE',(uid,)).fetchone()
            if not owner: raise HTTPException(409,'Войдите в аккаунт оплаты')
            email=owner['email']
            sub=c.execute('SELECT * FROM premium_subscriptions WHERE account_id=%s',(uid,)).fetchone()
            if c.execute("SELECT 1 FROM premium_orders WHERE account_id=%s AND status='pending'",(uid,)).fetchone(): raise HTTPException(409,'Уже есть незавершённый платёж')
            if not renew and owner['premium_until'] and owner['premium_until']>datetime.now(timezone.utc): raise HTTPException(409,'Подписка уже оплачена')
            if plan=='trial' and owner['trial_used']: raise HTTPException(409,'Пробный период уже использован')
            if renew and (not sub or not sub['auto_renew'] or not sub['method_id'] or sub['period_end']>datetime.now(timezone.utc)): return None
            order={'id':uuid4(),'plan_id':plan,'amount_rub':PLANS[plan][2]}
            body=payload(order,email,sub['method_id'] if renew else None)
            row=c.execute("INSERT INTO premium_orders(id,account_id,amount_rub,period_hours,status,plan_id,request_key,payload) VALUES(%s,%s,%s,%s,'pending',%s,%s,%s) RETURNING *",(order['id'],uid,order['amount_rub'],PLANS[plan][1]*24,plan,key,Jsonb(body))).fetchone()
            if not renew:
                c.execute("INSERT INTO premium_subscriptions(account_id,plan_id,email,consent_version) VALUES(%s,%s,%s,'2026-11-02') ON CONFLICT(account_id) DO UPDATE SET plan_id=EXCLUDED.plan_id,email=EXCLUDED.email,auto_renew=TRUE,method_id=NULL,consent_version=EXCLUDED.consent_version",(uid,plan,email))
            return row

def process(order_id,provider=None):
    provider=provider or Provider()
    with connect() as c:
        row=c.execute('SELECT * FROM premium_orders WHERE id=%s',(order_id,)).fetchone()
        if not row: raise HTTPException(404,'Счёт не найден')
        uid=row['account_id']
        if not uid: return row # Legacy unassigned invoices cannot trigger payments.
        c.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))',(str(uid),))
        try:
            row=c.execute('SELECT * FROM premium_orders WHERE id=%s',(order_id,)).fetchone()
            if row['status']!='pending' or (row['provider_status']=='manual_review' and not row['payment_id']): return row
            if not row['payload']: raise HTTPException(409,'Старый счёт не поддерживает оплату')
            if not row['payment_id'] and datetime.now(timezone.utc)-row['created_at']>timedelta(hours=23):
                c.execute("UPDATE premium_orders SET provider_status='manual_review' WHERE id=%s",(order_id,))
                c.execute('UPDATE premium_subscriptions SET auto_renew=FALSE WHERE account_id=%s',(uid,))
                return c.execute('SELECT * FROM premium_orders WHERE id=%s',(order_id,)).fetchone()
            payment=provider.request('GET','payments/'+row['payment_id']) if row['payment_id'] else provider.request('POST','payments',row['payload'],row['id'])
            provider.validate(payment,row)
            status=payment.get('status')
            if status not in ('pending','waiting_for_capture','succeeded','canceled'): raise HTTPException(409,'Неизвестный статус оплаты')
            with c.transaction():
                c.execute('UPDATE premium_orders SET payment_id=%s,provider_status=%s,confirmation_url=COALESCE(%s,confirmation_url) WHERE id=%s',(payment['id'],status,payment.get('confirmation',{}).get('confirmation_url'),order_id))
                if status=='succeeded' and not row['applied_at']:
                    if payment.get('paid') is not True: raise HTTPException(409,'Платёж не оплачен')
                    until=c.execute("UPDATE premium_accounts SET premium_until=GREATEST(COALESCE(premium_until,NOW()),NOW())+(%s * INTERVAL '1 hour'),trial_used=trial_used OR %s WHERE id=%s RETURNING premium_until",(row['period_hours'],row['plan_id']=='trial',uid)).fetchone()['premium_until']
                    method=payment.get('payment_method',{}); saved=method.get('saved') is True and bool(method.get('id'))
                    c.execute("UPDATE premium_subscriptions SET period_end=%s,plan_id=%s,trial_used=trial_used OR %s,method_id=%s,auto_renew=auto_renew AND %s WHERE account_id=%s",(until,'week' if row['plan_id']=='trial' else row['plan_id'],row['plan_id']=='trial',method.get('id') if saved else None,saved,uid))
                    c.execute("UPDATE premium_orders SET status='paid',applied_at=NOW() WHERE id=%s",(order_id,))
                elif status=='canceled':
                    c.execute("UPDATE premium_orders SET status='failed' WHERE id=%s",(order_id,))
                    c.execute('UPDATE premium_subscriptions SET auto_renew=FALSE WHERE account_id=%s',(uid,))
            return c.execute('SELECT * FROM premium_orders WHERE id=%s',(order_id,)).fetchone()
        finally: c.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(str(uid),))

def tick():
    from uuid import uuid5,NAMESPACE_URL
    with connect() as c:
        pending=c.execute("SELECT id FROM premium_orders WHERE status='pending' AND payload IS NOT NULL").fetchall()
    for row in pending:
        try: process(row['id'])
        except HTTPException: pass
    with connect() as c:
        due=c.execute('SELECT * FROM premium_subscriptions WHERE auto_renew AND method_id IS NOT NULL AND period_end<=NOW()').fetchall()
    for sub in due:
        try:
            key=uuid5(NAMESPACE_URL,f"premium:{sub['account_id']}:{sub['period_end'].isoformat()}")
            order=create_order(sub['account_id'],sub['plan_id'],key,sub['email'],True)
            if order: process(order['id'])
        except HTTPException: pass

def cancel(account_id):
    with connect() as c:
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(account_id),))
            c.execute('UPDATE premium_subscriptions SET auto_renew=FALSE WHERE account_id=%s',(account_id,))

def webhook(payment_id,provider=None):
    from uuid import UUID
    provider=provider or Provider()
    try: payment_id=str(UUID(payment_id))
    except (ValueError,TypeError,AttributeError): raise HTTPException(422,'Некорректный платёж')
    payment=provider.request('GET','payments/'+payment_id)
    if payment.get('id')!=payment_id: raise HTTPException(409,'Несоответствие ID платежа')
    try: order_id=UUID(payment.get('metadata',{}).get('order_id',''))
    except (ValueError,TypeError,AttributeError): return
    with connect() as c:
        row=c.execute('SELECT * FROM premium_orders WHERE id=%s',(order_id,)).fetchone()
        if not row or not row['account_id']: return
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(row['account_id']),))
            row=c.execute('SELECT * FROM premium_orders WHERE id=%s FOR UPDATE',(order_id,)).fetchone()
            provider.validate(payment,row)
            c.execute('UPDATE premium_orders SET payment_id=%s WHERE id=%s AND payment_id IS NULL',(payment_id,order_id))
    return process(order_id,provider)
