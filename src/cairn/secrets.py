"""API keys for model providers and USAJOBS, in secrets.toml beside config.toml.

The file is readable by its owner only and holds one [keys] table keyed by provider
id, or "usajobs". Keys never go in config.toml, so the settings the app returns
never carry one. An environment variable, CAIRN_<NAME>_KEY or
ANTHROPIC_API_KEY for anthropic, wins over the file.
"""
import json
import os
import tomllib

from cairn import events, paths

USAJOBS = "usajobs"
_warned = set()  # (name, where) of each unusable key already reported


class SecretsError(Exception):
    """A secrets.toml that cannot be read, or a key that cannot be stored."""


def secrets_file():
    return paths.home() / "secrets.toml"


def names():
    """Every name a key can be stored under: each provider id, and usajobs."""
    from cairn import llm  # llm reads keys from here, so the import stays local
    return (*llm.PROVIDERS, USAJOBS)


def _check_name(name):
    if name not in names():
        raise SecretsError(f"no key named {name!r}; one of: {', '.join(names())}")


def _env_names(name):
    env = [f"CAIRN_{name.upper().replace('-', '_')}_KEY"]
    return env + ["ANTHROPIC_API_KEY"] if name == "anthropic" else env


def _problem(key):
    """Why key cannot be sent in an HTTP header, or None when it can."""
    if not key:
        return "the key should be a non-empty string"
    if not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
        return "the key should be printable ASCII with no spaces"
    return None


def _read():
    path = secrets_file()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SecretsError(f"{path} is not valid TOML: {e}") from e
    keys = raw.get("keys", {})
    if not isinstance(keys, dict) or not all(isinstance(v, str) for v in keys.values()):
        raise SecretsError(f"{path}: [keys] should map provider ids to strings")
    return keys


def _write(keys):
    path = secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# API keys for Cairn. Keep this file private.", "", "[keys]"]
    lines += [f"{json.dumps(p)} = {json.dumps(k)}" for p, k in sorted(keys.items())]
    staging = path.with_name(path.name + ".tmp")
    # O_CREAT keeps the mode of a tmp file an interrupted write left behind, so it is
    # removed and the new one is created 0600
    staging.unlink(missing_ok=True)
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(staging, path)


def get_key(name):
    """The key stored under name, or None. A key that could not go in an HTTP header
    is reported as a warn event and treated as absent."""
    _check_name(name)
    for env in _env_names(name):
        if os.environ.get(env):
            key, where = os.environ[env].strip(), f"${env}"
            break
    else:
        key, where = _read().get(name), str(secrets_file())
    if not key:
        return None
    problem = _problem(key)
    if problem:
        if (name, where) not in _warned:
            _warned.add((name, where))
            events.emit("warn", text=f"ignoring the {name} key in {where}: {problem}")
        return None
    return key


def has_key(name):
    return get_key(name) is not None


def set_key(name, key):
    _check_name(name)
    key = key.strip() if isinstance(key, str) else key
    problem = _problem(key) if isinstance(key, str) else "the key should be a non-empty string"
    if problem:
        raise SecretsError(problem)
    keys = _read()
    keys[name] = key
    _write(keys)


def delete_key(name):
    """Remove a stored key. A key set in the environment stays in effect."""
    _check_name(name)
    keys = _read()
    if keys.pop(name, None) is not None:
        _write(keys)
