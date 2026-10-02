#!/usr/bin/env python3
"""claude-cache-timer — a Claude Code status line that counts down to prompt-cache expiry.

Shows a context-usage ring + % and a draining bar with an mm:ss countdown until the
main conversation's prompt cache goes cold. Optionally pings you (Telegram or any
command) once when the cache is about to expire, so you can come back and keep it warm.

Reads Claude Code's status line JSON on stdin. Stdlib only. See README.md.
"""
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.expanduser(
    os.environ.get("CCT_CONFIG", "~/.config/claude-cache-timer/config.json")
)
STATE_DIR = os.path.expanduser(
    os.environ.get("CCT_STATE_DIR", "~/.cache/claude-cache-timer")
)

DEFAULTS = {
    "warn_minutes": 10,        # amber + notification threshold
    "critical_minutes": 2,     # red threshold
    "bar_width": 12,
    "nerd_font": False,        # 8-step ring glyphs (needs a Nerd Font) vs 5-step Unicode
    "notify": True,
    "notify_command": None,    # e.g. "python3 ~/bin/send.py" — message on stdin
    "telegram_bot_token": None,
    "telegram_chat_id": None,
}

ENV_KEYS = {
    "warn_minutes": ("CCT_WARN_MINUTES", float),
    "critical_minutes": ("CCT_CRITICAL_MINUTES", float),
    "bar_width": ("CCT_BAR_WIDTH", int),
    "nerd_font": ("CCT_NERD_FONT", lambda v: v.lower() in ("1", "true", "yes")),
    "notify": ("CCT_NOTIFY", lambda v: v.lower() in ("1", "true", "yes")),
    "notify_command": ("CCT_NOTIFY_COMMAND", str),
    "telegram_bot_token": ("CCT_TELEGRAM_BOT_TOKEN", str),
    "telegram_chat_id": ("CCT_TELEGRAM_CHAT_ID", str),
}

RESET = "\033[0m"
DIM = "\033[2m"
GREEN = "\033[38;5;114m"
AMBER = "\033[38;5;214m"
RED = "\033[38;5;203m"
GREY = "\033[38;5;245m"

RING_UNICODE = ["○", "◔", "◑", "◕", "●"]
# nf-md-circle_slice_1 … circle_slice_8
RING_NERD = ["\U000f0a9e", "\U000f0a9f", "\U000f0aa0", "\U000f0aa1",
             "\U000f0aa2", "\U000f0aa3", "\U000f0aa4", "\U000f0aa5"]

TTL_SECONDS = {"5m": 300, "1h": 3600}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    for key, (env, cast) in ENV_KEYS.items():
        if os.environ.get(env):
            try:
                cfg[key] = cast(os.environ[env])
            except ValueError:
                pass
    return cfg


def ring(pct, nerd):
    glyphs = RING_NERD if nerd else RING_UNICODE
    if nerd:
        idx = min(len(glyphs) - 1, max(0, int(pct / 100 * len(glyphs))))
    else:
        idx = min(len(glyphs) - 1, max(0, round(pct / 100 * (len(glyphs) - 1))))
    return glyphs[idx]


def ctx_color(pct):
    if pct >= 80:
        return RED
    if pct >= 60:
        return AMBER
    return GREEN


def bar(fraction, width):
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    if fraction > 0 and filled == 0:
        filled = 1  # never look empty while there's still time left
    return "▰" * filled + "▱" * (width - filled)


