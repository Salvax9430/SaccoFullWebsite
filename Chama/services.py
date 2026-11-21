from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Chama, Cycle, Guarantor, Loan, Membership, PayoutSchedule, Transaction

PLATFORM_TX_FEE_STARTER = Decimal("10.00")   # KES 10 convenience fee per deposit on Starter plan
PLATFORM_TX_FEE_PRO = Decimal("5.00")        # KES 5 reduced convenience fee on Pro plan
PRO_MONTHLY_PRICE = Decimal("1000.00")       # KES 1,000 per 30 days for ChamaLedger Pro


# --- SaaS Monetization & Platform Revenue Engines ---

def calculate_platform_fee(chama: Chama, payment_purpose: str) -> Decimal:
    """Calculates the platform service fee added to M-Pesa deposits/repayments."""
    if payment_purpose == "SUBSCRIPTION":
        return Decimal("0.00")
    return PLATFORM_TX_FEE_PRO if chama.is_pro_active else PLATFORM_TX_FEE_STARTER


def get_total_platform_revenue() -> Decimal:
    """Aggregates total earnings for the platform owner from transaction fees and Pro plans."""
    tx_fees = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED
    ).aggregate(total=Sum("platform_fee"))["total"] or Decimal("0.00")

    sub_revenue = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED,
        tx_type=Transaction.Type.SUBSCRIPTION,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    return tx_fees + sub_revenue


# --- Cycle & Ledger Aggregation Engines ---

def get_or_create_active_cycle(chama: Chama) -> Cycle:
    """Returns the open contribution cycle for a Chama or initializes Cycle #1."""
    active_cycle = Cycle.objects.filter(chama=chama, is_closed=False).first()
    if active_cycle:
        return active_cycle

    last_cycle = Cycle.objects.filter(chama=chama).order_by("-cycle_number").first()
    next_num = (last_cycle.cycle_number + 1) if last_cycle else 1
    days = 7 if chama.frequency == Chama.Frequency.WEEKLY else 30
    today = timezone.localdate()

    return Cycle.objects.create(
        chama=chama,
        cycle_number=next_num,
        start_date=today,
        due_date=today + timedelta(days=days),
    )


def get_member_savings(membership: Membership) -> Decimal:
    """Calculates verified net savings strictly from COMPLETED deposit entries."""
    total = Transaction.objects.filter(
        membership=membership,
        tx_type=Transaction.Type.DEPOSIT,
        status=Transaction.Status.COMPLETED,
    ).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0.00")


def get_member_penalties(membership: Membership) -> Decimal:
    """Calculates total late penalties assessed to a member."""
    total = Transaction.objects.filter(
        membership=membership,
        tx_type=Transaction.Type.PENALTY,
        status=Transaction.Status.COMPLETED,
    ).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0.00")


