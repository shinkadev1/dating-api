"""Postgres-backed YooKassa orders. All methods are synchronous for FastAPI def handlers."""
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import HTTPException

PLANS = {'trial': ('Пробный', 3, 1), 'week': ('Неделя', 7, 299), 'month': ('Месяц', 30, 999), 'year': ('Год', 365, 2999)}
ALIASES = {'start':'week','optimal':'month'}
# Version of the consent text shown at checkout; stored with the subscription.
CONSENT_VERSION = '2026-11-04'
# Plan that is charged after the first period when it differs from the purchased one (trial -> week).
RENEWAL = {'trial':'week'}

def recurring():
    """Автоплатежи магазина (YOOKASSA_RECURRING). Выключены: карта не сохраняется, автосписаний и пробного тарифа нет,
    продлить можно заранее — дни добавляются к сроку. На сервере по умолчанию false (deploy/update.sh)."""
    return os.getenv('YOOKASSA_RECURRING','true').strip().lower()=='true'

def offered(plan):
    """Тариф продаётся сейчас: пробный — только вместе с автопродлением."""
    return plan in PLANS and (plan!='trial' or recurring())

def renewal_terms(plan):
    """(plan_id, price in RUB, days) of the automatic charge that follows `plan`."""
    plan=ALIASES.get(plan,plan)
    renewal=RENEWAL.get(plan,plan)
    return renewal,PLANS[renewal][2],PLANS[renewal][1]

BOT_USERS_SYNC = """INSERT INTO bot_users(platform,user_id,premium_until)
SELECT i.platform,i.user_id,a.premium_until FROM premium_identities i JOIN premium_accounts a ON a.id=i.account_id
WHERE a.premium_until IS NOT NULL AND {where}
ON CONFLICT(platform,user_id) DO UPDATE SET premium_until=GREATEST(bot_users.premium_until,EXCLUDED.premium_until),updated_at=NOW()
WHERE bot_users.premium_until IS DISTINCT FROM GREATEST(bot_users.premium_until,EXCLUDED.premium_until)"""

# A pending order parked for manual review without a provider payment can no longer complete by itself.
UNFINISHED = "o.status='pending' AND NOT (o.provider_status='manual_review' AND o.payment_id IS NULL)"

def has_bot_users(c):
    return c.execute("SELECT to_regclass('bot_users') IS NOT NULL AS ok").fetchone()['ok']

def sync_bot_users(c,account_id=None):
    """Chat API grants media access from bot_users.premium_until: copy the paid term to every linked
    (platform,user_id), creating missing rows. GREATEST keeps a longer manual grant. Runs inside the
    caller's transaction so payment and access commit together."""
    if not has_bot_users(c):
        logging.warning('bot_users table missing; premium access not synced (run Chat API schema first)')
        return 0
    if account_id is None: return c.execute(BOT_USERS_SYNC.format(where='a.premium_until>NOW()')).rowcount
    return c.execute(BOT_USERS_SYNC.format(where='a.id=%s'),(account_id,)).rowcount

def connect():
    dsn=os.getenv('DATABASE_URL','')
    if not dsn: raise HTTPException(503,'База оплаты не настроена')
    return psycopg.connect(dsn,autocommit=True,row_factory=dict_row)

