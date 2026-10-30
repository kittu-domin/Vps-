"""Tiny Turso / libSQL client over the HTTP "pipeline" API (no extra dependencies).

Exposes the small part of the sqlite3 API that db.py needs:
    conn.execute(sql, args) -> Result (fetchone / fetchall), rows support row["col"] and row[0]
Interactive transactions (BEGIN ... COMMIT) are kept on one server-side stream using the baton.
"""
import base64
import http.client
import json
from urllib.parse import urlparse


class TursoError(Exception):
    pass


class Row:
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols, self._vals = cols, vals

    def __getitem__(self, k):
        return self._vals[k] if isinstance(k, int) else self._vals[self._cols.index(k)]

    def get(self, k, default=None):
        """dict-like get — safe for missing columns (matches sqlite3.Row usage)."""
        try:
            if isinstance(k, int):
                return self._vals[k]
            if k in self._cols:
                return self._vals[self._cols.index(k)]
            return default
        except (IndexError, ValueError, KeyError):
            return default

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)


class Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _enc(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _dec(c):
    t = c.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(c["value"])
    if t == "float":
        return float(c["value"])
    if t == "blob":
        return base64.b64decode(c["base64"] + "=" * (-len(c["base64"]) % 4))
    return c.get("value")


def _split_url(url):
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    scheme = "http" if p.scheme == "http" else "https"  # libsql:// -> https
    return scheme, p.netloc


class TursoConn:
    def __init__(self, url, token):
        self.scheme, self.host = _split_url(url)
        self.token = token
        self.baton = None
        self._http = None
        self._http_host = None

    # --------------------------------------------------------------- http
    def _connection(self):
        if self._http is None or self._http_host != self.host:
            self._reset()
            cls = http.client.HTTPConnection if self.scheme == "http" else http.client.HTTPSConnection
            self._http = cls(self.host, timeout=30)
            self._http_host = self.host
        return self._http

    def _reset(self):
        if self._http is not None:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001
                pass
        self._http = None

    def _post(self, payload):
        body = json.dumps(payload)
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        for attempt in (0, 1):
            try:
                conn = self._connection()
                conn.request("POST", "/v2/pipeline", body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                break
            except (http.client.HTTPException, OSError) as e:
                self._reset()
                if self.baton or attempt == 1:  # an open transaction cannot be replayed
                    self.baton = None
                    raise TursoError(f"Turso connection error: {e}") from e
        if resp.status != 200:
            self.baton = None
            raise TursoError(f"Turso HTTP {resp.status}: {data[:300].decode('utf-8', 'replace')}")
        return json.loads(data)

    # ---------------------------------------------------------------- api
    def execute(self, sql, args=()):
        head = sql.lstrip().upper()
        ending = head.startswith(("COMMIT", "ROLLBACK", "END"))
        begin = head.startswith("BEGIN")
        in_tx = self.baton is not None
        reqs = [{"type": "execute", "stmt": {"sql": sql, "args": [_enc(a) for a in args]}}]
        if ending or not (begin or in_tx):
            reqs.append({"type": "close"})  # autocommit statement: close the stream
        payload = {"requests": reqs}
        if self.baton:
            payload["baton"] = self.baton
        data = self._post(payload)
        if ending or not (begin or in_tx):
            self.baton = None
        else:
            self.baton = data.get("baton")
        if data.get("base_url"):
            self.scheme, self.host = _split_url(data["base_url"])
        first = data["results"][0]
        if first.get("type") == "error":
            raise TursoError(first["error"].get("message", "Turso error"))
        res = first["response"]["result"]
        cols = [c.get("name") or "" for c in res.get("cols", [])]
        rows = [Row(cols, [_dec(c) for c in r]) for r in res.get("rows", [])]
        return Result(rows)
