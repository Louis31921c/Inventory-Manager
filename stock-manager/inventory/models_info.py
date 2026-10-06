"""Which model backends are set up on this machine, and switching between them."""

import os
import shutil
import subprocess

from .config import set_setting

PROVIDERS = ("claude", "gemini", "openai")
NAMES = {"claude": "Claude", "gemini": "Gemini", "openai": "Dots"}


def _claude():
    binary = shutil.which("claude") or shutil.which("claude-code")
    if binary:
        return "ready", f"found at {binary}"
    return "missing", "install the Claude Code CLI, then run claude and /login"


def _gemini():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    try:
        __import__("google.genai")
        installed = True
    except ImportError:
        installed = False
    if key and installed:
        return "ready", "API key set, SDK installed"
    if key:
        return "configured", "API key set, run pip install google-genai"
    if installed:
        return "configured", "SDK installed, GEMINI_API_KEY missing"
    return "missing", "needs GEMINI_API_KEY and the gemini extra"


def _openai():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    detail = "not built into this version yet"
    if key:
        detail += " (the key is there, the backend is not)"
    return "unsupported", detail


CHECKS = {"claude": _claude, "gemini": _gemini, "openai": _openai}


def status(current):
    """One row per provider: its state, a line of detail, and whether it is the one in use."""
    rows = []
    for provider in PROVIDERS:
        state, detail = CHECKS[provider]()
        rows.append({"id": provider, "name": NAMES[provider], "state": state, "detail": detail,
                     "current": provider == current})
    return rows


def probe(provider):
    """A real but cheap test of one provider. Never raises."""
    if provider == "claude":
        binary = shutil.which("claude") or shutil.which("claude-code")
        if not binary:
            return False, "the Claude Code CLI is not on PATH"
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                    timeout=20)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        if result.returncode != 0:
            return False, (result.stderr or result.stdout).strip()[:200] or "the CLI returned an error"
        return True, (result.stdout or result.stderr).strip()[:120] or "ready"

    if provider == "gemini":
        state, detail = _gemini()
        return state == "ready", detail

    state, detail = CHECKS.get(provider, _openai)()
    return False, detail


def use(provider):
    """Switch the backend and remember it in the settings file."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown model: {provider}")
    state, detail = CHECKS[provider]()
    if state in ("missing", "unsupported"):
        raise ValueError(detail)
    set_setting("LLM_BACKEND", provider)
    return provider
