"""Existing FastAPI/Jinja pay site, verified email and YooKassa subscription UI."""
import ipaddress
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from app import billing, identity, notify

APP_DIR=Path(__file__).resolve().parent
app=FastAPI(title='Premium')
app.mount('/static',StaticFiles(directory=APP_DIR/'static'),name='static')
templates=Jinja2Templates(directory=APP_DIR/'templates')

def format_time(value):
    return value.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC') if value else ''

def plural_days(value):
    n=int(value); tail=n%100
    if 11<=tail<=14: word='дней'
    elif n%10==1: word='день'
    elif 2<=n%10<=4: word='дня'
    else: word='дней'
    return f'{n} {word}'

templates.env.filters['dt']=format_time
templates.env.filters['days']=plural_days

@app.on_event('startup')
def prepare_database():
    if os.getenv('DATABASE_URL'): billing.schema(); identity.schema()

@app.middleware('http')
async def private_pages(request,call_next):
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    # same-origin, not no-referrer: with no-referrer Chrome sends "Origin: null" on same-site form POSTs
    # and identity.csrf() rejects every form. Cross-origin (ЮKassa, links out) still gets no referrer.
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Content-Type-Options']='nosniff'
    return response

def page(request,name,context=None,status=200):
    owner=identity.session(request)
    token=None
    if not owner: token,owner=identity.new_session()
    response=templates.TemplateResponse(request,name,{'owner':owner,'csrf':owner['csrf'],'profile':identity.profile_label(owner),'year':datetime.now(timezone.utc).year,**(context or {})},status_code=status)
    return identity.cookie(response,token) if token else response

def notice(request,message,status,order_id=None):
    """Human-readable error page that keeps the response code of the failure."""
    return page(request,'notice.html',{'message':message,'order_id':order_id},status)

def plan_cards():
    cards=[]
    for plan_id,(name,days,price) in billing.PLANS.items():
        renewal_id,renewal_price,renewal_days=billing.renewal_terms(plan_id)
        cards.append({'id':plan_id,'name':name,'days':days,'price':price,'renewal_price':renewal_price,'renewal_days':renewal_days,'trial':renewal_id!=plan_id})
    return cards

def owner(request):
    current=identity.session(request)
    if not current or not current['account_id']: raise HTTPException(401,'Откройте страницу оплаты по ссылке из бота VK или Telegram')
    return current

@app.get('/health')
def health(): return {'status':'ok'}

@app.get('/')
def offer(request:Request,plan:str='',utm_content:str='',link:str=''):
    selected=billing.ALIASES.get(plan,plan)
    if selected and selected not in billing.PLANS: raise HTTPException(422,'Неизвестный тариф')
    if utm_content:
        # Links issued before /checkout/<token> existed keep working.
        if len(utm_content)>256: return RedirectResponse('/?link=expired',303)
        return RedirectResponse('/checkout/'+quote(utm_content,safe='')+('?'+urlencode({'plan':selected}) if selected else ''),303)
    hint=LINK_HINTS.get(link)
    cards=plan_cards()
    chosen=next((card for card in cards if card['id']==selected),None)
    return page(request,'index.html',{'cards':cards,'chosen':chosen,'consent_version':billing.CONSENT_VERSION,'request_key':str(uuid4()),'hint':hint})

LINK_HINTS={'used':'Ссылка уже использована, запросите новую в чате','expired':'Ссылка из чата устарела, запросите новую в чате'}

@app.get('/checkout/{token}')
def link_offer(request:Request,token:str,plan:str=''):
    """Personal link from the VK/Telegram bot: tariffs and the order form for that chat profile.

    GET only shows the page: link previews (Telegram/VK crawlers) fetch this URL, so nothing is consumed
    or created here except the anonymous session the CSRF form needs. The single-use token signs in on
    POST /checkout, together with the order, and the premium goes to that chat profile."""
    claimed=identity.verify_link_token(token) if len(token)<=256 else None
    if not claimed: return RedirectResponse('/?link=expired',303)
    selected=billing.ALIASES.get(plan,plan)
    if selected and selected not in billing.PLANS: raise HTTPException(422,'Неизвестный тариф')
    current=identity.session(request)
    mine=bool(current and current['account_id'] and identity.identity_account(*claimed)==current['account_id'])
    leaving=bool(current and current['account_id'] and not mine)
    # Read-only check: a link already used in another browser cannot pay; nothing is consumed here.
    if not mine and identity.link_nonce_used(token): return RedirectResponse('/?link=used',303)
    cards=plan_cards()
    chosen=next((card for card in cards if card['id']==selected),None)
    return page(request,'index.html',{'cards':cards,'chosen':chosen,'consent_version':billing.CONSENT_VERSION,'request_key':str(uuid4()),
                                      'link_token':token,'profile':identity.profile_text(*claimed),'leaving':leaving})

