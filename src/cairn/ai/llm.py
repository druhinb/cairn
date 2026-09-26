"""Model providers other than the Claude Code CLI, over their HTTP APIs.

claude.run dispatches here when settings pick a provider other than claude-code, so
ranking, onboarding and summaries never call this module directly. Each call names a
tier: "strong" for ranking and onboarding, "cheap" for summaries.
"""
import datetime
import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from cairn import __version__
from cairn.core import events, paths, secrets, settings

REQUEST_TIMEOUT = 120
MAX_TIMEOUT = 300
READ_CHUNK = 64 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
RETRIES = 2
MAX_WAIT = 30
TEST_PROMPT = "Reply with the single word OK."
USER_AGENT = f"cairn/{__version__}"
# the project has no public URL yet; OpenRouter only uses this to group an app's traffic
APP_URL = "http://localhost"

PROVIDERS = {
    "claude-code": {
        "label": "Claude Code", "kind": "cli", "base_url": "",
        "key_url": "https://claude.com/claude-code",
        "models": {"strong": "sonnet", "cheap": "haiku"},
        "steps": ["Install Claude Code from claude.com/claude-code.",
                  "Open Terminal (PowerShell on Windows), type claude, and sign in with "
                  "your Claude account.",
                  "Come back here and press Test."],
        "free": False, "needs_key": False, "rpm": None,
        "notes": "Uses the limits of your Claude plan.",
    },
    "anthropic": {
        "label": "Anthropic", "kind": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "key_url": "https://console.anthropic.com/settings/keys",
        "models": {"strong": "claude-sonnet-5", "cheap": "claude-haiku-4-5-20251001"},
        "steps": ["Create an account at console.anthropic.com.",
                  "Add at least $5 of credit under Billing. A Claude subscription "
                  "doesn't cover it.",
                  "Create a key under Settings › API keys.",
                  "Paste the key here."],
        "free": False, "needs_key": True, "rpm": None,
        "notes": "Pay as you go. New accounts are fast enough for Cairn.",
    },
    "gemini": {
        "label": "Google Gemini", "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_url": "https://aistudio.google.com/app/apikey",
        "models": {"strong": "gemini-3.8-flash", "cheap": "gemini-3.5-flash-lite"},
        "steps": ["Sign in to Google AI Studio with a Google account.",
                  "Open Get API key and create a key. No card needed.",
                  "Paste the key here.",
                  "Free keys are slow, so a run can take a few minutes.",
                  "On the free plan, Google uses what Cairn sends, including your resume, "
                  "to improve its products."],
        "free": True, "needs_key": True, "rpm": 10,
        "notes": "Free. Runs take a few minutes.",
    },
    "groq": {
        "label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1",
        "key_url": "https://console.groq.com/keys",
        "models": {"strong": "llama-3.3-70b-versatile", "cheap": "llama-3.1-8b-instant"},
        "steps": ["Create a free account at console.groq.com. No card needed.",
                  "Open API Keys and create a key.",
                  "Paste the key here."],
        "free": True, "needs_key": True, "rpm": 25,
        "notes": "Free. A big first run can use up the daily limit.",
    },
    "mistral": {
        "label": "Mistral", "kind": "openai", "base_url": "https://api.mistral.ai/v1",
        "key_url": "https://console.mistral.ai/api-keys",
        "models": {"strong": "mistral-large-latest", "cheap": "mistral-small-latest"},
        "steps": ["Create an account at console.mistral.ai.",
                  "Choose the free Experiment plan. It needs no card.",
                  "Create a key under API Keys.",
                  "Paste the key here."],
        "free": True, "needs_key": True, "rpm": 30,
        "notes": "Free on the Experiment plan.",
    },
    "openrouter": {
        "label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
        "key_url": "https://openrouter.ai/settings/keys",
        "models": {"strong": "nvidia/nemotron-3-super-120b-a12b:free",
                   "cheap": "google/gemma-4-26b-a4b-it:free"},
        "steps": ["Create an account at openrouter.ai.",
                  "Create a key under Settings › Keys.",
                  "Paste the key here.",
                  "Free models allow 50 requests a day, and a one-time $10 credit raises "
                  "that to 1,000. A run uses 10 to 40."],
        "free": True, "needs_key": True, "rpm": 15,
        "notes": "Free, up to 50 requests a day.",
    },
    "ollama": {
        "label": "Ollama", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1",
        "key_url": "",
        "models": {"strong": "llama3.1:8b", "cheap": "llama3.2:3b"},
        "steps": ["Install Ollama from ollama.com and open it.",
                  "In Terminal (PowerShell on Windows), run ollama pull llama3.1:8b, then "
                  "ollama pull llama3.2:3b.",
                  "Small models on a laptop rank less reliably than online providers."],
        "free": True, "needs_key": False, "rpm": None,
        "notes": "Free and runs on this computer, at its speed.",
    },
    "custom": {
        "label": "Custom", "kind": "openai", "base_url": "", "key_url": "",
        "models": {"strong": "", "cheap": ""},
        "steps": ["Enter the service's address, the part before /chat/completions.",
                  "Enter the model names for ranking and for summaries.",
                  "Paste a key if the service needs one."],
        "free": False, "needs_key": False, "rpm": None,
        "notes": "Any other AI service. Its own limits apply.",
    },
}

