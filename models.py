import datetime
from datetime import timezone
import decimal
import logging
import io
import json
from urllib.parse import urlparse
import time

from flask import redirect, url_for, request, abort, flash, has_app_context, g
from flask_admin import expose
from flask_admin.actions import action
from flask_admin.babel import lazy_gettext
from flask_admin.helpers import get_form_data
from flask_admin.model import filters, typefmt
from flask_admin.contrib import sqla
from flask_mail import Message
from sqlalchemy import and_
from flask_admin.contrib.sqla.filters import BaseSQLAFilter
from wtforms import ValidationError, validators
from wtforms.fields import TextField, DecimalField, FileField
from flask_security import Security, SQLAlchemyUserDatastore, \
    UserMixin, RoleMixin, login_required, current_user
from flask_security.utils import encrypt_password
from flask_security.recoverable import send_reset_password_instructions
from marshmallow import Schema, fields
from markupsafe import Markup
import base58
import qrcode
import qrcode.image.svg
from wtforms.validators import DataRequired
import pywaves

from app_core import app, db, aw, mail
from utils import generate_key, ib4b_response, bankaccount_is_valid, blockchain_transactions, apply_merchant_rate, is_email, generate_random_password, is_address, is_mobile, generate_wallet_address 

logger = logging.getLogger(__name__)

#
# Define models
#

roles_users = db.Table(
    'roles_users',
    db.Column('user_id', db.Integer(), db.ForeignKey('user.id')),
    db.Column('role_id', db.Integer(), db.ForeignKey('role.id')))

class Role(db.Model, RoleMixin):
    id = db.Column(db.Integer(), primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))

    @classmethod
    def from_name(cls, session, name):
        return session.query(cls).filter(cls.name == name).first()

    def __str__(self):
        return self.name

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    merchant_name = db.Column(db.String(255))
    merchant_code = db.Column(db.String(255), unique=True)
    email = db.Column(db.String(255), unique=True)
    password = db.Column(db.String(255))
    active = db.Column(db.Boolean())
    confirmed_at = db.Column(db.DateTime())
    roles = db.relationship('Role', secondary=roles_users,
                            backref=db.backref('users', lazy='dynamic'))
    max_settlements_per_month = db.Column(db.Integer)
    settlement_fee = db.Column(db.Numeric)
    merchant_rate = db.Column(db.Numeric)
    customer_rate = db.Column(db.Numeric)
    wallet_address = db.Column(db.String(255))

    def __init__(self, **kwargs):
        self.merchant_code = generate_key(4)
        super().__init__(**kwargs)

    def on_admin_created(self):
        self.merchant_code = generate_key(4)
        self.password = encrypt_password(generate_random_password(16))
        self.confirmed_at = datetime.datetime.now()
        self.active = True

    @classmethod
    def from_email(cls, session, email):
        return session.query(cls).filter(cls.email == email).first()

    @classmethod
    def all(cls, session):
        return session.query(cls).all()

    def __str__(self):
        return '%s (%s)' % (self.merchant_code, self.merchant_name)

class Seeds(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('wallet_seeds', lazy='dynamic'))
    wallet_seed = db.Column(db.String(255), nullable=False)
    wallet_address = db.Column(db.String(255))

    settlement_fee = db.Column(db.Numeric)
    merchant_rate = db.Column(db.Numeric)
    customer_rate = db.Column(db.Numeric)

    def __init__(self, user, wallet_seed, wallet_address, settlement_fee, merchant_rate, customer_rate):
        self.user = user
        self.wallet_seed = wallet_seed
        self.wallet_address = wallet_address
        self.settlement_fee = settlement_fee
        self.merchant_rate = merchant_rate 
        self.customer_rate = customer_rate
        self.generate_defaults()

    def generate_defaults(self):
        self.user = current_user
        self.wallet_address = generate_wallet_address(self.wallet_seed)

    def __repr__(self):
        return self.wallet_seed

class BankSchema(Schema):
    token = fields.String()
    account_number = fields.String()
    account_name = fields.String()
    account_holder_address = fields.String()
    bank_name = fields.String()
    default_account = fields.Bool()

class Bank(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('banks', lazy='dynamic'))
    token = db.Column(db.String(255), unique=True, nullable=False)
    account_number = db.Column(db.String(255), nullable=False)
    account_name = db.Column(db.String(255), nullable=False)
    account_holder_address = db.Column(db.String(255), nullable=False)
    bank_name = db.Column(db.String(255), nullable=False)
    default_account = db.Column(db.Boolean, nullable=False)

    def __init__(self, token, account_number, account_name, account_holder_address, bank_name, default_account):
        self.account_number = account_number
        self.account_name = account_name
        self.account_holder_address = acount_holder_address
        self.bank_name = bank_name
        self.default_account = default_account
        self.generate_defaults()

    def generate_defaults(self):
        self.user = current_user
        self.token = generate_key(4)

    def ensure_default_account_exclusive(self, session):
        if self.default_account:
            session.query(Bank).filter(Bank.user_id == self.user_id, Bank.id != self.id).update(dict(default_account=False))

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    @classmethod
    def from_token(cls, session, token):
        return session.query(cls).filter(cls.token == token).first()

    @classmethod
    def from_user(cls, session, user):
        return session.query(cls).filter(cls.user_id == user.id).all()

    def __repr__(self):
        return self.account_number

    def to_json(self):
        schema = BankSchema()
        return schema.dump(self).data

class ClaimCodeSchema(Schema):
    date = fields.Float()
    token = fields.String()
    secret = fields.String()
    amount = fields.Integer()
    address = fields.String()
    status = fields.String()

class ClaimCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('claimcodes', lazy='dynamic'))
    date = db.Column(db.DateTime())
    token = db.Column(db.String(255), unique=True, nullable=False)
    secret = db.Column(db.String(255))
    amount = db.Column(db.Integer)
    address = db.Column(db.String(255))
    status = db.Column(db.String(255))

    def __init__(self, user, token, amount):
        self.user = user
        self.date = datetime.datetime.now()
        self.token = token
        self.secret = None
        self.amount = amount
        self.address = None
        self.status = "created"

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    @classmethod
    def from_token(cls, session, token):
        return session.query(cls).filter(cls.token == token).first()

    def __repr__(self):
        return "<ClaimCode %r>" % (self.token)

    def to_json(self):
        schema = ClaimCodeSchema()
        return schema.dump(self).data

class TxNotification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('txnotifications', lazy='dynamic'))
    date = db.Column(db.DateTime())
    txid = db.Column(db.String(255), unique=True)

    def __init__(self, user, txid):
        self.user = user
        self.date = datetime.datetime.now()
        self.txid = txid

    @classmethod
    def exists(cls, session, txid):
        return session.query(cls).filter(cls.txid == txid).first()

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    def __repr__(self):
        return "<TxNotification %r>" % (self.txid)

class ApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('apikeys', lazy='dynamic'))
    date = db.Column(db.DateTime(), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    token = db.Column(db.String(255), unique=True, nullable=False)
    nonce = db.Column(db.Integer, nullable=False)
    secret = db.Column(db.String(255), nullable=False)
    account_admin = db.Column(db.Boolean, nullable=False)

    def __init__(self, name):
        self.name = name
        self.generate_defaults()

    def generate_defaults(self):
        self.user = current_user
        self.date = datetime.datetime.now()
        self.token = generate_key(8)
        self.nonce = 0
        self.secret = generate_key(16)

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    @classmethod
    def from_token(cls, session, token):
        return session.query(cls).filter(cls.token == token).first()

    @classmethod
    def admin_exists(cls, session, user):
        return session.query(cls).filter(cls.user == user, cls.account_admin == True).first()

    def __repr__(self):
        return "<ApiKey %r>" % (self.token)

class MerchantTxSchema(Schema):
    date = fields.Float()
    wallet_address = fields.String()
    amount = fields.Integer()
    amount_nzd = fields.Integer()
    txid = fields.String()
    direction = fields.Integer()
    device_name = fields.String()

class MerchantTx(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime())
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('merchanttxs', lazy='dynamic'))
    wallet_address = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Integer)
    amount_nzd = db.Column(db.Integer)
    txid = db.Column(db.String(255), nullable=False)
    direction = db.Column(db.Boolean, nullable=False)
    category = db.Column(db.String(255))
    attachment = db.Column(db.String(255))
    device_name = db.Column(db.String(255))
    __table_args__ = (db.UniqueConstraint('user_id', 'txid', name='user_txid_uc'),)

    def __init__(self, user, date, wallet_address, amount, amount_nzd, txid, direction, attachment):
        self.date = date
        self.user = user
        self.wallet_address = wallet_address
        self.amount = amount
        self.amount_nzd = amount_nzd
        self.txid = txid
        self.direction = direction
        self.attachment = attachment
        try:
            self.device_name = json.loads(attachment)['device_name']
        except:
            pass
        try:
            self.category = json.loads(attachment)['category']
        except:
            pass

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    @classmethod
    def from_txid(cls, session, txid):
        return session.query(cls).filter(cls.txid == txid).first()

    @classmethod
    def oldest_txid(cls, session, user):
        last =  session.query(cls).filter(cls.user_id == user.id).order_by(cls.id.desc()).first()
        if last:
            return last.txid
        return None

    @classmethod
    def exists(cls, session, user, txid):
        return session.query(cls).filter(and_(cls.user_id == user.id), (cls.txid == txid)).scalar()

    @classmethod
