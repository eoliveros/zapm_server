from flask import url_for
import flask_admin
from flask_admin import helpers as admin_helpers

from app_core import app, db
from models import security, RestrictedModelView, AdminUserModelView, FinanceUserModelView, BankAdminModelView, BankModelView, ApiKeyModelView, ClaimCodeModelView, TxNotificationModelView, MerchantTxModelView, SettlementAdminModelView, SettlementModelView, ProposalAdminModelView, ProposalUserModelView, SeedsUserModelView, SeedsAdminModelView, WavesTxModelView, Role, User, ClaimCode, TxNotification, ApiKey, MerchantTx, Settlement, Bank, Category, Proposal, Seeds, WavesTx

# Create admin
admin = flask_admin.Admin(
    app,
    'Zap Retailer Server',
    base_template='my_master.html',
    template_mode='bootstrap3',
)

# Add model views
admin.add_view(RestrictedModelView(Role, db.session, category='Admin'))
admin.add_view(RestrictedModelView(Category, db.session, category='Admin'))
admin.add_view(AdminUserModelView(User, db.session, category='Admin', endpoint='user_admin'))
admin.add_view(FinanceUserModelView(User, db.session, category='Admin', endpoint='user_finance'))
admin.add_view(BankAdminModelView(Bank, db.session, category='Admin' ))
admin.add_view(SettlementAdminModelView(Settlement, db.session, category='Admin'))
admin.add_view(ProposalAdminModelView(Proposal, db.session, category='Admin', endpoint='admin_proposal'))
admin.add_view(WavesTxModelView(WavesTx, db.session, name='Waves Transactions', category='Admin'))
admin.add_view(SeedsAdminModelView(Seeds, db.session, endpoint='admin_seed', category='Admin'))
admin.add_view(ClaimCodeModelView(ClaimCode, db.session, category='Reports'))
admin.add_view(TxNotificationModelView(TxNotification, db.session, category='Reports'))
admin.add_view(MerchantTxModelView(MerchantTx, db.session, name='Retail Transactions' ,category='Retailer'))
admin.add_view(ApiKeyModelView(ApiKey, db.session, category='Retailer'))
admin.add_view(BankModelView(Bank, db.session, endpoint='BankUser', category='Retailer'))
admin.add_view(SettlementModelView(Settlement, db.session, endpoint='SettlementUser', category='Retailer'))
admin.add_view(SeedsUserModelView(Seeds, db.session, endpoint='user_seed', category='Retailer'))
admin.add_view(ProposalUserModelView(Proposal, db.session, endpoint='user_proposal'))

# define a context processor for merging flask-admin's template context into the
# flask-security views.
@security.context_processor
def security_context_processor():
    return dict(
        admin_base_template=admin.base_template,
        admin_view=admin.index_view,
        h=admin_helpers,
        get_url=url_for
    )

