import json
import os
import sys
import subprocess
import threading
import time
import collections
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db.models import Count, Q
from django.utils import timezone
from datetime import timedelta

from accounts.models import LinkedInProfile, CommentLog
from accounts.forms import LinkedInProfileForm

# ── Per-profile log ring buffers ──────────────────────────────────────────────
# profile_id → deque(maxlen=300)
_log_buffers: dict[int, collections.deque] = {}
_log_lock = threading.Lock()

def _get_log_buffer(profile_id: int) -> collections.deque:
    with _log_lock:
        if profile_id not in _log_buffers:
            _log_buffers[profile_id] = collections.deque(maxlen=300)
        return _log_buffers[profile_id]

def _tail_log(profile_id: int, log_path: str):
    """Background thread: tail a profile's log file into its ring buffer."""
    buf = _get_log_buffer(profile_id)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a"):
        pass  # ensure exists
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                buf.append(line.rstrip())
            else:
                time.sleep(0.3)

_tail_threads: set[int] = set()

def ensure_tail_thread(profile_id: int, log_path: str):
    if profile_id not in _tail_threads:
        _tail_threads.add(profile_id)
        t = threading.Thread(target=_tail_log, args=(profile_id, log_path), daemon=True)
        t.start()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log_path(profile: LinkedInProfile) -> str:
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"profile_{profile.pk}.log")

def _state_file(profile: LinkedInProfile) -> str:
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "states")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"profile_{profile.pk}.json")

def _get_stats(user):
    profiles = LinkedInProfile.objects.filter(user=user)
    total  = CommentLog.objects.filter(profile__in=profiles).count()
    today  = CommentLog.objects.filter(
        profile__in=profiles,
        created_at__date=timezone.now().date()
    ).count()
    running = sum(1 for p in profiles if p.is_running())
    return {"total": total, "today": today, "running": running, "profiles": profiles.count()}


# ── Views ─────────────────────────────────────────────────────────────────────

@login_required
def index(request):
    profiles = LinkedInProfile.objects.filter(user=request.user)
    stats    = _get_stats(request.user)
    for p in profiles:
        ensure_tail_thread(p.pk, _log_path(p))
    return render(request, "dashboard/index.html", {"profiles": profiles, "stats": stats})


@login_required
def profile_new(request):
    from saas.views import _ensure_subscription
    _ensure_subscription(request.user)
    sub = request.user.subscription
    can, reason = sub.can_add_profile()
    if not can:
        messages.error(request, reason)
        return redirect("index")
    form = LinkedInProfileForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.user       = request.user
        obj.state_file = _state_file(obj)
        obj.save()
        messages.success(request, f"Profile '{obj.label}' created.")
        return redirect("index")
    return render(request, "dashboard/profile_form.html", {"form": form, "action": "Add"})


