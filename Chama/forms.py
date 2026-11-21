from decimal import Decimal
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from .models import Chama, Membership, User


class UserRegistrationForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Create a strong password"}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Confirm password"}))

    class Meta:
        model = User
        fields = ["full_name", "phone_number", "national_id", "email"]
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "e.g., Bramuel Simiyu"}),
            "phone_number": forms.TextInput(attrs={"placeholder": "254745642459"}),
            "national_id": forms.TextInput(attrs={"placeholder": "National ID Number"}),
            "email": forms.EmailInput(attrs={"placeholder": "Optional email address"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("password") != cleaned_data.get("confirm_password"):
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data


class PhoneLoginForm(AuthenticationForm):
    username = forms.CharField(
        label="Phone Number",
        widget=forms.TextInput(attrs={"placeholder": "2547XXXXXXXX", "autofocus": True}),
    )


class ChamaCreateForm(forms.ModelForm):
    class Meta:
        model = Chama
        fields = [
            "name",
            "registration_no",
            "contribution_amount",
            "cycle_frequency",
            "late_penalty_rate",
            "loan_multiplier",
            "interest_rate",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g., Kirinyaga Tech Innovators Chama"}),
            "registration_no": forms.TextInput(attrs={"placeholder": "Optional Reg No. (e.g., CBO/2026/001)"}),
        }


class JoinChamaForm(forms.Form):
    invite_code = forms.CharField(
        max_length=12,
        widget=forms.TextInput(attrs={"placeholder": "Enter 8-character Invite Code (e.g., A1B2C3D4)"}),
    )


class MpesaPaymentForm(forms.Form):
    phone_number = forms.RegexField(
        regex=r"^254(7|1)\d{8}$",
        error_messages={"invalid": "Enter a valid Kenyan M-Pesa number (2547XXXXXXXX or 2541XXXXXXXX)."},
    )
    amount = forms.DecimalField(min_value=Decimal("1.00"), max_digits=12, decimal_places=2)
    payment_purpose = forms.ChoiceField(
        choices=[("DEPOSIT", "Cycle Contribution Deposit"), ("LOAN_REPAYMENT", "Active Loan Repayment")]
    )


class LoanRequestForm(forms.Form):
    principal = forms.DecimalField(min_value=Decimal("100.00"), max_digits=12, decimal_places=2)
    duration_days = forms.ChoiceField(
        choices=[(30, "30 Days (1 Month)"), (60, "60 Days (2 Months)"), (90, "90 Days (3 Months)")]
    )
    guarantor_id = forms.IntegerField()
    guaranteed_amount = forms.DecimalField(min_value=Decimal("100.00"), max_digits=12, decimal_places=2)