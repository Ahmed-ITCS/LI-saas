from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


# ─────────────────────────────────────────────────────────────────────────────
# Subscription Plans
# ─────────────────────────────────────────────────────────────────────────────

class Plan(models.Model):
    TIER_FREE   = "free"
    TIER_PRO    = "pro"
    TIER_AGENCY = "agency"
    TIER_CUSTOM = "custom"
    TIER_CHOICES = [
        (TIER_FREE,   "Free"),
        (TIER_PRO,    "Pro"),
        (TIER_AGENCY, "Agency"),
        (TIER_CUSTOM, "Custom"),
    ]

    name        = models.CharField(max_length=80)
    tier        = models.CharField(max_length=20, choices=TIER_CHOICES, default=TIER_CUSTOM)
    description = models.TextField(blank=True)

    # -1 = unlimited
    max_profiles         = models.IntegerField(default=1)
    max_comments_per_day = models.IntegerField(default=10)
    max_targets          = models.IntegerField(default=2)
    max_bot_hours_month  = models.IntegerField(default=10)

    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tier", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_tier_display()})"

    @classmethod
    def get_default(cls):
        plan, _ = cls.objects.get_or_create(
            tier=cls.TIER_FREE,
            name="Free",
            defaults={
                "description": "Get started — one LinkedIn profile.",
                "max_profiles": 1,
                "max_comments_per_day": 10,
                "max_targets": 2,
                "max_bot_hours_month": 10,
            }
        )
        return plan

    def lim(self, val):
        return "Unlimited" if val == -1 else str(val)


class UserSubscription(models.Model):
    STATUS_ACTIVE    = "active"
    STATUS_SUSPENDED = "suspended"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_ACTIVE,    "Active"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    user    = models.OneToOneField(User, on_delete=models.CASCADE, related_name="subscription")
    plan    = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status  = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    notes   = models.TextField(blank=True)

    bot_minutes_used_this_month = models.PositiveIntegerField(default=0)
    usage_month = models.CharField(max_length=7, default="")

    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} — {self.plan.name} ({self.get_status_display()})"

    def is_active_sub(self):
        return self.status == self.STATUS_ACTIVE

    def _reset_if_new_month(self):
        current = timezone.now().strftime("%Y-%m")
        if self.usage_month != current:
            self.bot_minutes_used_this_month = 0
            self.usage_month = current
            self.save(update_fields=["bot_minutes_used_this_month", "usage_month"])

    def bot_hours_used(self):
        self._reset_if_new_month()
        return round(self.bot_minutes_used_this_month / 60, 1)

    def comments_today(self):
        profiles = self.user.linkedin_profiles.all()
        return CommentLog.objects.filter(
            profile__in=profiles,
            created_at__date=timezone.now().date()
        ).count()

    def can_add_profile(self):
        if not self.is_active_sub():
            return False, "Your subscription is not active."
        lim = self.plan.max_profiles
        if lim == -1:
            return True, ""
        if self.user.linkedin_profiles.count() >= lim:
            return False, f"Your {self.plan.name} plan allows {lim} LinkedIn profile(s). Ask admin to upgrade."
        return True, ""

    def can_comment(self):
        if not self.is_active_sub():
            return False, "Subscription not active."
        lim = self.plan.max_comments_per_day
        if lim == -1:
            return True, ""
        if self.comments_today() >= lim:
            return False, f"Daily comment limit ({lim}) reached. Resets at midnight UTC."
        return True, ""

    def can_add_target(self, linkedin_profile):
        if not self.is_active_sub():
            return False, "Subscription not active."
        lim = self.plan.max_targets
        if lim == -1:
            return True, ""
        if linkedin_profile.targets.count() >= lim:
            return False, f"Your {self.plan.name} plan allows {lim} target(s) per profile."
        return True, ""

    def can_run_bot(self):
        if not self.is_active_sub():
            return False, "Subscription not active."
        lim = self.plan.max_bot_hours_month
        if lim == -1:
            return True, ""
        self._reset_if_new_month()
        if (self.bot_minutes_used_this_month / 60) >= lim:
            return False, f"Monthly bot hours limit ({lim}h) reached. Resets on the 1st."
        return True, ""

    def add_bot_minutes(self, minutes: int):
        self._reset_if_new_month()
        self.bot_minutes_used_this_month += minutes
        self.save(update_fields=["bot_minutes_used_this_month"])