def known_plan(plan):
    """Normalized plan id from a form or query, or '' when it is absent or unknown."""
    plan=billing.ALIASES.get(plan,plan)
    return plan if plan in billing.PLANS else ''

def link_redirect(plan,token):
    response=RedirectResponse('/'+('?'+urlencode({'plan':plan}) if plan else ''),303)
    return identity.cookie(response,token) if token else response

@app.post('/auth/link')
def confirm_link(request:Request,csrf:str=Form(),token:str=Form(max_length=256),plan:str=Form(default='')):
    identity.csrf(request,csrf)
    result,value=identity.link_login(request,token)
    if result=='invalid': raise HTTPException(409,'Ссылка из чата недействительна или устарела; запросите новую у бота')
    if result==identity.LINK_USED: return RedirectResponse('/?link=used',303)
    return link_redirect(known_plan(plan),value)

@app.post('/auth/request')
def request_code(request:Request,csrf:str=Form(),email:str=Form(),attach:str=Form(default=''),plan:str=Form(default='')):
    current=identity.csrf(request,csrf)
    plan=known_plan(plan)  # The chosen tariff survives the email step so the buyer lands on its order form.
    try: result=identity.request_code(current,email,request.client.host if request.client else 'unknown',attach=attach=='yes')
    except HTTPException as exc:
        if exc.detail==identity.EMAIL_TAKEN: return page(request,'notice.html',{'message':exc.detail},409)
        raise
    if result=='confirm':
        # Explicit, CSRF-protected step: the email is attached to the chat profile that opened this page.
        return page(request,'attach.html',{'email':identity.normalize_email(email),'plan':plan})
    return page(request,'verify.html',{'message':'Код отправлен на email','plan':plan})

@app.post('/auth/verify')
def verify(request:Request,csrf:str=Form(),code:str=Form(),plan:str=Form(default='')):
    current=identity.csrf(request,csrf)
    try: token=identity.verify_code(current,code.strip())
    except HTTPException as exc:
        if exc.detail==identity.EMAIL_TAKEN: return page(request,'notice.html',{'message':exc.detail},409)
        raise
    plan=known_plan(plan)
    return identity.cookie(RedirectResponse('/?'+urlencode({'plan':plan}) if plan else '/account',303),token)

@app.post('/auth/logout')
def logout(request:Request,csrf:str=Form()):
    current=identity.csrf(request,csrf)
    with billing.connect() as c: c.execute('DELETE FROM premium_sessions WHERE digest=%s',(current['digest'],))
    result=RedirectResponse('/',303);result.delete_cookie(identity.COOKIE,path='/');return result

@app.get('/account')
def account(request:Request):
    current=owner(request)
    with billing.connect() as c:
        subscription=c.execute('SELECT * FROM premium_subscriptions WHERE account_id=%s',(current['account_id'],)).fetchone()
        orders=c.execute('SELECT * FROM premium_orders WHERE account_id=%s ORDER BY created_at DESC LIMIT 20',(current['account_id'],)).fetchall()
        links=c.execute('SELECT platform,user_id FROM premium_identities WHERE account_id=%s',(current['account_id'],)).fetchall()
    renewal=None
    if subscription:
        renewal_id,price,days=billing.renewal_terms(subscription['plan_id'])
        renewal={'name':billing.PLANS[renewal_id][0],'price':price,'days':days}
    return page(request,'account.html',{'subscription':subscription,'orders':orders,'links':links,'can_cancel':can_cancel(current,subscription),'renewal':renewal})

def can_cancel(current,subscription):
    """A chat-link session may not cancel a subscription that was started after email sign-in."""
    return bool(subscription) and (not current['via_link'] or subscription['created_via_link'])

