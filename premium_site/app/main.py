"""Existing FastAPI/Jinja pay site, verified email and YooKassa subscription UI."""
import ipaddress
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from app import billing, identity

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
    response.headers['Referrer-Policy']='no-referrer'
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
    if not current or not current['account_id']: raise HTTPException(401,'Войдите по email на странице оплаты')
    return current

@app.get('/health')
def health(): return {'status':'ok'}

@app.get('/')
def offer(request:Request,plan:str='',utm_content:str='',link:str=''):
    selected=billing.ALIASES.get(plan,plan)
    if selected and selected not in billing.PLANS: raise HTTPException(422,'Неизвестный тариф')
    if utm_content:
        # GET only shows a "Продолжить" page: link previews (Telegram/VK crawlers) fetch this URL, so
        # nothing is consumed or created here except the anonymous session the CSRF form needs.
        # Sign-in happens on POST /auth/link. Invalid links fall back to email login.
        claimed=identity.verify_link_token(utm_content)
        if not claimed: return RedirectResponse('/',303)
        return page(request,'confirm.html',{'link_profile':identity.profile_text(*claimed),'token':utm_content,'plan':selected or ''})
    hint='Ссылка уже использована, запросите новую в чате' if link=='used' else None
    cards=plan_cards()
    chosen=next((card for card in cards if card['id']==selected),None)
    return page(request,'index.html',{'cards':cards,'chosen':chosen,'consent_version':billing.CONSENT_VERSION,'request_key':str(uuid4()),'hint':hint})

def link_redirect(plan,token):
    response=RedirectResponse('/'+('?'+urlencode({'plan':plan}) if plan else ''),303)
    return identity.cookie(response,token) if token else response

@app.post('/auth/link')
def confirm_link(request:Request,csrf:str=Form(),token:str=Form(max_length=256),plan:str=Form(default='')):
    identity.csrf(request,csrf)
    result,value=identity.link_login(request,token)
    if result=='invalid': raise HTTPException(409,'Ссылка из чата недействительна или устарела; запросите новую у бота')
    if result==identity.LINK_USED: return RedirectResponse('/?link=used',303)
    return link_redirect(billing.ALIASES.get(plan,plan) if billing.ALIASES.get(plan,plan) in billing.PLANS else '',value)

@app.post('/auth/request')
def request_code(request:Request,csrf:str=Form(),email:str=Form(),attach:str=Form(default='')):
    current=identity.csrf(request,csrf)
    try: result=identity.request_code(current,email,request.client.host if request.client else 'unknown',attach=attach=='yes')
    except HTTPException as exc:
        if exc.detail==identity.EMAIL_TAKEN: return page(request,'notice.html',{'message':exc.detail},409)
        raise
    if result=='confirm':
        # Explicit, CSRF-protected step: the email is attached to the chat profile that opened this page.
        return page(request,'attach.html',{'email':identity.normalize_email(email)})
    return page(request,'verify.html',{'message':'Код отправлен на email'})

@app.post('/auth/verify')
def verify(request:Request,csrf:str=Form(),code:str=Form()):
    current=identity.csrf(request,csrf)
    try: token=identity.verify_code(current,code.strip())
    except HTTPException as exc:
        if exc.detail==identity.EMAIL_TAKEN: return page(request,'notice.html',{'message':exc.detail},409)
        raise
    return identity.cookie(RedirectResponse('/account',303),token)

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
def checkout(request:Request,csrf:str=Form(),plan:str=Form(),request_key:UUID=Form(),consent_terms:str=Form(default=''),consent_recurring:str=Form(default='')):
    current=identity.csrf(request,csrf,True)
    # The button state in the page is only a convenience: both consents are enforced here.
    if consent_terms!='yes' or consent_recurring!=billing.CONSENT_VERSION:
        return notice(request,'Отметьте оба согласия: с политикой, условиями и тарифами, а также с автопродлением. Без них оплата невозможна.',422)
    try: order=billing.create_order(current['account_id'],plan,request_key,current['email'],via_link=current['via_link'],consent_version=consent_recurring)
    except HTTPException as exc: return notice(request,str(exc.detail),exc.status_code)
    try: order=billing.process(order['id']) or order
    except billing.ProviderRejected as exc: return notice(request,str(exc.detail),exc.status_code)
    except HTTPException as exc: return notice(request,str(exc.detail),exc.status_code,order['id'])  # The durable order stays available for reconciliation.
    if order['status']=='pending' and order['confirmation_url']: return RedirectResponse(order['confirmation_url'],303)
    return RedirectResponse('/orders/'+str(order['id']),303)

def get_order(request,order_id):
    current=owner(request)
    with billing.connect() as c: row=c.execute('SELECT * FROM premium_orders WHERE id=%s AND account_id=%s',(order_id,current['account_id'])).fetchone()
    if not row: raise HTTPException(404,'Счёт не найден')
    return row

@app.get('/orders/{order_id}')
def order_page(request:Request,order_id:UUID):
    row=get_order(request,order_id)
    return page(request,'order.html',{'order':row,'short_id':str(row['id'])[:8]})

@app.post('/orders/{order_id}/refresh')
def refresh(request:Request,order_id:UUID,csrf:str=Form()):
    identity.csrf(request,csrf,True);get_order(request,order_id)
    error=None
    try: billing.process(order_id)
    except HTTPException as exc: error=str(exc.detail)
    order=get_order(request,order_id)
    return page(request,'order.html',{'order':order,'short_id':str(order['id'])[:8],'error':error})

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
    return {'ok':True}
