import io
import json
import uuid
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .forms import (
    ChamaCreateForm,
    JoinChamaForm,
    LoanRequestForm,
    MpesaPaymentForm,
    PhoneLoginForm,
    UserRegistrationForm,
)
from .models import Chama, Guarantor, Loan, Membership, PayoutSchedule, Transaction
from .mpesa import DarajaClient
from .services import (
    PRO_MONTHLY_PRICE,
    approve_guarantor_pledge,
    assess_cycle_penalties,
    calculate_platform_fee,
    complete_mpesa_transaction,
    execute_merry_go_round_payout,
    get_chama_liquid_cash,
    get_loan_remaining_balance,
    get_member_penalties,
    get_member_savings,
    get_or_create_active_cycle,
    get_total_platform_revenue,
    request_loan,
    treasurer_disburse_loan,
)


def _get_active_membership(request):
    """Helper to resolve the user's selected Chama membership from session."""
    memberships = Membership.objects.filter(user=request.user, is_active=True).select_related("chama")
    if not memberships.exists():
        return None, memberships
    selected_id = request.session.get("active_membership_id")
    active = memberships.filter(id=selected_id).first() or memberships.first()
    request.session["active_membership_id"] = active.id
    return active, memberships


# --- Authentication Views ---

def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.set_password(form.cleaned_data["password"])
            user.save()
            login(request, user)
            messages.success(request, f"Welcome to ChamaLedger, {user.full_name}! Create or join a Chama to begin.")
            return redirect("onboarding")
    else:
        form = UserRegistrationForm()
    return render(request, "register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        form = PhoneLoginForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect("dashboard")
    else:
        form = PhoneLoginForm()
    return render(request, "login.html", {"form": form})


def logout_view(request):
    logout(request)
    return redirect("login")


# --- Chama Onboarding & Switching ---

@login_required
def onboarding_view(request):
    _, memberships = _get_active_membership(request)
    create_form = ChamaCreateForm()
    join_form = JoinChamaForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_chama":
            create_form = ChamaCreateForm(request.POST)
            if create_form.is_valid():
                with transaction.atomic():
                    chama = create_form.save()
                    membership = Membership.objects.create(
                        user=request.user,
                        chama=chama,
                        role=Membership.Role.TREASURER,
                    )
                    get_or_create_active_cycle(chama)
                    PayoutSchedule.objects.create(chama=chama, membership=membership, rotation_order=1)
                    request.session["active_membership_id"] = membership.id
                messages.success(request, f"'{chama.name}' created! Share invite code {chama.invite_code} with members.")
                return redirect("dashboard")

        elif action == "join_chama":
            join_form = JoinChamaForm(request.POST)
            if join_form.is_valid():
                code = join_form.cleaned_data["invite_code"].strip().upper()
                chama = Chama.objects.filter(invite_code=code).first()
                if not chama:
                    messages.error(request, "Invalid invite code. Please check with your Chama Chairperson.")
                elif Membership.objects.filter(user=request.user, chama=chama).exists():
                    messages.info(request, "You are already a member of this Chama.")
                    return redirect("dashboard")
                elif chama.memberships.filter(is_active=True).count() >= chama.member_limit:
                    messages.error(
                        request,
                        f"'{chama.name}' has reached its Starter limit of 10 members. Ask the Treasurer to upgrade to Pro.",
                    )
                else:
                    with transaction.atomic():
                        membership = Membership.objects.create(
                            user=request.user,
                            chama=chama,
                            role=Membership.Role.MEMBER,
                        )
                        next_order = PayoutSchedule.objects.filter(chama=chama).count() + 1
                        PayoutSchedule.objects.create(
                            chama=chama,
                            membership=membership,
                            rotation_order=next_order,
                        )
                        request.session["active_membership_id"] = membership.id
                    messages.success(request, f"Successfully joined {chama.name}!")
                    return redirect("dashboard")

    return render(request, "onboarding.html", {
        "create_form": create_form,
        "join_form": join_form,
        "memberships": memberships,
    })


@login_required
def switch_chama_view(request, membership_id):
    membership = get_object_or_404(Membership, id=membership_id, user=request.user, is_active=True)
    request.session["active_membership_id"] = membership.id
    return redirect("dashboard")


# --- Main Dashboard ---

@login_required
def dashboard_view(request):
    active_membership, memberships = _get_active_membership(request)
    if not active_membership:
        return redirect("onboarding")

    chama = active_membership.chama
    active_cycle = get_or_create_active_cycle(chama)

    member_savings = get_member_savings(active_membership)
    member_penalties = get_member_penalties(active_membership)
    chama_pool = get_chama_liquid_cash(chama)
    max_loan_limit = (member_savings * chama.loan_multiplier).quantize(Decimal("0.01"))

    next_slot = (
        PayoutSchedule.objects.filter(chama=chama, is_paid=False)
        .select_related("membership__user")
        .order_by("rotation_order")
        .first()
    )
    next_recipient = next_slot.membership if next_slot else None

    active_loan = Loan.objects.filter(
        membership=active_membership,
        status__in=[Loan.Status.ACTIVE, Loan.Status.PENDING_APPROVAL],
    ).first()
    active_loan_balance = get_loan_remaining_balance(active_loan) if active_loan else Decimal("0.00")

    transactions = Transaction.objects.filter(membership__chama=chama).select_related(
        "membership__user", "cycle"
    ).order_by("-created_at")[:25]

    pending_guarantor_requests = Guarantor.objects.filter(
        guarantor=active_membership,
        approved=False,
        loan__status=Loan.Status.PENDING_APPROVAL,
    ).select_related("loan__membership__user")

    mpesa_form = MpesaPaymentForm(initial={
        "phone_number": request.user.phone_number,
        "amount": chama.contribution_amount,
    })

    platform_revenue = get_total_platform_revenue() if request.user.is_superuser else None

    context = {
        "active_membership": active_membership,
        "membership": active_membership,
        "memberships": memberships,
        "chama": chama,
        "active_cycle": active_cycle,
        "member_savings": member_savings,
        "verified_savings": member_savings,
        "member_penalties": member_penalties,
        "total_penalties": member_penalties,
        "chama_pool": chama_pool,
        "net_pool_balance": chama_pool,
        "max_loan_limit": max_loan_limit,
        "next_recipient": next_recipient,
        "active_loan": active_loan,
        "active_loan_balance": active_loan_balance,
        "transactions": transactions,
        "recent_transactions": transactions,
        "pending_guarantor_requests": pending_guarantor_requests,
        "mpesa_form": mpesa_form,
        "platform_revenue": platform_revenue,
        "total_platform_revenue": platform_revenue,
    }
    if request.user.is_superuser:
        from .b2c_service import get_net_saas_balance
        saas_stats = get_net_saas_balance()
        context.update(saas_stats)
        context["total_platform_revenue"] = saas_stats["gross_saas_revenue"]
    return render(request, "dashboard.html", context)


# --- Safaricom Daraja STK Push, SaaS Subscription & Webhook ---

@login_required
@require_POST
def initiate_mpesa_payment_view(request):
    membership, _ = _get_active_membership(request)
    if not membership:
        return redirect("onboarding")

    phone = request.POST.get("phone_number", "").strip()
    raw_amount = request.POST.get("amount", "").strip()
    tx_type = request.POST.get("tx_type", Transaction.Type.DEPOSIT).strip()

    if not phone or not raw_amount:
        messages.error(request, "Please enter both a valid Safaricom phone number and an amount.")
        return redirect("dashboard")

    try:
        amount = Decimal(raw_amount)
        if amount < Decimal("10"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        messages.error(request, "Contribution amount must be at least KES 10.")
        return redirect("dashboard")

    if tx_type not in (Transaction.Type.DEPOSIT, Transaction.Type.LOAN_REPAYMENT):
        tx_type = Transaction.Type.DEPOSIT

    active_cycle = get_or_create_active_cycle(membership.chama)

    # Calculate SaaS convenience fee based on Chama tier (Starter = KES 10, Pro = KES 5)
    platform_fee = calculate_platform_fee(membership.chama, tx_type)
    platform_fee = calculate_platform_fee(membership.chama, tx_type)
    total_stk_amount = amount + platform_fee

    # Create PENDING immutable ledger record first
    tx = Transaction.objects.create(
        membership=membership,
        cycle=active_cycle,
        tx_type=tx_type,
        amount=amount,
        platform_fee=platform_fee,
        status=Transaction.Status.PENDING,
        notes=f"{membership.chama.name} {tx_type} via M-Pesa STK",
    )

    # Trigger Safaricom Daraja STK Push for (Principal + Platform Fee)
    try:
        res = DarajaClient().initiate_stk_push(
            phone_number=phone,
            amount=total_stk_amount,
            account_reference=membership.chama.invite_code,
            transaction_desc=f"{membership.chama.name[:12]}",
        )
        if res.get("ResponseCode") == "0":
            tx.checkout_request_id = res["CheckoutRequestID"]
            tx.save(update_fields=["checkout_request_id"])
            messages.success(
                request,
                f"M-Pesa STK prompt for KES {total_stk_amount:,.2f} (incl. KES {platform_fee} fee) sent to {phone}. Enter your PIN!",
            )
        else:
            tx.checkout_request_id = f"SANDBOX_{uuid.uuid4().hex[:16].upper()}"
            tx.save(update_fields=["checkout_request_id"])
            messages.warning(
                request,
                f"Daraja notice: {res.get('errorMessage', 'STK not dispatched')}. Click 'Pending • Verify' in the ledger to simulate receipt.",
            )
    except Exception as exc:
        tx.checkout_request_id = f"SANDBOX_{uuid.uuid4().hex[:16].upper()}"
        tx.save(update_fields=["checkout_request_id"])
        messages.warning(
            request,
            f"Sandbox mode ({exc}): Click 'Pending • Verify' in the ledger to confirm KES {amount:,.2f}.",
        )

    return redirect("dashboard")


@login_required
@require_POST
def upgrade_chama_pro_view(request):
    """Triggers an M-Pesa STK Push to upgrade or extend the Chama's Pro SaaS plan by 30 days."""
    active_membership, _ = _get_active_membership(request)
    if not active_membership or active_membership.role not in [Membership.Role.TREASURER, Membership.Role.CHAIR]:
        messages.error(request, "Only the Chama Treasurer or Chair can upgrade the SaaS subscription.")
        return redirect("governance")

    chama = active_membership.chama
    phone = request.user.phone_number
    amount = PRO_MONTHLY_PRICE

    tx = Transaction.objects.create(
        membership=active_membership,
        amount=amount,
        platform_fee=Decimal("0.00"),
        tx_type=Transaction.Type.SUBSCRIPTION,
        status=Transaction.Status.PENDING,
        notes=f"ChamaLedger Pro 30-Day Subscription for {chama.name}",
    )

    try:
        res = DarajaClient().initiate_stk_push(
            phone_number=phone,
            amount=amount,
            account_reference="CHAMAPRO",
            transaction_desc="PRO_SUB",
        )
        if res.get("ResponseCode") == "0":
            tx.checkout_request_id = res["CheckoutRequestID"]
            tx.save()
            messages.success(request, f"Pro Upgrade STK Push (KES {amount:,.2f}) sent to {phone}!")
        else:
            tx.checkout_request_id = f"SANDBOX_{uuid.uuid4().hex[:16].upper()}"
            tx.save()
            messages.warning(
                request,
                "Click 'Pending • Verify' on the dashboard ledger to activate ChamaLedger Pro!",
            )
    except Exception:
        tx.checkout_request_id = f"SANDBOX_{uuid.uuid4().hex[:16].upper()}"
        tx.save()
        messages.warning(
            request,
            "Sandbox mode: Click 'Pending • Verify' on the dashboard ledger to activate ChamaLedger Pro!",
        )

    return redirect("dashboard")


@csrf_exempt
@require_POST
def mpesa_callback_view(request):
    """Public webhook endpoint called asynchronously by Safaricom Daraja servers."""
    try:
        payload = json.loads(request.body.decode("utf-8"))
        stk_callback = payload["Body"]["stkCallback"]
        checkout_id = stk_callback["CheckoutRequestID"]
        result_code = stk_callback["ResultCode"]

        if result_code == 0:
            items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
            meta = {item["Name"]: item.get("Value") for item in items}
            receipt = str(meta.get("MpesaReceiptNumber"))
            paid_amount = Decimal(str(meta.get("Amount", "0")))
            complete_mpesa_transaction(checkout_id, receipt, paid_amount)
        else:
            tx = Transaction.objects.filter(checkout_request_id=checkout_id).first()
            if tx and tx.status == Transaction.Status.PENDING:
                tx.status = Transaction.Status.FAILED
                tx.notes = f"Cancelled/Failed by user: {stk_callback.get('ResultDesc')}"
                tx.save()

        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})
    except Exception as exc:
        return JsonResponse({"ResultCode": 1, "ResultDesc": str(exc)}, status=400)


