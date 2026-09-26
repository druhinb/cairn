"""Every model call goes through run, to the headless `claude` CLI by default or
through llm to the HTTP provider settings name.

Ranking and onboarding need Sonnet's judgement. Job-description distillation is
mechanical extraction, which Haiku does for a fraction of the cost.
"""
import json
import os
import shutil
import subprocess

from cairn.ai import llm
from cairn.core import paths, settings


def run(prompt, model=None, timeout=240, tier=None):
    """(text, err). Never raises; the caller decides how loud a failure is.

    For a provider other than claude-code, `tier` ("strong" or "cheap") picks the
    model; without it `model` stands for a tier. Through the claude CLI the cheap
    tier runs with thinking off.
    """
    tier = tier or tier_of(model)
    if llm.active() == "claude-code":
        return run_cli(prompt, model, timeout, thinking=tier != "cheap")
    return llm.complete(prompt, tier=tier, timeout=timeout)


def tier_of(model):
    """The llm tier a claude model name stands for, cheap for the summary model or
    a Haiku name and strong otherwise.

    When the summary model is also the ranking model this says strong, so a caller
    that summarises passes tier="cheap" to run.
    """
    cfg = settings.get()
    if not model or model == cfg.claude_model:
        return "strong"
    return "cheap" if model == cfg.description_model or "haiku" in model else "strong"


# A one-word prompt to haiku took 3.6-5.5 s and read 17,446 input tokens with
# `--tools ""` alone, and 1.7-2.3 s and about 400 tokens with every flag below.
LEAN_FLAGS = [
    # an empty list turns off the built-in tools; with them the model went reading
    # files off the disk and answered wrong, ~9x slower and ~3x dearer
    "--tools", "",
    # the user's MCP servers start for every call
    "--strict-mcp-config",
    # user and project settings bring hooks and plugins
    "--setting-sources", "",
    # skill descriptions ride along in every prompt
    "--disable-slash-commands",
    # a transcript per call would pile up under ~/.claude
    "--no-session-persistence",
    # stands in for Claude Code's own system prompt
    "--system-prompt", "Answer exactly as asked.",
]
# --bare would cut the same, but it skips the keychain, and logging in with a
# Claude plan keeps its credentials there.

# Haiku thought for 7,500-8,000 tokens over one posting summary and took 67-78 s;
# with thinking off it took 4.4 s and returned a valid block. Ranking with thinking
# off took as long, 42 s a batch, and moved fit scores 8 points on average against
# 3 between two runs with it on.
NO_THINKING = {"MAX_THINKING_TOKENS": "0"}


def run_cli(prompt, model=None, timeout=240, thinking=True):
    """(text, err) from the claude CLI, whatever provider settings name."""
    # The prompt goes on stdin: macOS caps a command line at 1 MiB, and a long
    # resume or ranking batch passed as an argument failed with OSError. The data
    # home as the working directory keeps a project's CLAUDE.md and memory out.
    cfg = settings.get()
    # which finds the claude.cmd an npm install leaves on Windows; a bare name there
    # only ever runs a .exe
    cmd = [shutil.which(cfg.claude_bin) or cfg.claude_bin, "-p", "--output-format", "json",
           *LEAN_FLAGS]
    model = model or cfg.claude_model
    if model:
        cmd += ["--model", model]
    home = paths.home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                             encoding="utf-8", timeout=timeout, cwd=home,
                             env=None if thinking else {**os.environ, **NO_THINKING})
    except FileNotFoundError:
        return None, "Couldn't find Claude Code on this computer. Install it, or pick another provider in Settings."
    except subprocess.TimeoutExpired:
        return None, "Claude Code didn't answer in time. Try again."
    except OSError as e:
        return None, f"Couldn't start Claude Code ({e}). Try again, or reinstall it."
    if out.returncode != 0:
        return None, f"Claude Code stopped with an error: {out.stderr.strip()[:300]}"
    return result(out.stdout)


_UNREADABLE = "Claude Code sent back an answer Cairn couldn't read. Try again."


def result(stdout):
    """(text, err) from the stdout of `claude --output-format json`.

    The CLI prints a JSON array of messages, and the answer is the one whose "type"
    is "result". Code that read the array as a dict once passed the raw 26KB
    transcript on as the answer, and every posting scored 0. An unrecognised shape
    is an error.
    """
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None, _UNREADABLE
    if isinstance(payload, dict):  # older CLI versions emitted a single object
        payload = [payload]
    if isinstance(payload, list):
        for msg in payload:
            if isinstance(msg, dict) and msg.get("type") == "result":
                text = msg.get("result")
                if msg.get("is_error"):
                    return None, f"Claude Code answered with an error: {text}"
                if isinstance(text, str) and text.strip():
                    return text, None
                return None, "Claude Code sent back an empty answer. Try again."
    return None, _UNREADABLE
