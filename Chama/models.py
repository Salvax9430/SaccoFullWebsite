import uuid
from decimal import Decimal
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords


phone_validator = RegexValidator(
    regex=r"^254[17]\d{8}$",
    message="Phone number must be in Kenyan international format: 2547XXXXXXXX or 2541XXXXXXXX.",
)


class UserManager(BaseUserManager):
    def create_user(self, phone_number, full_name, national_id, password=None, **extra_fields):
        if not phone_number:
            raise ValueError("Kenyan phone number (2547XXXXXXXX) is required.")
        if not national_id:
            raise ValueError("National ID is required for KYC compliance.")
        user = self.model(
            phone_number=phone_number.strip(),
            full_name=full_name.strip(),
            national_id=national_id.strip(),
            **extra_fields,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, full_name, national_id, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        return self.create_user(phone_number, full_name, national_id, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    phone_number = models.CharField(
        max_length=12,
        unique=True,
        validators=[phone_validator],
        help_text="Format: 2547XXXXXXXX",
    )
    full_name = models.CharField(max_length=150)
    national_id = models.CharField(max_length=20, unique=True)
    email = models.EmailField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    history = HistoricalRecords()
    objects = UserManager()

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS = ["full_name", "national_id"]

    def __str__(self):
        return f"{self.full_name} ({self.phone_number})"


def generate_invite_code():
    return uuid.uuid4().hex[:8].upper()


class Chama(models.Model):
    class Frequency(models.TextChoices):
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"

    class PlanTier(models.TextChoices):
        STARTER = "STARTER", "Starter (Free - Up to 10 Members)"
        PRO = "PRO", "Pro SACCO (KES 1,000/mo - Unlimited + Full Automation)"

    name = models.CharField(max_length=120, unique=True)
    registration_no = models.CharField(max_length=50, blank=True, null=True)
    description = models.TextField(blank=True)
    invite_code = models.CharField(max_length=12, unique=True, default=generate_invite_code)
    contribution_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("1000.00"),
        validators=[MinValueValidator(Decimal("10.00"))],
    )
    cycle_frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY)
    loan_multiplier = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal("3.00"),
        help_text="Max loan multiple of verified savings (e.g. 3.00x).",
    )
    interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("10.00"),
        help_text="Flat loan interest percentage per period (e.g. 10.00%).",
    )
    late_penalty_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("5.00"),
        help_text="Late contribution penalty percentage (e.g. 5.00%).",
    )
    plan_tier = models.CharField(max_length=15, choices=PlanTier.choices, default=PlanTier.STARTER)
    subscription_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    history = HistoricalRecords()

    @property
    def is_pro_active(self):
        if self.plan_tier == self.PlanTier.PRO and self.subscription_expires_at:
            return self.subscription_expires_at > timezone.now()
        return False

    @property
    def member_limit(self):
        return 10000 if self.is_pro_active else 10

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        CHAIR = "CHAIR", "Chairperson"
        TREASURER = "TREASURER", "Treasurer"
        SECRETARY = "SECRETARY", "Secretary"
        MEMBER = "MEMBER", "Member"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=15, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    history = HistoricalRecords()

    class Meta:
        unique_together = ("user", "chama")

    def __str__(self):
        return f"{self.user.full_name} — {self.chama.name} ({self.get_role_display()})"


class Cycle(models.Model):
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="cycles")
    cycle_number = models.PositiveIntegerField()
    start_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    is_closed = models.BooleanField(default=False)

    class Meta:
        unique_together = ("chama", "cycle_number")
        ordering = ["-cycle_number"]

    def __str__(self):
        return f"{self.chama.name} - Cycle #{self.cycle_number}"


class Loan(models.Model):
    class Status(models.TextChoices):
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Guarantor & Treasurer Approval"
        ACTIVE = "ACTIVE", "Active"
        PAID = "PAID", "Paid in Full"
        DEFAULTED = "DEFAULTED", "Defaulted"
        REJECTED = "REJECTED", "Rejected"

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="loans")
    principal = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("50.00"))])
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2)
    total_due = models.DecimalField(max_digits=12, decimal_places=2)
    duration_days = models.PositiveIntegerField(default=30)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_APPROVAL)
    treasurer_approved_by = models.ForeignKey(
        Membership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_loans",
    )
    disbursed_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    history = HistoricalRecords()

    def __str__(self):
        return f"Loan #{self.id} - {self.membership.user.full_name} (KES {self.principal})"


class Guarantor(models.Model):
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="guarantors")
    guarantor = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="guaranteed_loans")
    guaranteed_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("1.00"))])
    approved = models.BooleanField(default=False)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("loan", "guarantor")

    def __str__(self):
        return f"{self.guarantor.user.full_name} guarantees KES {self.guaranteed_amount} for Loan #{self.loan.id}"


class PayoutSchedule(models.Model):
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="payout_schedules")
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="payout_slots")
    rotation_order = models.PositiveIntegerField()
    is_paid = models.BooleanField(default=False)
    paid_in_cycle = models.ForeignKey(Cycle, on_delete=models.SET_NULL, null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("chama", "rotation_order")
        ordering = ["rotation_order"]

    def __str__(self):
        return f"#{self.rotation_order} - {self.membership.user.full_name} ({self.chama.name})"


class Transaction(models.Model):
    class Type(models.TextChoices):
        DEPOSIT = "DEPOSIT", "Contribution Deposit"
        LOAN_DISBURSEMENT = "LOAN_DISBURSEMENT", "Loan Disbursement"
        LOAN_REPAYMENT = "LOAN_REPAYMENT", "Loan Repayment"
        PENALTY = "PENALTY", "Late Penalty"
        PAYOUT = "PAYOUT", "Merry-Go-Round Payout"
        SUBSCRIPTION = "SUBSCRIPTION", "SaaS Pro Subscription"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending M-Pesa PIN"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed / Cancelled"

    reference_code = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="transactions")
    cycle = models.ForeignKey(Cycle, on_delete=models.PROTECT, null=True, blank=True, related_name="transactions")
    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, null=True, blank=True, related_name="transactions")
    tx_type = models.CharField(max_length=20, choices=Type.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("1.00"))])
    platform_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Platform SaaS convenience fee earned by system owner.",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    checkout_request_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    mpesa_receipt_no = models.CharField(max_length=30, unique=True, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            original = Transaction.objects.filter(pk=self.pk).first()
            if original and original.status == self.Status.COMPLETED:
                raise ValidationError("Immutable Ledger Violation: Completed financial records cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Immutable Ledger Violation: Financial ledger entries cannot be deleted.")

    def __str__(self):
        return f"{self.get_tx_type_display()} | KES {self.amount} | {self.status}"