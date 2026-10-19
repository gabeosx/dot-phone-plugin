from __future__ import annotations

import hashlib
import re
import secrets
import time
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPE = "phone:control"
# Adapted from Agent Call's reviewed owner-secret configuration (MIT notice in NOTICE).
HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)


def token_id(token):
    return hashlib.sha256(token.encode()).hexdigest()


class OwnerOAuth:
    """SDK OAuth 2.1 flow; opaque tokens, PKCE handled by SDK token route.

    Upstream's FastMCP-specific signing layer is deliberately omitted. No JWT
    signing credential is necessary: random bearer tokens are looked up by hash.
    """

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.events = None
        self.resource = settings.mcp_resource_url or settings.public_base_url.rstrip("/") + "/mcp"

    def rate_limit(self, bucket, limit=10):
        id = token_id(bucket)
        row = self.store.get("rate", id) or {"start": time.time(), "count": 0}
        if row["start"] + 300 < time.time():
            row = {"start": time.time(), "count": 0}
        row["count"] += 1
        self.store.put("rate", id, "system", row)
        if row["count"] > limit:
            raise AuthorizeError(error="access_denied", error_description="try again later")

    def verify_owner(self, password):
        self.rate_limit("owner-password", 10)
        encoded = self.settings.owner_secret_hash.get_secret_value()
        if (
            not encoded.startswith("$argon2id$")
            or not isinstance(password, str)
            or len(password) > 512
        ):
            return False
        try:
            return HASHER.verify(encoded, password)
        except (InvalidHashError, VerifyMismatchError, ValueError):
            return False

    async def get_client(self, client_id):
        value = self.store.get("oauth_client", client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def register_client(self, client_info):
        self.rate_limit("registration", 20)
        if (
            not client_info.client_id
            or len(client_info.model_dump_json()) > 8192
            or not client_info.redirect_uris
            or len(client_info.redirect_uris) > 4
        ):
            raise RegistrationError(error="invalid_client_metadata")
        if (
            client_info.token_endpoint_auth_method != "none"
            or set(client_info.grant_types or []) - {"authorization_code", "refresh_token"}
            or client_info.response_types != ["code"]
            or client_info.scope not in {None, SCOPE}
        ):
            raise RegistrationError(error="invalid_client_metadata")
        if any(
            str(u) not in self.settings.redirect_uris or urlsplit(str(u)).scheme != "https"
            for u in client_info.redirect_uris
        ):
            raise RegistrationError(error="invalid_redirect_uri")
        self.store.put(
            "oauth_client", client_info.client_id, "system", client_info.model_dump(mode="json")
        )

    async def authorize(self, client, params):
        self.rate_limit("authorization", 30)
        if (
            params.resource != self.resource
            or (params.scopes or [SCOPE]) != [SCOPE]
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge)
            or str(params.redirect_uri) not in self.settings.redirect_uris
        ):
            raise AuthorizeError(error="invalid_request")
        id = secrets.token_urlsafe(32)
        self.store.put(
            "oauth_transaction",
            token_id(id),
            "system",
            {
                "id": id,
                "client_id": client.client_id,
                "params": params.model_dump(mode="json"),
                "expires": time.time() + 300,
            },
        )
        return self.settings.public_base_url + "/owner/consent/" + id

    def consent(self, transaction_id):
        row = self.store.get("oauth_transaction", token_id(transaction_id))
        if not row or row["expires"] <= time.time():
            raise AuthorizeError(error="access_denied")
        code = secrets.token_urlsafe(32)
        params = row["params"]
        value = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=time.time() + 60,
            client_id=row["client_id"],
            code_challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=self.resource,
            subject=self.settings.owner,
        )
        with self.store.transaction():
            self.store.delete("oauth_transaction", token_id(transaction_id))
            self.store.put(
                "oauth_code", token_id(code), self.settings.owner, value.model_dump(mode="json")
            )
        return construct_redirect_uri(
            params["redirect_uri"],
            code=code,
            state=params.get("state"),
            iss=self.settings.public_base_url,
        )

    async def load_authorization_code(self, client, authorization_code):
        row = self.store.get("oauth_code", token_id(authorization_code))
        if row and row["client_id"] == client.client_id and row["expires_at"] > time.time():
            return AuthorizationCode.model_validate(row)
        return None

    def issue(self, client_id, family=None):
        family = family or secrets.token_hex(24)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for kind, value, lifetime in (
            ("oauth_access", access, 900),
            ("oauth_refresh", refresh, 86400),
        ):
            row = {
                "token": value,
                "client_id": client_id,
                "scopes": [SCOPE],
                "expires_at": int(time.time()) + lifetime,
                "resource": self.resource,
                "subject": self.settings.owner,
                "family": family,
                "revoked": False,
            }
            self.store.put(kind, token_id(value), self.settings.owner, row)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=900,
            refresh_token=refresh,
            scope=SCOPE,
        )

    async def exchange_authorization_code(self, client, authorization_code):
        with self.store.transaction():
            id = token_id(authorization_code.code)
            if not self.store.get("oauth_code", id):
                raise TokenError(error="invalid_grant")
            self.store.delete("oauth_code", id)
            return self.issue(client.client_id)

    async def load_refresh_token(self, client, refresh_token):
        row = self.store.get("oauth_refresh", token_id(refresh_token))
        if row and row["client_id"] == client.client_id and row["revoked"]:
            self.revoke_family(row["family"])
            return None
        if (
            row
            and not row["revoked"]
            and row["client_id"] == client.client_id
            and row["expires_at"] > time.time()
        ):
            return RefreshToken.model_validate(row)
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if scopes != [SCOPE]:
            raise TokenError(error="invalid_scope")
        with self.store.transaction():
            row = self.store.get("oauth_refresh", token_id(refresh_token.token))
            if not row or row["revoked"] or row["expires_at"] <= time.time():
                raise TokenError(error="invalid_grant")
            self.revoke_family(row["family"], subscriptions=False)
            return self.issue(client.client_id, row["family"])

    async def load_access_token(self, token):
        row = self.store.get("oauth_access", token_id(token))
        if (
            row
            and not row["revoked"]
            and row["expires_at"] > time.time()
            and row["resource"] == self.resource
            and row["subject"] == self.settings.owner
        ):
            return AccessToken.model_validate(row)
        return None

    async def verify_token(self, token):
        return await self.load_access_token(token)

    def revoke_family(self, family, subscriptions=True):
        for kind in ("oauth_access", "oauth_refresh"):
            for row in self.store.all(kind):
                if row["family"] == family:
                    row["revoked"] = True
                    self.store.put(kind, token_id(row["token"]), self.settings.owner, row)
                    if subscriptions and self.events:
                        self.events.revoke_client(row["client_id"])

    async def revoke_token(self, token):
        kind = "oauth_access" if isinstance(token, AccessToken) else "oauth_refresh"
        row = self.store.get(kind, token_id(token.token))
        if row:
            self.revoke_family(row["family"])
