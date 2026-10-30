"""SQLite storage + all business logic (rb_referrals, tiers, VPS stock/claims, points, redeem codes)."""
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

DEFAULT_TIERS = [10, 25, 50]
DEFAULT_VPS_DAYS = 30
POINTS_PER_REFERRAL = 1      # 1 refer = 1 point
POINTS_PER_NONROOT_VPS = 5   # 5 points = 1 non-root VPS

SCHEMA = """
CREATE TABLE IF NOT EXISTS rb_users(
    user_id          INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL DEFAULT '',
    referrer_id      INTEGER,
    joined_at        INTEGER NOT NULL,
    referral_counted INTEGER NOT NULL DEFAULT 0,
    referral_count   INTEGER NOT NULL DEFAULT 0,
    vps_claimed      INTEGER NOT NULL DEFAULT 0,
    blocked          INTEGER NOT NULL DEFAULT 0,
    points           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rb_referrals(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rb_referrals_referrer ON rb_referrals(referrer_id);
CREATE TABLE IF NOT EXISTS rb_vps(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host        TEXT NOT NULL,
    port        TEXT NOT NULL,
    username    TEXT NOT NULL,
    password    TEXT NOT NULL,
    days        INTEGER,
    added_at    INTEGER NOT NULL,
    assigned_to INTEGER,
    assigned_at INTEGER,
    expires_at  INTEGER,
    warned      INTEGER NOT NULL DEFAULT 0,
    vps_type    TEXT NOT NULL DEFAULT 'referral'
);
CREATE INDEX IF NOT EXISTS idx_rb_vps_assigned ON rb_vps(assigned_to);
CREATE TABLE IF NOT EXISTS rb_fj_chats(
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    username   TEXT,
    link       TEXT NOT NULL,
    is_private INTEGER NOT NULL DEFAULT 0,
    added_at   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rb_join_requests(
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS rb_settings(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rb_redeem_codes(
    code        TEXT PRIMARY KEY,
    points      INTEGER NOT NULL,
    max_claims  INTEGER NOT NULL DEFAULT 1,
    claims_used INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    created_by  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rb_redeem_claims(
    code    TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    PRIMARY KEY(code, user_id)
);
CREATE TABLE IF NOT EXISTS rb_nonroot_vps(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    host        TEXT NOT NULL,
    port        TEXT NOT NULL,
    username    TEXT NOT NULL,
    password    TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rb_nonroot_user ON rb_nonroot_vps(user_id);
"""


_MISSING = object()