def fmt_remaining(secs):
    secs = max(0, int(secs))
    m, s = divmod(secs, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def thresholds(cfg, total):
    """Warn/critical seconds. A short TTL (5m) can't use a 10-minute warning, so scale it."""
    warn, crit = cfg["warn_minutes"] * 60, cfg["critical_minutes"] * 60
    if total <= warn:
        warn, crit = total / 3, min(crit, total / 6)
    return warn, crit


def session_label(data):
    name = data.get("session_name")
    if name:
        return name
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or ""
    return os.path.basename(cwd.rstrip("/")) or "Claude Code"


def render(data, cfg, now):
    parts = []

    ctx = data.get("context_window") or {}
    pct = ctx.get("used_percentage")
    if pct is None:
        parts.append(f"{GREY}{ring(0, cfg['nerd_font'])} ctx --{RESET}")
    else:
        c = ctx_color(pct)
        parts.append(f"{c}{ring(pct, cfg['nerd_font'])} ctx {pct:.0f}%{RESET}")

    pc = data.get("prompt_cache")
    w = cfg["bar_width"]
    if not pc:
        parts.append(f"{GREY}cache {bar(0, w)} --:--{RESET}")
    elif not pc.get("caching_observed"):
        parts.append(f"{GREY}cache off{RESET}")
    else:
        ttl = pc.get("ttl") or "?"
        total = TTL_SECONDS.get(ttl)
        expires = pc.get("expires_at")
        remaining = (expires - now) if expires else 0
        if not pc.get("warm") or not expires or remaining <= 0 or not total:
            parts.append(f"{GREY}cache {bar(0, w)} cold{RESET}")
        else:
            warn, crit = thresholds(cfg, total)
            if remaining <= crit:
                c = RED
            elif remaining <= warn:
                c = AMBER
            else:
                c = GREEN
            parts.append(
                f"{c}cache {bar(remaining / total, w)} {fmt_remaining(remaining)}{RESET}"
                f" {DIM}{ttl}{RESET}"
            )
    return f"  {DIM}│{RESET}  ".join(parts)


# --- notification -----------------------------------------------------------

def _state_path(session_id):
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_") or "default"
    return os.path.join(STATE_DIR, f"{safe}.json")


def should_notify(data, cfg, now):
    """Return the expires_at to notify for, or None. One ping per cache window."""
    if not cfg["notify"]:
        return None
    pc = data.get("prompt_cache") or {}
    expires = pc.get("expires_at")
    total = TTL_SECONDS.get(pc.get("ttl"))
    warn = cfg["warn_minutes"] * 60
    if not (pc.get("warm") and expires and total):
        return None
    # A 5m cache is always inside a 10m warning window — pinging would fire every turn.
    if total <= warn:
        return None
    remaining = expires - now
    if not (0 < remaining <= warn):
        return None
    path = _state_path(data.get("session_id") or "default")
    try:
        with open(path) as f:
            if json.load(f).get("notified_for") == expires:
                return None
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"notified_for": expires}, f)
    except OSError:
        return None  # can't record it — don't risk pinging every second
    return expires


def build_message(data, now):
    pc = data.get("prompt_cache") or {}
    mins = max(0, round((pc.get("expires_at", now) - now) / 60))
    pct = (data.get("context_window") or {}).get("used_percentage")
    ctx = f" · ctx {pct:.0f}%" if pct is not None else ""
    return f"⏳ Claude cache cools in {mins} min — {session_label(data)}{ctx}"


def send_notification(msg, cfg):
    if cfg["notify_command"]:
        subprocess.run(shlex.split(os.path.expanduser(cfg["notify_command"])),
                       input=msg.encode(), timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    token, chat = cfg["telegram_bot_token"], cfg["telegram_chat_id"]
    if token and chat:
        body = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body)
        urllib.request.urlopen(req, timeout=15).read()


def notify_detached(msg):
    """Hand the send to a background process so the status line never blocks."""
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--send", msg],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# --- entry points -----------------------------------------------------------

def demo(cfg):
    now = time.time()
    samples = [
        ("fresh session", {}),
        ("warm, 47m left", {"context_window": {"used_percentage": 23},
            "prompt_cache": {"warm": True, "caching_observed": True, "ttl": "1h",
                             "expires_at": now + 47 * 60 + 12}}),
        ("warning, 8m left", {"context_window": {"used_percentage": 64},
            "prompt_cache": {"warm": True, "caching_observed": True, "ttl": "1h",
                             "expires_at": now + 8 * 60 + 3}}),
        ("critical, 1m left", {"context_window": {"used_percentage": 88},
            "prompt_cache": {"warm": True, "caching_observed": True, "ttl": "1h",
                             "expires_at": now + 71}}),
        ("cold", {"context_window": {"used_percentage": 41},
            "prompt_cache": {"warm": False, "caching_observed": True, "ttl": "1h",
                             "expires_at": None}}),
        ("5m TTL", {"context_window": {"used_percentage": 12},
            "prompt_cache": {"warm": True, "caching_observed": True, "ttl": "5m",
                             "expires_at": now + 200}}),
    ]
    for label, data in samples:
        print(f"{label:>18}  {render(data, cfg, now)}")


def main():
    cfg = load_config()
    args = sys.argv[1:]
    if args[:1] == ["--demo"]:
        demo(cfg)
        return
    if args[:1] == ["--send"]:
        send_notification(" ".join(args[1:]), cfg)
        return
    if args[:1] == ["--test-notify"]:
        send_notification("⏳ claude-cache-timer test ping — notifications work.", cfg)
        print("sent (check your notifier)")
        return

    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    now = time.time()
    print(render(data, cfg, now))
    try:
        if should_notify(data, cfg, now):
            notify_detached(build_message(data, now))
    except Exception:
        pass  # never break the status line over a notification


if __name__ == "__main__":
    main()
