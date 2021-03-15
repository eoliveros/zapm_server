#!/usr/bin/python3
import os
import logging
import sys
import json
import time
import requests
import decimal
import datetime
from urllib.parse import urlparse
import pywaves

import gevent
from gevent.pywsgi import WSGIServer
from flask import url_for, redirect, render_template, request, abort, jsonify, flash
from flask_security.utils import encrypt_password
from flask_socketio import Namespace, emit, join_room, leave_room
from flask_security import current_user
from flask_jsonrpc.exceptions import OtherError

from app_core import app, db, socketio, aw
from models import security, user_datastore, Role, User, Bank, ClaimCode, TxNotification, ApiKey, MerchantTx, Settlement, Category, Proposal, Payment, WavesTx, WavesTxSig, Seeds
import admin
from utils import check_hmac_auth, generate_key, apply_customer_rate, apply_merchant_rate, email_payment_claim, sms_payment_claim, qrcode_svg_create, is_address
import bnz_ib4b
import tx_utils

logger = logging.getLogger(__name__)
ws_api_keys = {}
ws_sids = {}
wallet_balances = {}

SERVER_MODE = app.config["SERVER_MODE"]
ASSET_ID = app.config["ASSET_ID"]
NODE_BASE_URL = app.config["NODE_ADDRESS"]

#
# Helper functions
#

def setup_logging(level):
    # setup logging
    logger.setLevel(level)
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("[%(name)s %(levelname)s] %(message)s"))
    logger.addHandler(ch)
    # clear loggers set by any imported modules
    logging.getLogger().handlers.clear()

def add_user(email, password):
    with app.app_context():
        user = User.from_email(db.session, email)
        if user:
            #logger.error("user already exists")
            #return
            user.password = encrypt_password(password)
        else:
            user = user_datastore.create_user(email=email, password=encrypt_password(password))
        db.session.commit()

def create_role(name, desc):
    role = Role.from_name(db.session, name)
    if not role:
        role = Role(name=name, description=desc)
    else:
        role.description = desc
    db.session.add(role)
    return role

def add_role(email, role_name):
    with app.app_context():
        user = User.from_email(db.session, email)
        if not user:
            logger.error("user does not exist")
            return
        role = create_role(role_name, None)
        if role not in user.roles:
            user.roles.append(role)
        else:
            logger.info("user already has role")
        db.session.commit()

def create_category(name, desc):
    category = Category.from_name(db.session, name)
    if not category:
        category = Category(name=name, description=desc)
    else:
        category.description = desc
    db.session.add(category)
    return category

def add_merchant_codes():
    with app.app_context():
        for user in User.all(db.session):
            if not user.merchant_code:
                user.merchant_code = generate_key(4)
                db.session.add(user)
        db.session.commit()

def check_auth(api_key_token, nonce, sig, body):
    api_key = ApiKey.from_token(db.session, api_key_token)
    if not api_key:
        return False, "not found", None
    if not api_key.user.active:
        return False, "inactive account", None
    res, reason = check_hmac_auth(api_key, nonce, sig, body)
    if not res:
        return False, reason, None
    # update api key nonce
    db.session.commit()
    return True, "", api_key

def transfer_tx_callback(api_keys, tx):
    txt = json.dumps(tx)
    print("transfer_tx_callback: tx %s" % txt)
    for api_key in api_keys:
        print("sending 'tx' event to room %s" % api_key)
        socketio.emit("tx", txt, json=True, room=api_key)
        if not TxNotification.exists(db.session, tx["id"]):
            print("adding to tx notification table")
            api_key = ApiKey.from_token(db.session, api_key)
            txnoti = TxNotification(api_key.user, tx["id"])
            db.session.add(txnoti)
            db.session.commit()

def get_balance(wallet_address):
    url = '%s/assets/balance/%s/%s' % (app.config["NODE_ADDRESS"], wallet_address, app.config["ASSET_ID"])
    print(':: getting balance - %s' % url)
    r = requests.get(url)
    print(':: balance request status - %d' % r.status_code)
    if r.status_code == 200:
        balance = r.json()['balance'] / 100
        return balance
    return -1

