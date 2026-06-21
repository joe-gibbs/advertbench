from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import get_settings

settings = get_settings()

if not settings.database_url:
    print("DATABASE_URL is not set. Database calls will fail until it is configured.")

pool = ConnectionPool(
    conninfo=settings.database_url or "postgresql://invalid",
    min_size=0,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
)


def open_pool() -> None:
    pool.open(wait=False)


def close_pool() -> None:
    pool.close()


@contextmanager
def connection() -> Iterator[Connection]:
    with pool.connection() as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[Connection]:
    with pool.connection() as conn:
        with conn.transaction():
            yield conn
