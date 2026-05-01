from django.contrib import admin
from .models import LinkedInProfile, CommentLog, TargetProfile, Plan, UserSubscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ["name", "tier", "max_profiles", "max_comments_per_day",
                    "max_targets", "max_bot_hours_month", "is_active"]
    list_filter  = ["tier", "is_active"]


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["user", "plan", "status", "bot_hours_used", "started_at"]
    list_filter  = ["status", "plan"]
    search_fields = ["user__username", "user__email"]


@admin.register(LinkedInProfile)
class LinkedInProfileAdmin(admin.ModelAdmin):
    list_display = ["label", "user", "llm_provider", "is_active", "created_at"]
    list_filter  = ["llm_provider", "is_active"]


@admin.register(CommentLog)
class CommentLogAdmin(admin.ModelAdmin):
    list_display = ["profile", "urn", "created_at"]
    list_filter  = ["profile"]


@admin.register(TargetProfile)
class TargetProfileAdmin(admin.ModelAdmin):
    list_display = ["label", "owner", "is_active", "created_at"]