class DB:
    """Uses Turso (libSQL over HTTP) when url+token are given, else a local SQLite file."""

    def __init__(self, path="referbot.db", url=None, token=None):
        self.lock = threading.RLock()
        self._rb_settings = {}  # cache, invalidated on write
        self._fj = None
        if url:
            from turso import TursoConn

            self.conn = TursoConn(url, token or "")
        else:
            self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
        with self.lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
        # Migrate existing tables (add missing columns)
        self._migrate()

    def _migrate(self):
        """Add new columns to existing tables if they don't exist."""
        try:
            self._run("ALTER TABLE rb_users ADD COLUMN points INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            self._run("ALTER TABLE rb_vps ADD COLUMN vps_type TEXT NOT NULL DEFAULT 'referral'")
        except Exception:
            pass

    # ------------------------------------------------------------ helpers
    def _all(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def _run(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    @contextmanager
    def _tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # ----------------------------------------------------------- rb_settings
    def get_setting(self, key, default=None):
        val = self._rb_settings.get(key, _MISSING)
        if val is _MISSING:
            row = self._one("SELECT value FROM rb_settings WHERE key=?", (key,))
            val = row["value"] if row else None
            self._rb_settings[key] = val
        return default if val is None else val

    def set_setting(self, key, value):
        self._run(
            "INSERT INTO rb_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self._rb_settings.pop(key, None)

    def del_setting(self, key):
        self._run("DELETE FROM rb_settings WHERE key=?", (key,))
        self._rb_settings.pop(key, None)

    def get_tiers(self):
        raw = self.get_setting("tiers")
        return json.loads(raw) if raw else list(DEFAULT_TIERS)

    def set_tiers(self, tiers):
        tiers = [int(x) for x in tiers]
        if not tiers:
            raise ValueError("At least one tier is required")
        if tiers[0] < 1 or any(b <= a for a, b in zip(tiers, tiers[1:])):
            raise ValueError("Targets must be positive and strictly increasing")
        self.set_setting("tiers", json.dumps(tiers))

    def get_vps_days(self):
        return int(self.get_setting("vps_days", DEFAULT_VPS_DAYS))

    def set_vps_days(self, days):
        days = int(days)
        if not 1 <= days <= 3650:
            raise ValueError("Days must be between 1 and 3650")
        self.set_setting("vps_days", days)

    # -------------------------------------------------------------- users
    def upsert_user(self, user_id, name, referrer_id=None):
        """Create the user if new. Returns True when the user is new."""
        with self.lock:
            cur = self._one("SELECT name, blocked FROM rb_users WHERE user_id=?", (user_id,))
            if cur:
                if cur["name"] != name or cur["blocked"]:
                    self._run("UPDATE rb_users SET name=?, blocked=0 WHERE user_id=?", (name, user_id))
                return False
            ref = None
            if referrer_id and referrer_id != user_id and self._one(
                "SELECT 1 FROM rb_users WHERE user_id=?", (referrer_id,)
            ):
                ref = referrer_id
            self._run(
                "INSERT INTO rb_users(user_id,name,referrer_id,joined_at,points) VALUES(?,?,?,?,0)",
                (user_id, name, ref, int(time.time())),
            )
            return True

    def get_user(self, user_id):
        return self._one("SELECT * FROM rb_users WHERE user_id=?", (user_id,))

    def all_user_ids(self):
        return [r["user_id"] for r in self._all("SELECT user_id FROM rb_users WHERE blocked=0")]

    def mark_blocked(self, user_id):
        self._run("UPDATE rb_users SET blocked=1 WHERE user_id=?", (user_id,))

    def get_user_points(self, user_id):
        row = self._one("SELECT points FROM rb_users WHERE user_id=?", (user_id,))
        return row["points"] if row else 0

    def add_points(self, user_id, pts):
        self._run("UPDATE rb_users SET points=points+? WHERE user_id=?", (pts, user_id))

    def deduct_points(self, user_id, pts):
        """Deduct points. Returns False if not enough points."""
        with self._tx():
            row = self._one("SELECT points FROM rb_users WHERE user_id=?", (user_id,))
            if not row or row["points"] < pts:
                return False
            self._run("UPDATE rb_users SET points=points-? WHERE user_id=?", (pts, user_id))
            return True

    # ---------------------------------------------------------- referrals
    def credit_referral(self, user_id):
        """Count the referral for a newly verified user. Idempotent.
        Returns {'referrer_id', 'count'} the first time, else None.
        Also gives 1 point to the referrer per referral."""
        with self._tx():
            u = self._one("SELECT * FROM rb_users WHERE user_id=?", (user_id,))
            if not u or u["referral_counted"] or not u["referrer_id"]:
                return None
            ref = u["referrer_id"]
            self._run("UPDATE rb_users SET referral_counted=1 WHERE user_id=?", (user_id,))
            self._run(
                "INSERT OR IGNORE INTO rb_referrals(referrer_id,referred_id,created_at) VALUES(?,?,?)",
                (ref, user_id, int(time.time())),
            )
            self._run(
                "UPDATE rb_users SET referral_count=referral_count+1, points=points+? WHERE user_id=?",
                (POINTS_PER_REFERRAL, ref),
            )
            count = self._one("SELECT referral_count FROM rb_users WHERE user_id=?", (ref,))[0]
            return {"referrer_id": ref, "count": count}

    def referrals_page(self, referrer_id, limit, offset):
        return self._all(
            "SELECT r.referred_id AS user_id, r.created_at, u.name "
            "FROM rb_referrals r LEFT JOIN rb_users u ON u.user_id=r.referred_id "
            "WHERE r.referrer_id=? ORDER BY r.id DESC LIMIT ? OFFSET ?",
            (referrer_id, limit, offset),
        )

    def leaderboard(self, limit=10):
        return self._all(
            "SELECT user_id, name, referral_count FROM rb_users WHERE referral_count>0 "
            "ORDER BY referral_count DESC, joined_at ASC LIMIT ?",
            (limit,),
        )

    def rank(self, user_id):
        u = self.get_user(user_id)
        if not u or u["referral_count"] <= 0:
            return None
        better = self._one(
            "SELECT COUNT(*) FROM rb_users WHERE referral_count>?", (u["referral_count"],)
        )[0]
        return better + 1

    # ------------------------------------------------------- reward logic
    def claim_status(self, user_id):
        """Sequential tiers only: after claiming tier N, next unlock is tiers[N].
        Extra referrals / lower-tier edits do not grant another VPS early."""
        u = self.get_user(user_id)
        tiers = self.get_tiers()
        count = u["referral_count"] if u else 0
        claimed = u["vps_claimed"] if u else 0
        nxt = tiers[claimed] if claimed < len(tiers) else None
        pending = 1 if (nxt is not None and count >= nxt) else 0
        entitled = 0
        for target in tiers:
            if count >= target:
                entitled += 1
            else:
                break
        return {
            "count": count,
            "claimed": claimed,
            "entitled": entitled,
            "tiers": tiers,
            "next": nxt,
            "pending": pending,
        }

    def claim_vps(self, user_id):
        """Sequential claim only: need referral_count >= tiers[vps_claimed].
        status: ok | no_stock | not_eligible | all_claimed | no_user"""
        with self._tx():
            u = self._one("SELECT * FROM rb_users WHERE user_id=?", (user_id,))
            if not u:
                return "no_user", None
            tiers = self.get_tiers()
            claimed = u["vps_claimed"]
            if claimed >= len(tiers):
                return "all_claimed", None
            next_target = tiers[claimed]
            if u["referral_count"] < next_target:
                return "not_eligible", {"count": u["referral_count"], "next": next_target}
            # Only give 'referral' type VPS for tier claims
            row = self._one(
                "SELECT * FROM rb_vps WHERE assigned_to IS NULL AND vps_type='referral' ORDER BY id LIMIT 1"
            )
            if not row:
                # Fallback: any unassigned VPS
                row = self._one(
                    "SELECT * FROM rb_vps WHERE assigned_to IS NULL ORDER BY id LIMIT 1"
                )
            if not row:
                return "no_stock", None
            now = int(time.time())
            days = row["days"] or self.get_vps_days()
            self._run(
                "UPDATE rb_vps SET assigned_to=?, assigned_at=?, expires_at=? WHERE id=?",
                (user_id, now, now + days * 86400, row["id"]),
            )
            self._run(
                "UPDATE rb_users SET vps_claimed=vps_claimed+1 WHERE user_id=?",
                (user_id,),
            )
            return "ok", self._one("SELECT * FROM rb_vps WHERE id=?", (row["id"],))

    # ---------------------------------------------------------------- vps
    def add_vps(self, items, vps_type="referral"):
        now = int(time.time())
        with self._tx():
            for it in items:
                self._run(
                    "INSERT INTO rb_vps(host,port,username,password,days,added_at,vps_type) VALUES(?,?,?,?,?,?,?)",
                    (it["host"], it["port"], it["username"], it["password"], it.get("days"), now, vps_type),
                )
        return len(items)

    def stock_vps(self, limit=30):
        """Unassigned VPS available for giveaway."""
        return self._all(
            "SELECT * FROM rb_vps WHERE assigned_to IS NULL ORDER BY id ASC LIMIT ?",
            (limit,),
        )

    def get_vps(self, vps_id):
        return self._one("SELECT * FROM rb_vps WHERE id=?", (vps_id,))

    def delete_vps(self, vps_id):
        """Delete only unassigned stock VPS so it can never be given out."""
        with self._tx():
            row = self._one(
                "SELECT id FROM rb_vps WHERE id=? AND assigned_to IS NULL", (vps_id,)
            )
            if not row:
                return False
            self._run("DELETE FROM rb_vps WHERE id=?", (vps_id,))
            return True

    def user_vps(self, user_id):
        return self._all(
            "SELECT * FROM rb_vps WHERE assigned_to=? ORDER BY assigned_at ASC", (user_id,)
        )

    def vps_to_warn(self, now, window=86400):
        return self._all(
            "SELECT * FROM rb_vps WHERE assigned_to IS NOT NULL AND warned=0 "
            "AND expires_at>? AND expires_at<=?",
            (now, now + window),
        )

    def mark_warned(self, vps_id):
        self._run("UPDATE rb_vps SET warned=1 WHERE id=?", (vps_id,))

    # -------------------------------------------------------------- stats
    def stats(self):
        now = int(time.time())
        day_start = now - (now % 86400)
        r = self._one(
            "SELECT "
            "(SELECT COUNT(*) FROM rb_users),"
            "(SELECT COUNT(*) FROM rb_users WHERE joined_at>=?),"
            "(SELECT COUNT(*) FROM rb_users WHERE blocked=1),"
            "(SELECT COUNT(*) FROM rb_referrals),"
            "(SELECT COUNT(*) FROM rb_vps WHERE assigned_to IS NULL),"
            "(SELECT COUNT(*) FROM rb_vps),"
            "(SELECT COUNT(*) FROM rb_vps WHERE assigned_to IS NOT NULL),"
            "(SELECT COUNT(DISTINCT assigned_to) FROM rb_vps WHERE assigned_to IS NOT NULL),"
            "(SELECT COUNT(*) FROM rb_vps WHERE assigned_to IS NOT NULL AND expires_at<=?)",
            (day_start, now),
        )
        keys = ("users", "today", "blocked", "referrals", "stock", "total_vps", "given", "winners", "expired")
        return {k: r[i] for i, k in enumerate(keys)}

    # --------------------------------------------------------- force join
    def add_fj(self, chat_id, title, kind, username, link, is_private):
        self._run(
            "INSERT INTO rb_fj_chats(chat_id,title,kind,username,link,is_private,added_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, kind=excluded.kind, "
            "username=excluded.username, link=excluded.link, is_private=excluded.is_private",
            (chat_id, title, kind, username, link, int(is_private), time.time_ns() // 1000),
        )
        self._fj = None

    def remove_fj(self, chat_id):
        with self._tx():
            self._run("DELETE FROM rb_fj_chats WHERE chat_id=?", (chat_id,))
            self._run("DELETE FROM rb_join_requests WHERE chat_id=?", (chat_id,))
        self._fj = None

    def list_fj(self):
        if self._fj is None:
            self._fj = self._all("SELECT * FROM rb_fj_chats ORDER BY added_at, chat_id")
        return self._fj

    def get_fj(self, chat_id):
        return next((c for c in self.list_fj() if c["chat_id"] == chat_id), None)

    def add_join_request(self, chat_id, user_id):
        self._run(
            "INSERT OR REPLACE INTO rb_join_requests(chat_id,user_id,created_at) VALUES(?,?,?)",
            (chat_id, user_id, int(time.time())),
        )

    def has_join_request(self, chat_id, user_id):
        return bool(
            self._one(
                "SELECT 1 FROM rb_join_requests WHERE chat_id=? AND user_id=?", (chat_id, user_id)
            )
        )

    # ------------------------------------------------------- redeem codes
    def create_redeem_code(self, points, max_claims, created_by, code=None):
        """Create a redeem code. Auto-generates code if not provided."""
        if not code:
            code = secrets.token_hex(4).upper()  # e.g. "A1B2C3D4"
        now = int(time.time())
        with self._tx():
            # Check if code exists
            existing = self._one("SELECT code FROM rb_redeem_codes WHERE code=?", (code,))
            if existing:
                return None, "Code already exists"
            self._run(
                "INSERT INTO rb_redeem_codes(code,points,max_claims,claims_used,created_at,created_by) "
                "VALUES(?,?,?,0,?,?)",
                (code, points, max_claims, now, created_by),
            )
        return code, None

    def list_redeem_codes(self):
        return self._all("SELECT * FROM rb_redeem_codes ORDER BY created_at DESC")

    def delete_redeem_code(self, code):
        self._run("DELETE FROM rb_redeem_codes WHERE code=?", (code,))

    def use_redeem_code(self, code, user_id):
        """User claims a redeem code.
        Returns: (pts_awarded, error_msg)"""
        with self._tx():
            rc = self._one("SELECT * FROM rb_redeem_codes WHERE code=?", (code,))
            if not rc:
                return 0, "❌ Invalid code"
            if rc["claims_used"] >= rc["max_claims"]:
                return 0, "❌ Code expired (max claims reached)"
            already = self._one(
                "SELECT 1 FROM rb_redeem_claims WHERE code=? AND user_id=?", (code, user_id)
            )
            if already:
                return 0, "❌ You already used this code"
            pts = rc["points"]
            self._run(
                "INSERT INTO rb_redeem_claims(code,user_id,claimed_at) VALUES(?,?,?)",
                (code, user_id, int(time.time())),
            )
            self._run(
                "UPDATE rb_redeem_codes SET claims_used=claims_used+1 WHERE code=?", (code,)
            )
            self._run("UPDATE rb_users SET points=points+? WHERE user_id=?", (pts, user_id))
            return pts, None

    # ------------------------------------------------------- non-root VPS
    def save_nonroot_vps(self, user_id, host, port, username, password, days=30):
        """Save a non-root VPS record for user."""
        now = int(time.time())
        self._run(
            "INSERT INTO rb_nonroot_vps(user_id,host,port,username,password,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, host, port, username, password, now, now + days * 86400),
        )

    def user_nonroot_vps(self, user_id):
        return self._all(
            "SELECT * FROM rb_nonroot_vps WHERE user_id=? ORDER BY created_at ASC", (user_id,)
        )
