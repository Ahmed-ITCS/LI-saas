from django.db.models.signals import post_save
from django.contrib.auth.models import User
from django.dispatch import receiver
from django.utils import timezone


@receiver(post_save, sender=User)
def create_subscription(sender, instance, created, **kwargs):
    if created and not instance.is_staff and not instance.is_superuser:
        from accounts.models import Plan, UserSubscription
        UserSubscription.objects.get_or_create(
            user=instance,
            defaults={
                "plan":       Plan.get_default(),
                "status":     UserSubscription.STATUS_ACTIVE,
                "usage_month": timezone.now().strftime("%Y-%m"),
            }
        )
