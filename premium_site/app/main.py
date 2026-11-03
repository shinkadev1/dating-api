"""Existing FastAPI/Jinja pay site, verified email and YooKassa subscription UI."""
import os
from pathlib import Path
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

def page(request,name,context=None):
    owner=identity.session(request)
    token=None
    if not owner: token,owner=identity.new_session()
    response=templates.TemplateResponse(request,name,{'owner':owner,'csrf':owner['csrf'],**(context or {})})
    return identity.cookie(response,token) if token else response

def owner(request):
    current=identity.session(request)
    if not current or not current['account_id']: raise HTTPException(401,'Войдите по email на странице оплаты')
    return current

@app.get('/health')
def health(): return {'status':'ok'}

@app.get('/')
def offer(request:Request,plan:str=''):
    selected=billing.ALIASES.get(plan,plan)
    if selected and selected not in billing.PLANS: raise HTTPException(422,'Неизвестный тариф')
    return page(request,'index.html',{'plans':billing.PLANS,'selected':selected,'request_key':str(uuid4())})

@app.post('/auth/request')
def request_code(request:Request,csrf:str=Form(),email:str=Form()):
    current=identity.csrf(request,csrf)
    identity.request_code(current,email,request.client.host if request.client else 'unknown')
    return page(request,'verify.html',{'message':'Код отправлен на email'})

@app.post('/auth/verify')
def verify(request:Request,csrf:str=Form(),code:str=Form()):
    current=identity.csrf(request,csrf)
    token=identity.verify_code(current,code.strip())
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
    return page(request,'account.html',{'subscription':subscription,'orders':orders,'links':links})

@app.post('/account/link-codes')
def link_code(request:Request,csrf:str=Form(),platform:str=Form()):
    current=identity.csrf(request,csrf,True)
    code=identity.link_code(current['account_id'],platform)
    return page(request,'code.html',{'code':code,'platform':platform})

@app.post('/subscription/cancel')
def cancel(request:Request,csrf:str=Form()):
    current=identity.csrf(request,csrf,True)
    billing.cancel(current['account_id'])
    return RedirectResponse('/account',303)

@app.post('/checkout')
def checkout(request:Request,csrf:str=Form(),plan:str=Form(),request_key:UUID=Form(),consent:str=Form(default='')):
    current=identity.csrf(request,csrf,True)
    if consent!='2026-11-02': raise HTTPException(422,'Подтвердите условия автопродления')
    order=billing.create_order(current['account_id'],plan,request_key,current['email'])
    try: billing.process(order['id'])
    except HTTPException: pass  # The durable order remains accessible for reconciliation.
    return RedirectResponse('/orders/'+str(order['id']),303)

def get_order(request,order_id):
    current=owner(request)
    with billing.connect() as c: row=c.execute('SELECT * FROM premium_orders WHERE id=%s AND account_id=%s',(order_id,current['account_id'])).fetchone()
    if not row: raise HTTPException(404,'Счёт не найден')
    return row

@app.get('/orders/{order_id}')
def order_page(request:Request,order_id:UUID):
    row=get_order(request,order_id)
    return page(request,'order.html',{'order':row})

@app.post('/orders/{order_id}/refresh')
def refresh(request:Request,order_id:UUID,csrf:str=Form()):
    identity.csrf(request,csrf,True);get_order(request,order_id)
    error=None
    try: billing.process(order_id)
    except HTTPException as exc: error=str(exc.detail)
    return page(request,'order.html',{'order':get_order(request,order_id),'error':error})

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

@app.post('/webhooks/yookassa')
def webhook(body:Notification):
    if body.type!='notification' or body.event not in ('payment.succeeded','payment.canceled','payment.waiting_for_capture'): return {'ok':True}
    try: payment_id=UUID(str(body.object.get('id')))
    except ValueError: raise HTTPException(422,'Некорректный платёж')
    billing.webhook(str(payment_id))
    return {'ok':True}