# providers whose base URL the user may change; every other one keeps its own, so a
# stored key only ever goes to the host it was made for
EDITABLE_BASE_URL = frozenset({"ollama", "custom"})


def valid_base_url(url):
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


@dataclass(frozen=True)
class Target:
    provider: str
    model: str
    base_url: str
    key: str

    @property
    def label(self):
        return PROVIDERS[self.provider]["label"]


def _setting(name, default=""):
    return getattr(settings.get(), name, default)


def active():
    return _setting("llm_provider", "claude-code")


def target(tier="strong", provider=None, model=None, base_url=None, key=None):
    """The Target for a tier from settings and secrets; each argument given overrides,
    except base_url for a provider outside EDITABLE_BASE_URL."""
    provider = provider or active()
    spec = PROVIDERS[provider]
    same = provider == active()
    if not model:
        chosen = _setting("llm_model" if tier == "strong" else "llm_model_cheap") if same else ""
        model = chosen or spec["models"][tier]
    if provider not in EDITABLE_BASE_URL:
        base_url = spec["base_url"]
    elif not base_url:
        base_url = (_setting("llm_base_url") if same else "") or spec["base_url"]
    if key is None:
        key = secrets.get_key(provider) or ""
    return Target(provider, model, base_url.rstrip("/"), key)


_pace_lock = threading.Lock()
_next_slot = {}


def _pace(provider):
    """Space calls to a free provider at least 60/rpm seconds apart."""
    rpm = PROVIDERS[provider]["rpm"]
    if not (PROVIDERS[provider]["free"] and rpm):
        return
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(provider, now))
        _next_slot[provider] = slot + 60 / rpm
    if slot > now:
        time.sleep(slot - now)


def complete(prompt, *, tier, timeout, retries=RETRIES):
    """(text, err) from the active HTTP provider. Never raises."""
    try:
        t = target(tier)
    except secrets.SecretsError as e:
        return None, str(e)
    _pace(t.provider)
    return send(t, prompt, timeout, retries)