# get the wallet balance, or update/create it if it does not exist or has expired
def get_update_balance(wallet_address):
    now = time.time()
    if wallet_address in wallet_balances:
        timestamp, balance = wallet_balances[wallet_address]
        # check if balance has expired
        if now - timestamp > 60 * 10:
            # update balance and timestamp
            balance = get_balance(wallet_address)
            wallet_balances[wallet_address] = (now, balance)
            # return new balance
            return balance
        else:
            # return cached balance
            return balance
    else:
        # initial balance and timestamp
        balance = get_balance(wallet_address)
        wallet_balances[wallet_address] = (now, balance)
        # return initial balance
        return balance

@app.before_first_request
def start_address_watcher():
    aw.transfer_tx_callback = transfer_tx_callback
    aw.start()

def bad_request(message):
    response = jsonify({"message": message})
    response.status_code = 400
    return response

@app.template_filter()
def int2asset(num):
    num = decimal.Decimal(num)
    return num/100

#
# Flask views
#

@app.context_processor
def inject_rates():
    return dict(sales_tax=app.config["SALES_TAX"], settlement_fee=app.config["SETTLEMENT_FEE"], merchant_rate=app.config["MERCHANT_RATE"], customer_rate=app.config["CUSTOMER_RATE"], settlement_address=app.config["SETTLEMENT_ADDRESS"])

@app.before_request
def before_request_func():
    if "DEBUG_REQUESTS" in app.config:
        print("URL: %s" % request.url)
        print(request.headers)

    if not current_user.is_anonymous:
        if current_user.has_role("admin") or current_user.has_role("finance") or current_user.has_role("merchant"):
            current_user.settlement_wallet_balance = get_update_balance(app.config["SETTLEMENT_ADDRESS"])
        if current_user.wallet_address:
            current_user.wallet_balance = get_update_balance(current_user.wallet_address)

@app.route("/")
def index():
    return render_template("index.html")

def process_claim_waves(payment, dbtx, recipient, asset_id):
    if payment.proposal.status != payment.proposal.STATE_AUTHORIZED:
        return dbtx, "payment not authorized"
    if payment.status != payment.STATE_SENT_CLAIM_LINK:
        return dbtx, "payment not authorized"
    # create/get transaction
    if not dbtx:
        if asset_id and asset_id != ASSET_ID:
            return dbtx, "'asset_id' does not match server"
        try:
            dbtx = _create_transaction_waves(recipient, payment.amount, "")
            payment.txid = dbtx.txid
            db.session.add(dbtx)
            db.session.add(payment)
            db.session.commit()
        except OtherError as ex:
            return dbtx, ex.message
        except ValueError as ex:
            return dbtx, ex
    # broadcast transaction
    try:
        dbtx = tx_utils.broadcast_transaction(db.session, dbtx.txid)
        payment.status = payment.STATE_SENT_FUNDS
        db.session.add(dbtx)
        db.session.commit()
    except OtherError as ex:
        return dbtx, ex.message
    return dbtx, None

