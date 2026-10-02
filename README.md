# claude-cache-timer

A [Claude Code](https://code.claude.com) status line that shows **how long until your prompt cache goes cold**, alongside **how much of the context window you've used**, and can ping you on Telegram before the cache expires.

```
◔ ctx 23%  │  cache ▰▰▰▰▰▰▰▰▰▱▱▱ 47:12 1h
```

## Why

Claude Code caches your conversation prefix. While the cache is warm, your next message is cheap and fast. Once it expires, the next message re-processes the whole context from scratch. On a Claude subscription, the main conversation's cache lasts **1 hour** from your last request; on API keys it's **5 minutes** ([docs](https://code.claude.com/docs/en/prompt-caching)).

If you step away from your desk, you have no way to tell how long you have left. This shows you.

## What it shows

| Segment | Meaning |
|---|---|
| `◔ ctx 23%` | Context-window usage: a ring plus a percentage. Green below 60%, amber below 80%, red above. |
| `cache ▰▰▰▱▱ 47:12` | The cache TTL draining down, with an mm:ss countdown. Green, then amber (≤ 10 min), then red (≤ 2 min). |
| `1h` / `5m` | The TTL of the current cached prefix. |
| `cold` | The cache has expired, so your next message is a full re-read. |

For a 5-minute cache, the warn and critical thresholds scale down automatically to ⅓ and ⅙ of the TTL.

Preview all states without Claude Code:

```sh
python3 cache_timer.py --demo
```

## Install

Requires Python 3.8+ (stdlib only) and Claude Code v2.1.251+ (for the `prompt_cache` status line field).

```sh
git clone https://github.com/<you>/claude-cache-timer ~/claude-cache-timer
```

Add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/claude-cache-timer/cache_timer.py",
    "padding": 0,
    "refreshInterval": 1
  }
}
```

`refreshInterval: 1` is what makes the countdown tick while you're idle. Without it, the status line only refreshes when something happens in the session.

## Expiry notifications

The timer sends **one** ping per cache window when the time remaining crosses the warn threshold (10 min by default):

> ⏳ Claude cache cools in 10 min — my-project · ctx 42%

Pings only fire for TTLs longer than the warn threshold, so a 5-minute cache never pings. They're sent from a detached process, so the status line never blocks. Notifications need the Claude Code session to stay open; the status line is what checks the time.

**Telegram:** create a bot with [@BotFather](https://t.me/BotFather), get your chat id, then put both in the config file:

```sh
mkdir -p ~/.config/claude-cache-timer
cp config.example.json ~/.config/claude-cache-timer/config.json   # edit token + chat id
python3 cache_timer.py --test-notify
```

**Anything else** (ntfy, Slack, `osascript`, …): set `notify_command`. The message arrives on stdin:

```json
{ "notify_command": "curl -s -d @- ntfy.sh/my-topic" }
```

## Configuration

Config file: `~/.config/claude-cache-timer/config.json` (override the path with `CCT_CONFIG`). Every key can also be set as an environment variable, and environment variables take precedence.

| Key | Env | Default |
|---|---|---|
| `warn_minutes` | `CCT_WARN_MINUTES` | `10` |
| `critical_minutes` | `CCT_CRITICAL_MINUTES` | `2` |
| `bar_width` | `CCT_BAR_WIDTH` | `12` |
| `nerd_font` | `CCT_NERD_FONT` | `false`: 5-step `○◔◑◕●`; `true`: 8-step Nerd Font circle slices |
| `notify` | `CCT_NOTIFY` | `true` |
| `notify_command` | `CCT_NOTIFY_COMMAND` | unset |
| `telegram_bot_token` | `CCT_TELEGRAM_BOT_TOKEN` | unset |
| `telegram_chat_id` | `CCT_TELEGRAM_CHAT_ID` | unset |

Keep your bot token out of version control: the config file lives in `~/.config`, not in this repo.

## How it works

Claude Code pipes session JSON to the status line on every refresh. The script reads `prompt_cache.expires_at` (epoch seconds), `prompt_cache.ttl` and `context_window.used_percentage`. Notification state (which `expires_at` has already been pinged) is stored per session in `~/.cache/claude-cache-timer/`.

## License

MIT
