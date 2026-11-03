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
from app import paylink
from app.billing import UNFINISHED, connect, has_bot_users, sync_bot_users

COOKIE = 'premium_session'
LOCK = 8917123  # Same schema lock as billing.schema().
PLATFORM_NAMES = {'vk':'VK','tg':'Telegram','web':'сайта'}
EMAIL_TAKEN = 'Этот email уже привязан к другому аккаунту. Чтобы объединить, используйте команду /link в чате'
LINK_USED = 'used'

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def schema():
    with connect() as c, c.transaction():
        c.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,))
        c.execute('CREATE TABLE IF NOT EXISTS premium_sessions(digest TEXT PRIMARY KEY,account_id UUID REFERENCES premium_accounts(id),csrf TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS premium_email_codes(session_digest TEXT PRIMARY KEY,email TEXT NOT NULL,code_hash TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,expires_at TIMESTAMPTZ NOT NULL)')
        # Set only when the user confirmed attaching this email to the chat-link account of the session.
        c.execute('ALTER TABLE premium_email_codes ADD COLUMN IF NOT EXISTS attach_account UUID')
        c.execute('CREATE TABLE IF NOT EXISTS premium_auth_limits(key TEXT PRIMARY KEY,count INTEGER NOT NULL,expires_at TIMESTAMPTZ NOT NULL)')
        c.execute("CREATE TABLE IF NOT EXISTS premium_identities(platform TEXT NOT NULL CHECK(platform IN ('vk','tg','web')),user_id BIGINT NOT NULL CHECK(user_id>0),account_id UUID NOT NULL REFERENCES premium_accounts(id),PRIMARY KEY(platform,user_id))")
        # Sessions opened by a signed chat link may pay, but may not link other profiles.
        c.execute('ALTER TABLE premium_sessions ADD COLUMN IF NOT EXISTS via_link BOOLEAN NOT NULL DEFAULT FALSE')
        c.execute('ALTER TABLE premium_sessions ADD COLUMN IF NOT EXISTS link_platform TEXT')
        c.execute('ALTER TABLE premium_sessions ADD COLUMN IF NOT EXISTS link_user_id BIGINT')
        c.execute("CREATE TABLE IF NOT EXISTS premium_link_codes(digest TEXT PRIMARY KEY,platform TEXT NOT NULL,account_id UUID NOT NULL REFERENCES premium_accounts(id),expires_at TIMESTAMPTZ NOT NULL,used_at TIMESTAMPTZ)")
        # Chat-link tokens sign in once; rows older than the token lifetime are removed by billing.tick().
        c.execute('CREATE TABLE IF NOT EXISTS premium_link_nonces(digest TEXT PRIMARY KEY,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())')

def session(request):
    token=request.cookies.get(COOKIE,'')
    if not token: return None
    with connect() as c:
        return c.execute('SELECT s.*,a.email,a.premium_until,a.trial_used,COALESCE(a.premium_until>NOW(),FALSE) premium_active FROM premium_sessions s LEFT JOIN premium_accounts a ON a.id=s.account_id WHERE s.digest=%s AND s.expires_at>NOW()',(digest(token),)).fetchone()

def new_session(account=None,via_link=False,profile=None):
    token=secrets.token_urlsafe(32)
    platform,user_id=profile or (None,None)
    with connect() as c:
        row=c.execute("INSERT INTO premium_sessions(digest,account_id,csrf,expires_at,via_link,link_platform,link_user_id) VALUES(%s,%s,%s,NOW()+INTERVAL '30 days',%s,%s,%s) RETURNING *",(digest(token),account,secrets.token_urlsafe(32),via_link,platform,user_id)).fetchone()
    return token,row

def profile_text(platform,user_id):
    """'VK ••••1943': whom the premium is for, without the full id."""
    return f"{PLATFORM_NAMES.get(platform,platform)} ••••{str(user_id)[-4:]}"

def profile_label(owner):
    if not owner or not owner.get('via_link') or not owner.get('link_platform'): return None
    return profile_text(owner['link_platform'],owner['link_user_id'])

def link_secret():
    return os.getenv('PREMIUM_LINK_SECRET','')