def _create_transaction_waves(recipient, amount, attachment):
    # get fee
    path = f"/assets/details/{ASSET_ID}"
    response = requests.get(NODE_BASE_URL + path)
    if response.ok:
        asset_fee = response.json()["minSponsoredAssetFee"]
    else:
        short_msg = "failed to get asset info"
        logger.error(f"{short_msg}: ({response.status_code}, {response.request.method} {response.url}):\n\t{response.text}")
        err = OtherError(short_msg, tx_utils.ERR_FAILED_TO_GET_ASSET_INFO)
        err.data = response.text
        raise err
    if not recipient:
        short_msg = "recipient is null or an empty string"
        logger.error(short_msg)
        err = OtherError(short_msg, tx_utils.ERR_EMPTY_ADDRESS)
        raise err
    if not is_address(recipient):
        short_msg = "recipient is not a valid address"
        logger.error(short_msg)
        err = OtherError(short_msg, tx_utils.ERR_EMPTY_ADDRESS)
        raise err
    pywaves.setNode(NODE_BASE_URL, app.config["NODE_BASE_ENV"])
    pywaves.setChain(app.config["NODE_BASE_ENV"])
    seed_words = Seeds.query.filter_by(user_id = current_user.id).first()
    sender = pywaves.Address(seed='{}'.format(seed_words))
    recipient = pywaves.Address(recipient)
    asset = pywaves.Asset(ASSET_ID)
    address_data = sender.sendAsset(recipient, asset, amount, attachment, feeAsset=asset, txFee=asset_fee)
    address_data["type"] = 4 # sendAsset does not include "type" - https://github.com/PyWaves/PyWaves/issues/131
    signed_tx = json.dumps(address_data)
    signed_tx = json.loads(signed_tx)
    logger.info(signed_tx)
    # calc txid properly
    txid = tx_utils.tx_to_txid(signed_tx)
    # store tx in db
    dbtx = WavesTx(txid, "transfer", tx_utils.CTX_CREATED, signed_tx["amount"], True, json.dumps(signed_tx))
    return dbtx

def process_proposals():
    with app.app_context():
        # set expired
        expired = 0
        now = datetime.datetime.now()
        proposals = Proposal.in_status(db.session, Proposal.STATE_AUTHORIZED)
        for proposal in proposals:
            if proposal.date_expiry < now:
                proposal.status = Proposal.STATE_EXPIRED
                expired += 1
                db.session.add(proposal)
        db.session.commit()
        # process authorized
        emails = 0
        sms_messages = 0
        proposals = Proposal.in_status(db.session, Proposal.STATE_AUTHORIZED)
        for proposal in proposals:
            for payment in proposal.payments:
                if payment.status == payment.STATE_CREATED:
                    if payment.email:
                        email_payment_claim(logger, payment, proposal.HOURS_EXPIRY)
                        payment.status = payment.STATE_SENT_CLAIM_LINK
                        db.session.add(payment)
                        logger.info(f"Sent payment claim url to {payment.email}")
                        emails += 1
                    elif payment.mobile:
                        sms_payment_claim(logger, payment, proposal.HOURS_EXPIRY)
                        payment.status = payment.STATE_SENT_CLAIM_LINK
                        db.session.add(payment)
                        logger.info(f"Sent payment claim url to {payment.mobile}")
                        sms_messages += 1
                    elif payment.wallet_address:
                        ##TODO: set status and commit before sending so we cannot send twice
                        raise Exception("not yet implemented")
        db.session.commit()
        logger.info(f"payment statuses commited")
        return f"done (expired {expired}, emails {emails}, SMS messages {sms_messages})"