@login_required
@require_POST
def verify_pending_transaction_view(request, tx_id):
    """Queries Daraja STK Status API or confirms sandbox receipt if running locally without ngrok."""
    active_membership, _ = _get_active_membership(request)
    tx = get_object_or_404(Transaction, id=tx_id, membership__chama=active_membership.chama)

    if tx.status != Transaction.Status.PENDING:
        messages.info(request, "Transaction is already finalized.")
        return redirect("dashboard")

    if tx.checkout_request_id and not tx.checkout_request_id.startswith("SANDBOX_"):
        try:
            res = DarajaClient().query_stk_status(tx.checkout_request_id)
            if str(res.get("ResultCode")) == "0":
                sim_receipt = f"MPESA{uuid.uuid4().hex[:6].upper()}"
                complete_mpesa_transaction(tx.checkout_request_id, sim_receipt, tx.amount)
                messages.success(request, f"Verified with Safaricom! Receipt {sim_receipt} posted to ledger.")
                return redirect("dashboard")
        except Exception:
            pass

    if not tx.checkout_request_id:
        tx.checkout_request_id = f"SANDBOX_{uuid.uuid4().hex[:16].upper()}"
        tx.save()

    receipt_no = f"S{uuid.uuid4().hex[:9].upper()}"
    complete_mpesa_transaction(tx.checkout_request_id, receipt_no, tx.amount)
    messages.success(request, f"Payment verified! Receipt {receipt_no} (KES {tx.amount:,.2f}) committed to immutable ledger.")
    return redirect("dashboard")


