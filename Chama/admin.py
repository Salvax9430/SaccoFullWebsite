from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from simple_history.admin import SimpleHistoryAdmin

from .models import Chama, Cycle, Guarantor, Loan, Membership, PayoutSchedule, Transaction, User


@admin.register(User)
class UserAdmin(BaseUserAdmin, SimpleHistoryAdmin):
    list_display = ("phone_number", "full_name", "national_id", "email", "is_staff", "is_active")
    search_fields = ("phone_number", "full_name", "national_id", "email")
    ordering = ("-date_joined",)
    fieldsets = (
        (None, {"fields": ("phone_number", "password")}),
        ("KYC Details", {"fields": ("full_name", "national_id", "email")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("phone_number", "full_name", "national_id", "email", "password1", "password2"),
            },
        ),
    )


@admin.register(Chama)
class ChamaAdmin(SimpleHistoryAdmin):
    list_display = ("name", "invite_code", "contribution_amount", "cycle_frequency", "plan_tier", "subscription_expires_at", "created_at")
    list_filter = ("cycle_frequency", "plan_tier")
    search_fields = ("name", "invite_code", "registration_no")


@admin.register(Membership)
class MembershipAdmin(SimpleHistoryAdmin):
    list_display = ("user", "chama", "role", "is_active", "joined_at")
    list_filter = ("role", "is_active", "chama")
    search_fields = ("user__full_name", "user__phone_number", "chama__name")


@admin.register(Cycle)
class CycleAdmin(admin.ModelAdmin):
    list_display = ("chama", "cycle_number", "start_date", "due_date", "is_closed")
    list_filter = ("is_closed", "chama")


@admin.register(Loan)
class LoanAdmin(SimpleHistoryAdmin):
    list_display = ("id", "membership", "principal", "total_due", "status", "due_date", "created_at")
    list_filter = ("status",)
    search_fields = ("membership__user__full_name", "membership__user__phone_number")


@admin.register(Guarantor)
class GuarantorAdmin(admin.ModelAdmin):
    list_display = ("loan", "guarantor", "guaranteed_amount", "approved", "responded_at")
    list_filter = ("approved",)


@admin.register(PayoutSchedule)
class PayoutScheduleAdmin(admin.ModelAdmin):
    list_display = ("chama", "rotation_order", "membership", "is_paid", "paid_in_cycle", "paid_at")
    list_filter = ("chama", "is_paid")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("reference_code", "membership", "tx_type", "amount", "platform_fee", "status", "mpesa_receipt_no", "created_at")
    list_filter = ("tx_type", "status")
    search_fields = ("mpesa_receipt_no", "checkout_request_id", "membership__user__full_name")
    readonly_fields = ("reference_code", "created_at")