def schema():
    with connect() as c:
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(8917123)')
            c.execute("CREATE TABLE IF NOT EXISTS premium_accounts(id UUID PRIMARY KEY,email TEXT UNIQUE NOT NULL,premium_until TIMESTAMPTZ,trial_used BOOLEAN NOT NULL DEFAULT FALSE)")
            # Accounts created from a signed chat link have no email until email linking exists.
            c.execute('ALTER TABLE premium_accounts ALTER COLUMN email DROP NOT NULL')
            # FALSE only for accounts made by guest checkout from a typed, unconfirmed email.
            c.execute('ALTER TABLE premium_accounts ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE')
            c.execute("CREATE TABLE IF NOT EXISTS premium_orders(id UUID PRIMARY KEY,vk_user_id BIGINT,amount_rub INTEGER NOT NULL,period_hours INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','paid','failed')),created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            for name,kind in [('account_id','UUID'),('plan_id','TEXT'),('request_key','UUID'),('payment_id','TEXT'),('payload','JSONB'),('applied_at','TIMESTAMPTZ'),('provider_status',"TEXT NOT NULL DEFAULT 'pending'"),('confirmation_url','TEXT'),('created_session','TEXT')]:
                c.execute(f'ALTER TABLE premium_orders ADD COLUMN IF NOT EXISTS {name} {kind}')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS premium_payment_id ON premium_orders(payment_id)')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS premium_request_key ON premium_orders(account_id,request_key)')
            c.execute("CREATE TABLE IF NOT EXISTS premium_subscriptions(account_id UUID PRIMARY KEY REFERENCES premium_accounts(id),plan_id TEXT NOT NULL,auto_renew BOOLEAN NOT NULL DEFAULT TRUE,trial_used BOOLEAN NOT NULL DEFAULT FALSE,method_id TEXT,period_end TIMESTAMPTZ,consent_version TEXT NOT NULL,email TEXT NOT NULL)")
            c.execute('ALTER TABLE premium_subscriptions ALTER COLUMN email DROP NOT NULL')
            # A subscription started from an email session cannot be cancelled from a chat-link session.
            c.execute('ALTER TABLE premium_subscriptions ADD COLUMN IF NOT EXISTS created_via_link BOOLEAN NOT NULL DEFAULT FALSE')
            # Card shown in the site account: only the last four digits and the brand, never the full number.
            c.execute('ALTER TABLE premium_subscriptions ADD COLUMN IF NOT EXISTS card_last4 TEXT')
            c.execute('ALTER TABLE premium_subscriptions ADD COLUMN IF NOT EXISTS card_brand TEXT')
            from . import notify
            notify.schema(c)

REJECTED_TEXT='ЮKassa отклонила платёж. Проверьте данные и попробуйте оформить заказ заново; если ошибка повторится, напишите в поддержку support@chatwithyou.site.'

class ProviderRejected(HTTPException):
    """YooKassa definitively refused to create a payment (HTTP 4xx other than 429): nothing exists on its side."""
    def __init__(self): super().__init__(409,REJECTED_TEXT)  # 4xx: a Cloudflare tunnel may replace 5xx bodies with its own page

class Provider:
    def request(self,method,path,body=None,key=None):
        shop=os.getenv('YOOKASSA_SHOP_ID',''); secret=os.getenv('YOOKASSA_SECRET_KEY','')
        if not shop or not secret: raise HTTPException(503,'ЮKassa не настроена')
        try:
            response=httpx.request(method,'https://api.yookassa.ru/v3/'+path,auth=(shop,secret),json=body,headers={'Idempotence-Key':str(key)} if key else {},timeout=20)
            if method=='POST' and response.status_code in (400,401,403,404,422):
                try: info=response.json()
                except ValueError: info={}
                if not isinstance(info,dict): info={}
                # Only the response code/description are logged: never keys, email or the request body.
                logging.warning('YooKassa rejected payment creation: http=%s code=%s description=%s',response.status_code,info.get('code'),info.get('description'))
                raise ProviderRejected()
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
    else: body.update(save_payment_method=recurring(),confirmation={'type':'redirect','return_url':os.getenv('PAY_PUBLIC_URL','https://pay.chatwithyou.site').rstrip('/')+'/orders/'+str(order['id'])})
    mode=os.getenv('YOOKASSA_RECEIPT_MODE','')
    if mode not in ('receipt','exempt'): raise HTTPException(503,'Не настроена политика чеков магазина')
    if mode=='receipt':
        vat=os.getenv('YOOKASSA_VAT_CODE',''); subject=os.getenv('YOOKASSA_PAYMENT_SUBJECT','')
        if not vat.isdigit() or not subject: raise HTTPException(503,'Не настроены параметры чеков')
        if not email: raise HTTPException(409,'Для чека нужен email: укажите его в форме заказа')
        body['receipt']={'customer':{'email':email},'items':[{'description':body['description'],'quantity':'1.00','amount':body['amount'],'vat_code':int(vat),'payment_subject':subject,'payment_mode':'full_payment'}]}
    return body

class OpenOrderOtherPlan(HTTPException):
    """This browser already has an unfinished payment for another tariff."""
    def __init__(self,order_id):
        super().__init__(409,'У вас есть незавершённый заказ на другой тариф. Завершите или отмените его оплату, затем оформите новый.')
        self.order_id=order_id

# One answer for every refusal of a guest: it must not reveal whether someone else's email has an account.
GUEST_REFUSED='Оплатить на этот email сейчас не получится. Укажите другой email или напишите на support@chatwithyou.site.'

