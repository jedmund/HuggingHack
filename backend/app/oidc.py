from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JsonWebToken
from authlib.oidc.core import CodeIDToken

from .auth import AuthService, utc_iso, utc_now
from .config import Settings
from .database import Database


SAFE_ID_TOKEN_ALGORITHMS = {
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
}


class OIDCService:
    def __init__(self, settings: Settings, database: Database, auth: AuthService):
        self.settings = settings
        self.database = database
        self.auth = auth
        self._client = httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": f"{settings.app_name}/{settings.app_version}"},
        )
        self._metadata_cache: dict[str, Any] | None = None
        self._metadata_expires_at = 0.0
        self._metadata_lock = threading.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def metadata(self) -> dict[str, Any]:
        with self._metadata_lock:
            if self._metadata_cache and self._metadata_expires_at > time.monotonic():
                return self._metadata_cache
        response = await self._client.get(
            f"{self.settings.oidc_issuer}/.well-known/openid-configuration"
        )
        response.raise_for_status()
        metadata = response.json()
        if not isinstance(metadata, dict):
            raise ValueError("The OIDC discovery response is invalid.")
        if str(metadata.get("issuer") or "").rstrip("/") != self.settings.oidc_issuer:
            raise ValueError("The OIDC discovery issuer does not match OIDC_ISSUER.")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not metadata.get(field):
                raise ValueError(f"The OIDC discovery response is missing {field}.")
        with self._metadata_lock:
            self._metadata_cache = metadata
            self._metadata_expires_at = time.monotonic() + 600
        return metadata

    async def authorization_url(self, return_to: str = "/") -> str:
        self.settings.validate_auth()
        metadata = await self.metadata()
        state = secrets.token_urlsafe(40)
        nonce = secrets.token_urlsafe(40)
        code_verifier = secrets.token_urlsafe(64)
        now = utc_now()
        self.database.create_oidc_login_state(
            {
                "state_hash": self._state_hash(state),
                "nonce": nonce,
                "code_verifier": code_verifier,
                "return_to": self._safe_return_to(return_to),
                "created_at": utc_iso(now),
                "expires_at": utc_iso(now + timedelta(minutes=10)),
            }
        )
        client = AsyncOAuth2Client(
            self.settings.oidc_client_id,
            self.settings.oidc_client_secret,
            scope=" ".join(self.settings.oidc_scopes),
            redirect_uri=self.settings.oidc_redirect_uri,
            code_challenge_method="S256",
        )
        try:
            url, _ = client.create_authorization_url(
                metadata["authorization_endpoint"],
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
            )
        finally:
            await client.aclose()
        return url

    async def authenticate_callback(self, code: str, state: str) -> tuple[dict[str, Any], str]:
        if not code or not state:
            raise ValueError("The OIDC callback is missing its code or state.")
        login_state = self.database.consume_oidc_login_state(self._state_hash(state))
        if not login_state:
            raise ValueError("The OIDC login state is missing, expired, or already used.")
        try:
            expires_at = datetime.fromisoformat(login_state["expires_at"])
        except (TypeError, ValueError) as error:
            raise ValueError("The OIDC login state is invalid.") from error
        if expires_at <= utc_now():
            raise ValueError("The OIDC login state has expired.")

        metadata = await self.metadata()
        auth_method = self._token_auth_method(metadata)
        client = AsyncOAuth2Client(
            self.settings.oidc_client_id,
            self.settings.oidc_client_secret,
            scope=" ".join(self.settings.oidc_scopes),
            redirect_uri=self.settings.oidc_redirect_uri,
            code_challenge_method="S256",
            token_endpoint_auth_method=auth_method,
        )
        try:
            token = await client.fetch_token(
                metadata["token_endpoint"],
                code=code,
                state=state,
                code_verifier=login_state["code_verifier"],
            )
        finally:
            await client.aclose()
        id_token = token.get("id_token")
        if not id_token:
            raise ValueError("Pocket ID did not return an ID token.")
        claims = await self._validate_id_token(
            str(id_token), metadata, login_state["nonce"], token.get("access_token")
        )
        user = self.auth.provision_oidc_user(claims)
        return user, self._safe_return_to(login_state["return_to"])

    async def logout_url(self) -> str | None:
        try:
            endpoint = (await self.metadata()).get("end_session_endpoint")
        except (httpx.HTTPError, ValueError):
            return None
        if not endpoint:
            return None
        query = urlencode(
            {
                "client_id": self.settings.oidc_client_id,
                "post_logout_redirect_uri": f"{self.settings.app_base_url}/",
            }
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    async def _validate_id_token(
        self,
        encoded: str,
        metadata: dict[str, Any],
        nonce: str,
        access_token: str | None,
    ) -> dict[str, Any]:
        response = await self._client.get(metadata["jwks_uri"])
        response.raise_for_status()
        jwks = response.json()
        advertised = metadata.get("id_token_signing_alg_values_supported") or ["RS256"]
        algorithms = [
            algorithm
            for algorithm in advertised
            if algorithm in SAFE_ID_TOKEN_ALGORITHMS
        ]
        if not algorithms:
            raise ValueError("Pocket ID does not advertise a supported ID-token algorithm.")
        decoder = JsonWebToken(algorithms)
        claims = decoder.decode(
            encoded,
            jwks,
            claims_cls=CodeIDToken,
            claims_options={
                "iss": {"essential": True, "value": self.settings.oidc_issuer},
                "sub": {"essential": True},
                "aud": {"essential": True, "value": self.settings.oidc_client_id},
            },
            claims_params={
                "client_id": self.settings.oidc_client_id,
                "nonce": nonce,
                "access_token": access_token,
            },
        )
        claims.validate(leeway=60)
        return dict(claims)

    @staticmethod
    def _state_hash(state: str) -> str:
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_return_to(value: str) -> str:
        target = str(value or "/").strip()
        if not target.startswith("/") or target.startswith("//"):
            return "/"
        return target[:500]

    @staticmethod
    def _token_auth_method(metadata: dict[str, Any]) -> str:
        supported = metadata.get("token_endpoint_auth_methods_supported") or []
        if "client_secret_basic" in supported or not supported:
            return "client_secret_basic"
        if "client_secret_post" in supported:
            return "client_secret_post"
        raise ValueError("Pocket ID does not advertise a supported client-secret method.")