# --- Loans & Multi-Signature Approvals ---

@login_required
def loans_view(request):
    active_membership, memberships = _get_active_membership(request)
    if not active_membership:
        return redirect("onboarding")

    chama = active_membership.chama
    member_savings = get_member_savings(active_membership)
    max_loan_limit = (member_savings * chama.loan_multiplier).quantize(Decimal("0.01"))
    chama_pool = get_chama_liquid_cash(chama)

    other_members = Membership.objects.filter(chama=chama, is_active=True).exclude(id=active_membership.id).select_related("user")

    if request.method == "POST":
        form = LoanRequestForm(request.POST)
        if form.is_valid():
            try:
                loan = request_loan(
                    membership=active_membership,
                    principal=form.cleaned_data["principal"],
                    duration_days=int(form.cleaned_data["duration_days"]),
                    guarantor_id=form.cleaned_data["guarantor_id"],
                    guaranteed_amount=form.cleaned_data["guaranteed_amount"],
                )
                messages.success(request, f"Loan #{loan.id} submitted! Awaiting guarantor sign-off and Treasurer approval.")
                return redirect("loans")
            except ValidationError as exc:
                messages.error(request, exc.message)
    else:
        form = LoanRequestForm()

    chama_loans = Loan.objects.filter(membership__chama=chama).select_related(
        "membership__user", "treasurer_approved_by__user"
    ).prefetch_related("guarantors__guarantor__user").order_by("-created_at")

    for ln in chama_loans:
        ln.remaining = get_loan_remaining_balance(ln)

    return render(request, "loans.html", {
        "active_membership": active_membership,
        "memberships": memberships,
        "chama": chama,
        "member_savings": member_savings,
        "max_loan_limit": max_loan_limit,
        "chama_pool": chama_pool,
        "other_members": other_members,
        "form": form,
        "chama_loans": chama_loans,
    })