@app.post('/account/link-codes')
def link_code(request:Request,csrf:str=Form(),platform:str=Form()):
    current=identity.csrf(request,csrf,True)
    if current['via_link'] or not current['email']: raise HTTPException(403,'Войдите по email, чтобы привязать другие профили')
    code=identity.link_code(current['account_id'],platform)
    return page(request,'code.html',{'code':code,'platform':platform})

@app.post('/subscription/cancel')
def cancel(request:Request,csrf:str=Form()):
    current=identity.csrf(request,csrf,True)
    with billing.connect() as c: subscription=c.execute('SELECT * FROM premium_subscriptions WHERE account_id=%s',(current['account_id'],)).fetchone()
    if subscription and not can_cancel(current,subscription): raise HTTPException(403,'Отключите автопродление после входа по email')
    billing.cancel(current['account_id'])
    return RedirectResponse('/account',303)

@app.post('/checkout')
def checkout(request:Request,csrf:str=Form(),plan:str=Form(),request_key:UUID=Form(),consent_terms:str=Form(default=''),consent_recurring:str=Form(default=''),email:str=Form(default='',max_length=254),token:str=Form(default='',max_length=256)):
    current=identity.csrf(request,csrf)
    # The button state in the page is only a convenience: both consents are enforced here.
    if consent_terms!='yes' or consent_recurring!=billing.CONSENT_VERSION:
        return notice(request,'Отметьте оба согласия: с политикой, условиями и тарифами, а также с автопродлением. Без них оплата невозможна.',422)
    signed=None
    if token:
        # Personal link from the bot: sign in to that chat profile now, so the payment credits it.
        result,signed=identity.link_login(request,token)
        if result=='invalid': return notice(request,'Ссылка из чата недействительна или устарела; запросите новую у бота',409)
        if result==identity.LINK_USED: return notice(request,LINK_HINTS['used'],409)
        if signed: current=identity.session(SimpleNamespace(cookies={identity.COOKIE:signed}))
    response=place_order(request,current,plan,request_key,consent_recurring,email)
    return identity.cookie(response,signed) if signed else response

def place_order(request,current,plan,request_key,consent_recurring,email):
    guest=not current['account_id']
    try:
        # A guest only types an email, no code: the purchase goes to the account of that email,
        # while this browser stays signed out and sees just the orders it created.
        typed=identity.normalize_email(email) if guest or email.strip() else None
        if guest: identity.guest_checkout_allowed(typed,request.client.host if request.client else 'unknown')
        order=billing.create_order(current['account_id'],plan,request_key,current['email'] or typed,via_link=guest or current['via_link'],consent_version=consent_recurring,session_digest=current['digest'],guest_email=typed if guest else None)
    except HTTPException as exc: return notice(request,str(exc.detail),exc.status_code,getattr(exc,'order_id',None))
    try: order=billing.process(order['id']) or order
    except billing.ProviderRejected as exc: return notice(request,str(exc.detail),exc.status_code)
    except HTTPException as exc: return notice(request,str(exc.detail),exc.status_code,order['id'])  # The durable order stays available for reconciliation.
    if order['status']=='pending' and order['confirmation_url']: return RedirectResponse(order['confirmation_url'],303)
    return RedirectResponse('/orders/'+str(order['id']),303)

def get_order(request,order_id):
    """An order of the signed-in account, or one this browser session created (guest checkout by email)."""
    current=identity.session(request)
    if not current: raise HTTPException(404,'Счёт не найден')
    with billing.connect() as c: row=c.execute('SELECT * FROM premium_orders WHERE id=%s AND (account_id=%s OR created_session=%s)',(order_id,current['account_id'],current['digest'])).fetchone()
    if not row: raise HTTPException(404,'Счёт не найден')
    return row

