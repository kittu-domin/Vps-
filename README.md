# 🤖 Refer & Earn VPS Bot

A Telegram bot that rewards users with VPS access for referring friends.  
Built with **aiogram 3.x** + **Turso (libSQL)** database.

---

## ✨ Features

### 👤 Users
- **Referral System** — Share your unique link, earn points for every referral
- **Points System** — 1 referral = 1 point
- **Non-Root VPS** — Redeem 5 points → bot auto-creates a non-root user on the host VPS via SSH
- **Referral Tiers** — 10 / 25 / 50 referrals → owner manually adds VPS from stock
- **Redeem Codes** — Enter a code to claim bonus points
- **Profile** — View your stats, points, referral count
- **Leaderboard** — Top referrers ranking

### 🛡 Admin
- Add/remove VPS stock for tier rewards
- Broadcast messages to all users
  - **Without Button** — forwards message, hides sender
  - **With Button** — same message + up to 3 custom inline buttons
- Create redeem codes (set points + max claims)
- Force join channel management
- Referral tier management
- Full stats dashboard

---

## ⚙️ Setup

### 1. Clone the repo
```bash
git clone https://github.com/yourusername/refer-vps-bot.git
cd refer-vps-bot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure `.env`
Copy the example and fill in your values:
```bash
cp .env.example .env
nano .env
```

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Your bot token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | Your Telegram user ID (get from [@userinfobot](https://t.me/userinfobot)) |
| `OWNER_USERNAME` | Your Telegram username (without @) |
| `TURSO_URL` | Your Turso database URL |
| `TURSO_AUTH_TOKEN` | Your Turso auth token |
| `HOST_VPS_PORT` | SSH port of your VPS (default: 22) |
| `HOST_VPS_ROOT_USER` | Root username of your VPS (default: root) |
| `HOST_VPS_ROOT_PASS` | Root password of your VPS |

> **Note:** `HOST_VPS_IP` is **auto-detected** — the bot detects the IP of the server it's running on automatically. No need to set it manually.

### 4. Run the bot
```bash
python bot.py
```

---

## 🗄 Database

This bot uses **[Turso](https://turso.tech)** (libSQL over HTTP) as the database.  
Create a free database at [turso.tech](https://turso.tech) and paste the URL + token in `.env`.

Tables created automatically on first run:
- `rb_users` — user data, points, referral info
- `rb_vps` — VPS stock (owner-added)
- `rb_redeem_codes` — redeem codes
- `rb_nonroot_vps` — auto-created non-root VPS records
- `rb_settings` — bot configuration

---

## 🖥 Non-Root VPS (Auto SSH)

When a user has **5 points**, they can claim a non-root VPS:

1. User taps **"Get Non-Root VPS"**
2. Bot asks for desired **username** and **password**
3. Bot SSHes into the host VPS (where it's running) and runs:
   ```bash
   useradd -m -s /bin/bash <username>
   echo '<username>:<password>' | chpasswd
   usermod -aG sudo <username>
   ```
4. User receives SSH credentials instantly

> The bot uses the VPS it's hosted on — make sure `HOST_VPS_ROOT_PASS` is set in `.env`.

---

## 🎟 Redeem Codes

**Admin:** Open Admin Panel → Redeem Codes → Create  
Enter: code name (or auto-generate) → points to award → max number of users who can claim

**User:** Tap **"🎟 Redeem Code"** → enter code → points credited instantly

---

## 📢 Broadcast

Admin Panel → Broadcast → Send your message → Choose:
- **Without Button** — copies message, hides sender identity
- **With Button** — adds up to 3 inline buttons  
  Format: `Button Name | https://link.com` (one per line)

---

## 🔒 Security

- `.env` is in `.gitignore` — **never commit it to GitHub**
- Use `.env.example` as a template for others
- Keep `HOST_VPS_ROOT_PASS` private

---

## 📦 Requirements

```
aiogram>=3.22
paramiko>=3.0
```

---

## 📄 License

MIT License — use freely, modify as needed.