def get_chama_liquid_cash(chama: Chama) -> Decimal:
    """Calculates actual cash sitting in the Chama pool (Inflows minus Outflows)."""
    completed = Transaction.objects.filter(
        membership__chama=chama,
        status=Transaction.Status.COMPLETED,
    )
    inflows = completed.filter(
        tx_type__in=[
            Transaction.Type.DEPOSIT,
            Transaction.Type.LOAN_REPAYMENT,
            Transaction.Type.PENALTY,
        ]
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    outflows = completed.filter(
        tx_type__in=[
            Transaction.Type.LOAN_DISBURSEMENT,
            Transaction.Type.PAYOUT,
        ]
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    return inflows - outflows


def get_loan_remaining_balance(loan: Loan) -> Decimal:
    """Returns remaining principal + interest balance on a loan."""
    repaid = Transaction.objects.filter(
        loan=loan,
        tx_type=Transaction.Type.LOAN_REPAYMENT,
        status=Transaction.Status.COMPLETED,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    remaining = loan.total_due - repaid
    return max(remaining, Decimal("0.00"))


# --- M-Pesa Idempotent Settlement ---

def complete_mpesa_transaction(checkout_request_id: str, mpesa_receipt: str, paid_amount: Decimal):
    """Atomically finalizes a pending M-Pesa transaction, activates Pro plans, and settles loans."""
    with transaction.atomic():
        tx = (
            Transaction.objects.select_for_update()
            .select_related("membership__chama", "loan")
            .filter(checkout_request_id=checkout_request_id)
            .first()
        )
        if not tx or tx.status == Transaction.Status.COMPLETED:
            return tx

        tx.status = Transaction.Status.COMPLETED
        tx.mpesa_receipt_no = mpesa_receipt
        tx.save()

        # Activate or extend ChamaLedger Pro subscription by 30 days
        if tx.tx_type == Transaction.Type.SUBSCRIPTION:
            chama = tx.membership.chama
            now = timezone.now()
            base_date = (
                chama.subscription_expires_at
                if (chama.subscription_expires_at and chama.subscription_expires_at > now)
                else now
            )
            chama.plan_tier = Chama.PlanTier.PRO
            chama.subscription_expires_at = base_date + timedelta(days=30)
            chama.save(update_fields=["plan_tier", "subscription_expires_at"])

        # Mark loan PAID in full if repayment clears balance
        if tx.tx_type == Transaction.Type.LOAN_REPAYMENT and tx.loan:
            if get_loan_remaining_balance(tx.loan) <= Decimal("0.00"):
                tx.loan.status = Loan.Status.PAID
                tx.loan.save(update_fields=["status"])

        return tx


# --- Multi-Signature Peer-Guaranteed Loans ---

def request_loan(
    membership: Membership,
    principal: Decimal,
    duration_days: int,
    guarantor_id: int,
    guaranteed_amount: Decimal,
) -> Loan:
    chama = membership.chama
    savings = get_member_savings(membership)
    max_limit = savings * chama.loan_multiplier

    if principal <= Decimal("0.00"):
        raise ValidationError("Loan principal must be greater than zero.")
    if principal > max_limit:
        raise ValidationError(f"Loan exceeds your {chama.loan_multiplier}x savings limit of KES {max_limit:,.2f}.")
    if Loan.objects.filter(membership=membership, status__in=[Loan.Status.ACTIVE, Loan.Status.PENDING_APPROVAL]).exists():
        raise ValidationError("You already have an active or pending loan in this Chama.")
    if principal > get_chama_liquid_cash(chama):
        raise ValidationError("Chama pool currently lacks sufficient liquid cash for this loan.")

    guarantor_member = Membership.objects.filter(id=guarantor_id, chama=chama, is_active=True).first()
    if not guarantor_member or guarantor_member.id == membership.id:
        raise ValidationError("Please select a valid fellow Chama member as your guarantor.")
    if guaranteed_amount <= Decimal("0.00") or guaranteed_amount > principal:
        raise ValidationError("Guaranteed amount must be between KES 1 and the loan principal.")

    interest = (principal * chama.interest_rate) / Decimal("100.00")
    total_due = (principal + interest).quantize(Decimal("0.01"))

    with transaction.atomic():
        loan = Loan.objects.create(
            membership=membership,
            principal=principal,
            interest_rate=chama.interest_rate,
            total_due=total_due,
            duration_days=duration_days,
            status=Loan.Status.PENDING_APPROVAL,
        )
        Guarantor.objects.create(
            loan=loan,
            guarantor=guarantor_member,
            guaranteed_amount=guaranteed_amount,
        )
    return loan


def approve_guarantor_pledge(pledge_id: int, guarantor_membership: Membership):
    with transaction.atomic():
        pledge = Guarantor.objects.select_for_update().select_related("loan").filter(
            id=pledge_id, guarantor=guarantor_membership
        ).first()
        if not pledge:
            raise ValidationError("Guarantor pledge not found.")
        if pledge.approved:
            raise ValidationError("You have already approved this guarantee.")
        if get_member_savings(guarantor_membership) < pledge.guaranteed_amount:
            raise ValidationError("Your verified savings are insufficient to cover this guarantee pledge.")

        pledge.approved = True
        pledge.responded_at = timezone.now()
        pledge.save(update_fields=["approved", "responded_at"])


def treasurer_disburse_loan(loan_id: int, treasurer_membership: Membership) -> Loan:
    if treasurer_membership.role not in [Membership.Role.TREASURER, Membership.Role.CHAIR]:
        raise ValidationError("Only the Treasurer or Chairperson can disburse loans.")

    with transaction.atomic():
        loan = Loan.objects.select_for_update().select_related("membership__chama").filter(
            id=loan_id, membership__chama=treasurer_membership.chama
        ).first()
        if not loan or loan.status != Loan.Status.PENDING_APPROVAL:
            raise ValidationError("Loan is not awaiting approval.")
        if loan.guarantors.filter(approved=False).exists():
            raise ValidationError("All nominated guarantors must sign off before disbursement.")
        if loan.principal > get_chama_liquid_cash(treasurer_membership.chama):
            raise ValidationError("Insufficient Chama liquid pool to disburse this loan.")

        loan.status = Loan.Status.ACTIVE
        loan.treasurer_approved_by = treasurer_membership
        loan.disbursed_at = timezone.now()
        loan.due_date = timezone.localdate() + timedelta(days=loan.duration_days)
        loan.save()

        Transaction.objects.create(
            membership=loan.membership,
            loan=loan,
            tx_type=Transaction.Type.LOAN_DISBURSEMENT,
            amount=loan.principal,
            status=Transaction.Status.COMPLETED,
            notes=f"Loan #{loan.id} disbursed by {treasurer_membership.user.full_name}",
        )
        return loan


# --- Governance: Automated Penalties & ROSCA Merry-Go-Round ---

def assess_cycle_penalties(chama: Chama, cycle: Cycle) -> int:
    """Audits active members for missed cycle contributions and posts idempotent penalties."""
    penalized_count = 0
    penalty_fee = ((chama.contribution_amount * chama.late_penalty_rate) / Decimal("100.00")).quantize(Decimal("0.01"))
    if penalty_fee <= Decimal("0.00"):
        return 0

    active_members = Membership.objects.filter(chama=chama, is_active=True)
    with transaction.atomic():
        for member in active_members:
            already_fined = Transaction.objects.filter(
                membership=member,
                cycle=cycle,
                tx_type=Transaction.Type.PENALTY,
                status=Transaction.Status.COMPLETED,
            ).exists()
            if already_fined:
                continue

            paid_in_cycle = Transaction.objects.filter(
                membership=member,
                cycle=cycle,
                tx_type=Transaction.Type.DEPOSIT,
                status=Transaction.Status.COMPLETED,
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

            if paid_in_cycle < chama.contribution_amount:
                Transaction.objects.create(
                    membership=member,
                    cycle=cycle,
                    tx_type=Transaction.Type.PENALTY,
                    amount=penalty_fee,
                    status=Transaction.Status.COMPLETED,
                    notes=f"Automated {chama.late_penalty_rate}% late penalty for Cycle #{cycle.cycle_number}",
                )
                penalized_count += 1

    return penalized_count


def execute_merry_go_round_payout(chama: Chama, official_membership: Membership) -> PayoutSchedule:
    """Disburses the ROSCA pooled pot to the next unpaid member in the rotation queue."""
    if official_membership.role not in [Membership.Role.TREASURER, Membership.Role.CHAIR]:
        raise ValidationError("Only the Treasurer or Chairperson can disburse Merry-Go-Round payouts.")

    active_cycle = get_or_create_active_cycle(chama)
    active_count = Membership.objects.filter(chama=chama, is_active=True).count()
    pot_amount = chama.contribution_amount * active_count

    with transaction.atomic():
        next_slot = (
            PayoutSchedule.objects.select_for_update()
            .select_related("membership__user")
            .filter(chama=chama, is_paid=False)
            .order_by("rotation_order")
            .first()
        )
        if not next_slot:
            # Reset rotation once everyone in the queue has been paid
            PayoutSchedule.objects.filter(chama=chama).update(is_paid=False, paid_in_cycle=None, paid_at=None)
            next_slot = (
                PayoutSchedule.objects.select_for_update()
                .select_related("membership__user")
                .filter(chama=chama, is_paid=False)
                .order_by("rotation_order")
                .first()
            )

        if not next_slot:
            raise ValidationError("No members found in the Merry-Go-Round queue.")
        if get_chama_liquid_cash(chama) < pot_amount:
            raise ValidationError(
                f"Chama liquid cash (KES {get_chama_liquid_cash(chama):,.2f}) is below the required pot of KES {pot_amount:,.2f}."
            )

        Transaction.objects.create(
            membership=next_slot.membership,
            cycle=active_cycle,
            tx_type=Transaction.Type.PAYOUT,
            amount=pot_amount,
            status=Transaction.Status.COMPLETED,
            notes=f"ROSCA Cycle #{active_cycle.cycle_number} pot payout (#{next_slot.rotation_order} in queue)",
        )

        next_slot.is_paid = True
        next_slot.paid_in_cycle = active_cycle
        next_slot.paid_at = timezone.now()
        next_slot.save()

        return next_slot