@app.route("/claim_payment/<token>", methods=["GET", "POST"])
def claim_payment(token):
    qrcode = None
    url = None
    attachment = None
    payment = Payment.from_token(db.session, token)
    if not payment:
        return bad_request('payment not found', 404)
    now = datetime.datetime.now()
    if now > payment.proposal.date_expiry and payment.status != payment.STATE_SENT_FUNDS:
        return bad_request('expired', 404)

    def render(recipient):
        url_parts = urlparse(request.url)
        url = url_parts._replace(scheme="zap", query='scheme={}'.format(url_parts.scheme)).geturl()
        qrcode_svg = qrcode_svg_create(url)
        return render_template("claim_payment.html", payment=payment, recipient=recipient, qrcode_svg=qrcode_svg, url=url)
    def render_waves(dbtx):
        recipient = None
        if dbtx:
            recipient = dbtx.tx_with_sigs()["recipient"]
        return render(recipient)

    if SERVER_MODE == 'waves':
        dbtx = WavesTx.from_txid(db.session, payment.txid)

    if request.method == "POST":
        content_type = request.content_type
        using_app = content_type.startswith('application/json')
        logger.info("claim_payment: content type - {}, using_app - {}".format(content_type, using_app))
        recipient = ""
        asset_id = ""
        if using_app:
            content = request.get_json(force=True)
            if content is None:
                return bad_request("failed to decode JSON object")
            if SERVER_MODE == 'waves':
                params, err_response = get_json_params(logger, content, ["recipient", "asset_id"])
                if err_response:
                    return err_response
                recipient, asset_id = params
            else: # paydb
                params, err_response = get_json_params(logger, content, ["recipient"])
                if err_response:
                    return err_response
                recipient, = params
        else: # using html form
            try:
                recipient = request.form["recipient"]
            except:
                flash("'recipient' parameter not present", "danger")
                return render_waves(dbtx)
            try:
                asset_id = request.form["asset_id"]
            except:
                pass
        if SERVER_MODE == 'waves':
            dbtx, err_msg = process_claim_waves(payment, dbtx, recipient, asset_id)
        else: # paydb
            err_msg = process_claim_paydb(payment, recipient)
        if err_msg:
            logger.error("claim_payment: {}".format(err_msg))
            if using_app:
                return bad_request(err_msg)
            flash(err_msg, "danger")
    if SERVER_MODE == 'waves':
        return render_waves(dbtx)
    else: # paydb
        return render(None)


#
# Test
#

@app.route("/test/claimcode/<token>")
def test_claimcode(token):
    if not app.config["DEBUG"]:
        return abort(404)
    claim_code = ClaimCode.from_token(db.session, token)
    for api_key in ws_api_keys.keys():
        print("sending claim code to %s" % api_key)
        socketio.emit("info", claim_code.to_json(), json=True, room=api_key)
    if claim_code:
        return jsonify(claim_code.to_json())
    return abort(404)

@app.route("/test/watched")
def test_watched():
    if not app.config["DEBUG"]:
        return abort(404)
    return jsonify(aw.watched())

@app.route("/test/ws")
def test_ws():
    if not app.config["DEBUG"]:
        return abort(404)
    return jsonify(ws_api_keys)

#
# Websocket events
#

def alert_claimed(claim_code):
    for apikey in claim_code.user.apikeys:
        socketio.emit("claimed", claim_code.to_json(), json=True, room=apikey.token)

class SocketIoNamespace(Namespace):
    def trigger_event(self, event, sid, *args):
        if sid not in self.server.environ:
            # we don't have record of this client, ignore this event
            return '', 400
        app = self.server.environ[sid]['flask.app']
        if "DEBUG_REQUESTS" in app.config:
            with app.request_context(self.server.environ[sid]):
                before_request_func()
        return super(SocketIoNamespace, self).trigger_event(event, sid, *args)

    def on_error(self, e):
        print(e)

    def on_connect(self):
        print("connect sid: %s" % request.sid)

    def on_auth(self, auth):
        # check auth
        res, reason, api_key = check_auth(auth["api_key"], auth["nonce"], auth["signature"], str(auth["nonce"]))
        if res:
            emit("info", "authenticated!")
            # join room and store user
            print("join room for api_key: %s" % auth["api_key"])
            join_room(auth["api_key"])
            ws_api_keys[auth["api_key"]] = request.sid
            ws_sids[request.sid] = auth["api_key"]

    def on_disconnect(self):
        print("disconnect sid: %s" % request.sid)
        if request.sid in ws_sids:
            api_key = ws_sids[request.sid]
            if api_key in ws_api_keys:
                print("leave room for api key: %s" % api_key)
                leave_room(api_key)
                del ws_api_keys[api_key]
            del ws_sids[request.sid]

socketio.on_namespace(SocketIoNamespace("/"))

#
# Private (merchant) API
#

