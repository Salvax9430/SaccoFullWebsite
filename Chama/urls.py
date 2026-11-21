from django.urls import path
from . import views

urlpatterns = [
    path("saas/withdraw/", views.withdraw_saas_revenue, name="withdraw_saas_revenue"),
    path("", views.dashboard_view, name="dashboard"),
    path("register/", views.register_view, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("onboarding/", views.onboarding_view, name="onboarding"),
    path("switch/<int:membership_id>/", views.switch_chama_view, name="switch_chama"),
    path("mpesa/initiate/", views.initiate_mpesa_payment_view, name="initiate_mpesa"),
    path("mpesa/callback/", views.mpesa_callback_view, name="mpesa_callback"),
    path("mpesa/verify/<int:tx_id>/", views.verify_pending_transaction_view, name="verify_pending_tx"),
    path("mpesa/confirm/<int:tx_id>/", views.verify_pending_transaction_view, name="verify_tx"),
    path("subscription/upgrade/", views.upgrade_chama_pro_view, name="upgrade_chama_pro"),
    path("loans/", views.loans_view, name="loans"),
    path("loans/guarantor/<int:pledge_id>/approve/", views.approve_guarantor_view, name="approve_guarantor"),
    path("loans/<int:loan_id>/disburse/", views.disburse_loan_view, name="disburse_loan"),
    path("governance/", views.governance_view, name="governance"),
    path("statement/pdf/", views.download_statement_pdf, name="download_statement_pdf"),
    path("statement/download/", views.download_statement_pdf, name="download_statement"),
]