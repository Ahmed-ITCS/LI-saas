from django.urls import path
from . import views

urlpatterns = [
    # Admin panel
    path("admin-panel/",                          views.admin_dashboard,   name="admin_dashboard"),
    path("admin-panel/plans/",                    views.plan_list,         name="plan_list"),
    path("admin-panel/plans/new/",                views.plan_new,          name="plan_new"),
    path("admin-panel/plans/<int:pk>/edit/",      views.plan_edit,         name="plan_edit"),
    path("admin-panel/plans/<int:pk>/delete/",    views.plan_delete,       name="plan_delete"),
    path("admin-panel/users/<int:user_id>/",      views.user_detail,       name="admin_user_detail"),
    path("admin-panel/users/<int:user_id>/assign/", views.user_assign_plan, name="admin_assign_plan"),
    path("admin-panel/users/<int:user_id>/status/", views.user_set_status,  name="admin_user_status"),
    path("admin-panel/users/<int:user_id>/reset-usage/", views.user_reset_usage, name="admin_reset_usage"),

    # User-facing
    path("subscription/",                         views.my_subscription,   name="my_subscription"),
]
