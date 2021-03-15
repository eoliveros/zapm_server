import os
import decimal

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_mail_sendgrid import MailSendGrid
from flask_socketio import SocketIO
from flask_cors import CORS

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
app.config["NODE_ADDRESS"] = "https://testnet1.wavesnodes.com"
app.config["NODE_BASE_ENV"] = "testnet"
app.config["BLOCKCHAIN_EXPLORER"] = "https://testnet.wavesexplorer.com"
if os.getenv("PRODUCTION"):
    app.config["TESTNET"] = False
    app.config["ASSET_ID"] = "9R3iLi4qGLVWKc16Tg98gmRvgg1usGEYd7SgC1W5D6HB"
    app.config["NODE_ADDRESS"] = "https://nodes.wavesnodes.com"
    app.config["BLOCKCHAIN_EXPLORER"] = "https://wavesexplorer.com"
    app.config["NODE_BASE_ENV"] = "mainnet"
if os.getenv("DATABASE_URL"):
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
if os.getenv("SESSION_KEY"):
    app.config["SECRET_KEY"] = os.getenv("SESSION_KEY")
if os.getenv("PASSWORD_SALT"):
    app.config["SECURITY_PASSWORD_SALT"] = os.getenv("PASSWORD_SALT")
if os.getenv("SENDGRID_API_KEY"):
    app.config["MAIL_SENDGRID_API_KEY"] = os.getenv("SENDGRID_API_KEY")
if os.getenv("SALES_TAX"):
    app.config["SALES_TAX"] = decimal.Decimal(os.getenv("SALES_TAX"))
else:
    app.config["SALES_TAX"] = decimal.Decimal("0.15")
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
if os.getenv("SERVER_NAME"):
    app.config["SERVER_NAME"] = os.getenv("SERVER_NAME")
if os.getenv("ADMIN_EMAIL"):
    app.config["ADMIN_EMAIL"] = os.getenv("ADMIN_EMAIL")
if os.getenv("FROM_EMAIL"):
    app.config["FROM_EMAIL"] = os.getenv("FROM_EMAIL")
if os.getenv("FROM_NAME"):
    app.config["FROM_NAME"] = os.getenv("FROM_NAME")
if os.getenv("SERVER_MODE"):
    app.config["SERVER_MODE"] = os.getenv("SERVER_MODE")
if os.getenv("SERVER_TYPE"):
    app.config["SERVER_TYPE"] = os.getenv("SERVER_TYPE")
if os.getenv("ASSET_NAME"):
    app.config["ASSET_NAME"] = os.getenv("ASSET_NAME")

db = SQLAlchemy(app)
mail = MailSendGrid(app)
socketio = SocketIO(app)
cors = CORS(app, origins=['https://nodes.wavesnodes.com', 'https://testnet1.wavesnodes.com'])

aw = AddressWatcher(app.config["TESTNET"])