def create_order(uid,plan,key,email,renew=False,via_link=False,consent_version=CONSENT_VERSION,session_digest=None,guest_email=None):
    """guest_email: checkout without sign-in. The order goes to the account of that unconfirmed email,
    created here in the same transaction (so a refused order leaves nothing behind); an account whose
    email was confirmed by a code is never paid into by a guest."""
    plan=ALIASES.get(plan,plan)
    if plan not in PLANS: raise HTTPException(422,'Неизвестный тариф')
    if not renew and not offered(plan): raise HTTPException(422,'Этот тариф сейчас недоступен')
    guest=bool(guest_email)
    def refuse(text): raise HTTPException(409,GUEST_REFUSED if guest else text)
    with connect() as c:
        with c.transaction():
            if guest:
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('account-email:'+guest_email,))
                found=c.execute('SELECT id,email_verified FROM premium_accounts WHERE email=%s',(guest_email,)).fetchone()
                if found and found['email_verified']: refuse('')
                uid=found['id'] if found else c.execute('INSERT INTO premium_accounts(id,email,email_verified) VALUES(%s,%s,FALSE) RETURNING id',(uuid4(),guest_email)).fetchone()['id']
            if session_digest and not renew:
                # The same browser pressing "pay" again (e.g. back from ЮKassa) gets its unfinished order back.
                mine=c.execute(f'SELECT o.* FROM premium_orders o WHERE o.account_id=%s AND o.created_session=%s AND {UNFINISHED} ORDER BY o.created_at DESC LIMIT 1',(uid,session_digest)).fetchone()
                if mine and mine['plan_id']==plan: return mine
                if mine: raise OpenOrderOtherPlan(mine['id'])
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(uid),))
            existing=c.execute('SELECT * FROM premium_orders WHERE account_id=%s AND request_key=%s',(uid,key)).fetchone()
            if existing:
                if existing['plan_id']!=plan: raise HTTPException(409,'Ключ использован с другим тарифом')
                return existing
            owner=c.execute('SELECT * FROM premium_accounts WHERE id=%s FOR UPDATE',(uid,)).fetchone()
            if not owner: raise HTTPException(409,'Войдите в аккаунт оплаты')
            email=owner['email'] or email  # A chat-link account has no email: the one typed at checkout goes to the receipt.
            sub=c.execute('SELECT * FROM premium_subscriptions WHERE account_id=%s',(uid,)).fetchone()
            if c.execute(f'SELECT 1 FROM premium_orders o WHERE o.account_id=%s AND {UNFINISHED}',(uid,)).fetchone(): refuse('Уже есть незавершённый платёж')
            # Без автопродления премиум продлевают заранее: оплаченные дни прибавляются к текущему сроку.
            if not renew and recurring() and owner['premium_until'] and owner['premium_until']>datetime.now(timezone.utc): refuse('Подписка уже оплачена')
            if plan=='trial' and owner['trial_used']: refuse('Пробный период уже использован')
            if renew and (not sub or not sub['auto_renew'] or not sub['method_id'] or sub['period_end']>datetime.now(timezone.utc)): return None
            # An unverified buyer must not replace the saved card of someone else's subscription.
            if not renew and guest and sub and sub['method_id']: refuse('')
            if not renew and via_link and sub and sub['method_id']: raise HTTPException(409,'У подписки сохранена карта. Изменить тариф можно после входа по email')
            order={'id':uuid4(),'plan_id':plan,'amount_rub':PLANS[plan][2]}
            body=payload(order,email,sub['method_id'] if renew else None)
            row=c.execute("INSERT INTO premium_orders(id,account_id,amount_rub,period_hours,status,plan_id,request_key,payload,created_session) VALUES(%s,%s,%s,%s,'pending',%s,%s,%s,%s) RETURNING *",(order['id'],uid,order['amount_rub'],PLANS[plan][1]*24,plan,key,Jsonb(body),session_digest)).fetchone()
            if not renew:
                c.execute("INSERT INTO premium_subscriptions(account_id,plan_id,email,consent_version,created_via_link,auto_renew) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(account_id) DO UPDATE SET plan_id=EXCLUDED.plan_id,email=EXCLUDED.email,auto_renew=EXCLUDED.auto_renew,method_id=NULL,card_last4=NULL,card_brand=NULL,consent_version=EXCLUDED.consent_version,created_via_link=premium_subscriptions.created_via_link AND EXCLUDED.created_via_link",(uid,plan,email,consent_version,via_link,recurring()))
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
            if row['status']=='failed' and row['payment_id'] and row['provider_status'] not in ('manual_review','canceled'):
                # YooKassa created a payment for an order we already closed: never grant silently, flag it for support.
                payment=provider.request('GET','payments/'+row['payment_id'])
                provider.validate(payment,row)
                if payment.get('status')=='succeeded' and payment.get('paid') is True:
                    logging.error('Payment succeeded for a closed order, manual review needed: order=%s payment=%s',row['id'],row['payment_id'])
                    c.execute("UPDATE premium_orders SET provider_status='manual_review' WHERE id=%s",(order_id,))
                    row=c.execute('SELECT * FROM premium_orders WHERE id=%s',(order_id,)).fetchone()
                return row
            if row['status']!='pending' or (row['provider_status']=='manual_review' and not row['payment_id']): return row
            if not row['payload']: raise HTTPException(409,'Старый счёт не поддерживает оплату')
            if not row['payment_id'] and datetime.now(timezone.utc)-row['created_at']>timedelta(hours=23):
                c.execute("UPDATE premium_orders SET provider_status='manual_review' WHERE id=%s",(order_id,))
                c.execute('UPDATE premium_subscriptions SET auto_renew=FALSE WHERE account_id=%s',(uid,))
                return c.execute('SELECT * FROM premium_orders WHERE id=%s',(order_id,)).fetchone()
            if not row['payment_id'] and not recurring() and 'payment_method_id' in row['payload']:
                # Продление, созданное до выключения автоплатежей: по сохранённой карте не списываем.
                c.execute("UPDATE premium_orders SET status='failed',provider_status='rejected' WHERE id=%s",(order_id,))
                c.execute('UPDATE premium_subscriptions SET auto_renew=FALSE WHERE account_id=%s',(uid,))
                return c.execute('SELECT * FROM premium_orders WHERE id=%s',(order_id,)).fetchone()
            try: payment=provider.request('GET','payments/'+row['payment_id']) if row['payment_id'] else provider.request('POST','payments',row['payload'],row['id'])
            except ProviderRejected:
                # Confirmed refusal at creation: no payment exists, so the order is final and a new one may be made.
                renewal='payment_method_id' in row['payload']
                with c.transaction():
                    c.execute("UPDATE premium_orders SET status='failed',provider_status='rejected' WHERE id=%s",(order_id,))
                    if renewal: c.execute('UPDATE premium_subscriptions SET auto_renew=FALSE WHERE account_id=%s',(uid,))
                raise
            provider.validate(payment,row)
            status=payment.get('status')
            if status not in ('pending','waiting_for_capture','succeeded','canceled'): raise HTTPException(409,'Неизвестный статус оплаты')
            with c.transaction():
                c.execute('UPDATE premium_orders SET payment_id=%s,provider_status=%s,confirmation_url=COALESCE(%s,confirmation_url) WHERE id=%s',(payment['id'],status,payment.get('confirmation',{}).get('confirmation_url'),order_id))
                if status=='succeeded' and not row['applied_at']:
                    if payment.get('paid') is not True: raise HTTPException(409,'Платёж не оплачен')
                    until=c.execute("UPDATE premium_accounts SET premium_until=GREATEST(COALESCE(premium_until,NOW()),NOW())+(%s * INTERVAL '1 hour'),trial_used=trial_used OR %s WHERE id=%s RETURNING premium_until",(row['period_hours'],row['plan_id']=='trial',uid)).fetchone()['premium_until']
                    method=payment.get('payment_method',{}); saved=method.get('saved') is True and bool(method.get('id'))
                    card=method.get('card'); card=card if isinstance(card,dict) else {}
                    last4=card.get('last4'); last4=last4 if isinstance(last4,str) and re.fullmatch(r'\d{4}',last4) else None
                    brand=card.get('card_type'); brand=brand[:32] if isinstance(brand,str) and brand else None
                    c.execute("UPDATE premium_subscriptions SET period_end=%s,plan_id=%s,trial_used=trial_used OR %s,method_id=CASE WHEN auto_renew AND %s THEN %s ELSE NULL END,card_last4=CASE WHEN auto_renew AND %s THEN %s ELSE NULL END,card_brand=CASE WHEN auto_renew AND %s THEN %s ELSE NULL END,auto_renew=auto_renew AND %s WHERE account_id=%s",(until,renewal_terms(row['plan_id'])[0],row['plan_id']=='trial',saved,method.get('id') if saved else None,saved,last4,saved,brand,saved,uid))
                    c.execute("UPDATE premium_orders SET status='paid',applied_at=NOW() WHERE id=%s",(order_id,))
                    sync_bot_users(c,uid)
                    from . import notify
                    notify.enqueue(c,uid,until,'payment_method_id' in row['payload'])  # «Премиум активен» в бот, тем же коммитом
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
        with c.transaction(): sync_bot_users(c)  # Repairs access if a sync was ever missed.
        # Used chat-link nonces only matter while a token can still be valid (24h + clock skew).
        if c.execute("SELECT to_regclass('premium_link_nonces') IS NOT NULL AS ok").fetchone()['ok']:
            c.execute("DELETE FROM premium_link_nonces WHERE created_at<NOW()-INTERVAL '25 hours'")
        # Без автоплатежей магазина не списываем, в том числе по картам, сохранённым раньше (например, в тестовом магазине).
        due=c.execute('SELECT * FROM premium_subscriptions WHERE auto_renew AND method_id IS NOT NULL AND period_end<=NOW()').fetchall() if recurring() else []
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
            c.execute('UPDATE premium_subscriptions SET auto_renew=FALSE,method_id=NULL,card_last4=NULL,card_brand=NULL WHERE account_id=%s',(account_id,))

