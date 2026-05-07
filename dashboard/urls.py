from django.urls import path
from . import views

urlpatterns = [
    path("",                                 views.index,                  name="index"),
    path("profiles/new/",                    views.profile_new,            name="profile_new"),
    path("profiles/<int:pk>/edit/",          views.profile_edit,           name="profile_edit"),
    path("profiles/<int:pk>/delete/",        views.profile_delete,         name="profile_delete"),
    path("profiles/<int:pk>/",               views.profile_detail,         name="profile_detail"),
    path("profiles/<int:pk>/session/",       views.session_setup,          name="session_setup"),

    # Core API
    path("api/stats/",                            views.api_stats,              name="api_stats"),
    path("api/profiles/<int:pk>/stats/",          views.api_profile_stats,      name="api_profile_stats"),
    path("api/profiles/<int:pk>/comments/",       views.api_comments,           name="api_comments"),
    path("api/profiles/<int:pk>/start/",          views.api_bot_start,          name="api_bot_start"),
    path("api/profiles/<int:pk>/stop/",           views.api_bot_stop,           name="api_bot_stop"),
    path("api/profiles/<int:pk>/session/status/", views.api_session_status,     name="api_session_status"),
    path("api/profiles/<int:pk>/session/clear/",  views.api_session_clear,      name="api_session_clear"),
    path("api/profiles/<int:pk>/session/save/",   views.api_session_save_cookies, name="api_session_save_cookies"),
    path("api/profiles/<int:pk>/session/upload/", views.api_session_upload,     name="api_session_upload"),

    # Logs
    path("api/profiles/<int:pk>/log/download/",   views.log_download,           name="log_download"),
    path("api/profiles/<int:pk>/log/search/",     views.log_search,             name="log_search"),

    # Target profiles
    path("api/profiles/<int:pk>/targets/",                   views.api_targets_list,       name="api_targets_list"),
    path("api/profiles/<int:pk>/targets/add/",               views.api_target_add,         name="api_target_add"),
    path("api/profiles/<int:pk>/targets/<int:tid>/delete/",  views.api_target_delete,      name="api_target_delete"),
    path("api/profiles/<int:pk>/targets/<int:tid>/toggle/",  views.api_target_toggle,      name="api_target_toggle"),
    path("api/profiles/<int:pk>/targets/<int:tid>/start/",   views.api_bot_start_targeted, name="api_bot_start_targeted"),

    # SSE
    path("stream/logs/<int:pk>/",                views.stream_logs,            name="stream_logs"),
]
