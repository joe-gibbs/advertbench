import hashlib
import hmac
from uuid import uuid4

from fastapi import Request, Response

from .settings import get_settings

COOKIE_NAME = "advertbench_voter"
LAST_PAIR_COOKIE_NAME = "advertbench_last_pair"
ONE_YEAR_SECONDS = 60 * 60 * 24 * 365


def hash_value(value: str) -> str:
    secret = get_settings().voter_hash_secret.encode("utf-8")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def request_identity(request: Request) -> dict[str, str | bool]:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    ip = forwarded_for.split(",")[0].strip() if forwarded_for else request.headers.get("x-real-ip", "unknown")
    user_agent = request.headers.get("user-agent", "unknown")
    existing_voter_id = request.cookies.get(COOKIE_NAME)
    voter_id = existing_voter_id or str(uuid4())
    return {
        "voter_id": voter_id,
        "is_new_voter": existing_voter_id is None,
        "voter_hash": hash_value(voter_id),
        "ip_hash": hash_value(ip),
        "user_agent_hash": hash_value(user_agent),
        "user_agent": user_agent,
    }


def attach_voter_cookie(response: Response, voter_id: str) -> Response:
    response.set_cookie(
        COOKIE_NAME,
        voter_id,
        max_age=ONE_YEAR_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


def attach_last_pair_cookie(response: Response, pair_key: str | None) -> Response:
    if not pair_key:
        response.delete_cookie(LAST_PAIR_COOKIE_NAME, path="/")
        return response
    response.set_cookie(
        LAST_PAIR_COOKIE_NAME,
        pair_key,
        max_age=ONE_YEAR_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response
