import os
import json
import logging
from typing import Optional, Dict, Any

import psycopg2
from psycopg2.extras import Json
from cryptography.fernet import Fernet

# Connection string for PostgreSQL (prefer DATABASE_URL)
DB_DSN = os.getenv("DATABASE_URL")
if not DB_DSN:
    # Fallback for legacy deployments
    DB_DSN = os.getenv("DB_DSN", "")

# psycopg2/libpq expects the scheme to be ``postgresql://``
if DB_DSN.startswith("postgres://"):
    DB_DSN = DB_DSN.replace("postgres://", "postgresql://", 1)

# Encryption key for cookies (base64 encoded string)
ENC_KEY = os.getenv("COOKIE_ENC_KEY")
if not ENC_KEY:
    logging.warning(
        "COOKIE_ENC_KEY environment variable is not set; generating a temporary key"
    )
    ENC_KEY = Fernet.generate_key().decode()

fernet = Fernet(ENC_KEY.encode())


def _get_conn():
    if not DB_DSN:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DB_DSN, sslmode="require")


def init_db() -> None:
    """Create tables if they don't exist."""
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_auth_cookies (
              discord_user_id   TEXT PRIMARY KEY,
              encrypted_cookies BYTEA NOT NULL,
              key_version       INTEGER NOT NULL DEFAULT 1,
              user_agent        TEXT,
              last_ip           INET,
              expires_at        TIMESTAMPTZ,
              is_active         BOOLEAN NOT NULL DEFAULT TRUE,
              created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_cookie_history (
              id              BIGSERIAL PRIMARY KEY,
              discord_user_id TEXT NOT NULL,
              event           TEXT NOT NULL,
              meta            JSONB,
              created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS auth_cookie_history_idx ON auth_cookie_history (discord_user_id, created_at DESC)"
        )


def save_cookies(
    discord_user_id: str,
    cookies: Dict[str, str],
    *,
    user_agent: Optional[str] = None,
    last_ip: Optional[str] = None,
) -> None:
    """Encrypt and store cookies for a Discord user."""
    # Always store as TEXT to avoid bigint comparisons in SQL
    discord_user_id = str(discord_user_id)

    encoded = json.dumps(cookies).encode()
    encrypted = fernet.encrypt(encoded)
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_auth_cookies (
              discord_user_id, encrypted_cookies, user_agent, last_ip
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (discord_user_id) DO UPDATE SET
              encrypted_cookies = EXCLUDED.encrypted_cookies,
              user_agent = EXCLUDED.user_agent,
              last_ip = EXCLUDED.last_ip,
              updated_at = NOW()
            ;
            """,
            (discord_user_id, psycopg2.Binary(encrypted), user_agent, last_ip),
        )
        cur.execute(
            """INSERT INTO auth_cookie_history (discord_user_id, event, meta) VALUES (%s, %s, %s);""",
            (discord_user_id, "saved", Json(cookies)),
        )


def get_cookies(discord_user_id: str) -> Optional[Dict[str, str]]:
    """Retrieve and decrypt cookies for a Discord user."""
    discord_user_id = str(discord_user_id)

    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT encrypted_cookies FROM user_auth_cookies WHERE discord_user_id = %s AND is_active",
            (discord_user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        encrypted = bytes(row[0])
        decoded = fernet.decrypt(encrypted)
        return json.loads(decoded.decode())


def get_cookies_and_meta(discord_user_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve cookies + metadata (currently just user_agent).
    Returns {"cookies": Dict[str,str], "user_agent": Optional[str]} or None.
    """
    discord_user_id = str(discord_user_id)

    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT encrypted_cookies, user_agent FROM user_auth_cookies WHERE discord_user_id = %s AND is_active",
            (discord_user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        encrypted = bytes(row[0])
        decoded = fernet.decrypt(encrypted)
        cookies: Dict[str, str] = json.loads(decoded.decode())
        user_agent: Optional[str] = row[1]
        return {"cookies": cookies, "user_agent": user_agent}