def verify_link_token(token):
    """(platform,user_id) from a signed utm_content, or None: callers fall back to email login."""
    secret=link_secret()
    if len(secret)<32 or not token: return None
    try: return paylink.verify(token,secret)
    except paylink.InvalidLinkToken: return None

def consume_link_nonce(token):
    """True the first time a (verified) link token signs in, False on reuse."""
    with connect() as c:
        return c.execute('INSERT INTO premium_link_nonces(digest) VALUES(%s) ON CONFLICT DO NOTHING RETURNING digest',(hashlib.sha256(paylink.nonce(token)).hexdigest(),)).fetchone() is not None

def platform_account(platform,user_id):
    """Find or create the per-platform premium account for a chat user and sync its access."""
    with connect() as c:
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'identity:{platform}:{user_id}',))
            row=c.execute('SELECT account_id FROM premium_identities WHERE platform=%s AND user_id=%s',(platform,user_id)).fetchone()
            if row: account=row['account_id']
            else:
                account=c.execute('INSERT INTO premium_accounts(id,email) VALUES(%s,NULL) RETURNING id',(uuid4(),)).fetchone()['id']
                c.execute('INSERT INTO premium_identities(platform,user_id,account_id) VALUES(%s,%s,%s)',(platform,user_id,account))
            sync_bot_users(c,account)
    return account

def identity_account(platform,user_id):
    with connect() as c:
        row=c.execute('SELECT account_id FROM premium_identities WHERE platform=%s AND user_id=%s',(platform,user_id)).fetchone()
    return row['account_id'] if row else None

def link_login(request,token):
    """('ok', session token or None) | ('invalid', None) | ('used', None).

    Called only from the CSRF-protected POST /auth/link, never on GET: link previews (Telegram/VK
    crawlers) fetch the URL and must neither burn the single-use token nor create accounts. A token
    signs in once; reuse is reported as 'used'. The same account already signed in keeps its session."""
    claimed=verify_link_token(token)
    if not claimed: return 'invalid',None
    current=session(request)
    if current and current['account_id'] and identity_account(*claimed)==current['account_id']: return 'ok',None
    if not consume_link_nonce(token): return LINK_USED,None
    account=platform_account(*claimed)
    return 'ok',new_session(account,via_link=True,profile=claimed)[0]

def lock_accounts(c,*accounts):
    """Same per-account advisory key as billing.create_order/process; sorted to avoid deadlocks."""
    for account in sorted({str(a) for a in accounts if a}):
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(account,))

def merge_link_account(c,source,target):
    """Move an email-less chat-link account into an email account inside the caller's transaction.

    Only claim() calls this: the link code is issued by the email account and redeemed by the bot for
    the profile, which proves both sides. Remaining paid time is added to the target; orders and identities move;
    the subscription with the later period survives, but a moved one loses its saved card and
    auto-renew (the card owner never agreed to pay for the other account). Refused while the source
    has an unfinished payment (process() keys on its id)."""
    lock_accounts(c,source,target)
    if c.execute(f'SELECT 1 FROM premium_orders o WHERE o.account_id=%s AND {UNFINISHED}',(source,)).fetchone():
        raise HTTPException(409,'Дождитесь завершения платежа и повторите привязку')
    src=c.execute('SELECT * FROM premium_accounts WHERE id=%s FOR UPDATE',(source,)).fetchone()
    # Both terms were paid for: the source's remaining time is added on top of the target's term.
    c.execute("UPDATE premium_accounts SET premium_until=CASE WHEN %(src)s::timestamptz>NOW() THEN GREATEST(COALESCE(premium_until,NOW()),NOW())+(%(src)s::timestamptz-NOW()) ELSE premium_until END,trial_used=trial_used OR %(trial)s WHERE id=%(target)s",{'src':src['premium_until'],'trial':src['trial_used'],'target':target})
    subs={row['account_id']:row for row in c.execute('SELECT * FROM premium_subscriptions WHERE account_id IN (%s,%s) FOR UPDATE',(source,target)).fetchall()}
    if source in subs:
        floor=datetime.min.replace(tzinfo=timezone.utc)
        mine,theirs=subs[source],subs.get(target)
        if theirs is None or (mine['period_end'] or floor)>(theirs['period_end'] or floor):
            c.execute('DELETE FROM premium_subscriptions WHERE account_id=%s',(target,))
            c.execute('UPDATE premium_subscriptions SET account_id=%s,method_id=NULL,auto_renew=FALSE WHERE account_id=%s',(target,source))
        else: c.execute('DELETE FROM premium_subscriptions WHERE account_id=%s',(source,))
    c.execute('UPDATE premium_subscriptions s SET email=a.email FROM premium_accounts a WHERE a.id=%s AND s.account_id=a.id',(target,))
    c.execute('UPDATE premium_orders SET account_id=%s WHERE account_id=%s',(target,source))
    c.execute('UPDATE premium_identities SET account_id=%s WHERE account_id=%s',(target,source))
    c.execute('UPDATE premium_link_codes SET account_id=%s WHERE account_id=%s',(target,source))
    c.execute('DELETE FROM premium_sessions WHERE account_id=%s',(source,))
    c.execute('DELETE FROM premium_accounts WHERE id=%s',(source,))
    sync_bot_users(c,target)

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