#    def update_wallet_address(cls, session, user):
#        if user.wallet_address:
#            # update txs
#            limit = 100
#            oldest_txid = None
#            txs = []
#            while True:
#                have_tx = False
#                txs = blockchain_transactions(logger, app.config["NODE_ADDRESS"], user.wallet_address, limit, oldest_txid)
#                for tx in txs:
#                    oldest_txid = tx["id"]
#                    have_tx = MerchantTx.exists(db.session, user, oldest_txid)
#                    if have_tx:
#                        break
#                    if tx["type"] == 4 and tx["assetId"] == app.config["ASSET_ID"]:
#                        amount_nzd = apply_merchant_rate(tx['amount'], user, app.config, use_fixed_fee=False)
#                        date = datetime.datetime.fromtimestamp(tx['timestamp'] / 1000)
#                        session.add(MerchantTx(user, date, user.wallet_address, tx['amount'], amount_nzd, tx['id'], tx['direction'], tx['attachment']))
#                if have_tx or len(txs) < limit:
#                    break
#            session.commit()

    def update_wallet_address(cls, session, user):
        seeds = db.session.query(Seeds).filter(Seeds.user_id == user.id).all()
        print(seeds)
        logger.info(seeds)
        if user.wallet_address:
            # update txs
            limit = 100
            oldest_txid = None
            txs = []
            while True:
                have_tx = False
                txs = blockchain_transactions(logger, app.config["NODE_ADDRESS"], user.wallet_address, limit, oldest_txid)
                for tx in txs:
                    oldest_txid = tx["id"]
                    have_tx = MerchantTx.exists(db.session, user, oldest_txid)
                    if have_tx:
                        break
                    if tx["type"] == 4 and tx["assetId"] == app.config["ASSET_ID"]:
                        amount_nzd = apply_merchant_rate(tx['amount'], user, app.config, use_fixed_fee=False)
                        date = datetime.datetime.fromtimestamp(tx['timestamp'] / 1000)
                        session.add(MerchantTx(user, date, user.wallet_address, tx['amount'], amount_nzd, tx['id'], tx['direction'], tx['attachment']))
                if have_tx or len(txs) < limit:
                    break
            session.commit()

    def __repr__(self):
        return"<MerchantTx %r>" % (self.txid)

    def to_json(self):
        schema = MerchantTxSchema()
        return schema.dump(self).data

class Payment(db.Model):
    STATE_CREATED = "created"
    STATE_SENT_CLAIM_LINK = "sent_claim_link"
    STATE_EXPIRED = "expired"
    STATE_SENT_FUNDS = "sent_funds"

    id = db.Column(db.Integer, primary_key=True)
    proposal_id = db.Column(db.Integer, db.ForeignKey('proposal.id'), nullable=False)
    proposal = db.relationship('Proposal', backref=db.backref('payments', lazy='dynamic'))
    token = db.Column(db.String(255), unique=True, nullable=False)
    mobile = db.Column(db.String(255))
    email = db.Column(db.String(255))
    wallet_address = db.Column(db.String(255))
    message = db.Column(db.String())
    amount = db.Column(db.Integer)
    status = db.Column(db.String(255))
    txid = db.Column(db.String(255))

    def __init__(self, proposal, mobile, email, wallet_address, message, amount):
        self.proposal = proposal
        self.token = generate_key(8)
        self.mobile = mobile
        self.email = email
        self.wallet_address = wallet_address
        self.message = message
        self.amount = amount
        self.status = self.STATE_CREATED
        self.txid = None

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    @classmethod
    def from_token(cls, session, token):
        return session.query(cls).filter(cls.token == token).first()

    def __repr__(self):
        return "<Payment %r>" % (self.token)

categories_proposals = db.Table(
    'categories_proposals',
    db.Column('proposal_id', db.Integer(), db.ForeignKey('proposal.id')),
    db.Column('category_id', db.Integer(), db.ForeignKey('category.id'))
)

class Category(db.Model):
    id = db.Column(db.Integer(), primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))

    @classmethod
    def from_name(cls, session, name):
        return session.query(cls).filter(cls.name == name).first()

    def __str__(self):
        return self.name

class Proposal(db.Model):
    STATE_CREATED = "created"
    STATE_AUTHORIZED = "authorized"
    STATE_DECLINED = "declined"
    STATE_EXPIRED = "expired"

    HOURS_EXPIRY = 72

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime(), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('proposals_user', lazy='dynamic'))
    proposer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    proposer = db.relationship('User', foreign_keys=[proposer_id], backref=db.backref('proposals_proposed', lazy='dynamic'))
    reason = db.Column(db.String())
    authorizer_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    authorizer = db.relationship('User', foreign_keys=[authorizer_id], backref=db.backref('proposals_authorized', lazy='dynamic'))
    merchant_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    merchant = db.relationship('User', foreign_keys=[merchant_id], backref=db.backref('proposals_merchants', lazy='dynamic'))
    date_authorized = db.Column(db.DateTime())
    date_expiry = db.Column(db.DateTime())
    status = db.Column(db.String(255))
    categories = db.relationship('Category', secondary=categories_proposals,
    backref=db.backref('proposals', lazy='dynamic'))

    def __init__(self, proposer, merchant, reason):
        self.generate_defaults()
        self.proposer= proposer
        self.merchant = merchant
        self.reason = reason

    def generate_defaults(self):
        self.date = datetime.datetime.now()
        self.proposer = current_user
        self.merchant = current_user
        self.user = current_user
        self.authorizer = None
        self.date_authorized = None
        self.date_expiry = None
        self.status = self.STATE_CREATED

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    @classmethod
    def in_status(cls, session, status):
        return session.query(cls).filter(cls.status == status).all()

    def __repr__(self):
        return "<Proposal %r>" % (self.id)

class SettlementSchema(Schema):
    date = fields.Float()
    token = fields.String()
    bank_account = fields.String()
    amount = fields.Integer()
    settlement_address = fields.String()
    amount_receive = fields.Integer()
    txid = fields.String()
    status = fields.String()

class Settlement(db.Model):
    STATE_CREATED = "created"
    STATE_SENT_ZAP = "sent_zap"
    STATE_VALIDATED = "validated"
    STATE_SENT_NZD = "sent_nzd"
    STATE_ERROR = "error"
    STATE_SUSPENDED = "suspended"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime())
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('settlements', lazy='dynamic'))
    token = db.Column(db.String(255), nullable=False, unique=True)
    bank_id = db.Column(db.Integer, db.ForeignKey('bank.id'), nullable=False)
    bank = db.relationship('Bank', backref=db.backref('settlements', lazy='dynamic'))
    amount = db.Column(db.Integer, nullable=False)
    settlement_address = db.Column(db.String(255), nullable=False)
    amount_receive = db.Column(db.Integer, nullable=False)
    txid = db.Column(db.String(255))
    status = db.Column(db.String(255), nullable=False)

    def __init__(self, user, bank, amount, settlement_address, amount_receive):
        self.date = datetime.datetime.now()
        self.user = user
        self.token = generate_key(4)
        self.bank = bank
        self.amount = amount
        self.settlement_address = settlement_address
        self.amount_receive = amount_receive
        self.txid = None
        self.status = Settlement.STATE_CREATED

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    @classmethod
    def from_token(cls, session, token):
        return session.query(cls).filter(cls.token == token).first()

    @classmethod
    def count_this_month(cls, session, user):
        now = datetime.datetime.now()
        # month start
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # month end
        next_month = now.replace(day=28) + datetime.timedelta(days=4)  # this will never fail
        last_day_of_month = next_month - datetime.timedelta(days=next_month.day)
        month_end = last_day_of_month.replace(hour=23, minute=59, second=59, microsecond=999999)
        return session.query(cls).filter(cls.user_id == user.id, cls.date >= month_start, cls.date <= month_end).count()

    @classmethod
    def all_sent_zap(cls, session):
        return session.query(cls).filter(cls.status == cls.STATE_SENT_ZAP).all()

    @classmethod
    def all_validated(cls, session):
        return session.query(cls).filter(cls.status == cls.STATE_VALIDATED).all()

    @classmethod
    def from_id_list(cls, session, ids):
        return session.query(cls).filter(cls.id.in_(ids)).all()

    def __repr__(self):
        return"<Settlement %r>" % (self.token)

    def to_json(self):
        schema = SettlementSchema()
        return schema.dump(self).data

