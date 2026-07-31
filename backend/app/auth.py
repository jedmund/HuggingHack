from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .database import INTEGRITY_ERRORS, Database


USERNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{1,30}[a-z0-9])?$")
PASSWORD_MIN_LENGTH = 12
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Username must be 3-32 lowercase letters, numbers, underscores, or hyphens."
        )
    return username


def oidc_username(claims: dict[str, Any]) -> str:
    raw = str(claims.get("preferred_username") or "").strip()
    if not raw and claims.get("email"):
        raw = str(claims["email"]).split("@", 1)[0]
    if not raw:
        raw = f"user-{str(claims.get('sub') or '')[:12]}"
    candidate = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-_")
    if len(candidate) < 3:
        candidate = f"user-{candidate or 'oidc'}"
    candidate = candidate[:32].rstrip("-_")
    return normalize_username(candidate)


def oidc_groups(claims: dict[str, Any]) -> set[str]:
    value = claims.get("groups") or []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(group).strip().casefold() for group in value if str(group).strip()}


def validate_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(value) > 256:
        raise ValueError("Password must be 256 characters or fewer.")
    return value


def hash_password(password: str) -> str:
    validated = validate_password(password)
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        validated.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


class AuthService:
    cookie_name = "hugginghack_session"

    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._attempt_lock = threading.Lock()
        self._setup_lock = threading.Lock()

    def ensure_local_user(self) -> None:
        if self.settings.effective_auth_mode != "disabled":
            return
        if self.database.get_user("local") is None:
            self.database.create_user(
                {
                    "id": "local",
                    "username": "local",
                    "display_name": "Local user",
                    "password_hash": "disabled",
                    "role": "admin",
                    "created_at": utc_iso(),
                    "updated_at": utc_iso(),
                }
            )

    def setup_required(self) -> bool:
        return (
            self.settings.effective_auth_mode == "local"
            and self.database.count_users() == 0
        )

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        role: str = "member",
    ) -> dict[str, Any]:
        normalized = normalize_username(username)
        name = display_name.strip() or normalized
        if len(name) > 80:
            raise ValueError("Display name must be 80 characters or fewer.")
        if role not in {"admin", "member"}:
            raise ValueError("Role must be admin or member.")
        timestamp = utc_iso()
        return self.database.create_user(
            {
                "id": uuid.uuid4().hex,
                "username": normalized,
                "display_name": name,
                "password_hash": hash_password(password),
                "role": role,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

    def create_owner(
        self, username: str, display_name: str, password: str
    ) -> dict[str, Any]:
        with self._setup_lock:
            if self.database.count_users() != 0:
                raise ValueError("The owner account already exists.")
            return self.create_user(username, display_name, password, role="admin")

    def authenticate(self, username: str, password: str, client_key: str) -> dict[str, Any] | None:
        self._check_rate_limit(client_key)
        try:
            normalized = normalize_username(username)
        except ValueError:
            self._record_failure(client_key)
            return None
        user = self.database.get_user_by_username(normalized)
        if not user or not verify_password(password, user["password_hash"]):
            self._record_failure(client_key)
            return None
        with self._attempt_lock:
            self._attempts.pop(client_key, None)
        return user

    def create_session(self, user_id: str) -> tuple[str, str]:
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        csrf_token = secrets.token_urlsafe(32)
        created = utc_now()
        self.database.create_session(
            {
                "token_hash": token_hash,
                "user_id": user_id,
                "csrf_token": csrf_token,
                "created_at": utc_iso(created),
                "expires_at": utc_iso(
                    created
                    + timedelta(hours=self.settings.effective_session_ttl_hours)
                ),
            }
        )
        return raw_token, csrf_token

    def session(self, raw_token: str | None) -> dict[str, Any] | None:
        if self.settings.effective_auth_mode == "disabled":
            user = self.database.get_user("local", include_secret=False)
            return {"user": user, "csrf_token": "accounts-disabled"} if user else None
        if not raw_token:
            return None
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        session = self.database.get_session(token_hash)
        if not session:
            return None
        try:
            expires = datetime.fromisoformat(session["expires_at"])
        except (TypeError, ValueError):
            self.database.delete_session(token_hash)
            return None
        if expires <= utc_now():
            self.database.delete_session(token_hash)
            return None
        return session

    def revoke(self, raw_token: str | None) -> None:
        if raw_token:
            self.database.delete_session(
                hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            )

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        raw_session_token: str,
    ) -> None:
        user = self.database.get_user(user_id)
        if not user or not verify_password(current_password, user["password_hash"]):
            raise ValueError("Current password is incorrect.")
        replacement = hash_password(new_password)
        self.database.update_user_password(user_id, replacement, utc_iso())
        keep_hash = hashlib.sha256(raw_session_token.encode("utf-8")).hexdigest()
        self.database.delete_other_sessions(user_id, keep_hash)

    def verify_csrf(self, session: dict[str, Any], token: str | None) -> bool:
        if self.settings.effective_auth_mode == "disabled":
            return True
        return bool(token) and hmac.compare_digest(
            str(session.get("csrf_token") or ""), token
        )

    def provision_oidc_user(self, claims: dict[str, Any]) -> dict[str, Any]:
        issuer = str(claims.get("iss") or "")
        subject = str(claims.get("sub") or "")
        if not issuer or not subject:
            raise ValueError("The identity token is missing its issuer or subject.")

        groups = oidc_groups(claims)
        allowed_groups = {group.casefold() for group in self.settings.oidc_allowed_groups}
        if allowed_groups and not groups.intersection(allowed_groups):
            raise PermissionError("Your Pocket ID groups do not allow access to HuggingHack.")

        now = utc_iso()
        preferred_username = str(claims.get("preferred_username") or "").strip() or None
        email = str(claims.get("email") or "").strip() or None
        display_name = str(
            claims.get("name") or preferred_username or email or "Pocket ID user"
        ).strip()[:80]
        admin_groups = {group.casefold() for group in self.settings.oidc_admin_groups}
        mapped_role = "admin" if groups.intersection(admin_groups) else "member"

        identity = self.database.get_oidc_identity(issuer, subject)
        user = self.database.get_user(identity["user_id"]) if identity else None
        if user is None:
            base_username = oidc_username(claims)
            username = base_username
            suffix = 1
            while True:
                existing = self.database.get_user_by_username(username)
                if existing is None:
                    user = self.database.create_user(
                        {
                            "id": uuid.uuid4().hex,
                            "username": username,
                            "display_name": display_name,
                            "password_hash": "oidc-only",
                            "role": mapped_role,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    break
                if self.database.get_oidc_identity_for_user(existing["id"]) is None:
                    user = existing
                    break
                suffix += 1
                suffix_text = f"-{suffix}"
                username = f"{base_username[:32 - len(suffix_text)].rstrip('-_')}{suffix_text}"

            try:
                self.database.link_oidc_identity(
                    {
                        "issuer": issuer,
                        "subject": subject,
                        "user_id": user["id"],
                        "preferred_username": preferred_username,
                        "email": email,
                        "created_at": now,
                        "last_login_at": now,
                    }
                )
            except INTEGRITY_ERRORS:
                identity = self.database.get_oidc_identity(issuer, subject)
                if not identity:
                    raise
                user = self.database.get_user(identity["user_id"])
                if not user:
                    raise
        else:
            self.database.update_oidc_identity(
                issuer, subject, preferred_username, email, now
            )

        role = mapped_role if admin_groups else str(user["role"])
        updated = self.database.update_user_profile(user["id"], display_name, role, now)
        if not updated:
            raise ValueError("Unable to provision the Pocket ID account.")
        return updated

    def _check_rate_limit(self, key: str) -> None:
        cutoff = utc_now().timestamp() - 300
        with self._attempt_lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= 8:
                raise ValueError("Too many sign-in attempts. Try again in a few minutes.")

    def _record_failure(self, key: str) -> None:
        with self._attempt_lock:
            self._attempts[key].append(utc_now().timestamp())