def link_account_of(owner):
    """Email-less chat-link account of this session: email login attaches to it instead of switching."""
    return owner['account_id'] if owner and owner['account_id'] and not owner['email'] else None

def email_taken(c,email):
    return c.execute('SELECT 1 FROM premium_accounts WHERE email=%s',(email,)).fetchone() is not None

def normalize_email(email):
    email=email.strip().lower()
    if len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): raise HTTPException(422,'Некорректный email')
    return email

def request_code(owner,email,ip,attach=False):
    """Send a sign-in code. Returns 'confirm' (nothing sent) when a chat-link session must first confirm
    attaching the email to its chat profile; refuses an email that already has its own account."""
    email=normalize_email(email)
    link_account=link_account_of(owner)
    if link_account:
        with connect() as c:
            if email_taken(c,email): raise HTTPException(409,EMAIL_TAKEN)
        if not attach: return 'confirm'
    code=f'{secrets.randbelow(1000000):06d}'
    hashed=code_hash(owner['digest'],email,code)
    with connect() as c:
        with c.transaction():
            for key,limit,seconds in [('email:'+digest(email),1,60),('ip:'+digest(ip),10,600)]:
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
                row=c.execute('SELECT * FROM premium_auth_limits WHERE key=%s FOR UPDATE',(key,)).fetchone()
                if row and row['expires_at']>datetime.now(timezone.utc) and row['count']>=limit: raise HTTPException(429,'Слишком много запросов; попробуйте позже')
                c.execute('INSERT INTO premium_auth_limits(key,count,expires_at) VALUES(%s,1,NOW()+%s*INTERVAL \'1 second\') ON CONFLICT(key) DO UPDATE SET count=CASE WHEN premium_auth_limits.expires_at>NOW() THEN premium_auth_limits.count+1 ELSE 1 END,expires_at=CASE WHEN premium_auth_limits.expires_at>NOW() THEN premium_auth_limits.expires_at ELSE EXCLUDED.expires_at END',(key,seconds))
            c.execute("INSERT INTO premium_email_codes(session_digest,email,code_hash,expires_at,attach_account) VALUES(%s,%s,%s,NOW()+INTERVAL '10 minutes',%s) ON CONFLICT(session_digest) DO UPDATE SET email=EXCLUDED.email,code_hash=EXCLUDED.code_hash,attempts=0,expires_at=EXCLUDED.expires_at,attach_account=EXCLUDED.attach_account",(owner['digest'],email,hashed,link_account))
    try:
        secret=os.getenv('RESEND_API_KEY','')
        if not secret: raise ValueError('mail not configured')
        result=httpx.post('https://api.resend.com/emails',headers={'Authorization':'Bearer '+secret},json={'from':os.getenv('AUTH_MAIL_FROM','neon chat <noreply@chatwithyou.site>'),'to':[email],'subject':'Код входа в Premium','text':f'Код: {code}. Действует 10 минут.'},timeout=15)
        result.raise_for_status()
    except (ValueError,httpx.HTTPError):
        with connect() as c: c.execute('DELETE FROM premium_email_codes WHERE session_digest=%s',(owner['digest'],))
        raise HTTPException(503,'Письмо не отправлено. Проверьте настройки почты и попробуйте позже')
    return 'sent'

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
                email=row['email']
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('account-email:'+email,))
                link_account=link_account_of(owner)
                if link_account:
                    # Never merge from the web: a forwarded link must not pull its premium into the
                    # visitor's email account. Merging goes through /link in the chat (claim()).
                    if email_taken(c,email): raise HTTPException(409,EMAIL_TAKEN)
                    if row['attach_account']!=link_account: raise HTTPException(409,'Подтвердите привязку email к профилю из чата и запросите код заново')
                    lock_accounts(c,link_account)
                    c.execute('UPDATE premium_accounts SET email=%s WHERE id=%s AND email IS NULL',(email,link_account))
                    c.execute('UPDATE premium_subscriptions SET email=%s WHERE account_id=%s',(email,link_account))
                    account=link_account
                else:
                    account=c.execute('INSERT INTO premium_accounts(id,email) VALUES(%s,%s) ON CONFLICT(email) DO UPDATE SET email=EXCLUDED.email RETURNING id',(uuid4(),email)).fetchone()['id']
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
    """Same answer Chat API enforces: bot_users.premium_until (synced from the account) wins when longer."""
    with connect() as c:
        bot="(SELECT premium_until FROM bot_users WHERE platform=k.platform AND user_id=k.user_id)" if has_bot_users(c) else 'NULL::timestamptz'
        row=c.execute(f'SELECT i.account_id IS NOT NULL linked,GREATEST(a.premium_until,{bot}) premium_until FROM (SELECT %s::text platform,%s::bigint user_id) k LEFT JOIN premium_identities i ON i.platform=k.platform AND i.user_id=k.user_id LEFT JOIN premium_accounts a ON a.id=i.account_id',(platform,user_id)).fetchone()
    until=row['premium_until']
    return {'linked':row['linked'],'premium_until':until,'premium_active':bool(until and until>datetime.now(timezone.utc))}