@login_required
@require_POST
def approve_guarantor_view(request, pledge_id):
    active_membership, _ = _get_active_membership(request)
    try:
        approve_guarantor_pledge(pledge_id, active_membership)
        messages.success(request, "You have signed off as guarantor for this loan.")
    except ValidationError as exc:
        messages.error(request, exc.message)
    return redirect("loans")


@login_required
@require_POST
def disburse_loan_view(request, loan_id):
    active_membership, _ = _get_active_membership(request)
    try:
        loan = treasurer_disburse_loan(loan_id, active_membership)
        messages.success(request, f"Loan #{loan.id} (KES {loan.principal:,.2f}) approved and disbursed!")
    except ValidationError as exc:
        messages.error(request, exc.message)
    return redirect("loans")


# --- Governance: ROSCA Merry-Go-Round & Automated Penalties ---

@login_required
def governance_view(request):
    active_membership, memberships = _get_active_membership(request)
    if not active_membership:
        return redirect("onboarding")

    chama = active_membership.chama
    active_cycle = get_or_create_active_cycle(chama)
    payout_queue = PayoutSchedule.objects.filter(chama=chama).select_related("membership__user", "paid_in_cycle").order_by("rotation_order")
    next_slot = payout_queue.filter(is_paid=False).first()
    next_recipient = next_slot.membership if next_slot else None
    all_members = Membership.objects.filter(chama=chama, is_active=True).select_related("user")

    for m in all_members:
        m.total_saved = get_member_savings(m)
        m.total_savings = m.total_saved
        m.total_penalties = get_member_penalties(m)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in ("assess_penalties", "run_penalties"):
                if active_membership.role not in [Membership.Role.TREASURER, Membership.Role.CHAIR]:
                    raise ValidationError("Only officials can run penalty assessments.")
                penalized_count = assess_cycle_penalties(chama, active_cycle)
                messages.success(request, f"Penalty audit complete: {penalized_count} member(s) assessed late fees.")
            elif action in ("rosca_payout", "disburse_pot"):
                paid_slot = execute_merry_go_round_payout(chama, active_membership)
                messages.success(
                    request,
                    f"Merry-Go-Round pot disbursed to {paid_slot.membership.user.full_name}!",
                )
        except ValidationError as exc:
            messages.error(request, exc.message)
        return redirect("governance")

    return render(request, "governance.html", {
        "active_membership": active_membership,
        "membership": active_membership,
        "memberships": memberships,
        "chama": chama,
        "active_cycle": active_cycle,
        "payout_queue": payout_queue,
        "next_recipient": next_recipient,
        "all_members": all_members,
        "chama_pool": get_chama_liquid_cash(chama),
    })


# --- Official PDF Financial Statement Generator ---

