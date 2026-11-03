"""Server-side sessions, verified email and atomic platform linking."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi import HTTPException
from app.billing import connect

COOKIE = 'premium_session'

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def schema():
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS premium_sessions(digest TEXT PRIMARY KEY,account_id UUID REFERENCES premium_accounts(id),csrf TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS premium_email_codes(session_digest TEXT PRIMARY KEY,email TEXT NOT NULL,code_hash TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,expires_at TIMESTAMPTZ NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS premium_auth_limits(key TEXT PRIMARY KEY,count INTEGER NOT NULL,expires_at TIMESTAMPTZ NOT NULL)')
        c.execute("CREATE TABLE IF NOT EXISTS premium_identities(platform TEXT NOT NULL CHECK(platform IN ('vk','tg','web')),user_id BIGINT NOT NULL CHECK(user_id>0),account_id UUID NOT NULL REFERENCES premium_accounts(id),PRIMARY KEY(platform,user_id))")
        c.execute("CREATE TABLE IF NOT EXISTS premium_link_codes(digest TEXT PRIMARY KEY,platform TEXT NOT NULL,account_id UUID NOT NULL REFERENCES premium_accounts(id),expires_at TIMESTAMPTZ NOT NULL,used_at TIMESTAMPTZ)")

def session(request):
    token=request.cookies.get(COOKIE,'')
    if not token: return None
    with connect() as c:
        return c.execute('SELECT s.*,a.email,a.premium_until,a.trial_used FROM premium_sessions s LEFT JOIN premium_accounts a ON a.id=s.account_id WHERE s.digest=%s AND s.expires_at>NOW()',(digest(token),)).fetchone()

def new_session(account=None):
    token=secrets.token_urlsafe(32)
    with connect() as c:
        row=c.execute("INSERT INTO premium_sessions(digest,account_id,csrf,expires_at) VALUES(%s,%s,%s,NOW()+INTERVAL '30 days') RETURNING *",(digest(token),account,secrets.token_urlsafe(32))).fetchone()
    return token,row

def cookie(response,token):
    response.set_cookie(COOKIE,token,httponly=True,secure=os.getenv('PAY_SECURE_COOKIE','true').lower()=='true',samesite='lax',max_age=2592000,path='/')
    return response

def csrf(request,form,authenticated=False):
    owner=session(request)
    if not owner or (authenticated and not owner['account_id']): raise HTTPException(401,'Войдите по email')
    origin=request.headers.get('origin')
    expected=os.getenv('PAY_PUBLIC_URL','https://pay.chatwithyou.site').rstrip('/')
    if origin and origin!=expected: raise HTTPException(403,'Недопустимый источник')
    if not hmac.compare_digest(str(form),owner['csrf']): raise HTTPException(403,'Обновите страницу и повторите запрос')
    return owner

def code_hash(session_digest,email,code):
    secret=os.getenv('PAY_AUTH_SECRET','')
    if len(secret)<32: raise HTTPException(503,'Настройте PAY_AUTH_SECRET')
    return hmac.new(secret.encode(),f'{session_digest}:{email}:{code}'.encode(),hashlib.sha256).hexdigest()

def request_code(owner,email,ip):
    email=email.strip().lower()
    if len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): raise HTTPException(422,'Некорректный email')
    code=f'{secrets.randbelow(1000000):06d}'
    hashed=code_hash(owner['digest'],email,code)
    with connect() as c:
        with c.transaction():
            for key,limit,seconds in [('email:'+digest(email),1,60),('ip:'+digest(ip),10,600)]:
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
                row=c.execute('SELECT * FROM premium_auth_limits WHERE key=%s FOR UPDATE',(key,)).fetchone()
                if row and row['expires_at']>datetime.now(timezone.utc) and row['count']>=limit: raise HTTPException(429,'Слишком много запросов; попробуйте позже')
                c.execute('INSERT INTO premium_auth_limits(key,count,expires_at) VALUES(%s,1,NOW()+%s*INTERVAL \'1 second\') ON CONFLICT(key) DO UPDATE SET count=CASE WHEN premium_auth_limits.expires_at>NOW() THEN premium_auth_limits.count+1 ELSE 1 END,expires_at=CASE WHEN premium_auth_limits.expires_at>NOW() THEN premium_auth_limits.expires_at ELSE EXCLUDED.expires_at END',(key,seconds))
            c.execute("INSERT INTO premium_email_codes(session_digest,email,code_hash,expires_at) VALUES(%s,%s,%s,NOW()+INTERVAL '10 minutes') ON CONFLICT(session_digest) DO UPDATE SET email=EXCLUDED.email,code_hash=EXCLUDED.code_hash,attempts=0,expires_at=EXCLUDED.expires_at",(owner['digest'],email,hashed))
    try:
        secret=os.getenv('RESEND_API_KEY','')
        if not secret: raise ValueError('mail not configured')
        result=httpx.post('https://api.resend.com/emails',headers={'Authorization':'Bearer '+secret},json={'from':os.getenv('AUTH_MAIL_FROM','neon chat <noreply@chatwithyou.site>'),'to':[email],'subject':'Код входа в Premium','text':f'Код: {code}. Действует 10 минут.'},timeout=15)
        result.raise_for_status()
    except (ValueError,httpx.HTTPError):
        with connect() as c: c.execute('DELETE FROM premium_email_codes WHERE session_digest=%s',(owner['digest'],))
        raise HTTPException(503,'Письмо не отправлено. Проверьте настройки почты и попробуйте позже')

def verify_code(owner,code):
    failure=False
    account=None
    with connect() as c:
        with c.transaction():
            row=c.execute('SELECT * FROM premium_email_codes WHERE session_digest=%s FOR UPDATE',(owner['digest'],)).fetchone()
            if not row or row['expires_at']<=datetime.now(timezone.utc) or row['attempts']>=5: raise HTTPException(409,'Код истёк или исчерпаны попытки')
            if not hmac.compare_digest(code_hash(owner['digest'],row['email'],code),row['code_hash']):
                c.execute('UPDATE premium_email_codes SET attempts=attempts+1 WHERE session_digest=%s',(owner['digest'],))
                failure=True
            else:
                account=c.execute('INSERT INTO premium_accounts(id,email) VALUES(%s,%s) ON CONFLICT(email) DO UPDATE SET email=EXCLUDED.email RETURNING id',(uuid4(),row['email'])).fetchone()['id']
                c.execute('DELETE FROM premium_email_codes WHERE session_digest=%s',(owner['digest'],))
                c.execute('DELETE FROM premium_sessions WHERE digest=%s',(owner['digest'],))
    if failure: raise HTTPException(409,'Неверный код')
    return new_session(account)[0]

def service_auth(platform,authorization):
    name={'vk':'PREM_SERVICE_VK_TOKEN','tg':'PREM_SERVICE_TG_TOKEN','web':'PREM_SERVICE_WEB_TOKEN'}.get(platform)
    expected=os.getenv(name,'') if name else ''
    token=(authorization or '').removeprefix('Bearer ')
    configured=[os.getenv('PREM_SERVICE_'+key+'_TOKEN','') for key in ('VK','TG','WEB')]
    configured=[value for value in configured if value]
    if len(set(configured))!=len(configured): raise HTTPException(503,'Сервисные токены платформ должны различаться')
    if len(expected)<32 or not hmac.compare_digest(token,expected): raise HTTPException(403,'Доступ запрещён')

def peer_status(platform,user_id):
    with connect() as c:
        row=c.execute('SELECT a.premium_until,COALESCE(a.premium_until>NOW(),FALSE) premium_active FROM premium_identities i JOIN premium_accounts a ON a.id=i.account_id WHERE i.platform=%s AND i.user_id=%s',(platform,user_id)).fetchone()
    return {'linked':bool(row),'premium_until':row['premium_until'] if row else None,'premium_active':bool(row and row['premium_active'])}

def link_code(account,platform):
    if platform not in ('vk','tg','web'): raise HTTPException(422,'Неизвестная платформа')
    code=secrets.token_urlsafe(24)
    with connect() as c: c.execute("INSERT INTO premium_link_codes(digest,platform,account_id,expires_at) VALUES(%s,%s,%s,NOW()+INTERVAL '10 minutes')",(digest(code),platform,account))
    return code

def claim(platform,user_id,code):
    with connect() as c:
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'identity:{platform}:{user_id}',))
            row=c.execute('SELECT * FROM premium_link_codes WHERE digest=%s AND platform=%s AND used_at IS NULL AND expires_at>NOW() FOR UPDATE',(digest(code),platform)).fetchone()
            if not row: raise HTTPException(409,'Код истёк или уже использован')
            previous=c.execute('SELECT account_id FROM premium_identities WHERE platform=%s AND user_id=%s',(platform,user_id)).fetchone()
            if previous and previous['account_id']!=row['account_id']: raise HTTPException(409,'Профиль связан с другим аккаунтом')
            c.execute('INSERT INTO premium_identities(platform,user_id,account_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(platform,user_id,row['account_id']))
            c.execute('UPDATE premium_link_codes SET used_at=NOW() WHERE digest=%s',(digest(code),))
    return peer_status(platform,user_id)
