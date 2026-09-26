"""Whether this machine can run the pipeline: one check per thing a run depends on.

Shared by the CLI and the web route. A check that itself fails is reported as a
failed check, so checks() never raises. An optional check reports what it found
but never makes the machine unfit to run.
"""
import datetime
import shutil
import subprocess
import sys

from cairn import llm, paths, schedule, secrets, settings

TIMEOUT = 5  # seconds for any subprocess a check runs
LLM_TIMEOUT = 20
# a provider that answered this recently is not asked again, since each ask counts
# against a free tier's daily requests
LLM_FRESH = datetime.timedelta(hours=24)


def _run(cmd):
    """(ok, output) for a short command; a missing binary or a hang is a failure."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    return out.returncode == 0, (out.stdout or out.stderr).strip()


def _python():
    version = ".".join(map(str, sys.version_info[:3]))
    return (sys.version_info >= (3, 11), f"Python {version}",
            "install Python 3.11 or newer")


def _claude():
    fix = "install Claude Code from claude.com/claude-code and sign in"
    found = shutil.which(settings.get().claude_bin)
    if not found:
        return False, "Claude Code isn't installed on this computer", fix
    ok, output = _run([found, "--version"])
    return ok, "Installed" if ok else output or "Claude Code didn't start", fix


def _llm_fix(spec):
    where = f" and paste a key from {spec['key_url']}" if spec["key_url"] else ""
    return f"open Settings › AI provider{where}"


def _llm_key():
    spec = llm.PROVIDERS[llm.active()]
    found = secrets.has_key(llm.active())
    detail = f"{spec['label']} key saved" if found else f"No {spec['label']} key yet"
    return found, detail, _llm_fix(spec)


def _llm_answers():
    provider = llm.active()
    spec = llm.PROVIDERS[provider]
    if spec["needs_key"] and not secrets.has_key(provider):
        return False, f"Add your {spec['label']} key first", _llm_fix(spec)
    answered = llm.last_answer(provider)
    age = answered and datetime.datetime.now(datetime.UTC) - answered
    if age is not None and datetime.timedelta(0) <= age < LLM_FRESH:
        return True, f"Answered {int(age.total_seconds() // 60)} min ago", ""
    reply, err = llm.complete(llm.TEST_PROMPT, tier="strong", timeout=LLM_TIMEOUT, retries=0)
    if err:
        return False, err, _llm_fix(spec)
    return True, f"{spec['label']} answered", ""


def _llm_checks():
    """(name, check, required) for the active model provider."""
    provider = llm.active()
    if provider == "claude-code":
        return [("claude", _claude, True)]
    needs_key = llm.PROVIDERS.get(provider, {}).get("needs_key", True)
    key = [("llm key", _llm_key, True)] if needs_key else []
    return key + [("llm", _llm_answers, False)]


def _home():
    wanted = (paths.config_file(), paths.profile_md())
    missing = [p.name for p in wanted if not p.exists()]
    detail = "Setup isn't finished" if missing else "Your profile and settings are in place"
    return not missing, detail, "finish setup in the app"


def _schedule():
    job = schedule.status()
    if job["error"]:
        return True, "Couldn't read the schedule. Set it again in Settings › Schedule", ""
    if not job["installed"]:
        return True, "Off. Turn it on in Settings › Schedule", ""
    loaded = "" if job["loaded"] else " (not running yet)"
    when = "-" if job["hour"] is None else f"{job['hour']}:{job['minute'] or 0:02d}"
    detail = f"Daily at {when}{loaded}"
    return True, detail, ""


def _autostart():
    item = schedule.autostart_status()
    if item["error"]:
        return True, "Couldn't check whether Cairn opens at login", ""
    if not item["installed"]:
        return True, "Off. Turn it on in Settings › System", ""
    when = ", starting at your next login" if not item["loaded"] else ""
    return True, f"On{when}", ""


def _pdftotext():
    found = shutil.which("pdftotext")
    detail = "Installed" if found else "Can't read PDF resumes yet. You can paste the text instead"
    fix = ("install Poppler and put its bin folder on your PATH" if sys.platform == "win32"
           else "brew install poppler")
    return bool(found), detail, fix


def _checks():
    """(name, check, required) for every check, in a fixed order."""
    return [("python", _python, True), *_llm_checks(), ("home", _home, True),
            ("schedule", _schedule, False), ("start at login", _autostart, False),
            ("pdftotext", _pdftotext, False)]


def checks():
    """[{name, ok, required, detail, fix}] for every check, in a fixed order."""
    results = []
    for name, check, required in _checks():
        try:
            ok, detail, fix = check()
        except Exception as e:  # noqa: BLE001 - a broken check is a failed check
            ok, detail, fix = False, f"{type(e).__name__}: {e}", ""
        results.append({"name": name, "ok": ok, "required": required, "detail": detail,
                        "fix": "" if ok else fix})
    return results