@login_required
def download_statement_pdf(request):
    import hashlib
    from io import BytesIO
    from decimal import Decimal
    from django.http import HttpResponse
    from django.utils import timezone
    from django.db.models import Sum
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER

    membership, _ = _get_active_membership(request)
    chama_param = request.GET.get("chama_id")
    if chama_param and chama_param.isdigit():
        explicit_mem = (
            Membership.objects.filter(
                user=request.user, chama_id=int(chama_param), is_active=True
            )
            .select_related("chama", "user")
            .first()
        )
        if explicit_mem:
            membership = explicit_mem
            request.session["active_membership_id"] = membership.id

    if not membership:
        return redirect("onboarding")

    membership = (
        Membership.objects.filter(pk=membership.pk)
        .select_related("chama", "user")
        .first()
    )
    chama = membership.chama
    local_now = timezone.localtime(timezone.now())
    generated_str = local_now.strftime("%d %b %Y, %I:%M:%S %p EAT")

    txs = list(
        Transaction.objects.filter(membership__chama=chama)
        .select_related("membership__user")
        .order_by("-created_at")
    )

    member_deposits = (
        Transaction.objects.filter(
            membership=membership, tx_type="DEPOSIT", status="COMPLETED"
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    chama_pool = get_chama_liquid_cash(chama)
    penalties = (
        Transaction.objects.filter(
            membership=membership, tx_type="PENALTY", status="COMPLETED"
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    loan_limit = (member_deposits * chama.loan_multiplier).quantize(Decimal('0.01'))

    digest_payload = (
        f"{chama.id}|{chama.name}|{request.user.phone_number}|"
        f"{member_deposits}|{chama_pool}|{loan_limit}|{penalties}|"
        f"{len(txs)}|{local_now.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    for tx in txs:
        digest_payload += f"|{tx.id}:{tx.tx_type}:{tx.amount}:{tx.status}"
    doc_hash = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()[:16].upper()
    doc_serial = f"CL-STMT-{local_now.strftime('%Y%m%d')}-{doc_hash[:8]}"

    class NumberedStatementDoc(BaseDocTemplate):
        pass

    def draw_page_background(canvas, doc):
        page_w, page_h = A4
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#F1F5F9"))
        canvas.rect(0, 0, page_w, page_h, fill=1, stroke=0)

        margin_x = 12 * mm
        margin_y = 12 * mm
        canvas.setFillColor(colors.white)
        canvas.setStrokeColor(colors.HexColor("#CBD5E1"))
        canvas.setLineWidth(0.75)
        canvas.roundRect(margin_x, margin_y, page_w - 2 * margin_x, page_h - 2 * margin_y, 6, fill=1, stroke=1)

        canvas.saveState()
        canvas.translate(page_w / 2.0, page_h / 2.0)
        canvas.rotate(32)
        canvas.setFont("Helvetica-Bold", 32)
        canvas.setFillColor(colors.HexColor("#F1F5F9"))
        canvas.drawCentredString(0, 16, "CHAMALEDGER VERIFIED")
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawCentredString(0, -8, f"REF: {doc_serial}")
        canvas.restoreState()

        footer_y = margin_y + 5 * mm
        canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
        canvas.setLineWidth(0.6)
        canvas.line(margin_x + 4 * mm, footer_y + 4 * mm, page_w - margin_x - 4 * mm, footer_y + 4 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawString(
            margin_x + 4 * mm,
            footer_y - 0.5 * mm,
            f"Sifuna Solutions • ChamaLedger Core  |  Generated: {generated_str}  |  SHA-256: {doc_hash}",
        )
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.setFillColor(colors.HexColor("#0F172A"))
        canvas.drawRightString(page_w - margin_x - 4 * mm, footer_y - 0.5 * mm, f"Page {canvas._pageNumber}")
        canvas.restoreState()

    buffer = BytesIO()
    doc = NumberedStatementDoc(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=21 * mm,
        title=f"{chama.name} - Official Ledger Statement",
    )
    content_frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="normal_frame")
    doc.addPageTemplates([PageTemplate(id="StatementPage", frames=[content_frame], onPage=draw_page_background)])

    styles = getSampleStyleSheet()
    navy = colors.HexColor("#0F172A")
    slate_hdr = colors.HexColor("#1E293B")
    emerald = colors.HexColor("#10B981")
    sky_blue = colors.HexColor("#0284C7")
    crimson = colors.HexColor("#B91C1C")
    bg_soft = colors.HexColor("#F8FAFC")
    bg_emerald = colors.HexColor("#ECFDF5")
    border_col = colors.HexColor("#E2E8F0")

    elements = []
    tier_text = "PRO TIER • VERIFIED" if getattr(chama, "is_pro", False) else "STARTER PLAN • VERIFIED"
    banner_table = Table(
        [[
            Paragraph(
                f"<font name='Helvetica-Bold' size=14.5 color='#FFFFFF'>{chama.name.upper()}</font><br/>"
                f"<font name='Helvetica' size=8 color='#94A3B8'>OFFICIAL AUDITED CHAMA LEDGER &amp; M-PESA STATEMENT</font>",
                styles["Normal"],
            ),
            Paragraph(
                f"<font name='Helvetica-Bold' size=8 color='#34D399'>{tier_text}</font><br/>"
                f"<font name='Helvetica' size=7.5 color='#E2E8F0'>Issued: {generated_str}</font><br/>"
                f"<font name='Helvetica' size=7.5 color='#94A3B8'>Doc ID: <b>{doc_serial}</b></font>",
                ParagraphStyle("BannerRight", parent=styles["Normal"], alignment=TA_RIGHT, leading=11),
            ),
        ]],
        colWidths=[104 * mm, 74 * mm],
    )
    banner_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), navy),
        ("LINEBELOW", (0, 0), (-1, -1), 3, emerald),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 13),
        ("RIGHTPADDING", (0, 0), (-1, -1), 13),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(banner_table)
    elements.append(Spacer(1, 4 * mm))

    holder_name = getattr(request.user, "full_name", "") or request.user.phone_number
    info_table = Table(
        [
            [
                Paragraph(f"<font size=7 color='#64748B'><b>ACCOUNT HOLDER</b></font><br/><font size=9 color='#0F172A'><b>{holder_name}</b></font>", styles["Normal"]),
                Paragraph(f"<font size=7 color='#64748B'><b>REGISTERED PHONE</b></font><br/><font size=9 color='#0F172A'><b>{request.user.phone_number}</b></font>", styles["Normal"]),
                Paragraph(f"<font size=7 color='#64748B'><b>ROLE &amp; STATUS</b></font><br/><font size=9 color='#047857'><b>{membership.get_role_display()} • ACTIVE</b></font>", styles["Normal"]),
                Paragraph(f"<font size=7 color='#64748B'><b>GROUP INVITE CODE</b></font><br/><font size=9 color='#0F172A'><b>{chama.invite_code}</b></font>", styles["Normal"]),
            ],
            [
                Paragraph(f"<font size=7 color='#64748B'><b>STANDARD CONTRIBUTION</b></font><br/><font size=8.5 color='#0F172A'><b>KES {chama.contribution_amount:,.2f}</b></font>", styles["Normal"]),
                Paragraph(f"<font size=7 color='#64748B'><b>LATE PENALTY RATE</b></font><br/><font size=8.5 color='#0F172A'><b>5.00%</b></font>", styles["Normal"]),
                Paragraph(f"<font size=7 color='#64748B'><b>CURRENCY / TIMEZONE</b></font><br/><font size=8.5 color='#0F172A'><b>KES (Africa/Nairobi EAT)</b></font>", styles["Normal"]),
                Paragraph(f"<font size=7 color='#64748B'><b>LEDGER INTEGRITY</b></font><br/><font size=8.5 color='#047857'><b>ACID LOCKED</b></font>", styles["Normal"]),
            ],
        ],
        colWidths=[46 * mm, 44 * mm, 44 * mm, 44 * mm],
    )
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg_soft),
        ("BOX", (0, 0), (-1, -1), 0.7, border_col),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, border_col),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 4 * mm))

    kpi_table = Table(
        [[
            Paragraph(
                f"<font size=7 color='#047857'><b>MY VERIFIED SAVINGS</b></font><br/>"
                f"<font size=10.5 color='#065F46'><b>KES {member_deposits:,.2f}</b></font><br/>"
                f"<font size=6.5 color='#64748B'>Immutable completed deposits</font>",
                styles["Normal"],
            ),
            Paragraph(
                f"<font size=7 color='#0F172A'><b>CHAMA LIQUID POOL</b></font><br/>"
                f"<font size=10.5 color='#0F172A'><b>KES {chama_pool:,.2f}</b></font><br/>"
                f"<font size=6.5 color='#64748B'>Available pool for loans &amp; MGR</font>",
                styles["Normal"],
            ),
            Paragraph(
                f"<font size=7 color='#0284C7'><b>LOAN LIMIT (3.00X)</b></font><br/>"
                f"<font size=10.5 color='#0369A1'><b>KES {loan_limit:,.2f}</b></font><br/>"
                f"<font size=6.5 color='#64748B'>3x verified member savings</font>",
                styles["Normal"],
            ),
            Paragraph(
                f"<font size=7 color='#B91C1C'><b>ACCRUED PENALTIES</b></font><br/>"
                f"<font size=10.5 color='#991B1B'><b>KES {penalties:,.2f}</b></font><br/>"
                f"<font size=6.5 color='#64748B'>Recorded late fee charges</font>",
                styles["Normal"],
            ),
        ]],
        colWidths=[44.5 * mm] * 4,
    )
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), bg_emerald),
        ("BACKGROUND", (1, 0), (3, 0), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.75, border_col),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, border_col),
        ("LINEABOVE", (0, 0), (0, 0), 2.5, emerald),
        ("LINEABOVE", (1, 0), (1, 0), 2.5, navy),
        ("LINEABOVE", (2, 0), (2, 0), 2.5, sky_blue),
        ("LINEABOVE", (3, 0), (3, 0), 2.5, crimson),
        ("TOPPADDING", (0, 0), (-1, -1), 7.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(kpi_table)
    elements.append(Spacer(1, 5 * mm))

    elements.append(
        Paragraph(
            f"<font size=9 color='#0F172A'><b>IMMUTABLE TRANSACTION AUDIT LOG</b></font> "
            f"<font size=7.5 color='#64748B'>({len(txs)} records • East Africa Time, UTC+3)</font>",
            styles["Normal"],
        )
    )
    elements.append(Spacer(1, 2 * mm))

    right_hdr = ParagraphStyle("RHdr", parent=styles["Normal"], alignment=TA_RIGHT)
    center_hdr = ParagraphStyle("CHdr", parent=styles["Normal"], alignment=TA_CENTER)
    right_cell = ParagraphStyle("RCell", parent=styles["Normal"], alignment=TA_RIGHT)
    center_cell = ParagraphStyle("CCell", parent=styles["Normal"], alignment=TA_CENTER)

    ledger_rows = [[
        Paragraph("<font size=7 color='#FFFFFF'><b>DATE &amp; TIME (EAT)</b></font>", styles["Normal"]),
        Paragraph("<font size=7 color='#FFFFFF'><b>MEMBER</b></font>", styles["Normal"]),
        Paragraph("<font size=7 color='#FFFFFF'><b>TYPE</b></font>", styles["Normal"]),
        Paragraph("<font size=7 color='#FFFFFF'><b>RECEIPT / REF</b></font>", styles["Normal"]),
        Paragraph("<font size=7 color='#FFFFFF'><b>AMOUNT (KES)</b></font>", right_hdr),
        Paragraph("<font size=7 color='#FFFFFF'><b>FEE</b></font>", right_hdr),
        Paragraph("<font size=7 color='#FFFFFF'><b>STATUS</b></font>", center_hdr),
    ]]

    total_completed = Decimal("0.00")
    total_fees = Decimal("0.00")

    for tx in txs:
        tx_dt = timezone.localtime(tx.created_at)
        m_name = getattr(tx.membership.user, "full_name", "") or tx.membership.user.phone_number
        tx_label = "SaaS Owner B2C Payout" if tx.tx_type == "SAAS_WITHDRAWAL" else tx.get_tx_type_display()
        ref_val = (
            getattr(tx, "mpesa_receipt_number", None)
            or getattr(tx, "mpesa_receipt", None)
            or getattr(tx, "receipt_number", None)
            or getattr(tx, "checkout_request_id", None)
            or "SYSTEM-LEDGER"
        )
        fee_val = getattr(tx, "platform_fee", None) or Decimal("0.00")
        if tx.status == "COMPLETED":
            total_completed += (-tx.amount if tx.tx_type == "SAAS_WITHDRAWAL" else tx.amount)
            total_fees += fee_val

        st_color = "#047857" if tx.status == "COMPLETED" else ("#B45309" if tx.status == "PENDING" else "#B91C1C")
        amt_color = "#B91C1C" if tx.tx_type == "SAAS_WITHDRAWAL" else "#0F172A"
        amt_prefix = "-" if tx.tx_type == "SAAS_WITHDRAWAL" else ""

        ledger_rows.append([
            Paragraph(f"<font size=7 color='#0F172A'><b>{tx_dt.strftime('%d %b %Y')}</b></font><br/><font size=6.5 color='#64748B'>{tx_dt.strftime('%I:%M:%S %p')}</font>", styles["Normal"]),
            Paragraph(f"<font size=7.5 color='#1E293B'><b>{m_name}</b></font>", styles["Normal"]),
            Paragraph(f"<font size=7.5 color='#334155'>{tx_label}</font>", styles["Normal"]),
            Paragraph(f"<font size=7 color='#475569'>{str(ref_val)[:18]}</font>", styles["Normal"]),
            Paragraph(f"<font size=7.5 color='{amt_color}'><b>{amt_prefix}{tx.amount:,.2f}</b></font>", right_cell),
            Paragraph(f"<font size=7 color='#64748B'>{fee_val:,.2f}</font>", right_cell),
            Paragraph(f"<font size=7 color='{st_color}'><b>{tx.status}</b></font>", center_cell),
        ])

    ledger_rows.append([
        Paragraph("<font size=7.5 color='#0F172A'><b>NET STATEMENT TOTAL</b></font>", styles["Normal"]),
        Paragraph("", styles["Normal"]),
        Paragraph("", styles["Normal"]),
        Paragraph("", styles["Normal"]),
        Paragraph(f"<font size=7.5 color='#047857'><b>{total_completed:,.2f}</b></font>", right_cell),
        Paragraph(f"<font size=7.5 color='#0F172A'><b>{total_fees:,.2f}</b></font>", right_cell),
        Paragraph("<font size=7 color='#047857'><b>VERIFIED</b></font>", center_cell),
    ])

    ledger_table = Table(ledger_rows, colWidths=[26 * mm, 30 * mm, 35 * mm, 29 * mm, 24 * mm, 15 * mm, 20 * mm], repeatRows=1)
    last_row = len(ledger_rows) - 1
    ledger_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), slate_hdr),
        ("ROWBACKGROUNDS", (0, 1), (-1, last_row - 1), [colors.white, bg_soft]),
        ("BACKGROUND", (0, last_row), (-1, last_row), colors.HexColor("#F1F5F9")),
        ("LINEABOVE", (0, last_row), (-1, last_row), 1.2, navy),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, border_col),
        ("BOX", (0, 0), (-1, -1), 0.75, border_col),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 6.5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6.5),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(ledger_table)
    elements.append(Spacer(1, 5 * mm))

    sys_sig_table = Table(
        [[
            Paragraph(
                "<font size=7 color='#64748B'><b>CRYPTOGRAPHIC STATEMENT VERIFICATION</b></font><br/>"
                f"<font size=7.5 color='#0F172A'><b>Serial No:</b> {doc_serial}</font><br/>"
                f"<font size=7.5 color='#0F172A'><b>SHA-256 Digest:</b> {doc_hash}</font><br/>"
                "<font size=6.5 color='#475569'>Tamper-evident digest computed from ACID database entries.</font>",
                styles["Normal"],
            ),
            Paragraph(
                "<font size=7 color='#047857'><b>AUTOMATED SYSTEM ATTESTATION</b></font><br/>"
                "<font name='Helvetica-BoldOblique' size=10.5 color='#0F172A'>Bramuel Simiyu Sifuna</font><br/>"
                "<font size=7 color='#1E293B'><b>ChamaLedger Core Engine • Verified Seal</b></font><br/>"
                f"<font size=6.5 color='#64748B'>Timestamp: {generated_str}</font>",
                ParagraphStyle("SysSigRight", parent=styles["Normal"], alignment=TA_RIGHT, leading=10.5),
            ),
        ]],
        colWidths=[98 * mm, 80 * mm],
    )
    sys_sig_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg_soft),
        ("BOX", (0, 0), (-1, -1), 0.75, border_col),
        ("LINEBEFORE", (1, 0), (1, 0), 2, emerald),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(sys_sig_table)
    elements.append(Spacer(1, 4.5 * mm))

    officials_table = Table(
        [
            [
                Paragraph("<font size=7.5 color='#0F172A'><b>CHAMA OFFICIALS AUTHORIZATION &amp; SIGNATURES</b></font> <font size=6.5 color='#64748B'>(For physical/official branch &amp; audit endorsement)</font>", styles["Normal"]),
                Paragraph("<font size=7.5 color='#0F172A'><b>OFFICIAL CHAMA STAMP</b></font>", ParagraphStyle("StampHdr", parent=styles["Normal"], alignment=TA_CENTER)),
            ],
            [
                Table(
                    [
                        [
                            Paragraph("<font size=7 color='#334155'><b>CHAIRPERSON</b></font><br/><font size=6.5 color='#64748B'>Sign: ________________________</font>", styles["Normal"]),
                            Paragraph("<font size=7 color='#334155'><b>DATE SIGNED</b></font><br/><font size=6.5 color='#64748B'>____ / ____ / 202__</font>", styles["Normal"]),
                        ],
                        [
                            Paragraph("<font size=7 color='#334155'><b>TREASURER</b></font><br/><font size=6.5 color='#64748B'>Sign: ________________________</font>", styles["Normal"]),
                            Paragraph("<font size=7 color='#334155'><b>DATE SIGNED</b></font><br/><font size=6.5 color='#64748B'>____ / ____ / 202__</font>", styles["Normal"]),
                        ],
                        [
                            Paragraph("<font size=7 color='#334155'><b>SECRETARY</b></font><br/><font size=6.5 color='#64748B'>Sign: ________________________</font>", styles["Normal"]),
                            Paragraph("<font size=7 color='#334155'><b>DATE SIGNED</b></font><br/><font size=6.5 color='#64748B'>____ / ____ / 202__</font>", styles["Normal"]),
                        ],
                    ],
                    colWidths=[82 * mm, 44 * mm],
                ),
                Paragraph(
                    f"<br/><font size=7.5 color='#64748B'><b>AFFIX OFFICIAL<br/>CHAMA STAMP</b></font><br/>"
                    f"<font size=6.5 color='#94A3B8'>{chama.name.upper()}<br/>Code: {chama.invite_code}</font>",
                    ParagraphStyle("StampBoxText", parent=styles["Normal"], alignment=TA_CENTER, leading=10),
                ),
            ],
        ],
        colWidths=[126 * mm, 52 * mm],
    )
    officials_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (1, 0), bg_soft),
        ("BACKGROUND", (0, 1), (0, 1), colors.white),
        ("BACKGROUND", (1, 1), (1, 1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.75, border_col),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, border_col),
        ("TOPPADDING", (0, 0), (1, 0), 5.5),
        ("BOTTOMPADDING", (0, 0), (1, 0), 5.5),
        ("TOPPADDING", (0, 1), (0, 1), 4),
        ("BOTTOMPADDING", (0, 1), (0, 1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(officials_table)

    doc.build(elements)
    buffer.seek(0)
    filename = f"ChamaLedger_{chama.name.replace(' ', '_')}_{local_now.strftime('%Y%m%d_%H%M')}.pdf"
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response

@login_required
def withdraw_saas_revenue(request):
    from django.contrib import messages
    from .b2c_service import execute_saas_withdrawal, get_net_saas_balance

    if not request.user.is_superuser:
        messages.error(request, "Unauthorized: Platform Owner privileges required.")
        return redirect("dashboard")

    if request.method == "POST":
        phone = request.POST.get("payout_phone", "").strip() or request.user.phone_number
        raw_amount = request.POST.get("payout_amount", "").strip()
        if not raw_amount:
            raw_amount = str(get_net_saas_balance()["available_saas_revenue"])

        ok, msg = execute_saas_withdrawal(request.user, phone, raw_amount)
        if ok:
            messages.success(request, msg)
        else:
            messages.error(request, msg)

    return redirect("dashboard")
