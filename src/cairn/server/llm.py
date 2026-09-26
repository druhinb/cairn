"""Routes for the AI provider: which one runs, its models, its key, and a test call.

No response carries a key: the provider list and the current choice report only
whether one is stored, and a test call's reply and error are redacted.
"""
import shutil
import time

from fastapi import APIRouter, Body, HTTPException

from cairn import claude, llm, secrets, settings

router = APIRouter()

TEST_TIMEOUT = 20
_SETTING_FIELDS = {"model": "llm_model", "model_cheap": "llm_model_cheap",
                   "base_url": "llm_base_url"}


def configured(provider):
    spec = llm.PROVIDERS[provider]
    if spec["kind"] == "cli":
        return shutil.which(settings.get().claude_bin) is not None
    if spec["needs_key"]:
        return secrets.has_key(provider)
    return bool(llm.target(provider=provider).base_url)


@router.get("/api/llm/providers")
def list_providers():
    return {"active": llm.active(),
            "providers": [{"id": pid, **spec, "configured": configured(pid)}
                          for pid, spec in llm.PROVIDERS.items()]}


def _current():
    cfg = settings.get()
    provider = llm.active()
    return {"provider": provider, "model": getattr(cfg, "llm_model", ""),
            "model_cheap": getattr(cfg, "llm_model_cheap", ""),
            "base_url": getattr(cfg, "llm_base_url", ""),
            "has_key": secrets.has_key(provider)}


@router.get("/api/llm")
def get_llm():
    return _current()


def _provider(body, required):
    provider = body.get("provider")
    if provider is None and not required:
        return llm.active()
    if provider not in llm.PROVIDERS:
        raise HTTPException(400, f"provider: should be one of {', '.join(llm.PROVIDERS)}, "
                                 f"got {provider!r}")
    return provider


def _text(body, name):
    value = body.get(name) or ""
    if not isinstance(value, str):
        raise HTTPException(400, f"{name}: should be a string")
    return value.strip()


def _base_url(body, provider):
    url = _text(body, "base_url")
    if url and provider not in llm.EDITABLE_BASE_URL:
        raise HTTPException(400, "base_url: only ollama and custom take one")
    if url and not llm.valid_base_url(url):
        raise HTTPException(400, "the address should start with http:// or https://")
    return url


def _check_keys(body, allowed):
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(400, f"unknown field(s): {', '.join(unknown)}")


@router.put("/api/llm")
def put_llm(body: dict = Body(...)):
    """Omitted model and base_url fields fall back to the provider's defaults. An
    empty or omitted key keeps the stored one; a null key deletes it."""
    _check_keys(body, {"provider", "model", "model_cheap", "base_url", "key"})
    provider = _provider(body, required=True)
    values = {"model": _text(body, "model"), "model_cheap": _text(body, "model_cheap"),
              "base_url": _base_url(body, provider)}
    if provider == "custom" and not values["base_url"]:
        raise HTTPException(400, "Custom needs an address")
    key = body.get("key", "")
    if key is not None and not isinstance(key, str):
        raise HTTPException(400, "key: should be a string or null")
    try:
        new = settings.from_dict({"llm_provider": provider,
                                  **{_SETTING_FIELDS[k]: v for k, v in values.items()}},
                                 base=settings.base(), source="request")
    except settings.SettingsError as e:
        raise HTTPException(400, str(e)) from None
    try:
        if key is None:
            secrets.delete_key(provider)
        elif key.strip():
            secrets.set_key(provider, key)
    except secrets.SecretsError as e:
        raise HTTPException(400, str(e)) from None
    settings.save(new)
    settings.use(new)
    return _current()


@router.post("/api/llm/test")
def test_llm(body: dict = Body(default={})):
    """One short call with the given values, none of them saved.

    The stored key goes only to the saved base URL: a test against another URL sends
    the key in the body, or none.
    """
    _check_keys(body, {"provider", "key", "model", "base_url"})
    provider = _provider(body, required=False)
    key = _text(body, "key") or None
    model = _text(body, "model") or None
    base_url = _base_url(body, provider).rstrip("/") or None
    if key is None and base_url and base_url != llm.target(provider=provider).base_url:
        key = ""
    target = llm.target(provider=provider, model=model, base_url=base_url, key=key)
    started = time.monotonic()
    if llm.PROVIDERS[provider]["kind"] == "cli":
        reply, error = claude.run_cli(llm.TEST_PROMPT, model, TEST_TIMEOUT)
    else:
        reply, error = llm.send(target, llm.TEST_PROMPT, TEST_TIMEOUT, retries=0)
    return {"ok": error is None, "latency_ms": round((time.monotonic() - started) * 1000),
            "reply": llm.redact((reply or "").strip()[:200], target.key),
            "error": llm.redact(error, target.key)}


@router.delete("/api/llm/key")
def delete_llm_key(provider: str | None = None):
    provider = _provider({"provider": provider}, required=False)
    secrets.delete_key(provider)
    return {"provider": provider, "has_key": secrets.has_key(provider)}