@login_required
def profile_edit(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    form    = LinkedInProfileForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Profile '{profile.label}' updated.")
        return redirect("index")
    return render(request, "dashboard/profile_form.html", {"form": form, "action": "Edit", "profile": profile})


@login_required
def profile_delete(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    if profile.is_running():
        messages.error(request, "Stop the bot before deleting.")
        return redirect("index")
    profile.delete()
    messages.success(request, "Profile deleted.")
    return redirect("index")


@login_required
def profile_detail(request, pk):
    profile  = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    comments = CommentLog.objects.filter(profile=profile)[:50]
    ensure_tail_thread(profile.pk, _log_path(profile))
    return render(request, "dashboard/profile_detail.html", {
        "profile":  profile,
        "comments": comments,
    })


# ── API ───────────────────────────────────────────────────────────────────────

@login_required
def api_stats(request):
    return JsonResponse(_get_stats(request.user))


@login_required
def api_profile_stats(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    total   = CommentLog.objects.filter(profile=profile).count()
    today   = CommentLog.objects.filter(profile=profile, created_at__date=timezone.now().date()).count()
    return JsonResponse({
        "running": profile.is_running(),
        "total":   total,
        "today":   today,
        "pid":     profile.pid,
    })


@login_required
def api_comments(request, pk):
    profile  = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    comments = CommentLog.objects.filter(profile=profile).values(
        "urn", "post_text", "comment", "post_age_minutes", "created_at"
    )[:50]
    data = []
    for c in comments:
        c["created_at"] = str(c["created_at"])
        data.append(c)
    return JsonResponse(data, safe=False)


@login_required
@require_POST
def api_bot_start(request, pk):
    from saas.views import _ensure_subscription
    _ensure_subscription(request.user)
    sub = request.user.subscription
    can, reason = sub.can_run_bot()
    if not can:
        return JsonResponse({"ok": False, "msg": reason})
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    if profile.is_running():
        return JsonResponse({"ok": False, "msg": "Already running"})

    bot_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot", "runner.py")
    log_path   = _log_path(profile)
    state_path = _state_file(profile)

    # Update state_file path in DB
    if not profile.state_file:
        profile.state_file = state_path
        profile.save(update_fields=["state_file"])

    env = os.environ.copy()
    env["BOT_PROFILE_ID"] = str(profile.pk)
    env["BOT_LOG_FILE"]   = log_path
    env["BOT_STATE_FILE"] = state_path
    env["DJANGO_SETTINGS_MODULE"] = "libot.settings"

    runner_log = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, bot_script],
            env=env,
            stdout=runner_log,
            stderr=runner_log,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
    finally:
        runner_log.close()

    # Early crash detection (e.g. bad env/deps/session bootstrap).
    time.sleep(0.6)
    exit_code = proc.poll()
    if exit_code is not None:
        return JsonResponse({"ok": False, "msg": f"Bot failed to start (exit {exit_code}). Check profile log."})

    profile.pid = proc.pid
    profile.save(update_fields=["pid"])

    ensure_tail_thread(profile.pk, log_path)
    return JsonResponse({"ok": True, "msg": f"Bot started (PID {proc.pid})"})


@login_required
@require_POST
def api_bot_stop(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    if not profile.is_running():
        return JsonResponse({"ok": False, "msg": "Bot is not running"})

    import signal
    try:
        os.kill(profile.pid, signal.SIGTERM)
        # Give it a moment, then SIGKILL if needed
        for _ in range(16):
            time.sleep(0.5)
            try:
                os.kill(profile.pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(profile.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    profile.pid = None
    profile.save(update_fields=["pid"])
    return JsonResponse({"ok": True, "msg": "Bot stopped"})


@login_required
def stream_logs(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    buf = _get_log_buffer(profile.pk)

    def generate():
        with _log_lock:
            snapshot = list(buf)
        for line in snapshot:
            yield f"data: {line}\n\n"

        last_seen = len(snapshot)
        last_heartbeat = time.time()
        started_at = time.time()
        while True:
            with _log_lock:
                current = list(buf)
            for line in current[last_seen:]:
                yield f"data: {line}\n\n"
            last_seen = len(current)

            # Keep SSE connections alive so gunicorn doesn't time out idle workers.
            now = time.time()
            if now - last_heartbeat >= 15:
                yield ": keepalive\n\n"
                last_heartbeat = now

            # Sync gunicorn workers cannot hold one request forever; rotate stream.
            # EventSource on the browser reconnects automatically.
            if now - started_at >= 20:
                break

            time.sleep(0.5)

    return StreamingHttpResponse(
        generate(),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

@login_required
@require_POST
def api_session_clear(request, pk):
    """Delete the saved Playwright session file so the bot re-authenticates next start."""
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    if profile.is_running():
        return JsonResponse({"ok": False, "msg": "Stop the bot before clearing the session."})
    if profile.state_file and os.path.exists(profile.state_file):
        os.remove(profile.state_file)
        return JsonResponse({"ok": True, "msg": "Session cleared. Run setup_session to re-authenticate."})
    return JsonResponse({"ok": False, "msg": "No session file found."})


@login_required
def api_session_status(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    return JsonResponse({
        "has_session": profile.has_session(),
        "state_file":  profile.state_file or "",
    })


# ── Log download ──────────────────────────────────────────────────────────────

@login_required
def log_download(request, pk):
    from django.http import FileResponse, Http404
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    path = _log_path(profile)
    if not os.path.exists(path):
        raise Http404("No log file yet.")
    return FileResponse(
        open(path, "rb"),
        as_attachment=True,
        filename=f"libot_profile_{pk}_{profile.label}.log",
        content_type="text/plain",
    )


@login_required
def log_search(request, pk):
    """Return log lines matching ?q= as JSON array."""
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    q = request.GET.get("q", "").strip().lower()
    path = _log_path(profile)
    if not os.path.exists(path) or not q:
        return JsonResponse({"lines": []})
    matches = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if q in line.lower():
                matches.append(line.rstrip())
    return JsonResponse({"lines": matches[-300:]})  # cap at 300


# ── Target profiles ───────────────────────────────────────────────────────────

@login_required
@require_POST
def api_target_add(request, pk):
    from accounts.models import TargetProfile
    from saas.views import _ensure_subscription
    _ensure_subscription(request.user)
    sub = request.user.subscription
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    url   = request.POST.get("linkedin_url", "").strip()
    label = request.POST.get("label", "").strip()
    if not url:
        return JsonResponse({"ok": False, "msg": "URL required."})
    if "linkedin.com/in/" not in url:
        return JsonResponse({"ok": False, "msg": "Must be a linkedin.com/in/... URL."})
    can, reason = sub.can_add_target(profile)
    if not can:
        return JsonResponse({"ok": False, "msg": reason})
    obj, created = TargetProfile.objects.get_or_create(
        owner=profile, linkedin_url=url,
        defaults={"label": label}
    )
    if not created:
        return JsonResponse({"ok": False, "msg": "Target already exists."})
    return JsonResponse({"ok": True, "msg": f"Target added.", "id": obj.pk, "label": str(obj), "url": url, "active": obj.is_active})


@login_required
@require_POST
def api_target_delete(request, pk, tid):
    from accounts.models import TargetProfile
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    target  = get_object_or_404(TargetProfile, pk=tid, owner=profile)
    target.delete()
    return JsonResponse({"ok": True, "msg": "Target removed."})


@login_required
@require_POST
def api_target_toggle(request, pk, tid):
    from accounts.models import TargetProfile
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    target  = get_object_or_404(TargetProfile, pk=tid, owner=profile)
    target.is_active = not target.is_active
    target.save(update_fields=["is_active"])
    return JsonResponse({"ok": True, "active": target.is_active})


@login_required
def api_targets_list(request, pk):
    from accounts.models import TargetProfile
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    data = list(profile.targets.values("id", "linkedin_url", "label", "is_active"))
    return JsonResponse(data, safe=False)


@login_required
@require_POST
def api_bot_start_targeted(request, pk, tid):
    """Start the bot for a single target profile URL (one-shot run)."""
    from accounts.models import TargetProfile
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    target  = get_object_or_404(TargetProfile, pk=tid, owner=profile)

    if profile.is_running():
        return JsonResponse({"ok": False, "msg": "Bot already running — stop it first."})

    bot_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot", "runner.py")
    log_path   = _log_path(profile)
    state_path = _state_file(profile)

    if not profile.state_file:
        profile.state_file = state_path
        profile.save(update_fields=["state_file"])

    env = os.environ.copy()
    env["BOT_PROFILE_ID"]    = str(profile.pk)
    env["BOT_LOG_FILE"]      = log_path
    env["BOT_STATE_FILE"]    = state_path
    env["BOT_TARGET_URL"]    = target.activity_url()
    env["BOT_TARGET_LABEL"]  = str(target)
    env["DJANGO_SETTINGS_MODULE"] = "libot.settings"

    runner_log = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, bot_script],
            env=env,
            stdout=runner_log,
            stderr=runner_log,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
    finally:
        runner_log.close()

    # Early crash detection (e.g. invalid target/session/deps).
    time.sleep(0.6)
    exit_code = proc.poll()
    if exit_code is not None:
        return JsonResponse({"ok": False, "msg": f"Targeted bot failed to start (exit {exit_code}). Check profile log."})

    profile.pid = proc.pid
    profile.save(update_fields=["pid"])
    ensure_tail_thread(profile.pk, log_path)
    return JsonResponse({"ok": True, "msg": f"Targeting '{target}' (PID {proc.pid})"})


# ── Session setup ─────────────────────────────────────────────────────────────
# We track in-progress session setup processes: profile_id → Popen
_session_procs: dict[int, subprocess.Popen] = {}
_session_lock = threading.Lock()


@login_required
def session_setup(request, pk):
    """Render the session setup page for a profile."""
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    return render(request, "dashboard/session_setup.html", {"profile": profile})


@login_required
@require_POST
def api_session_launch(request, pk):
    """
    Spawn a headed Chromium browser pre-filled with LinkedIn credentials.
    The user logs in manually in that window, then calls /save/ to capture state.
    """
    from accounts.crypto import decrypt

    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)

    with _session_lock:
        # Kill any previous session-setup browser for this profile
        existing = _session_procs.get(pk)
        if existing and existing.poll() is None:
            existing.terminate()

    state_path = _state_file(profile)
    log_path   = _log_path(profile)

    launcher = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot", "session_launcher.py")

    try:
        email    = decrypt(profile.li_email)
        password = decrypt(profile.li_password)
    except Exception:
        return JsonResponse({"ok": False, "msg": "Could not decrypt credentials — check FERNET_KEY."})

    env = os.environ.copy()
    env["SESSION_PROFILE_ID"] = str(profile.pk)
    env["SESSION_EMAIL"]      = email
    env["SESSION_PASSWORD"]   = password
    env["SESSION_STATE_FILE"] = state_path
    env["SESSION_LOG_FILE"]   = log_path
    env["DJANGO_SETTINGS_MODULE"] = "libot.settings"

    proc = subprocess.Popen(
        [sys.executable, launcher],
        env=env,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )

    with _session_lock:
        _session_procs[pk] = proc

    # Update state_file path in DB now so the bot knows where to look
    profile.state_file = state_path
    profile.save(update_fields=["state_file"])

    return JsonResponse({"ok": True, "msg": "Browser launched — log in to LinkedIn, then click Save Session."})


@login_required
@require_POST
def api_session_save(request, pk):
    """
    Signal the launcher process to save the Playwright storage state and close.
    We do this by writing a sentinel file that the launcher is watching for.
    """
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    state_path = _state_file(profile)

    sentinel = state_path + ".save_now"
    with open(sentinel, "w") as f:
        f.write("1")

    # Wait up to 8 seconds for the state file to appear
    for _ in range(16):
        time.sleep(0.5)
        if os.path.exists(state_path):
            # Clean up proc
            with _session_lock:
                proc = _session_procs.pop(pk, None)
            if proc and proc.poll() is None:
                proc.terminate()
            return JsonResponse({"ok": True, "msg": "✅ Session saved! You can now start the bot."})

    return JsonResponse({"ok": False, "msg": "Session file not found — did you complete the LinkedIn login?"})


@login_required
@require_POST
def api_session_abort(request, pk):
    """Cancel a session setup in progress."""
    get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    with _session_lock:
        proc = _session_procs.pop(pk, None)
    if proc and proc.poll() is None:
        proc.terminate()
    return JsonResponse({"ok": True, "msg": "Session setup cancelled."})


# ── Session upload ────────────────────────────────────────────────────────────

@login_required
def session_setup(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    return render(request, "dashboard/session_setup.html", {"profile": profile})


@login_required
@require_POST
def api_session_upload(request, pk):
    """Accept an uploaded Playwright state.json file and save it for this profile."""
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)

    if profile.is_running():
        return JsonResponse({"ok": False, "msg": "Stop the bot before replacing the session."})

    f = request.FILES.get("session_file")
    if not f:
        return JsonResponse({"ok": False, "msg": "No file received."})
    if not f.name.endswith(".json"):
        return JsonResponse({"ok": False, "msg": "Must be a .json file."})

    # Basic sanity check — must be valid JSON with at least cookies or origins
    import json as _json
    try:
        raw = f.read()
        data = _json.loads(raw)
        if "cookies" not in data and "origins" not in data:
            return JsonResponse({"ok": False, "msg": "File doesn't look like a Playwright state file (missing cookies/origins)."})
    except Exception:
        return JsonResponse({"ok": False, "msg": "Invalid JSON file."})

    state_path = _state_file(profile)
    with open(state_path, "wb") as out:
        out.write(raw)

    profile.state_file = state_path
    profile.save(update_fields=["state_file"])

    return JsonResponse({"ok": True, "msg": "Session uploaded successfully. Bot is ready to run."})


# ── Session via cookies ───────────────────────────────────────────────────────

@login_required
def session_setup(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)
    return render(request, "dashboard/session_setup.html", {"profile": profile})


@login_required
@require_POST
def api_session_save_cookies(request, pk):
    """
    Build a Playwright storage_state JSON from the two LinkedIn cookies
    the user pastes in: li_at (auth token) and JSESSIONID.
    Optionally accepts li_rm and lidc too for completeness.
    """
    import json as _json

    profile = get_object_or_404(LinkedInProfile, pk=pk, user=request.user)

    if profile.is_running():
        return JsonResponse({"ok": False, "msg": "Stop the bot before updating the session."})

    def _clean_cookie(raw: str, name: str) -> str:
        value = (raw or "").strip().strip(";")
        lower_name = name.lower()
        if value.lower().startswith(lower_name + "="):
            value = value.split("=", 1)[1].strip()
        return value.strip().strip(";")

    li_at      = _clean_cookie(request.POST.get("li_at", ""), "li_at")
    jsessionid = _clean_cookie(request.POST.get("jsessionid", ""), "JSESSIONID")
    li_rm      = _clean_cookie(request.POST.get("li_rm", ""), "li_rm")
    lidc       = _clean_cookie(request.POST.get("lidc", ""), "lidc")

    if not li_at:
        return JsonResponse({"ok": False, "msg": "li_at cookie is required."})
    if not jsessionid:
        return JsonResponse({"ok": False, "msg": "JSESSIONID cookie is required."})

    # LinkedIn often stores JSESSIONID in quoted form. If a user pastes an
    # unquoted value, normalize it so CSRF-linked flows behave as expected.
    if not (jsessionid.startswith('"') and jsessionid.endswith('"')):
        jsessionid = f'"{jsessionid}"'

    # Build a Playwright-compatible storage_state structure
    cookies = [
        {
            "name":     "li_at",
            "value":    li_at,
            "domain":   ".linkedin.com",
            "path":     "/",
            "httpOnly": True,
            "secure":   True,
            "sameSite": "None",
        },
        {
            "name":     "JSESSIONID",
            "value":    jsessionid,
            "domain":   ".linkedin.com",
            "path":     "/",
            "httpOnly": True,
            "secure":   True,
            "sameSite": "None",
        },
    ]

    if li_rm:
        cookies.append({
            "name": "li_rm", "value": li_rm,
            "domain": ".linkedin.com", "path": "/",
            "httpOnly": True, "secure": True, "sameSite": "None",
        })
    if lidc:
        cookies.append({
            "name": "lidc", "value": lidc,
            "domain": ".linkedin.com", "path": "/",
            "httpOnly": False, "secure": True, "sameSite": "None",
        })

    state = {"cookies": cookies, "origins": []}

    state_path = _state_file(profile)
    with open(state_path, "w", encoding="utf-8") as f:
        _json.dump(state, f, indent=2)

    profile.state_file = state_path
    profile.save(update_fields=["state_file"])

    return JsonResponse({"ok": True, "msg": "Session saved! Bot is ready to run."})
