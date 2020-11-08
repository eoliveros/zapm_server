import os
import decimal

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_mail_sendgrid import MailSendGrid
from flask_socketio import SocketIO

from addresswatcher import AddressWatcher

# Create Flask application
app = Flask(__name__)
app.config.from_pyfile("config.py")
if os.getenv("DEBUG"):
    app.config["DEBUG"] = True
if os.getenv("DEBUG_REQUESTS"):
    app.config["DEBUG_REQUESTS"] = True
if os.getenv("DEBUG_SQL"):
    app.config["SQLALCHEMY_ECHO"] = True
else:
    app.config["SQLALCHEMY_ECHO"] = False
app.config["TESTNET"] = True
app.config["ASSET_ID"] = "CgUrFtinLXEbJwJVjwwcppk4Vpz1nMmR3H5cQaDcUcfe"
app.config["NODE_ADDRESS"] = "http://testnode1.wavesnodes.com"
if os.getenv("PRODUCTION"):
    app.config["TESTNET"] = False
    app.config["ASSET_ID"] = "9R3iLi4qGLVWKc16Tg98gmRvgg1usGEYd7SgC1W5D6HB"
    app.config["NODE_ADDRESS"] = "http://nodes.wavesnodes.com"
if os.getenv("DATABASE_URL"):
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
if os.getenv("SESSION_KEY"):
    app.config["SECRET_KEY"] = os.getenv("SESSION_KEY")
if os.getenv("PASSWORD_SALT"):
    app.config["SECURITY_PASSWORD_SALT"] = os.getenv("PASSWORD_SALT")
if os.getenv("SENDGRID_API_KEY"):
    app.config["MAIL_SENDGRID_API_KEY"] = os.getenv("SENDGRID_API_KEY")
if os.getenv("SETTLEMENT_FEE"):
    app.config["SETTLEMENT_FEE"] = decimal.Decimal(os.getenv("SETTLEMENT_FEE"))
else:
    app.config["SETTLEMENT_FEE"] = decimal.Decimal("3")
if os.getenv("CUSTOMER_RATE"):
    app.config["CUSTOMER_RATE"] = decimal.Decimal(os.getenv("CUSTOMER_RATE"))
else:
    app.config["CUSTOMER_RATE"] = decimal.Decimal("0.08")
if os.getenv("MERCHANT_RATE"):
    app.config["MERCHANT_RATE"] = decimal.Decimal(os.getenv("MERCHANT_RATE"))
else:
    app.config["MERCHANT_RATE"] = decimal.Decimal("0.08")
if os.getenv("SETTLEMENT_ADDRESS"):
    app.config["SETTLEMENT_ADDRESS"] = os.getenv("SETTLEMENT_ADDRESS")
if os.getenv("SENDER_BANK_ACCOUNT"):
    app.config["SENDER_BANK_ACCOUNT"] = os.getenv("SENDER_BANK_ACCOUNT")
if os.getenv("SENDER_NAME"):
    app.config["SENDER_NAME"] = os.getenv("SENDER_NAME")
if os.getenv("SITE_URL"):
    app.config["SITE_URL"] = os.getenv("SITE_URL")
if os.getenv("ALLOW_USER_REGISTRATION"):
    app.config["SECURITY_REGISTERABLE"] = True
    app.config["SECURITY_CONFIRMABLE"] = True
db = SQLAlchemy(app)
mail = MailSendGrid(app)
socketio = SocketIO(app)

aw = AddressWatcher(app.config["TESTNET"])
