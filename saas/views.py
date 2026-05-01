from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db.models import Count, Q

from accounts.models import Plan, UserSubscription, CommentLog, LinkedInProfile


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_subscription(user):
    """Ensure every user has a subscription row. Called on signup signal."""
    if not hasattr(user, "subscription"):
        UserSubscription.objects.create(
            user=user,
            plan=Plan.get_default(),
            status=UserSubscription.STATUS_ACTIVE,
            usage_month=timezone.now().strftime("%Y-%m"),
        )


def _user_stats(user):
    profiles = user.linkedin_profiles.all()
    total_comments = CommentLog.objects.filter(profile__in=profiles).count()
    today_comments = CommentLog.objects.filter(
        profile__in=profiles,
        created_at__date=timezone.now().date()
    ).count()
    running = sum(1 for p in profiles if p.is_running())
    return {
        "total_comments": total_comments,
        "today_comments": today_comments,
        "profiles":       profiles.count(),
        "running":        running,
    }


# ── Admin: Dashboard ─────────────────────────────────────────────────────────

@staff_member_required
def admin_dashboard(request):
    users = User.objects.filter(is_staff=False).select_related("subscription__plan").order_by("-date_joined")

    # Annotate each user with stats
    user_rows = []
    for u in users:
        _ensure_subscription(u)
        sub  = u.subscription
        stats = _user_stats(u)
        user_rows.append({"user": u, "sub": sub, "stats": stats})

    plans  = Plan.objects.all()
    total_users    = users.count()
    active_subs    = UserSubscription.objects.filter(status=UserSubscription.STATUS_ACTIVE).count()
    suspended_subs = UserSubscription.objects.filter(status=UserSubscription.STATUS_SUSPENDED).count()
    total_comments = CommentLog.objects.count()

    return render(request, "saas/admin_dashboard.html", {
        "user_rows":      user_rows,
        "plans":          plans,
        "total_users":    total_users,
        "active_subs":    active_subs,
        "suspended_subs": suspended_subs,
        "total_comments": total_comments,
    })


# ── Admin: Plan CRUD ─────────────────────────────────────────────────────────

@staff_member_required
def plan_list(request):
    plans = Plan.objects.annotate(sub_count=Count("subscriptions"))
    return render(request, "saas/plan_list.html", {"plans": plans})


@staff_member_required
def plan_new(request):
    if request.method == "POST":
        p = request.POST
        try:
            plan = Plan.objects.create(
                name=p["name"],
                tier=p.get("tier", Plan.TIER_CUSTOM),
                description=p.get("description", ""),
                max_profiles=int(p.get("max_profiles", 1)),
                max_comments_per_day=int(p.get("max_comments_per_day", 10)),
                max_targets=int(p.get("max_targets", 2)),
                max_bot_hours_month=int(p.get("max_bot_hours_month", 10)),
                is_active=bool(p.get("is_active")),
            )
            messages.success(request, f"Plan '{plan.name}' created.")
            return redirect("plan_list")
        except Exception as e:
            messages.error(request, f"Error: {e}")
    return render(request, "saas/plan_form.html", {"action": "Create", "plan": None,
        "tiers": Plan.TIER_CHOICES})


@staff_member_required
def plan_edit(request, pk):
    plan = get_object_or_404(Plan, pk=pk)
    if request.method == "POST":
        p = request.POST
        try:
            plan.name                 = p["name"]
            plan.tier                 = p.get("tier", plan.tier)
            plan.description          = p.get("description", "")
            plan.max_profiles         = int(p.get("max_profiles", plan.max_profiles))
            plan.max_comments_per_day = int(p.get("max_comments_per_day", plan.max_comments_per_day))
            plan.max_targets          = int(p.get("max_targets", plan.max_targets))
            plan.max_bot_hours_month  = int(p.get("max_bot_hours_month", plan.max_bot_hours_month))
            plan.is_active            = bool(p.get("is_active"))
            plan.save()
            messages.success(request, f"Plan '{plan.name}' updated.")
            return redirect("plan_list")
        except Exception as e:
            messages.error(request, f"Error: {e}")
    return render(request, "saas/plan_form.html", {"action": "Edit", "plan": plan,
        "tiers": Plan.TIER_CHOICES})


@staff_member_required
@require_POST
def plan_delete(request, pk):
    plan = get_object_or_404(Plan, pk=pk)
    if plan.subscriptions.exists():
        messages.error(request, "Cannot delete a plan with active subscribers. Reassign them first.")
        return redirect("plan_list")
    name = plan.name
    plan.delete()
    messages.success(request, f"Plan '{name}' deleted.")
    return redirect("plan_list")


# ── Admin: User subscription management ──────────────────────────────────────

@staff_member_required
def user_detail(request, user_id):
    u    = get_object_or_404(User, pk=user_id)
    _ensure_subscription(u)
    sub  = u.subscription
    plans = Plan.objects.filter(is_active=True)
    stats = _user_stats(u)
    profiles = u.linkedin_profiles.prefetch_related("comment_logs", "targets")
    return render(request, "saas/user_detail.html", {
        "u": u, "sub": sub, "plans": plans,
        "stats": stats, "profiles": profiles,
    })


@staff_member_required
@require_POST
def user_assign_plan(request, user_id):
    u    = get_object_or_404(User, pk=user_id)
    _ensure_subscription(u)
    plan = get_object_or_404(Plan, pk=request.POST.get("plan_id"))
    sub  = u.subscription
    sub.plan   = plan
    sub.status = UserSubscription.STATUS_ACTIVE
    sub.notes  = request.POST.get("notes", sub.notes)
    sub.save()
    messages.success(request, f"{u.username} assigned to '{plan.name}'.")
    return redirect("admin_user_detail", user_id=user_id)


@staff_member_required
@require_POST
def user_set_status(request, user_id):
    u   = get_object_or_404(User, pk=user_id)
    _ensure_subscription(u)
    sub = u.subscription
    new_status = request.POST.get("status")
    if new_status not in [UserSubscription.STATUS_ACTIVE,
                          UserSubscription.STATUS_SUSPENDED,
                          UserSubscription.STATUS_CANCELLED]:
        messages.error(request, "Invalid status.")
        return redirect("admin_user_detail", user_id=user_id)
    sub.status = new_status
    sub.save(update_fields=["status"])
    messages.success(request, f"{u.username} status set to {sub.get_status_display()}.")
    return redirect("admin_user_detail", user_id=user_id)


@staff_member_required
@require_POST
def user_reset_usage(request, user_id):
    u   = get_object_or_404(User, pk=user_id)
    _ensure_subscription(u)
    sub = u.subscription
    sub.bot_minutes_used_this_month = 0
    sub.usage_month = timezone.now().strftime("%Y-%m")
    sub.save(update_fields=["bot_minutes_used_this_month", "usage_month"])
    messages.success(request, f"Usage reset for {u.username}.")
    return redirect("admin_user_detail", user_id=user_id)


# ── User-facing: Subscription page ───────────────────────────────────────────

from django.contrib.auth.decorators import login_required

@login_required
def my_subscription(request):
    from accounts.models import Plan
    _ensure_subscription(request.user)
    sub   = request.user.subscription
    plans = Plan.objects.filter(is_active=True).exclude(tier=Plan.TIER_CUSTOM)
    return render(request, "saas/my_subscription.html", {
        "sub":   sub,
        "plans": plans,
    })
