"""Text helpers: 𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱 font, templates, date formatting, VPS parser."""
import html
import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


# ------------------------------------------------------------------ font
def sb(text):
    """Convert A-Z a-z 0-9 to Mathematical Bold (𝐀𝐚 style — same as OSINT)."""
    out = []
    for ch in str(text):
        if "A" <= ch <= "Z":
            out.append(chr(0x1D400 + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D41A + ord(ch) - 97))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7CE + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


_SPLIT = re.compile(r"(\{[a-z_0-9]+\}|<[^>]+>|&[a-z]+;|&#\d+;)")


def t(template, **kw):
    """Render an HTML template in the bold font.
    - static text is converted to 𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱 (tags / entities / {placeholders} are untouched)
    - int values are converted too; str values are inserted RAW (escape them first!)."""
    parts = _SPLIT.split(template)
    conv = "".join(p if i % 2 else sb(p) for i, p in enumerate(parts))
    vals = {
        k: sb(v) if isinstance(v, int) and not isinstance(v, bool) else v for k, v in kw.items()
    }
    return conv.format(**vals)


def esc(s):
    return html.escape(str(s), quote=False)


def mention(uid, name):
    """Clickable name – the @username is never shown."""
    return f'<a href="tg://user?id={uid}">{esc((name or "User").strip() or "User")}</a>'


# ------------------------------------------------------------ formatting
def fmt_dt(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %I:%M %p")


def fmt_date(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y")


def remaining(exp, now):
    left = exp - now
    if left <= 0:
        return "Expired"
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    return f"{h}h {m}m"


def bar(cur, total, width=10):
    total = max(total, 1)
    filled = min(width, int(width * cur / total))
    return "▰" * filled + "▱" * (width - filled)


_ORD = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth"]


def ordinal(i):
    """i is 0-based."""
    return sb(_ORD[i] if i < len(_ORD) else f"{i + 1}th")


# ---------------------------------------------------------- VPS parsing
_KEYMAP = {
    "IP": "host", "HOST": "host", "ADDRESS": "host", "SERVER": "host",
    "PORT": "port",
    "USER": "username", "USERNAME": "username", "LOGIN": "username",
    "PASS": "password", "PASSWORD": "password", "PWD": "password",
    "DAYS": "days", "DAY": "days", "VALIDITY": "days",
}
_LINE = re.compile(r"^\W*([A-Za-z ]+?)\s*[:=]\s*(.+)$")


def parse_vps(text):
    """Parse one or many VPS blocks (separated by a blank line or a new IP line).
    Returns (items, bad_block_count)."""
    items, state = [], {"cur": {}, "bad": 0}

    def flush():
        cur = state["cur"]
        if cur:
            if all(cur.get(k) for k in ("host", "port", "username", "password")):
                digits = re.sub(r"\D", "", cur.get("days", "") or "")
                cur["days"] = int(digits) if digits else None
                items.append(cur)
            else:
                state["bad"] += 1
        state["cur"] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            flush()
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key = _KEYMAP.get(m.group(1).strip().upper().replace(" ", ""))
        if not key:
            continue
        if key == "host" and "host" in state["cur"]:
            flush()
        state["cur"][key] = m.group(2).strip()
    flush()
    return items, state["bad"]
