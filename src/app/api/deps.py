from __future__ import annotations

from collections.abc import AsyncIterator
import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.gateway import AIGateway, get_gateway
from ..config import Settings, get_settings
from ..db.session import get_session
from ..infrastructure.queue import JobQueue
from ..infrastructure.storage import ObjectStorage, get_storage
from ..services.errors import ServiceError


async def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """Bearer auth.

    Fails closed. An unset token used to disable authentication entirely, which meant
    one missed environment variable published the whole API; now it returns 503 unless
    the operator has explicitly opted out with WC_ALLOW_UNAUTHENTICATED.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        if settings.allow_unauthenticated:
            return
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "WC_API_TOKEN is not configured; refusing unauthenticated requests")
    expected = f"Bearer {settings.api_token}"
    if not hmac.compare_digest((authorization or "").encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing bearer token")


def settings_dep() -> Settings:
    return get_settings()


def storage_dep() -> ObjectStorage:
    return get_storage()


def gateway_dep() -> AIGateway:
    return get_gateway()


def queue_dep() -> JobQueue:
    return JobQueue(get_settings())


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(settings_dep)]
StorageDep = Annotated[ObjectStorage, Depends(storage_dep)]
GatewayDep = Annotated[AIGateway, Depends(gateway_dep)]
QueueDep = Annotated[JobQueue, Depends(queue_dep)]
Authenticated = Depends(require_token)


def http_error(error: ServiceError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=error.detail or error.code)
