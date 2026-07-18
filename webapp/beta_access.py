from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass


_PASSWORD_SCHEME = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 210_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = _PASSWORD_ITERATIONS,
) -> str:
    if len(password) < 12:
        raise ValueError("Le mot de passe bêta doit contenir au moins 12 caractères")
    resolved_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        resolved_salt,
        iterations,
    )
    return "$".join(
        (
            _PASSWORD_SCHEME,
            str(iterations),
            _b64encode(resolved_salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt, expected = encoded.split("$", 3)
        if scheme != _PASSWORD_SCHEME:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(candidate, _b64decode(expected))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True, slots=True)
class BetaUser:
    username: str
    display_name: str
    password_hash: str


def parse_beta_users(raw_json: str) -> dict[str, BetaUser]:
    if not raw_json.strip():
        return {}
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("INFOFIN_BETA_USERS_JSON doit être un objet JSON valide") from exc
    if not isinstance(payload, dict):
        raise ValueError("INFOFIN_BETA_USERS_JSON doit être un objet JSON")

    users: dict[str, BetaUser] = {}
    for raw_username, raw_record in payload.items():
        username = str(raw_username).strip().casefold()
        if not username or len(username) > 80:
            raise ValueError("Identifiant bêta invalide")
        if isinstance(raw_record, str):
            password_hash = raw_record
            display_name = str(raw_username).strip()
        elif isinstance(raw_record, dict):
            password_hash = str(raw_record.get("password_hash") or "")
            display_name = str(raw_record.get("display_name") or raw_username).strip()
        else:
            raise ValueError(f"Compte bêta invalide: {raw_username}")
        if not password_hash.startswith(f"{_PASSWORD_SCHEME}$"):
            raise ValueError(f"Hash de mot de passe invalide: {raw_username}")
        users[username] = BetaUser(
            username=username,
            display_name=display_name[:120] or username,
            password_hash=password_hash,
        )
    return users


class BetaAuthenticator:
    cookie_name = "infofin_beta_session"

    def __init__(
        self,
        *,
        users_json: str,
        session_secret: str,
        session_hours: int = 168,
    ) -> None:
        self.users = parse_beta_users(users_json)
        self.session_secret = session_secret.encode("utf-8")
        self.session_seconds = max(1, session_hours) * 3600
        if self.users and len(self.session_secret) < 32:
            raise ValueError(
                "INFOFIN_BETA_SESSION_SECRET doit contenir au moins 32 caractères"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.users)

    def authenticate(self, username: str, password: str) -> BetaUser | None:
        user = self.users.get(username.strip().casefold())
        if user is None or not verify_password(password, user.password_hash):
            return None
        return user

    def create_session(self, user: BetaUser, *, now: int | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = _b64encode(
            json.dumps(
                {"sub": user.username, "exp": issued_at + self.session_seconds},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self.session_secret, payload.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{payload}.{signature}"

    def read_session(self, token: str, *, now: int | None = None) -> BetaUser | None:
        try:
            payload, signature = token.split(".", 1)
            expected = _b64encode(
                hmac.new(
                    self.session_secret,
                    payload.encode("ascii"),
                    hashlib.sha256,
                ).digest()
            )
            if not hmac.compare_digest(signature, expected):
                return None
            data = json.loads(_b64decode(payload))
            current_time = int(time.time() if now is None else now)
            if int(data["exp"]) < current_time:
                return None
            return self.users.get(str(data["sub"]).casefold())
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
