import base64
from datetime import datetime
from decimal import Decimal
import requests
from requests.auth import HTTPBasicAuth
from django.conf import settings


class DarajaClient:
    """Safaricom Daraja API integration for M-Pesa Express (STK Push) & Status Queries."""

    def __init__(self):
        self.env = getattr(settings, "MPESA_ENVIRONMENT", "sandbox")
        self.base_url = (
            "https://api.safaricom.co.ke"
            if self.env == "production"
            else "https://sandbox.safaricom.co.ke"
        )
        self.consumer_key = settings.MPESA_CONSUMER_KEY
        self.consumer_secret = settings.MPESA_CONSUMER_SECRET
        self.shortcode = settings.MPESA_SHORTCODE
        self.passkey = settings.MPESA_PASSKEY
        self.callback_url = settings.MPESA_CALLBACK_URL

    def get_access_token(self) -> str:
        url = f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials"
        response = requests.get(
            url,
            auth=HTTPBasicAuth(self.consumer_key, self.consumer_secret),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _generate_password(self) -> tuple[str, str]:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        raw_str = f"{self.shortcode}{self.passkey}{timestamp}"
        password = base64.b64encode(raw_str.encode("utf-8")).decode("utf-8")
        return password, timestamp

    def initiate_stk_push(
        self,
        phone_number: str,
        amount: Decimal,
        account_reference: str,
        transaction_desc: str = "ChamaLedger Payment",
    ) -> dict:
        """
        Sends an M-Pesa STK Push prompt to the member's phone.
        Returns Safaricom's JSON response containing CheckoutRequestID.
        """
        access_token = self.get_access_token()
        password, timestamp = self._generate_password()

        # Safaricom STK Push expects an integer amount (minimum KES 1)
        int_amount = max(1, int(amount))

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int_amount,
            "PartyA": phone_number,
            "PartyB": self.shortcode,
            "PhoneNumber": phone_number,
            "CallBackURL": self.callback_url,
            "AccountReference": account_reference[:12],
            "TransactionDesc": transaction_desc[:13],
        }

        url = f"{self.base_url}/mpesa/stkpush/v1/processrequest"
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        return response.json()

    def query_stk_status(self, checkout_request_id: str) -> dict:
        """
        Fallback query to verify STK Push status directly with Safaricom
        if the webhook callback hasn't arrived yet.
        """
        access_token = self.get_access_token()
        password, timestamp = self._generate_password()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }

        url = f"{self.base_url}/mpesa/stkpushquery/v1/query"
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        return response.json()