class WavesTxSchema(Schema):
    date = fields.Date()
    txid = fields.String()
    type = fields.String()
    state = fields.String()
    amount = fields.Integer()
    json_data_signed = fields.Boolean()
    json_data = fields.String()

class WavesTx(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Integer, nullable=False)
    txid = db.Column(db.String, nullable=False, unique=True)
    type = db.Column(db.String, nullable=False)
    state = db.Column(db.String, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    json_data_signed = db.Column(db.Boolean, nullable=False)
    json_data = db.Column(db.String, nullable=False)

    def __init__(self, txid, type, state, amount, json_data_signed, json_data):
        self.date = time.time()
        self.type = type
        self.state = state
        self.txid = txid
        self.amount = amount
        self.json_data_signed = json_data_signed
        self.json_data = json_data

    @classmethod
    def from_txid(cls, session, txid):
        return session.query(cls).filter(cls.txid == txid).first()

    @classmethod
    def expire_transactions(cls, session, above_age, from_state, to_state):
        now = time.time()
        txs = session.query(cls).filter(cls.date < now - above_age, cls.state == from_state).all()
        for tx in txs:
            tx.state = to_state
            tx.json_data = ""
            session.add(tx)
        return len(txs)

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    def __repr__(self):
        return '<WavesTx %r>' % (self.txid)

    def to_json(self):
        tx_schema = WavesTxSchema()
        return tx_schema.dump(self).data

    def tx_with_sigs(self):
        tx = json.loads(self.json_data)
        if self.json_data_signed:
            return tx
        proofs = tx["proofs"]
        for sig in self.signatures:
            while sig.signer_index >= len(proofs):
                proofs.append('todo')
            proofs[sig.signer_index] = sig.value
        return tx

class WavesTxSig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    waves_tx_id = db.Column(db.Integer, db.ForeignKey('waves_tx.id'), nullable=False)
    waves_tx = db.relationship('WavesTx', backref=db.backref('signatures', lazy='dynamic'))
    signer_index = db.Column(db.Integer, nullable=False)
    value = db.Column(db.String, unique=False)

    def __init__(self, waves_tx, signer_index, value):
        self.waves_tx = waves_tx
        self.signer_index = signer_index
        self.value = value

#
# Setup Flask-Security-Too
#

user_datastore = SQLAlchemyUserDatastore(db, User, Role)
security = Security(app, user_datastore)

#
# Define model views
#

def date_format(view, value):
    return value.strftime('%Y.%m.%d %H:%M')

MY_DEFAULT_FORMATTERS = dict(typefmt.BASE_FORMATTERS)
MY_DEFAULT_FORMATTERS.update({
    datetime.date: date_format,
})

class DateBetweenFilter(BaseSQLAFilter, filters.BaseDateBetweenFilter):
    def __init__(self, column, name, options=None, data_type=None):
        super(DateBetweenFilter, self).__init__(column,
                                                name,
                                                options,
                                                data_type='daterangepicker')

    def apply(self, query, value, alias=None):
        start, end = value
        return query.filter(self.get_column(alias).between(start, end))

class FilterStartsWithInsensitive(BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.filter(self.get_column(alias).ilike(value + '%'))

    def operation(self):
        return lazy_gettext('starts with')

class FilterUserMerchantName(BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.join(Settlement.user).filter(User.merchant_code == value)
 
    def operation(self):
        return lazy_gettext('equals')

    def get_options(self, view):
        # This will return a generator which is reloaded every time it is used.
        # Without this we need to restart the server to update the cache of device names.
        return ReloadingIterator(get_merchant_names)

class FilterBoolean(BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.filter(self.get_column(alias) == value)
 
    def operation(self):
        return lazy_gettext('equals')

    def get_options(self, view):
        return [(True, 'True'), (False, 'False')]

class FilterEqual(BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.filter(self.get_column(alias) == value)

    def operation(self):
        return lazy_gettext('equals')

class FilterNotEqual(BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.filter(self.get_column(alias) != value)

    def operation(self):
        return lazy_gettext('not equal')

class FilterGreater(BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.filter(self.get_column(alias) > value)

    def operation(self):
        return lazy_gettext('greater than')

class FilterSmaller(BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.filter(self.get_column(alias) < value)

    def operation(self):
        return lazy_gettext('smaller than')

class DateTimeGreaterFilter(FilterGreater, filters.BaseDateTimeFilter):
    pass

class DateSmallerFilter(FilterSmaller, filters.BaseDateFilter):
    pass

def _format_amount(view, context, model, name):
    if name == 'amount':
        return Markup(model.amount / 100)
    if name == 'amount_receive':
        return Markup(model.amount_receive / 100)
    if name == 'amount_nzd':
        return round((model.amount_nzd / 100),2)

def get_merchant_names():
    if has_app_context():
        if not hasattr(g, 'merchant_names'):
            query = db.session.query(User)
            g.merchant_names = [(row.merchant_code, row.merchant_name) for row in query.all()]
        for merchant_code, merchant_name in g.merchant_names:
            yield merchant_code, '{0} - {1}'.format(merchant_name, merchant_code)

def get_device_names():
    if has_app_context():
        if not hasattr(g, 'device_names'):
            query = db.session.query(MerchantTx.device_name.distinct().label('device_name')).filter(MerchantTx.user_id == current_user.id)
            g.device_names = [row.device_name for row in query.all()]
        for device_name in g.device_names:
            yield device_name, device_name

def get_categories():
    if has_app_context():
        if not hasattr(g, 'categories'):
            query = db.session.query(MerchantTx.category.distinct().label('category')).filter(MerchantTx.user_id == current_user.id, MerchantTx.category != None)
            g.categories = [row.category for row in query.all()]
        for category in g.categories:
            yield category, category

def get_settlement_statuses():
    if has_app_context():
        if not hasattr(g, 'settlement_statuses'):
            query = db.session.query(Settlement.status.distinct().label('status'))
            g.settlement_statuses = [row.status for row in query.all()]
        for status in g.settlement_statuses:
            yield status, status

def _format_direction(view, context, model, name):
    if model.direction == 0:
        return Markup('out')
    elif model.direction == 1:
        return Markup('in')

def format_rebate_address(view, context, model, name):
    NODE_BASE_URL = app.config["NODE_ADDRESS"]
    pywaves.setNode(NODE_BASE_URL, app.config["NODE_BASE_ENV"])
    pywaves.setChain(app.config["NODE_BASE_ENV"])
    if model.wallet_seed:
        rebate_addr = pywaves.Address(seed='{}'.format(model.wallet_seed))
        return rebate_addr.address
    return False

class ReloadingIterator:
    def __init__(self, iterator_factory):
        self.iterator_factory = iterator_factory

    def __iter__(self):
        return self.iterator_factory()

class FilterByDeviceName(BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.filter(MerchantTx.device_name == value)

    def operation(self):
        return u'equals'

    def get_options(self, view):
        # This will return a generator which is reloaded every time it is used.
        # Without this we need to restart the server to update the cache of device names.
        return ReloadingIterator(get_device_names)

class FilterByCategory(BaseSQLAFilter):
    def apply(self, query, value):
        return query.filter(MerchantTx.category == value)

    def operation(self):
        return u'equals'

    def get_options(self, view):
        # This will return a generator which is reloaded every time it is used.
        # Without this we need to restart the server to update the cache of device names.
        return ReloadingIterator(get_categories)

class FilterBySettlementStatus(BaseSQLAFilter):
    def apply(self, query, value):
        return query.filter(Settlement.status == value)

    def operation(self):
        return u'equals'

    def get_options(self, view):
        # This will return a generator which is reloaded every time it is used.
        # Without this we need to restart the server to update the cache of device names.
        return ReloadingIterator(get_settlement_statuses)

class BaseModelView(sqla.ModelView):
    def _handle_view(self, name, **kwargs):
        """
        Override builtin _handle_view in order to redirect users when a view is not accessible.
        """
        if not self.is_accessible():
            if current_user.is_authenticated:
                # permission denied
                abort(403)
            else:
                # login
                return redirect(url_for('security.login', next=request.url))

class BaseOnlyUserOwnedModelView(BaseModelView):
    def is_accessible(self):
        return (current_user.is_active and
                current_user.is_authenticated)

    def get_query(self):
        return self.session.query(self.model).filter(self.model.user==current_user)

    def get_count_query(self):
        return self.session.query(db.func.count('*')).filter(self.model.user==current_user)

class RestrictedModelView(BaseModelView):
    can_create = False
    can_delete = False
    can_edit = False
    column_exclude_list = ['password', 'secret']

    extra_roles = []

    def check_roles(self):
        if current_user.has_role('admin'):
            return True
        for role in self.extra_roles:
            if current_user.has_role(role):
                return True
        return False

    def is_accessible(self):
        return (current_user.is_active and
                current_user.is_authenticated and
                self.check_roles())

def validate_recipient(recipient):
    if not recipient or \
       (not is_email(recipient) and not is_mobile(recipient) and not is_address(recipient)):
       return False
    ##TODO: direct wallet address not yet implemented
    if is_address(recipient):
        return False
    return True

def validate_csv(data):
    rows = []
    try:
        data = data.decode('utf-8')
    except:
        return False
    data = data.splitlines()
    reader = csv.reader(data)
    for row in reader:
        if len(row) != 3:
            return False
        recipient, message, amount = row
        if not validate_recipient(recipient):
            return False
        try:
            amount = decimal.Decimal(amount)
        except ValueError:
            return False
        if amount <= 0:
            return False
        rows.append((recipient, message, amount))
    return rows

class UserModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False

    def validate_email_address(form, field):
        if not is_email(field.data):
            raise ValidationError('invalid email address format')

    def on_model_change(self, form, model, is_created):
        if is_created:
            model.on_admin_created()

    def after_model_change(self, form, model, is_created):
        if is_created:
            send_reset_password_instructions(model)

    def _format_information_column(view, context, model, name):
        for seed_wallet, in db.session.query(Seeds.wallet_seed).filter(Seeds.user_id == model.id):
            if not seed_wallet:
        #if not seed_wallet:
            #return ''
                return ''
            NODE_BASE_URL = app.config["NODE_ADDRESS"]
            pywaves.setNode(NODE_BASE_URL, app.config["NODE_BASE_ENV"])
            rebate_addr = pywaves.Address(seed='{}'.format(seed_wallet))
            node_url = app.config["NODE_ADDRESS"]
            asset_id = app.config["ASSET_ID"]
            explorer_url = app.config["BLOCKCHAIN_EXPLORER"]
            html = '''
                wallet address: <a href="{explorer_url}/address/{address}/tx">{address}</a>
                wallet balance:
                <span id="{address}-balance"></span>
                <script>
                    var xhr = new XMLHttpRequest();
                    var url = "{node_url}/assets/balance/{address}/{asset_id}";
                    xhr.onreadystatechange = function() {{
                        if (this.readyState == 4 && this.status == 200) {{
                            var json = JSON.parse(this.responseText);
                            var bal = (json.balance / 100).toFixed(2);
                            document.getElementById("{address}-balance").textContent = bal + ' ZAP';
                        }}
                    }};
                    xhr.open("GET", url, true);
                    xhr.send();
                </script><br/>
                bank account number: {bank_account_number}
            '''.format(address=rebate_addr.address, node_url=node_url, asset_id=asset_id, explorer_url=explorer_url, bank_account_number=model.banks.first())
            return Markup(html)

    def _format_address_column_export(view, context, model, name):
        if not model.wallet_address:
            return Markup('-')
        return Markup(model.wallet_address)

    def _format_bank_account(view, context, model, name):
        bank_account_number=model.banks.first()
        if bank_account_number:
            return Markup(bank_account_number)
        return Markup('-')

    def _get_wallet_seed(view, context, model, name):
        NODE_BASE_URL = app.config["NODE_ADDRESS"]
        pywaves.setNode(NODE_BASE_URL, app.config["NODE_BASE_ENV"])
        for seed_wallet, in db.session.query(Seeds.wallet_seed).filter(Seeds.user_id == model.id):
            return seed_wallet

    def _get_wallet_addr(view, context, model, name):
        NODE_BASE_URL = app.config["NODE_ADDRESS"]
        pywaves.setNode(NODE_BASE_URL, app.config["NODE_BASE_ENV"])
        for seed_wallet, in db.session.query(Seeds.wallet_seed).filter(Seeds.user_id == model.id):
            if seed_wallet:
                rebate_addr = pywaves.Address(seed='{}'.format(seed_wallet))
                return rebate_addr.address
            return False 

    column_list = ['merchant_name', 'email', 'roles', 'active', 'max_settlements_per_month', 'settlement_fee', 'merchant_rate', 'customer_rate', 'wallet_address', 'information']
    column_formatters = dict(information=_format_information_column, bank_account_number=_format_bank_account)
    column_filters = [ FilterStartsWithInsensitive(User.merchant_name, 'Search Merchant Name'), FilterStartsWithInsensitive(User.email, 'Search Email'), FilterBoolean(User.active, 'Filter Active') ]
    column_labels = {'bank_account_number': 'Bank AccountNumber(Active)'}
    column_formatters_export = {'wallet_address': _format_address_column_export, 'bank_account_number': _format_bank_account}
    column_details_list = ['merchant_name', 'merchant_code', 'email', 'roles', 'active', 'confirmed_at', 'max_settlements_per_month', 'settlement_fee', 'merchant_rate', 'customer_rate', 'wallet_address', 'bank_account_number']
    column_export_list = ['merchant_name', 'merchant_code', 'email', 'roles', 'active', 'confirmed_at', 'max_settlements_per_month', 'settlement_fee', 'merchant_rate', 'customer_rate', 'wallet_address', 'bank_account_number']
    form_args = dict(
        email=dict(validators=[DataRequired(), validate_email_address]),
        merchant_name=dict(validators=[DataRequired()])
    )

class AdminUserModelView(UserModelView):
    can_create = True
    can_export = True
    can_view_details = True
    def is_accessible(self):
        return (current_user.has_role('admin'))

    #column_editable_list = ['merchant_name', 'roles', 'max_settlements_per_month', 'settlement_fee', 'merchant_rate', 'customer_rate', 'active']
    #column_editable_list = ['merchant_name', 'roles', 'max_settlements_per_month', 'settlement_fee', 'active']
    column_editable_list = ['merchant_name', 'roles', 'max_settlements_per_month', 'active']
    form_columns = ['roles', 'merchant_name', 'email']

class FinanceUserModelView(UserModelView):
    can_create = True
    can_export = True
    can_view_details = True
    def is_accessible(self):
        return (current_user.has_role('finance'))

    #column_editable_list = ['merchant_name', 'max_settlements_per_month', 'settlement_fee', 'merchant_rate', 'customer_rate', 'active']
    column_editable_list = ['merchant_name', 'max_settlements_per_month', 'active']
    form_columns = ['merchant_name', 'email']

class BankAdminModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    can_export = True

class ClaimCodeModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    can_export = True
    column_exclude_list = ['password', 'secret']
    column_export_exclude_list = ['secret']
    column_filters = [ DateBetweenFilter(ClaimCode.date, 'Search Date'), DateTimeGreaterFilter(ClaimCode.date, 'Search Date'), DateSmallerFilter(ClaimCode.date, 'Search Date'), FilterEqual(ClaimCode.status, 'Search Status'), FilterNotEqual(ClaimCode.status, 'Search Status') ]

class TxNotificationModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    can_export = True
    column_filters = [ DateBetweenFilter(TxNotification.date, 'Search Date'), DateTimeGreaterFilter(TxNotification.date, 'Search Date'), DateSmallerFilter(TxNotification.date, 'Search Date') ]

class SettlementAdminModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    can_export = True
    column_filters = [DateBetweenFilter(Settlement.date, 'Search Date'), DateTimeGreaterFilter(Settlement.date, 'Search Date'), DateSmallerFilter(Settlement.date, 'Search Date'), FilterGreater(Settlement.amount, 'Search Amount'), FilterSmaller(Settlement.amount, 'Search Amount'), FilterBySettlementStatus(Settlement.status, 'Search Status'), FilterUserMerchantName(None, 'Search Merchant Name')]
    list_template = 'settlement_list.html'

    extra_roles = ['finance']

    def _format_status_column(view, context, model, name):
        if model.status in (model.STATE_CREATED, model.STATE_SENT_NZD):
            return model.status
        if current_user.has_role('admin') or current_user.has_role('finance'):
            if model.status in (model.STATE_ERROR, model.STATE_SUSPENDED):
                reset_url = url_for('.reset_view')
                html = '''
                    {status}
                    <form action="{reset_url}" method="POST">
                        <input id="settlement_id" name="settlement_id"  type="hidden" value="{settlement_id}">
                        <button type='submit'>Reset</button>
                    </form>
                '''.format(status=model.status, reset_url=reset_url, settlement_id=model.id)
                return Markup(html)
            if model.status in (model.STATE_SENT_ZAP, model.STATE_VALIDATED):
                suspend_url = url_for('.suspend_view')
                html = '''
                    {status}
                    <form action="{suspend_url}" method="POST">
                        <input id="settlement_id" name="settlement_id"  type="hidden" value="{settlement_id}">
                        <button type='submit'>Suspend</button>
                    </form>
                '''.format(status=model.status, suspend_url=suspend_url, settlement_id=model.id)
                return Markup(html)
        return model.status

    column_formatters = dict(amount=_format_amount, amount_receive=_format_amount, status=_format_status_column)
    column_labels = dict(amount='ZAP Amount', amount_receive='NZD Amount')

    def settlement_validated(self, settlement):
        if not settlement.txid:
            return None
        tx = aw.transfer_tx(settlement.txid)
        if not tx:
            logger.error("settlement (%s) tx %s not found" % (settlement.token, settlement.txid))
            return None
        if tx["recipient"] != settlement.settlement_address:
            logger.error("settlement (%s) tx recipient is not correct" % (settlement.token, tx["recipient"]))
            return False
        if tx["assetId"] != aw.asset_id:
            return False
            logger.error("settlement (%s) tx asset ID (%s) is not correct" % (settlement.token, tx["assetId"]))
        amount = int(decimal.Decimal(tx["amount"]) * 100)
        if amount != settlement.amount:
            logger.error("settlement (%s) tx amount (%d) is not correct" % (settlement.token, amount))
            return False
        if not tx["attachment"]:
            logger.error("settlement (%s) tx attachment is empty" % settlement.token)
            return False
        attachment = base58.b58decode(tx["attachment"]).decode("utf-8")
        found_token = attachment == settlement.token
        if not found_token:
            try:
                found_token = json.loads(attachment)["msg"] == settlement.token
            except:
                pass
        if not found_token:
            logger.error("settlement (%s) tx attachment (%s) is not correct" % (settlement.token, attachment))
            return False
        return True

    @expose('reset', methods=['POST'])
    def reset_view(self):
        return_url = self.get_url('.index_view')
        # check permission
        if not (current_user.has_role('admin') or current_user.has_role('finance')):
            # permission denied
            flash('Not authorized.', 'error')
            return redirect(return_url)
        # get the model from the database
        form = get_form_data()
        if not form:
            flash('Could not get form data.', 'error')
            return redirect(return_url)
        settlement_id = form['settlement_id']
        settlement = self.get_one(settlement_id)
        if settlement is None:
            flash('Settlement not not found.', 'error')
            return redirect(return_url)
        # process the settlement
        if settlement.status in (settlement.STATE_ERROR, settlement.STATE_SUSPENDED):
            settlement.status = settlement.STATE_SENT_ZAP
        # commit to db
        try:
            self.session.commit()
            flash('Settlement {settlement_id} set as sent_zap'.format(settlement_id=settlement_id))
        except Exception as ex:
            if not self.handle_view_exception(ex):
                raise
            flash('Failed to set Settlement {settlement_id} as sent_zap'.format(settlement_id=settlement_id), 'error')
        return redirect(return_url)

    @expose('suspend', methods=['POST'])
    def suspend_view(self):
        return_url = self.get_url('.index_view')
        # check permission
        if not (current_user.has_role('admin') or current_user.has_role('finance')):
            # permission denied
            flash('Not authorized.', 'error')
            return redirect(return_url)
        # get the model from the database
        form = get_form_data()
        if not form:
            flash('Could not get form data.', 'error')
            return redirect(return_url)
        settlement_id = form['settlement_id']
        settlement = self.get_one(settlement_id)
        if settlement is None:
            flash('Settlement not not found.', 'error')
            return redirect(return_url)
        # process the settlement
        if settlement.status in (settlement.STATE_SENT_ZAP, settlement.STATE_VALIDATED):
            settlement.status = settlement.STATE_SUSPENDED
        # commit to db
        try:
            self.session.commit()
            flash('Settlement {settlement_id} set as suspended'.format(settlement_id=settlement_id))
        except Exception as ex:
            if not self.handle_view_exception(ex):
                raise
            flash('Failed to set Settlement {settlement_id} as suspended'.format(settlement_id=settlement_id), 'error')
        return redirect(return_url)

    @expose("/validate")
    def validate(self):
        count = 0
        settlements = Settlement.all_sent_zap(db.session)
        for settlement in settlements:
            res = self.settlement_validated(settlement)
            if res == None:
                continue
            if res:
                settlement.status = Settlement.STATE_VALIDATED
            else:
                settlement.status = Settlement.STATE_ERROR
            count += 1
            db.session.add(settlement)
        db.session.commit()
        flash('%d Settlements validated' % count)
        return redirect('./')

    @expose('/settle', methods=['GET', 'POST'])
    def execute(self):
        process = request.args.get('process', False, bool)
        ids = request.args.get('ids')
        if ids:
            ids = [int(id_) for id_ in ids.split(',')]
            settlements = Settlement.from_id_list(db.session, ids)
        else:
            settlements = Settlement.all_validated(db.session)
        count = len(settlements)
        if process and ids and request.method == 'POST':
            for settlement in settlements:
                settlement.status = Settlement.STATE_SENT_NZD
                db.session.add(settlement)
            db.session.commit()
            flash('Settlements processed')
            return redirect('')
        ids = ','.join([str(settlement.id) for settlement in settlements])
        return self.render('settle.html', count=count, settlements=settlements, ids=ids, process=process)

    @expose('/ib4b')
    def ib4b(self):
        ids = request.args.get('ids')
        if ids:
            ids = [int(id_) for id_ in ids.split(',')]
            settlements = Settlement.from_id_list(db.session, ids)
        else:
            abort(400)
        return ib4b_response("bnz_batch.txt", settlements, app.config["SENDER_NAME"], app.config["SENDER_BANK_ACCOUNT"])

class MerchantTxModelView(BaseOnlyUserOwnedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    can_export = True
    column_default_sort = ('date', True)
    column_exclude_list = ['user', 'wallet_address']
    column_formatters = {'amount':_format_amount, 'direction':_format_direction, 'amount_nzd':_format_amount}
    column_list = ['date', 'amount', 'amount_nzd', 'txid', 'direction', 'category', 'attachment', 'device_name']
    column_labels = dict(amount_nzd='Amount (NZD)')
    column_filters = [ DateBetweenFilter(MerchantTx.date, 'Search Date'), DateTimeGreaterFilter(MerchantTx.date, 'Search Date'), DateSmallerFilter(MerchantTx.date, 'Search Date'), FilterGreater(MerchantTx.amount, 'Search Amount'), FilterSmaller(MerchantTx.amount, 'Search Amount'), FilterByDeviceName(MerchantTx.device_name, 'Search Device Name'), FilterByCategory(MerchantTx.category, 'Search Category') ]
    list_template = 'merchanttx_list.html'

    @expose("/update")
    def update(self):
        if not current_user.wallet_address:
            flash('Account does not have wallet address set')
        else:
            MerchantTx.update_wallet_address(db.session, current_user)
            flash('Updated')
        return redirect('./')

    def _add_rebate():
        customer_rate = user.customer_rate / 100 if user.customer_rate else app.config["CUSTOMER_RATE"]
        merchant_rate = user.merchant_rate / 100 if user.merchant_rate else app.config["MERCHANT_RATE"]
        amount = amount * (merchant_rate / 100)
        email = recipient if is_email(recipient) else None
        mobile = recipient if is_mobile(recipient) else None
        address = recipient if is_address(recipient) else None
        message = "You can claim a payment of {} Zap".format(amount)
        #payment = Payment(model, mobile, email, address, message, amount)
        #self.session.add(payment)
        return "this is a test"

    def after_model_change(self, form, model, is_created):
        test = self._add_rebate()
        print(test)
        logger.info(test)

class BankModelView(BaseOnlyUserOwnedModelView):
    can_create = True
    can_delete = False
    can_edit = False
    can_export = True
    column_exclude_list = ['user', 'token', 'settlements']
    form_excluded_columns = ['user', 'token', 'settlements']
    column_editable_list = ['default_account']

    def validate_bank_account(form, field):
        if not bankaccount_is_valid(field.data):
            raise ValidationError('invalid bank account')

    form_args = dict(account_number=dict(validators=[validate_bank_account]))

    def on_model_change(self, form, model, is_created):
        if is_created:
            model.generate_defaults()

    def after_model_change(self, form, model, is_created):
        model.ensure_default_account_exclusive(db.session)
        db.session.commit()

class ApiKeyModelView(BaseOnlyUserOwnedModelView):
    can_create = True
    can_delete = True
    can_edit = False
    column_list = ('date', 'name', 'token', 'secret', 'QRCode', 'account_admin')
    form_excluded_columns = ['user', 'date', 'token', 'nonce', 'secret']
    column_labels = dict(token='API Key', secret='API Secret')

    def _format_qrcode(view, context, model, name):
        admin = model.account_admin if model.account_admin else False
        address = model.user.wallet_address if model.user.wallet_address else ''
        url = urlparse(request.base_url)
        scheme = url.scheme
        if 'X-Forwarded-Proto' in request.headers:
            scheme = request.headers['X-Forwarded-Proto']
        server = '{}://{}/'.format(scheme, url.netloc)
        data = 'zapm_apikey:%s?secret=%s&name=%s&admin=%r&address=%s&server=%s' % (model.token, model.secret, model.name, admin, address, server)
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(data, image_factory=factory)
        output = io.BytesIO()
        img.save(output)
        svg = output.getvalue().decode('utf-8')
        modal = '''
<div id="modal_%s" class="modal fade" role="dialog">
  <div class="modal-dialog">
    <!-- Modal content-->
    <div class="modal-content">
      <div class="modal-header">
        <button type="button" class="close" data-dismiss="modal" aria-hidden="true">x</button>
        <h4>Api Key QR Code</h4>
      </div>
      <div class="modal-body" style="text-align: center">
        %s
      </div>
    </div>
  </div>
</div>''' % (model.token, svg)
        
        link = '<a href="#" data-keyboard="true" data-toggle="modal" data-target="#modal_%s"><img src="/static/qrcode.svg"/></a>' % model.token
        html = '%s %s' % (modal, link)
        return Markup(html)

    column_formatters = dict(QRCode=_format_qrcode)

    def on_model_change(self, form, model, is_created):
        if is_created:
            with db.session.no_autoflush:
                if form.account_admin.data and ApiKey.admin_exists(db.session, current_user):
                    raise ValidationError('Account admin already exists')
            model.generate_defaults()

class SettlementModelView(BaseOnlyUserOwnedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    column_exclude_list = ['user']
    column_export_exclude_list = ['user']

    column_formatters = dict(amount=_format_amount, amount_receive=_format_amount)
    column_labels = dict(amount='ZAP Amount', amount_receive='NZD Amount')

class ProposalUserModelView(BaseOnlyUserOwnedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    can_export = True

    def _format_proposer_column(view, context, model, name):
        if name == 'proposer':
            if not model.proposer:
                return ''
            email = model.proposer.email
        elif name == 'authorizer':
            if not model.authorizer:
                return ''
            email = model.authorizer.email
        else:
            raise Exception('invalid column')
        name = email.split("@")[0]
        html = '<span title="{email}">{name}</span>'.format(email=email, name=name)
        return Markup(html)

    def _format_status_column(view, context, model, name):
        if model.status in (model.STATE_AUTHORIZED, model.STATE_DECLINED, model.STATE_EXPIRED):
            return model.status
        if current_user.has_role('admin') or current_user.has_role('finance') or current_user.has_role('merchant'):
            authorize_url = url_for('.authorize_view')
            decline_url = url_for('.decline_view')
            html = '''
                <form action="{authorize_url}" method="POST">
                    <input id="proposal_id" name="proposal_id"  type="hidden" value="{proposal_id}">
                    <button type='submit'>Authorise</button>
                </form>
                <form action="{decline_url}" method="POST">
                    <input id="proposal_id" name="proposal_id"  type="hidden" value="{proposal_id}">
                    <button type='submit'>Decline</button>
                </form>
            '''.format(authorize_url=authorize_url, decline_url=decline_url, proposal_id=model.id)
            return Markup(html)
        return model.status

    def _format_claimed(view, model):
        if model.status == model.STATE_DECLINED:
            return '-'
        total_claimed = 0
        for payment in model.payments:
            if payment.status == payment.STATE_SENT_FUNDS:
                total_claimed += payment.amount
        total_claimed = decimal.Decimal(total_claimed) / 100
        return total_claimed

    def _format_claimed_column(view, context, model, name):
        total_claimed = view._format_claimed(model)
        payments_url = url_for('.payments_view', proposal_id=model.id)

        html = '''
            <a href="{payments_url}">{total_claimed}</a>
        '''.format(payments_url=payments_url, total_claimed=total_claimed)
        return Markup(html)

    def _format_total_column(view, context, model, name):
        if model.status == model.STATE_DECLINED:
            return Markup('-')
        total = 0
        for payment in model.payments:
            total += payment.amount
        total = total / 100
        return Markup(total)

    def _format_totalclaimed_column_export(view, context, model, name):
        total_claimed = view._format_claimed(model)
        return Markup(total_claimed)

    column_default_sort = ('id', True)
    column_list = ('id', 'date', 'proposer', 'categories', 'authorizer', 'reason', 'date_authorized', 'date_expiry', 'status', 'Proposed Total', 'Claimed')
    column_labels = {'proposer': 'Proposed by', 'authorizer': 'Authorized by'}
    column_type_formatters = MY_DEFAULT_FORMATTERS
    column_formatters = {'proposer': _format_proposer_column, 'authorizer': _format_proposer_column, 'status': _format_status_column, 'Proposed Total': _format_total_column, 'Claimed': _format_claimed_column}
    #column_filters = [ DateBetweenFilter(Proposal.date, 'Search Date'), DateTimeGreaterFilter(Proposal.date, 'Search Date'), DateSmallerFilter(Proposal.date, 'Search Date'), FilterByStatusEqual(None, 'Search Status'), FilterByStatusNotEqual(None, 'Search Status'), FilterByProposer(None, 'Search Proposer'), FilterByAuthorizer(None, 'Search Authorizer'), FilterByCategory(None, 'Search Category') ]
    column_export_list = ('id', 'date', 'merchant', 'proposer', 'categories', 'authorizer', 'reason', 'date_authorized', 'date_expiry', 'status', 'total', 'claimed')
    column_formatters_export = {'total': _format_total_column, 'claimed': _format_totalclaimed_column_export}
    form_columns = ['reason', 'categories', 'recipient', 'message', 'amount', 'csvfile']
    form_extra_fields = {'recipient': TextField('Recipient'), 'message': TextField('Message'), 'amount': DecimalField('Amount', validators=[validators.Optional()]), 'csvfile': FileField('CSV File')}

    def _validate_form(self, form):
        csv_rows = None
        if not form.reason.data:
            return False, "Empty reason value", csv_rows
        # do csv file first
        if form.csvfile.data:
            csv_rows = validate_csv(form.csvfile.data.stream.read())
            if not csv_rows:
                return False, "Invalid CSV file", csv_rows
        else:
            # if not csv file then do other:
            if not validate_recipient(form.recipient.data):
                return False, "Recipient is invalid", csv_rows
            if not form.amount.data or form.amount.data <= 0:
                return False, "Amount must be greater then 0", csv_rows
        return True, "", csv_rows

    def _add_payment(self, model, recipient, message, amount):
        email = recipient if is_email(recipient) else None
        mobile = recipient if is_mobile(recipient) else None
        address = recipient if is_address(recipient) else None
        amount = int(amount * 100)
        payment = Payment(model, mobile, email, address, message, amount)
        self.session.add(payment)

    def on_model_change(self, form, model, is_created):
        if is_created:
            # validate
            res, msg, csv_rows = self._validate_form(form)
            if not res:
                raise validators.ValidationError(msg)
            # generate model defaults
            model.generate_defaults()
            # check csv file first
            if form.csvfile.data:
                for recipient, message, amount in csv_rows:
                    self._add_payment(model, recipient, message, amount)
            # or just process basic fields
            else:
                recipient = form.recipient.data
                message = form.message.data
                amount = form.amount.data
                self._add_payment(model, recipient, message, amount)

    def is_accessible(self):
        if not (current_user.is_active and current_user.is_authenticated):
            return False
        if current_user.has_role('admin'):
            self.can_create = True
            return True
        if current_user.has_role('finance'):
            self.can_create = True
            return True
        if current_user.has_role('merchant'):
            self.can_create = True
            return True
        return False

    @expose('authorize', methods=['POST'])
    def authorize_view(self):
        return_url = self.get_url('.index_view')
        # check permission
        if not (current_user.has_role('admin') or current_user.has_role('finance') or current_user.has_role('merchant')):
            # permission denied
            flash('Not authorized.', 'error')
            return redirect(return_url)
        # get the model from the database
        form = get_form_data()
        if not form:
            flash('Could not get form data.', 'error')
            return redirect(return_url)
        proposal_id = form['proposal_id']
        proposal = self.get_one(proposal_id)
        if proposal is None:
            flash('Proposal not not found.', 'error')
            return redirect(return_url)
        # process the proposal
        if proposal.status == proposal.STATE_CREATED:
            proposal.status = proposal.STATE_AUTHORIZED
            now = datetime.datetime.now()
            proposal.date_authorized = now
            proposal.date_expiry = now + datetime.timedelta(hours = Proposal.HOURS_EXPIRY)
            proposal.authorizer = current_user

        # commit to db
        try:
            self.session.commit()
            flash('Proposal {proposal_id} set as authorized'.format(proposal_id=proposal_id))
        except Exception as ex:
            if not self.handle_view_exception(ex):
                raise
            flash('Failed to set proposal {proposal_id} as authorized'.format(proposal_id=proposal_id), 'error')
        return redirect(return_url)

    @expose('decline', methods=['POST'])
    def decline_view(self):
        return_url = self.get_url('.index_view')
        # check permission
        if not (current_user.has_role('admin') or current_user.has_role('finance') or current_user.has_role('merchant')):
            # permission denied
            flash('Not authorized.', 'error')
            return redirect(return_url)
        # get the model from the database
        form = get_form_data()
        if not form:
            flash('Could not get form data.', 'error')
            return redirect(return_url)
        proposal_id = form['proposal_id']
        proposal = self.get_one(proposal_id)
        if proposal is None:
            flash('Proposal not not found.', 'error')
            return redirect(return_url)
        # process the proposal
        if proposal.status == proposal.STATE_CREATED:
            proposal.status = proposal.STATE_DECLINED
            proposal.authorizer = current_user
        # commit to db
        try:
            self.session.commit()
            flash('Proposal {proposal_id} set as declined'.format(proposal_id=proposal_id))
        except Exception as ex:
            if not self.handle_view_exception(ex):
                raise
            flash('Failed to set proposal {proposal_id} as declined'.format(proposal_id=proposal_id), 'error')
        return redirect(return_url)

    @expose('payments/<proposal_id>', methods=['GET'])
    def payments_view(self, proposal_id):
        return_url = self.get_url('.index_view')
        # check permission
        if not (current_user.has_role('admin') or current_user.has_role('finance') or current_user.has_role('merchant')):
            # permission denied
            flash('Not authorized.', 'error')
            return redirect(return_url)
        # get the model from the database
        proposal = self.get_one(proposal_id)
        if proposal is None:
            flash('Proposal not not found.', 'error')
            return redirect(return_url)
        # show the proposal payments
        return self.render('admin/payments.html', payments=proposal.payments)

class SeedsUserModelView(BaseOnlyUserOwnedModelView):
    can_create = True
    can_delete = True
    can_edit = False
    can_export = False

    def on_model_change(self, form, model, is_created):
        if is_created:
            model.generate_defaults()

    def after_model_change(self, form, model, is_created):
        if is_created:
            db.session.query(User).filter(User.id == model.user_id).update({User.wallet_address: model.wallet_address})
        db.session.query(User).filter(User.id == model.user_id).update({User.settlement_fee: model.settlement_fee, User.customer_rate: model.customer_rate, User.merchant_rate: model.merchant_rate})
        db.session.commit()

    form_columns = ['wallet_seed']
    column_editable_list = ['settlement_fee', 'merchant_rate', 'customer_rate']

class SeedsAdminModelView(RestrictedModelView):
    can_create = True
    can_delete = True
    can_edit = False
    can_export = False

    def on_model_change(self, form, model, is_created):
        if is_created:
            model.generate_defaults()

    def after_model_change(self, form, model, is_created):
        if is_created:
            db.session.query(User).filter(User.id == model.user_id).update({User.wallet_address: model.wallet_address})
        db.session.query(User).filter(User.id == model.user_id).update({User.settlement_fee: model.settlement_fee, User.customer_rate: model.customer_rate, User.merchant_rate: model.merchant_rate})
        db.session.commit()

    form_columns = ['wallet_seed']
    column_editable_list = ['settlement_fee', 'merchant_rate', 'customer_rate']

class ProposalAdminModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    can_export = True

    def _format_proposer_column(view, context, model, name):
        if name == 'proposer':
            if not model.proposer:
                return ''
            email = model.proposer.email
        elif name == 'authorizer':
            if not model.authorizer:
                return ''
            email = model.authorizer.email
        else:
            raise Exception('invalid column')
        name = email.split("@")[0]
        html = '<span title="{email}">{name}</span>'.format(email=email, name=name)
        return Markup(html)

    def _format_status_column(view, context, model, name):
        if model.status in (model.STATE_AUTHORIZED, model.STATE_DECLINED, model.STATE_EXPIRED):
            return model.status
        if current_user.has_role('admin') or current_user.has_role('finance') or current_user.has_role('merchant'):
            authorize_url = url_for('.authorize_view')
            decline_url = url_for('.decline_view')
            html = '''
                <form action="{authorize_url}" method="POST">
                    <input id="proposal_id" name="proposal_id"  type="hidden" value="{proposal_id}">
                    <button type='submit'>Authorise</button>
                </form>
                <form action="{decline_url}" method="POST">
                    <input id="proposal_id" name="proposal_id"  type="hidden" value="{proposal_id}">
                    <button type='submit'>Decline</button>
                </form>
            '''.format(authorize_url=authorize_url, decline_url=decline_url, proposal_id=model.id)
            return Markup(html)
        return model.status

    def _format_claimed(view, model):
        if model.status == model.STATE_DECLINED:
            return '-'
        total_claimed = 0
        for payment in model.payments:
            if payment.status == payment.STATE_SENT_FUNDS:
                total_claimed += payment.amount
        total_claimed = decimal.Decimal(total_claimed) / 100
        return total_claimed

    def _format_claimed_column(view, context, model, name):
        total_claimed = view._format_claimed(model)
        payments_url = url_for('.payments_view', proposal_id=model.id)

        html = '''
            <a href="{payments_url}">{total_claimed}</a>
        '''.format(payments_url=payments_url, total_claimed=total_claimed)
        return Markup(html)

    def _format_total_column(view, context, model, name):
        if model.status == model.STATE_DECLINED:
            return Markup('-')
        total = 0
        for payment in model.payments:
            total += payment.amount
        total = total / 100
        return Markup(total)

    def _format_totalclaimed_column_export(view, context, model, name):
        total_claimed = view._format_claimed(model)
        return Markup(total_claimed)

    column_default_sort = ('id', True)
    column_list = ('id', 'date', 'proposer', 'categories', 'authorizer', 'reason', 'date_authorized', 'date_expiry', 'status', 'Proposed Total', 'Claimed')
    column_labels = {'proposer': 'Proposed by', 'authorizer': 'Authorized by'}
    column_type_formatters = MY_DEFAULT_FORMATTERS
    column_formatters = {'proposer': _format_proposer_column, 'authorizer': _format_proposer_column, 'status': _format_status_column, 'Proposed Total': _format_total_column, 'Claimed': _format_claimed_column}
    #column_filters = [ DateBetweenFilter(Proposal.date, 'Search Date'), DateTimeGreaterFilter(Proposal.date, 'Search Date'), DateSmallerFilter(Proposal.date, 'Search Date'), FilterByStatusEqual(None, 'Search Status'), FilterByStatusNotEqual(None, 'Search Status'), FilterByProposer(None, 'Search Proposer'), FilterByAuthorizer(None, 'Search Authorizer'), FilterByCategory(None, 'Search Category') ]
    column_export_list = ('id', 'date', 'merchant', 'proposer', 'categories', 'authorizer', 'reason', 'date_authorized', 'date_expiry', 'status', 'total', 'claimed')
    column_formatters_export = {'total': _format_total_column, 'claimed': _format_totalclaimed_column_export}
    form_columns = ['reason', 'categories', 'recipient', 'message', 'amount', 'csvfile']
    form_extra_fields = {'recipient': TextField('Recipient'), 'message': TextField('Message'), 'amount': DecimalField('Amount', validators=[validators.Optional()]), 'csvfile': FileField('CSV File')}

    def _validate_form(self, form):
        csv_rows = None
        if not form.reason.data:
            return False, "Empty reason value", csv_rows
        # do csv file first
        if form.csvfile.data:
            csv_rows = validate_csv(form.csvfile.data.stream.read())
            if not csv_rows:
                return False, "Invalid CSV file", csv_rows
        else:
            # if not csv file then do other:
            if not validate_recipient(form.recipient.data):
                return False, "Recipient is invalid", csv_rows
            if not form.amount.data or form.amount.data <= 0:
                return False, "Amount must be greater then 0", csv_rows
        return True, "", csv_rows

    def _add_payment(self, model, recipient, message, amount):
        email = recipient if is_email(recipient) else None
        mobile = recipient if is_mobile(recipient) else None
        address = recipient if is_address(recipient) else None
        amount = int(amount * 100)
        payment = Payment(model, mobile, email, address, message, amount)
        self.session.add(payment)

    def on_model_change(self, form, model, is_created):
        if is_created:
            # validate
            res, msg, csv_rows = self._validate_form(form)
            if not res:
                raise validators.ValidationError(msg)
            # generate model defaults
            model.generate_defaults()
            # check csv file first
            if form.csvfile.data:
                for recipient, message, amount in csv_rows:
                    self._add_payment(model, recipient, message, amount)
            # or just process basic fields
            else:
                recipient = form.recipient.data
                message = form.message.data
                amount = form.amount.data
                self._add_payment(model, recipient, message, amount)

    def is_accessible(self):
        if not (current_user.is_active and current_user.is_authenticated):
            return False
        if current_user.has_role('admin'):
            self.can_create = True
            return True
        if current_user.has_role('finance'):
            self.can_create = True
            return True
        return False

    @expose('authorize', methods=['POST'])
    def authorize_view(self):
        return_url = self.get_url('.index_view')
        # check permission
        if not (current_user.has_role('admin') or current_user.has_role('finance') or current_user.has_role('merchant')):
            # permission denied
            flash('Not authorized.', 'error')
            return redirect(return_url)
        # get the model from the database
        form = get_form_data()
        if not form:
            flash('Could not get form data.', 'error')
            return redirect(return_url)
        proposal_id = form['proposal_id']
        proposal = self.get_one(proposal_id)
        if proposal is None:
            flash('Proposal not not found.', 'error')
            return redirect(return_url)
        # process the proposal
        if proposal.status == proposal.STATE_CREATED:
            proposal.status = proposal.STATE_AUTHORIZED
            now = datetime.datetime.now()
            proposal.date_authorized = now
            proposal.date_expiry = now + datetime.timedelta(hours = Proposal.HOURS_EXPIRY)
            proposal.authorizer = current_user

        # commit to db
        try:
            self.session.commit()
            flash('Proposal {proposal_id} set as authorized'.format(proposal_id=proposal_id))
        except Exception as ex:
            if not self.handle_view_exception(ex):
                raise
            flash('Failed to set proposal {proposal_id} as authorized'.format(proposal_id=proposal_id), 'error')
        return redirect(return_url)

    @expose('decline', methods=['POST'])
    def decline_view(self):
        return_url = self.get_url('.index_view')
        # check permission
        if not (current_user.has_role('admin') or current_user.has_role('finance') or current_user.has_role('merchant')):
            # permission denied
            flash('Not authorized.', 'error')
            return redirect(return_url)
        # get the model from the database
        form = get_form_data()
        if not form:
            flash('Could not get form data.', 'error')
            return redirect(return_url)
        proposal_id = form['proposal_id']
        proposal = self.get_one(proposal_id)
        if proposal is None:
            flash('Proposal not not found.', 'error')
            return redirect(return_url)
        # process the proposal
        if proposal.status == proposal.STATE_CREATED:
            proposal.status = proposal.STATE_DECLINED
            proposal.authorizer = current_user
        # commit to db
        try:
            self.session.commit()
            flash('Proposal {proposal_id} set as declined'.format(proposal_id=proposal_id))
        except Exception as ex:
            if not self.handle_view_exception(ex):
                raise
            flash('Failed to set proposal {proposal_id} as declined'.format(proposal_id=proposal_id), 'error')
        return redirect(return_url)

    @expose('payments/<proposal_id>', methods=['GET'])
    def payments_view(self, proposal_id):
        return_url = self.get_url('.index_view')
        # check permission
        if not (current_user.has_role('admin') or current_user.has_role('finance') or current_user.has_role('merchant')):
            # permission denied
            flash('Not authorized.', 'error')
            return redirect(return_url)
        # get the model from the database
        proposal = self.get_one(proposal_id)
        if proposal is None:
            flash('Proposal not not found.', 'error')
            return redirect(return_url)
        # show the proposal payments
        return self.render('admin/payments.html', payments=proposal.payments)

class WavesTxModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False

    def _format_date(view, context, model, name):
        if model.date:
            return datetime.datetime.fromtimestamp(model.date).strftime('%Y-%m-%d %H:%M:%S')

    def _format_json_data_html_link(view, context, model, name):
        ids = model.id
        json_obj = json.loads(model.json_data)
        assetId = json_obj["assetId"]
        feeAssetId = json_obj["feeAssetId"]
        senderPublicKey = json_obj["senderPublicKey"]
        recipient = json_obj["recipient"]
        amount = json_obj["amount"]/100
        fee = json_obj["fee"]/100
        timestamp = json_obj["timestamp"]
        attachment = json_obj["attachment"]
        txtype = json_obj["type"]

        html = '''
        <button type="button" class="btn btn-primary" data-toggle="modal" data-target="#TxDetailsModal{}">
        Tx Details
        </button>
<div class="modal fade" id="TxDetailsModal{}" tabindex="-1" role="dialog" aria-labelledby="TxDetailsModalLabel{}" aria-hidden="true">
  <div class="modal-dialog" role="document">
    <div class="modal-content">
      <div class="modal-header">
        <h4 class="modal-title" id="TxDetailModalLabel{}">Transaction Details</h4>
        <button type="button" class="close" data-dismiss="modal" aria-label="Close">
          <span aria-hidden="true">&times;</span>
        </button>
      </div>
      <div class="modal-body">
         assetId: {}<br/>
         feeAssetId: {}<br/>
         senderPublicKey: {}<br/>
         recipient: {}<br/>
         amount: {} {}<br/> 
         fee: {}</br>
         timestamp: {}<br/>
         attachment: {}<br/>
         type: {}<br/>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-dismiss="modal">Close</button>
      </div>
    </div>
  </div>
</div>
        '''.format(ids, ids, ids, ids, assetId, feeAssetId, senderPublicKey, recipient, amount, app.config["ASSET_NAME"], fee, timestamp, attachment, txtype)
        return Markup(html)

    def _format_txid_html(view, context, model, name):
        ids = model.txid
        truncate_txids = str(ids[:6]+'...')
        html = '''
        <button type="button" class="btn btn-primary" data-toggle="modal" data-target="#TxidModal{}">
        {}
        </button>
<div class="modal fade" id="TxidModal{}" tabindex="-1" role="dialog" aria-labelledby="TxidModalLabel{}" aria-hidden="true">
  <div class="modal-dialog" role="document">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="TxidModalLabel{}">Transaction ID</h5>
        <button type="button" class="close" data-dismiss="modal" aria-label="Close">
          <span aria-hidden="true">&times;</span>
        </button>
      </div>
      <div class="modal-body">
         {}
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-dismiss="modal">Close</button>
      </div>
    </div>
  </div>
</div>
        '''.format(ids, truncate_txids, ids, ids, ids, ids)
        return Markup(html)

    column_list = ['date', 'txid', 'type', 'state', 'amount', 'json_data_signed', 'json_data']
    column_formatters = {'date': _format_date, 'txid':_format_txid_html, 'json_data': _format_json_data_html_link}