def renewal_owner(current,order):
    """A guest may switch off renewal only from the session that bought the account's latest subscription."""
    if current['account_id'] or order['created_session']!=current['digest']: return False
    with billing.connect() as c:
        # Once the email owner confirmed it with a code, the account is theirs to manage.
        if c.execute('SELECT email_verified FROM premium_accounts WHERE id=%s',(order['account_id'],)).fetchone()['email_verified']: return False
        latest=c.execute("SELECT created_session FROM premium_orders WHERE account_id=%s AND NOT (payload ? 'payment_method_id') ORDER BY created_at DESC LIMIT 1",(order['account_id'],)).fetchone()
        sub=c.execute('SELECT auto_renew FROM premium_subscriptions WHERE account_id=%s',(order['account_id'],)).fetchone()
    return bool(latest and latest['created_session']==current['digest'] and sub and sub['auto_renew'])

def order_view(request,order,error=None):
    current=identity.session(request)
    return page(request,'order.html',{'order':order,'short_id':str(order['id'])[:8],'error':error,'can_stop_renewal':bool(current) and renewal_owner(current,order)})

@app.get('/orders/{order_id}')
def order_page(request:Request,order_id:UUID):
    return order_view(request,get_order(request,order_id))

@app.post('/orders/{order_id}/refresh')
def refresh(request:Request,order_id:UUID,csrf:str=Form()):
    identity.csrf(request,csrf);get_order(request,order_id)
    error=None
    try: billing.process(order_id)
    except HTTPException as exc: error=str(exc.detail)
    notify.drain_quietly(limit=5)
    return order_view(request,get_order(request,order_id),error)

@app.post('/orders/{order_id}/stop-renewal')
def stop_renewal(request:Request,order_id:UUID,csrf:str=Form()):
    current=identity.csrf(request,csrf);order=get_order(request,order_id)
    if not renewal_owner(current,order): raise HTTPException(403,'Отключить продление может только тот, кто оформил подписку')
    billing.cancel(order['account_id'])
    return RedirectResponse('/orders/'+str(order_id),303)

class Claim(BaseModel):
    code:str=Field(min_length=20,max_length=128)

@app.get('/internal/peers/{platform}/{user_id}/account')
def status(platform:str,user_id:int,authorization:str|None=Header(default=None)):
    identity.service_auth(platform,authorization)
    if user_id<=0: raise HTTPException(422,'Некорректный ID')
    return identity.peer_status(platform,user_id)

@app.post('/internal/peers/{platform}/{user_id}/account-link')
def claim(platform:str,user_id:int,body:Claim,authorization:str|None=Header(default=None)):
    identity.service_auth(platform,authorization)
    if user_id<=0: raise HTTPException(422,'Некорректный ID')
    return identity.claim(platform,user_id,body.code)

class WebEmail(BaseModel):
    email:str=Field(max_length=254)

@app.post('/internal/web/account')
def web_account(body:WebEmail,authorization:str|None=Header(default=None)):
    identity.web_service_auth(authorization)
    return billing.web_account(identity.normalize_email(body.email))

@app.post('/internal/web/account/cancel')
def web_account_cancel(body:WebEmail,authorization:str|None=Header(default=None)):
    identity.web_service_auth(authorization)
    return billing.web_cancel(identity.normalize_email(body.email))

class Notification(BaseModel):
    type:str
    event:str
    object:dict

def webhook_networks():
    """Optional allowlist from YOOKASSA_WEBHOOK_IPS (comma-separated IPs/CIDRs). Empty = off: the webhook
    only triggers a re-read of the payment from the YooKassa API, so a forged call grants nothing."""
    networks=[]
    for item in os.getenv('YOOKASSA_WEBHOOK_IPS','').split(','):
        if item.strip(): networks.append(ipaddress.ip_network(item.strip(),strict=False))
    return networks

def webhook_allowed(host):
    networks=webhook_networks()
    if not networks: return True
    try: address=ipaddress.ip_address(host)
    except ValueError: return False
    return any(address in network for network in networks)

@app.post('/webhooks/yookassa')
def webhook(request:Request,body:Notification):
    if not webhook_allowed(request.client.host if request.client else ''): raise HTTPException(403,'Недопустимый источник уведомления')
    if body.type!='notification' or body.event not in ('payment.succeeded','payment.canceled','payment.waiting_for_capture'): return {'ok':True}
    try: payment_id=UUID(str(body.object.get('id')))
    except ValueError: raise HTTPException(422,'Некорректный платёж')
    billing.webhook(str(payment_id))
    notify.drain_quietly(limit=5)
    return {'ok':True}
