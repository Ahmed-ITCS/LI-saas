"""
python manage.py seed_plans

Creates the three built-in plans (Free, Pro, Agency) if they don't exist.
Safe to run multiple times.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the default Free, Pro, and Agency subscription plans."

    def handle(self, *args, **options):
        from accounts.models import Plan

        defaults = [
            {
                "name": "Free",
                "tier": Plan.TIER_FREE,
                "description": "Get started — one LinkedIn profile, limited daily comments.",
                "max_profiles": 1,
                "max_comments_per_day": 10,
                "max_targets": 2,
                "max_bot_hours_month": 10,
                "is_active": True,
            },
            {
                "name": "Pro",
                "tier": Plan.TIER_PRO,
                "description": "For professionals — 3 profiles, 50 comments/day, 100 bot hours/month.",
                "max_profiles": 3,
                "max_comments_per_day": 50,
                "max_targets": 10,
                "max_bot_hours_month": 100,
                "is_active": True,
            },
            {
                "name": "Agency",
                "tier": Plan.TIER_AGENCY,
                "description": "For agencies — 10 profiles, 200 comments/day, unlimited bot hours.",
                "max_profiles": 10,
                "max_comments_per_day": 200,
                "max_targets": 50,
                "max_bot_hours_month": -1,
                "is_active": True,
            },
        ]

        for d in defaults:
            tier = d.pop("tier")
            name = d["name"]
            plan, created = Plan.objects.update_or_create(
                tier=tier,
                name=name,
                defaults=d,
            )
            status = "Created" if created else "Already exists"
            self.stdout.write(self.style.SUCCESS(f"  {status}: {plan}"))

        self.stdout.write(self.style.SUCCESS("\n✅ Default plans ready."))