# ─────────────────────────────────────────────────────────────────────────────
# LinkedIn Profiles
# ─────────────────────────────────────────────────────────────────────────────

class LinkedInProfile(models.Model):
    user  = models.ForeignKey(User, on_delete=models.CASCADE, related_name="linkedin_profiles")
    label = models.CharField(max_length=120)

    li_email    = models.TextField()
    li_password = models.TextField()

    LLM_GEMINI = "gemini"
    LLM_MOCK   = "mock"
    LLM_CHOICES = [(LLM_GEMINI, "Gemini"), (LLM_MOCK, "Mock")]
    llm_provider = models.CharField(max_length=20, choices=LLM_CHOICES, default=LLM_GEMINI)
    gemini_keys  = models.TextField(blank=True)

    min_post_age_minutes   = models.PositiveIntegerField(default=30)
    max_post_age_minutes   = models.PositiveIntegerField(default=360)
    max_comments_per_round = models.PositiveSmallIntegerField(default=6)
    persona_prompt = models.TextField(
        default=(
            "you are a software engineer, you work in backend but can handle a bit of frontend "
            "and devops, aspire to be a solution architect."
        )
    )
    run_parallel = models.BooleanField(default=True)
    is_active    = models.BooleanField(default=False)
    state_file   = models.CharField(max_length=255, blank=True)
    pid          = models.IntegerField(null=True, blank=True)
    bot_started_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return f"{self.user.username} / {self.label}"

    def get_gemini_keys(self):
        return [k.strip() for k in self.gemini_keys.splitlines() if k.strip()]

    def has_session(self):
        import os
        return bool(self.state_file) and os.path.exists(self.state_file)

    def is_running(self):
        if not self.pid:
            return False
        import os
        try:
            os.kill(self.pid, 0)
            # PID reuse is common on Linux; make sure this PID is our bot runner.
            if os.name == "posix":
                cmdline_path = f"/proc/{self.pid}/cmdline"
                try:
                    with open(cmdline_path, "rb") as f:
                        raw = f.read().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
                    if "bot/runner.py" not in raw and "bot\\runner.py" not in raw:
                        return False
                except FileNotFoundError:
                    return False
                except Exception:
                    # If proc cmdline cannot be read, keep legacy behavior.
                    pass
            return True
        except (ProcessLookupError, PermissionError):
            return False


# ─────────────────────────────────────────────────────────────────────────────
# Comment Log
# ─────────────────────────────────────────────────────────────────────────────

class CommentLog(models.Model):
    profile          = models.ForeignKey(LinkedInProfile, on_delete=models.CASCADE, related_name="comment_logs")
    urn              = models.CharField(max_length=120)
    post_text        = models.TextField(blank=True)
    comment          = models.TextField()
    post_age_minutes = models.IntegerField(null=True, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.profile.label} → {self.urn[:40]}"


# ─────────────────────────────────────────────────────────────────────────────
# Target Profiles
# ─────────────────────────────────────────────────────────────────────────────

class TargetProfile(models.Model):
    owner        = models.ForeignKey(LinkedInProfile, on_delete=models.CASCADE, related_name="targets")
    linkedin_url = models.URLField()
    label        = models.CharField(max_length=120, blank=True)
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["label", "linkedin_url"]
        unique_together = [["owner", "linkedin_url"]]

    def __str__(self):
        return self.label or self.linkedin_url

    def username(self):
        import re
        m = re.search(r"/in/([^/?#]+)", self.linkedin_url)
        return m.group(1) if m else self.linkedin_url

    def activity_url(self):
        return f"https://www.linkedin.com/in/{self.username()}/recent-activity/all/"
