from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status

from lobster_phone_agent.config import Settings


def make_auth_dependency(settings: Settings):
    async def require_api_key(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> None:
        if not settings.api_key:
            return
        supplied = x_api_key
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not secrets.compare_digest(supplied, settings.api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.authenticated = True

    return require_api_key