@app.route("/watch", methods=["POST"])
def watch():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    addr = content["address"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    aw.watch(addr, api_key.token)
    return "ok"

@app.route("/register", methods=["POST"])
def register():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    token = content["token"]
    amount = content["amount"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    claim_code = ClaimCode(api_key.user, token, amount)
    db.session.add(claim_code)
    db.session.commit()
    return jsonify(claim_code.to_json())

@app.route("/check", methods=["POST"])
def check():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    token = content["token"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    claim_code = ClaimCode.from_token(db.session, token)
    if claim_code and claim_code.user == api_key.user:
        return jsonify(claim_code.to_json())
    return abort(404)

@app.route("/merchanttx", methods=["POST"])
def merchanttx():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    MerchantTx.update_wallet_address(db.session, api_key.user)
    return "ok"

@app.route("/wallet_address", methods=["POST"])
def wallet_address():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    address = content["address"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    if not api_key.account_admin:
        return bad_request("not account admin")
    if api_key.user.wallet_address:
        return bad_request("wallet address already set")
    api_key.user.wallet_address = address
    db.session.add(api_key.user)
    db.session.commit()
    return "ok"

@app.route("/rates", methods=["POST"])
def rates():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    settlement_fee = api_key.user.settlement_fee if api_key.user.settlement_fee else app.config["SETTLEMENT_FEE"]
    merchant_rate = api_key.user.merchant_rate if api_key.user.merchant_rate else app.config["MERCHANT_RATE"]
    customer_rate = api_key.user.customer_rate if api_key.user.customer_rate else app.config["CUSTOMER_RATE"]
    rates = {"settlement_fee": str(settlement_fee), "merchant": str(merchant_rate), "customer": str(customer_rate), "settlement_address": app.config["SETTLEMENT_ADDRESS"], "sales_tax": str(app.config["SALES_TAX"])}
    return jsonify(rates)

def _zap_calc(api_key, nzd_required):
    zap = apply_customer_rate(nzd_required, api_key.user, app.config)
    return int(zap)

@app.route("/zap_calc", methods=["POST"])
def zap_calc():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    nzd_required = content["nzd_required"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    zap = _zap_calc(api_key, nzd_required)
    return jsonify(dict(nzd_required=nzd_required, zap=zap))

@app.route("/banks", methods=["POST"])
def banks():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    banks = Bank.from_user(db.session, api_key.user)
    banks = [bank.to_json() for bank in banks]
    return jsonify(banks)

def _settlement_calc(api_key, amount):
    amount_receive = apply_merchant_rate(amount, api_key.user, app.config)
    return int(amount_receive)

@app.route("/settlement_calc", methods=["POST"])
def settlement_calc():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    amount = content["amount"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    amount_receive = _settlement_calc(api_key, amount)
    return jsonify({"amount": amount, "amount_receive": amount_receive})

@app.route("/settlement", methods=["POST"])
def settlement():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    bank = content["bank"]
    amount = content["amount"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    bank = Bank.from_token(db.session, bank)
    if not bank or bank.user != api_key.user:
        return bad_request("invalid bank account")
    amount_receive = _settlement_calc(api_key, amount)
    if amount_receive <= 0:
        return bad_request("Settlement amount less then or equal to 0");
    count_this_month = Settlement.count_this_month(db.session, api_key.user)
    max_this_month = api_key.user.max_settlements_per_month if api_key.user.max_settlements_per_month else 1
    if count_this_month >= max_this_month:
        return bad_request("Settlement count max reached for this month")
    settlement = Settlement(api_key.user, bank, amount, app.config["SETTLEMENT_ADDRESS"], amount_receive)
    db.session.add(settlement)
    db.session.commit()
    return jsonify(settlement.to_json())

@app.route("/settlement_set_txid", methods=["POST"])
def settlement_set_txid():
    sig = request.headers.get("X-Signature")
    content = request.json
    api_key = content["api_key"]
    nonce = content["nonce"]
    token = content["token"]
    txid = content["txid"]
    res, reason, api_key = check_auth(api_key, nonce, sig, request.data)
    if not res:
        return bad_request(reason)
    settlement = Settlement.from_token(db.session, token)
    if not settlement:
        return bad_request("Settlement not found")
    if settlement.user != api_key.user:
        return bad_request("Settlement not found")
    if settlement.txid:
        return bad_request("Transaction ID already set")
    settlement.txid = txid
    settlement.status = Settlement.STATE_SENT_ZAP
    db.session.add(settlement)
    db.session.commit()
    return jsonify(settlement.to_json())

#
# Public (customer) API
#

@app.route("/claim", methods=["POST"])
def claim():
    content = request.json
    token = content["token"]
    secret = content["secret"]
    address = content["address"]
    claim_code = ClaimCode.from_token(db.session, token)
    if claim_code:
        if claim_code.status == "created":
            claim_code.secret = secret
            claim_code.address = address
            claim_code.status = "claimed"
            alert_claimed(claim_code)
            db.session.add(claim_code)
            db.session.commit()
            return jsonify(claim_code.to_json())
        else:
            return bad_request("already claimed")
    return abort(404)

class WebGreenlet():

    #def __init__(self, exception_func, addr="0.0.0.0", port=5000):
    def __init__(self, addr="0.0.0.0", port=5000):
        self.addr = addr
        self.port = port
        self.runloop_greenlet = None
        #self.exception_func = exception_func

        # create tables
        logger.info("creating tables..")
        db.create_all()
        create_role("admin", "super user")
        create_role("finance", "Can view/action settlements")
        create_role("merchant", "Merchants can view/action their own rebate")
        create_category("rebate", "")
        db.session.commit()

        # process commands
        if len(sys.argv) > 1:
            if sys.argv[1] == "add_user":
                add_user(sys.argv[2], sys.argv[3])
            if sys.argv[1] == "add_role":
                add_role(sys.argv[2], sys.argv[3])
            if sys.argv[1] == "add_merchant_codes":
                add_merchant_codes()
        else:
            # check config
            if "SETTLEMENT_ADDRESS" not in app.config:
                logger.error("SETTLEMENT_ADDRESS does not exist")
                sys.exit(1)
            if "SENDER_BANK_ACCOUNT" not in app.config:
                logger.error("SENDER_BANK_ACCOUNT does not exist")
                sys.exit(1)
            if "SENDER_NAME" not in app.config:
                logger.error("SENDER_NAME does not exist")
                sys.exit(1)
    
            ## Bind to PORT if defined, otherwise default to 5000.
            #port = int(os.environ.get("PORT", 5000))
            #logger.info("binding to port: %d" % port)
            #socketio.run(app, host="0.0.0.0", port=port)
            ## stop addresswatcher
            if aw:
                aw.kill()

    def start(self):
        def runloop():
            logger.info("WebGreenlet runloop started")
            logger.info(f"WebGreenlet webserver starting (addr: {self.addr}, port: {self.port})")
            http_server = WSGIServer((self.addr, self.port), app)
            http_server.serve_forever()

        def process_proposals_loop():
            while True:
                gevent.spawn(process_proposals)
                gevent.sleep(30)

        def start_greenlets():
            logger.info("starting WebGreenlet runloop...")
            self.runloop_greenlet.start()
            self.process_proposals_greenlet.start()

        # create greenlet
        self.runloop_greenlet = gevent.Greenlet(runloop)
        self.process_proposals_greenlet = gevent.Greenlet(process_proposals_loop)
        #if self.exception_func:
        #    self.runloop_greenlet.link_exception(self.exception_func)
        ## check node/wallet and start greenlets
        gevent.spawn(start_greenlets)

    def stop(self):
        self.runloop_greenlet.kill()
        self.process_proposals_greenlet.kill()


if __name__ == "__main__":
    # setup logging
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(logging.Formatter('[%(name)s %(levelname)s] %(message)s'))
    logger.addHandler(ch)
    # clear loggers set by any imported modules
    logging.getLogger().handlers.clear()

    web_greenlet = WebGreenlet()
    web_greenlet.start()

    while 1:
        gevent.sleep(1)

    web_greenlet.stop()