def _request(t, prompt):
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if PROVIDERS[t.provider]["kind"] == "anthropic":
        if t.key:
            headers["x-api-key"] = t.key
        headers["anthropic-version"] = "2023-06-01"
        body = {"model": t.model, "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}]}
        return f"{t.base_url}/messages", headers, body
    if t.key:
        headers["Authorization"] = f"Bearer {t.key}"
    if t.provider == "openrouter":
        # OpenRouter's docs name X-OpenRouter-Title and still accept X-Title
        headers["HTTP-Referer"] = APP_URL
        headers["X-Title"] = "Cairn"
    body = {"model": t.model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0}
    return f"{t.base_url}/chat/completions", headers, body


def _answer(t, payload):
    if PROVIDERS[t.provider]["kind"] == "anthropic":
        blocks = payload.get("content") if isinstance(payload, dict) else None
        text = "".join(b.get("text", "") for b in blocks or ()
                       if isinstance(b, dict) and b.get("type") == "text")
    else:
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None, f"{t.label} sent an answer Cairn couldn't read. Try again."
    if not isinstance(text, str) or not text.strip():
        return None, f"{t.label} sent an empty answer. Try again."
    return text, None


def _error_message(raw):
    """The message an API put in its JSON error body, or the start of the raw body."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace").strip()[:300]
    if isinstance(payload, list) and payload:  # Gemini wraps its error in a list
        payload = payload[0]
    if not isinstance(payload, dict):
        return raw.decode("utf-8", "replace").strip()[:300]
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("message")
    # Mistral puts its message in "detail"
    for message in (error, payload.get("detail"), payload.get("message")):
        if isinstance(message, str) and message:
            return message[:300]
    return raw.decode("utf-8", "replace").strip()[:300]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    # A followed redirect would resend the key to a host the user never configured.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback(url):
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _opener(url):
    """An opener for url that never follows a redirect. A proxy carries only https
    requests to a remote host, so a key sent over plain http never passes one."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    proxies = {"https": proxy} if proxy and not _loopback(url) else {}
    return urllib.request.build_opener(_RefuseRedirects, urllib.request.ProxyHandler(proxies))


def _read(response, deadline=None):
    """The body, refused past MAX_RESPONSE and, given a monotonic deadline, past it."""
    chunks, size = [], 0
    while chunk := response.read(READ_CHUNK):
        size += len(chunk)
        if size > MAX_RESPONSE:
            raise ValueError(f"reply is larger than {MAX_RESPONSE // (1024 * 1024)} MiB")
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError
        chunks.append(chunk)
    return b"".join(chunks)


def _http_error_message(error):
    try:
        return _error_message(_read(error))
    except (OSError, ValueError):
        return error.reason


def _refusal(t, code, message):
    if code in (401, 403):
        return f"{t.label} didn't accept the key. Check it and paste it again."
    if code == 429:
        return f"{t.label} says Cairn asked too often. Wait a few minutes and try again."
    if code >= 500:
        return f"{t.label} is having trouble right now. Try again later."
    if code < 400:
        return (f"{t.label} answered from a different address. Check the address in "
                "Settings › AI provider.")
    return f"{t.label} turned down the request: {message or 'no reason given'}"


def _unreachable(t):
    if _loopback(t.base_url):
        return f"Couldn't reach {t.label}. Open it on this computer and try again."
    return f"Couldn't reach {t.label}. Check your internet connection and try again."


def _wait(retry_after, attempt):
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = 2 ** (attempt + 1)
    return max(0, min(seconds, MAX_WAIT))


def _post(t, prompt, timeout, retries):
    """(payload, err) for one prompt, retrying a 429 or 5xx, all within timeout seconds."""
    url, headers, body = _request(t, prompt)
    data = json.dumps(body).encode()
    opener = _opener(url)
    timeout = min(timeout, MAX_TIMEOUT)
    deadline = time.monotonic() + timeout
    timed_out = f"{t.label} didn't answer within {timeout:g} seconds. Try again."
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with opener.open(request, timeout=remaining) as response:
                return json.loads(_read(response, deadline)), None
        except urllib.error.HTTPError as e:
            with e:
                retryable = e.code == 429 or e.code >= 500
                seconds = _wait(e.headers.get("Retry-After"), attempt)
                if not (retryable and attempt < retries) \
                        or time.monotonic() + seconds >= deadline:
                    return None, _refusal(t, e.code, _http_error_message(e))
            if e.code != 429:
                time.sleep(seconds)
                continue
            events.emit("warn", text=f"[llm] {t.label} rate limit hit, waiting {seconds:g}s")
            time.sleep(seconds)
            # a retry keeps to the provider's rate like any other call
            _pace(t.provider)
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                return None, timed_out
            return None, _unreachable(t)
        except TimeoutError:
            return None, timed_out
        except ValueError:
            return None, (f"Cairn couldn't talk to {t.label}. Check your {t.label} setup in "
                          "Settings › AI provider and try again.")
        except OSError:
            return None, _unreachable(t)
    raise AssertionError("unreachable: the last attempt always returns")


def redact(text, key):
    """text without key, in any form an error message can quote it: as is, escaped
    by repr(), or with its line breaks dropped."""
    if not (text and key):
        return text
    forms = {key, repr(key)[1:-1], key.replace("\r", "").replace("\n", "")}
    for form in sorted(filter(None, forms), key=len, reverse=True):
        text = text.replace(form, "[key]")
    return text


def check_file():
    return paths.home() / "llm_check.json"


_check_lock = threading.Lock()


def _record_answer(provider):
    """Note in llm_check.json when provider last answered, which doctor reads."""
    path = check_file()
    with _check_lock:
        try:
            answered = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            answered = {}
        if not isinstance(answered, dict):
            answered = {}
        answered[provider] = datetime.datetime.now(datetime.UTC).isoformat()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_name(f".{path.name}.tmp")
            staging.write_text(json.dumps(answered), encoding="utf-8")
            staging.replace(path)
        except OSError as e:
            events.emit("warn", text=f"[llm] could not write {path}: {e}")


def last_answer(provider):
    """When provider last answered a call, as an aware datetime, or None."""
    try:
        answered = json.loads(check_file().read_text(encoding="utf-8"))
        at = datetime.datetime.fromisoformat(answered[provider])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return at if at.tzinfo is not None else None


def send(t, prompt, timeout=REQUEST_TIMEOUT, retries=RETRIES):
    """(text, err) for one prompt to Target t. Never raises; no string carries the key."""
    if not t.base_url:
        return None, f"{t.label} has no address yet. Add it in Settings › AI provider."
    if not t.model:
        return None, f"{t.label} has no model yet. Add one in Settings › AI provider."
    payload, err = _post(t, prompt, timeout, retries)
    if err:
        return None, redact(err, t.key)
    text, err = _answer(t, payload)
    if err is None:
        _record_answer(t.provider)
    return redact(text, t.key), redact(err, t.key)
