def subscription(request):
    """
    Injects the current user's subscription into every template context.
    Falls back gracefully if the user is not authenticated or has no subscription.
    """
    if not request.user.is_authenticated:
        return {}
    try:
        return {"user_subscription": request.user.subscription}
    except Exception:
        return {}