def link_code(account,platform):
    if platform not in ('vk','tg','web'): raise HTTPException(422,'Неизвестная платформа')
    code=secrets.token_urlsafe(24)
    with connect() as c: c.execute("INSERT INTO premium_link_codes(digest,platform,account_id,expires_at) VALUES(%s,%s,%s,NOW()+INTERVAL '10 minutes')",(digest(code),platform,account))
    return code

def claim(platform,user_id,code):
    """Bot-confirmed linking: the code comes from a signed-in email account (link_code refuses chat-link
    sessions) and the bot redeems it for its own user. A profile still on its email-less chat-link
    account is merged into the email account, paid or not; any other owner is refused."""
    with connect() as c:
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'identity:{platform}:{user_id}',))
            row=c.execute('SELECT * FROM premium_link_codes WHERE digest=%s AND platform=%s AND used_at IS NULL AND expires_at>NOW() FOR UPDATE',(digest(code),platform)).fetchone()
            if not row: raise HTTPException(409,'Код истёк или уже использован')
            target=row['account_id']
            if not c.execute('SELECT 1 FROM premium_accounts WHERE id=%s AND email IS NOT NULL',(target,)).fetchone(): raise HTTPException(409,'Код выдан аккаунтом без email')
            previous=c.execute('SELECT account_id FROM premium_identities WHERE platform=%s AND user_id=%s',(platform,user_id)).fetchone()
            if previous and previous['account_id']!=target:
                source=previous['account_id']
                lock_accounts(c,source,target)
                if not c.execute('SELECT 1 FROM premium_accounts WHERE id=%s AND email IS NULL',(source,)).fetchone(): raise HTTPException(409,'Профиль связан с другим аккаунтом')
                merge_link_account(c,source,target)
            c.execute('INSERT INTO premium_identities(platform,user_id,account_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(platform,user_id,target))
            c.execute('UPDATE premium_link_codes SET used_at=NOW() WHERE digest=%s',(digest(code),))
            sync_bot_users(c,target)
    return peer_status(platform,user_id)
