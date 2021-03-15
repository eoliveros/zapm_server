import os
import binascii
import hmac
import hashlib
import base64
import io
import re
import string
import secrets

import requests
import base58
import pywaves
from flask import make_response
from flask import url_for
from stdnum.nz import bankaccount
import bnz_ib4b
import qrcode
import qrcode.image.svg
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, From

from app_core import app

def generate_key(num=20):
    return binascii.hexlify(os.urandom(num)).decode()

def to_bytes(data):
    if not isinstance(data, (bytes, bytearray)):
        return data.encode("utf-8")
    return data

def create_hmac_sig(api_secret, message):
    _hmac = hmac.new(to_bytes(api_secret), msg=to_bytes(message), digestmod=hashlib.sha256)
    signature = _hmac.digest()
    signature = base64.b64encode(signature).decode("utf-8")
    return signature

def check_hmac_auth(api_key, nonce, sig, body):
    if nonce <= api_key.nonce:
        return False, "old nonce"
    our_sig = create_hmac_sig(api_key.secret, body)
    if sig == our_sig:
        api_key.nonce = nonce
        return True, ""
    return False, "invalid signature"

def bankaccount_is_valid(account):
    return bankaccount.is_valid(account)

def ib4b_response(filename, settlements, sender_name, sender_bank_account):
    # create output 
    output = io.StringIO()
    # process settlements
    txs = []
    for settlement in settlements:
        bank_account = settlement.bank.account_number.replace('-', '')
        tx = (bank_account, settlement.amount_receive, "zap settlement", settlement.token, settlement.user.merchant_code, "zap settlement", settlement.token)
        txs.append(tx)
    sender_bank_account = sender_bank_account.replace('-', '')
    bnz_ib4b.write_txs(output, "", sender_bank_account, sender_name, txs)
    # return file response
    resp = make_response(output.getvalue())
    resp.headers['Content-Type'] = "application/octet-stream"
    resp.headers['Content-Disposition'] = "inline; filename=%s" % filename
    return resp

def blockchain_transactions(logger, node, wallet_address, limit, after=None):
    url = '%s/transactions/address/%s/limit/%s' % (node, wallet_address, limit)
    if after:
        url += '?after=%s' % after
    print(':: requesting %s..' % url)
    r = requests.get(url)
    if r.status_code != 200:
        logger.error('status code is %d' % r.status_code)
        return []
    txs = r.json()[0]
    print(':: retrieved %d records' % len(txs))
    txs_result = []
    for tx in txs:
        if 'attachment' in tx:
            attachment = tx['attachment']
            if attachment:
                tx['attachment'] = base58.b58decode(attachment).decode('utf-8')
        if 'recipient' in tx:
            tx['direction'] = tx['recipient'] == wallet_address and tx['sender'] != wallet_address
        else:
            tx['direction'] = False
        txs_result.append(tx)
    return txs_result

def apply_customer_rate(nzd, user, config):
    print('nzd: ' + str(nzd))
    sales_tax = config["SALES_TAX"]
    print('sales_tax: ' + str(sales_tax))
    customer_rate = user.customer_rate if user.customer_rate else config["CUSTOMER_RATE"]
    merchant_fee = (nzd * (1 + customer_rate)) - nzd
    print('merchant_fee: ' + str(merchant_fee))
    merchant_fee = merchant_fee * (1 + sales_tax)
    print('merchant_fee (inc tax): ' + str(merchant_fee))
    zap = nzd + merchant_fee
    print('zap: ' + str(zap))
    return zap

def apply_merchant_rate(amount, user, config, use_fixed_fee=True):
    print('amount: ' + str(amount))
    sales_tax = config["SALES_TAX"]
    print('sales_tax: ' + str(sales_tax))
    settlement_fee = user.settlement_fee if user.settlement_fee != None else config["SETTLEMENT_FEE"]
    print('settlement_fee: ' + str(settlement_fee))
    merchant_rate = user.merchant_rate if user.merchant_rate != None else config["MERCHANT_RATE"]
    print('merchant_rate: ' + str(merchant_rate))
    if use_fixed_fee:
        fixed_fee = int(settlement_fee * 100)
    else:
        fixed_fee = 0
    print('fixed_fee: ' + str(fixed_fee))
    fixed_fee = fixed_fee * (1 + sales_tax)
    print('fixed_fee (inc tax): ' + str(fixed_fee))

    #          r
    # a = ----------
    #     ms - s + 1
    #
    # where:
    #  a = amount nzd
    #  r = amount zap to settle
    #  m = merchant rate
    #  s = sales tax
    amount_nzd = amount / ((1 + merchant_rate) * (1 + sales_tax) - (1 + sales_tax) + 1)

    # fixed fee
    amount_nzd = amount_nzd - fixed_fee

    return amount_nzd

def is_email(email):
    return re.match("[^@]+@[^@]+\.[^@]+", email)

def generate_random_password(length):
    password_characters = string.ascii_letters + string.digits + string.punctuation
    secret_password = ''.join(secrets.choice(password_characters) for i in range(length))
    return secret_password

def is_address(s):
    NODE_BASE_URL = app.config["NODE_ADDRESS"]
    pywaves.setNode(NODE_BASE_URL, app.config["NODE_BASE_ENV"])
    pywaves.setChain(app.config["NODE_BASE_ENV"])

    try:
       return pywaves.validateAddress(s)
    except:
       return False

def is_mobile(s):
    return s.isnumeric()

def send_email(logger, subject, msg, to=None):
    if not to:
        to = app.config["ADMIN_EMAIL"]
    from_email = From(app.config["FROM_EMAIL"], app.config["FROM_NAME"])
    message = Mail(from_email=from_email, to_emails=to, subject=subject, html_content=msg)
    try:
        sg = SendGridAPIClient(app.config["MAIL_SENDGRID_API_KEY"])
        response = sg.send(message)
    except Exception as ex:
        logger.error(f"email '{subject}': {ex}")

def email_payment_claim(logger, payment, hours_expiry):
    url = url_for("claim_payment", token=payment.token, _external=True)
    msg = f"You have a ZAP payment waiting!<br/><br/>Claim your payment <a href='{url}'>here</a><br/><br/>Claim within {hours_expiry} hours"
    send_email(logger, "Claim your ZAP payment", msg, payment.email)

def sms_payment_claim(logger, payment, hours_expiry):
    # SMS messages are sent by burst SMS
    #  - the authorization is by the sender email
    #  - the country code is configured by the account
    url = url_for("claim_payment", token=payment.token, _external=True)
    msg = f"You have a ZAP payment waiting! Claim your payment (within {hours_expiry} hours) {url}"
    email = str(payment.mobile) + "@transmitsms.com"
    send_email(logger, "ZAP Payment", msg, email)

def qrcode_svg_create(data, box_size=10):
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(data, image_factory=factory, box_size=box_size)
    output = io.BytesIO()
    img.save(output)
    svg = output.getvalue().decode('utf-8')
    return svg



def generate_wallet_address(wallet_seed):
    wallet_addr = None
    if wallet_seed:
        NODE_BASE_URL = app.config["NODE_ADDRESS"]
        pywaves.setNode(NODE_BASE_URL, app.config["NODE_BASE_ENV"])
        wallet_addr = pywaves.Address(seed='{}'.format(wallet_seed))
        return wallet_addr.address
    return wallet_addr
