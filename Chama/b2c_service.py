import base64
import uuid
import requests
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from .models import Transaction, Membership

def get_net_saas_balance():
    sub_rev = Transaction.objects.filter(
        tx_type="SUBSCRIPTION", status="COMPLETED"
    ).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")

    fee_rev = Transaction.objects.filter(
        status="COMPLETED"
    ).exclude(tx_type__in=["SUBSCRIPTION", "SAAS_WITHDRAWAL"]).aggregate(t=Sum("platform_fee"))["t"] or Decimal("0.00")

    withdrawn = Transaction.objects.filter(
        tx_type="SAAS_WITHDRAWAL", status="COMPLETED"
    ).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")

    gross = sub_rev + fee_rev
    available = max(Decimal("0.00"), gross - withdrawn)
    return {
        "gross_saas_revenue": gross,
        "withdrawn_saas_revenue": withdrawn,
        "available_saas_revenue": available,
    }

def trigger_mpesa_b2c(phone_number, amount, remarks="ChamaLedger SaaS Owner Payout"):
    env = getattr(settings, "MPESA_ENVIRONMENT", "sandbox").lower()
    consumer_key = getattr(settings, "MPESA_CONSUMER_KEY", "")
    consumer_secret = getattr(settings, "MPESA_CONSUMER_SECRET", "")
    shortcode = getattr(settings, "MPESA_SHORTCODE", "174379")
    initiator = getattr(settings, "MPESA_INITIATOR_NAME", "testapi")
    security_cred = getattr(settings, "MPESA_SECURITY_CREDENTIAL", "")
    callback_base = getattr(settings, "MPESA_CALLBACK_URL", "https://example.com/mpesa/callback/").rsplit("/mpesa/", 1)[0]

    base_url = "https://sandbox.safaricom.co.ke" if env == "sandbox" else "https://api.safaricom.co.ke"

    # In sandbox without B2C SecurityCredential configured, return a clean simulated Daraja B2C response
    if env == "sandbox" and not security_cred:
        conv_id = f"AG_{uuid.uuid4().hex[:14].upper()}"
        return True, conv_id, "Sandbox B2C payout simulated and settled immediately."

    auth_str = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
    token_res = requests.get(
        f"{base_url}/oauth/v1/generate?grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth_str}"},
        timeout=15,
    )
    token_res.raise_for_status()
    access_token = token_res.json().get("access_token")

    payload = {
        "InitiatorName": initiator,
        "SecurityCredential": security_cred,
        "CommandID": "BusinessPayment",
        "Amount": int(Decimal(amount)),
        "PartyA": shortcode,
        "PartyB": phone_number,
        "Remarks": remarks,
        "QueueTimeOutURL": f"{callback_base}/mpesa/b2c/timeout/",
        "ResultURL": f"{callback_base}/mpesa/b2c/result/",
        "Occasion": "SaaSRevenuePayout",
    }
    res = requests.post(
        f"{base_url}/mpesa/b2c/v3/paymentrequest",
        json=payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    data = res.json()
    if data.get("ResponseCode") == "0":
        return True, data.get("ConversationID", uuid.uuid4().hex[:12].upper()), data.get("ResponseDescription", "B2C Accepted")
    return False, "", data.get("errorMessage") or data.get("ResponseDescription") or "B2C request rejected"

@transaction.atomic
def execute_saas_withdrawal(user, phone_number, amount):
    amount = Decimal(str(amount))
    balances = get_net_saas_balance()
    if amount < Decimal("10.00"):
        return False, "Minimum B2C withdrawal amount is KES 10.00."
    if amount > balances["available_saas_revenue"]:
        return False, f"Requested KES {amount:,.2f} exceeds available SaaS balance (KES {balances['available_saas_revenue']:,.2f})."

    membership = Membership.objects.filter(user=user).first()
    if not membership:
        return False, "Superuser requires at least one Chama membership anchor for ledger integrity."

    ok, ref_code, msg = trigger_mpesa_b2c(phone_number, amount)
    if not ok:
        return False, f"Safaricom B2C Error: {msg}"

    Transaction.objects.create(
        membership=membership,
        tx_type="SAAS_WITHDRAWAL",
        amount=amount,
        platform_fee=Decimal("0.00"),
        checkout_request_id=f"B2C-{uuid.uuid4().hex[:12].upper()}",
        status="COMPLETED",
    )
    return True, f"KES {amount:,.2f} sent to {phone_number} via M-Pesa B2C (Ref: {ref_code})."
