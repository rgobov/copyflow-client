from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from copyflow_client.config import ClientSettings
from copyflow_client.models import Base


def make_engine(settings: ClientSettings):
    return create_async_engine(settings.database_url, echo=False)


async def ensure_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def make_session_factory(engine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