def plan_info(plan_id):
    plan_id=ALIASES.get(plan_id,plan_id)
    plan=PLANS.get(plan_id)
    return plan_id,(plan[0] if plan else plan_id),(plan[2] if plan else None),(plan[1] if plan else None)

def web_account(email):
    """Read-only account state for the site cabinet. Never creates anything; unknown email -> empty answer."""
    from .identity import normalize_email
    email=normalize_email(email)
    with connect() as c:
        acc=c.execute('SELECT id,email,premium_until FROM premium_accounts WHERE email=%s',(email,)).fetchone()
        if not acc: return {'email':None,'premium_until':None,'premium_active':False,'subscription':None,'payments':[]}
        until=acc['premium_until']
        result={'email':acc['email'],'premium_until':until.isoformat() if until else None,'premium_active':bool(until and until>datetime.now(timezone.utc)),'subscription':None,'payments':[]}
        sub=c.execute('SELECT * FROM premium_subscriptions WHERE account_id=%s',(acc['id'],)).fetchone()
        if sub:
            last=c.execute("SELECT plan_id FROM premium_orders WHERE account_id=%s AND status='paid' ORDER BY applied_at DESC NULLS LAST,created_at DESC LIMIT 1",(acc['id'],)).fetchone()
            first=c.execute("SELECT applied_at FROM premium_orders WHERE account_id=%s AND status='paid' AND NOT (COALESCE(payload,'{}'::jsonb) ? 'payment_method_id') ORDER BY applied_at DESC NULLS LAST,created_at DESC LIMIT 1",(acc['id'],)).fetchone()
            plan_id,plan_name,price,days=plan_info(last['plan_id'] if last and last['plan_id'] else sub['plan_id'])
            nxt=None
            renews=recurring() and bool(sub['auto_renew'])
            if renews and sub['method_id'] and sub['period_end']:
                rid,amount,_=renewal_terms(sub['plan_id'])
                nxt={'at':sub['period_end'].isoformat(),'amount_rub':amount,'plan_name':PLANS[rid][0]}
            result['subscription']={'plan_id':plan_id,'plan_name':plan_name,'price_rub':price,'days':days,
                'auto_renew':renews,'card_attached':renews and bool(sub['method_id']),'card_last4':sub['card_last4'] if renews else None,'card_brand':sub['card_brand'] if renews else None,
                'period_end':sub['period_end'].isoformat() if sub['period_end'] else None,
                'started_at':first['applied_at'].isoformat() if first and first['applied_at'] else None,'next_charge':nxt}
        rows=c.execute("SELECT created_at,plan_id,amount_rub,status FROM premium_orders WHERE account_id=%s AND status IN ('paid','failed') ORDER BY created_at DESC LIMIT 100",(acc['id'],)).fetchall()
        for r in rows:
            pid,name,_,_=plan_info(r['plan_id'])
            result['payments'].append({'created_at':r['created_at'].isoformat(),'plan_id':pid,'plan_name':name,'amount_rub':r['amount_rub'],'status':r['status']})
        return result

def web_cancel(email):
    from .identity import normalize_email
    email=normalize_email(email)
    with connect() as c:
        row=c.execute('SELECT a.id FROM premium_accounts a JOIN premium_subscriptions s ON s.account_id=a.id WHERE a.email=%s',(email,)).fetchone()
    if not row: raise HTTPException(409,'Нет подписки для отмены')
    cancel(row['id'])
    return web_account(email)

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
