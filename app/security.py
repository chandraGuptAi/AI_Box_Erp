"""Request authentication.

- Service token: when SERVICE_TOKEN is set, every request except /health must carry it in
  `X-Ai-Box-Service-Token`. The gateway adds it, so only the gateway can reach this service.
- Panel login: the request must carry the user's ERP panel token (`Authorization: Bearer <token>` or
  `X-Shipment-Authorization: Bearer <token>`). This service calls no ERP API, so the token is only
  required to be present, NOT verified: it keeps casual callers out, it is not real authentication.
- User: saved reports belong to the `X-User-Id` the panel sends (see current_user).
"""
import hashlib
import re
import secrets

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.errors import ApiError

SERVICE_TOKEN_HEADER = "X-Ai-Box-Service-Token"
_USER_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def panel_login(authorization: str | None = Header(default=None),
                x_shipment_authorization: str | None = Header(default=None)) -> str:
    """401 unless a Bearer token is present (not verified, see the module docstring)."""
    for value in (authorization, x_shipment_authorization):
        scheme, _, token = (value or "").strip().partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return f"Bearer {token.strip()}"
    raise ApiError(401, "You are not logged in. Please log in again.")


async def current_user(request: Request, authorization: str = Depends(panel_login),
                       x_user_id: str | None = Header(default=None)) -> str:
    """The ERP login user id the panel sends in `X-User-Id` (from its stored login details).

    The ERP offers no API to look a user up from a token, so the id comes from the panel. To stop one
    login from reading another user's reports, the first user id seen with an ERP token owns that token:
    the same token with a different user id gets 403. Every saved-report query filters by this id.
    """
    user_id = (x_user_id or "").strip()
    if not _USER_ID.match(user_id):
        raise ApiError(401, "Your login details are missing. Please log in again.")
    store = getattr(request.app.state, "saved", None)
    if store is not None:
        token_hash = hashlib.sha256(authorization.encode()).hexdigest()
        if not await store.claim_token(token_hash, user_id):
            raise ApiError(403, "This login belongs to another user. Please log in again.")
    return user_id


async def service_token_middleware(request: Request, call_next):
    expected = get_settings().SERVICE_TOKEN
    if expected and request.url.path != "/health":
        sent = request.headers.get(SERVICE_TOKEN_HEADER, "")
        if not secrets.compare_digest(sent.encode(), expected.encode()):
            return JSONResponse({"message": "Invalid or missing service token"}, status_code=403)
    return await call_next(request)
