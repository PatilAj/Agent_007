"""
Kite Connect session management.

Daily flow (Zerodha requires a fresh access_token every trading day):

  1. POST to Kite login endpoint with user_id + password   -> request_id
  2. POST 2FA with TOTP (generated locally from secret)    -> redirect with request_token
  3. Exchange request_token + api_secret -> access_token
  4. Store access_token in DB + Redis; valid until ~6 AM next day

Interactive mode: if TOTP secret isn't configured, falls back to manual entry
of the request_token (operator copy-pastes from the redirect URL).

References:
  - https://kite.trade/docs/connect/v3/user/#login-flow
  - https://github.com/zerodha/pykiteconnect
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pyotp
import pytz
from sqlalchemy import update
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.config import settings
from src.core.exceptions import AuthError
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import KiteToken

if TYPE_CHECKING:
    from kiteconnect import KiteConnect

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

KITE_BASE = "https://kite.zerodha.com"


def _ensure_kite_imported() -> "type[KiteConnect]":
    """Lazy import so tests don't need kiteconnect installed for non-broker code."""
    from kiteconnect import KiteConnect

    return KiteConnect


async def get_active_token() -> str | None:
    """Return the current active access_token from DB, or None.

    Filters by the current configured api_key so that switching apps
    (e.g. after subscribing to a new Kite Connect app) doesn't return
    a stale token tied to the previous api_key.
    """
    from sqlalchemy import select

    current_api_key = settings.kite_api_key.get_secret_value()
    async with get_session() as s:
        stmt = (
            select(KiteToken)
            .where(KiteToken.is_active.is_(True))
            .where(KiteToken.api_key == current_api_key)
            .order_by(KiteToken.issued_at.desc())
            .limit(1)
        )
        row = (await s.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        if row.expires_at <= datetime.now(tz=pytz.UTC):
            return None
        return row.access_token


async def store_token(api_key: str, user_id: str, access_token: str, public_token: str | None) -> None:
    """Deactivate previous tokens and insert the new one."""
    expires_at = _next_token_expiry()

    async with get_session() as s:
        # mark old tokens inactive
        await s.execute(
            update(KiteToken).where(KiteToken.is_active.is_(True)).values(is_active=False)
        )
        token = KiteToken(
            api_key=api_key,
            user_id=user_id,
            access_token=access_token,
            public_token=public_token,
            expires_at=expires_at,
            is_active=True,
        )
        s.add(token)
    log.info("kite_token_stored", expires_at=expires_at.isoformat(), user_id=user_id)


def _next_token_expiry() -> datetime:
    """Kite tokens expire at ~6 AM IST next day."""
    now_ist = datetime.now(IST)
    expiry_ist = IST.localize(datetime.combine(now_ist.date() + timedelta(days=1), time(6, 0)))
    return expiry_ist.astimezone(pytz.UTC)


# ----------------------------- Interactive login flow -----------------------------


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
async def _kite_login_request(client: httpx.AsyncClient, user_id: str, password: str) -> str:
    """Returns request_id needed for TOTP step."""
    r = await client.post(
        f"{KITE_BASE}/api/login",
        data={"user_id": user_id, "password": password},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise AuthError(f"Kite login failed: {body}")
    return body["data"]["request_id"]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
async def _kite_twofa(
    client: httpx.AsyncClient, user_id: str, request_id: str, twofa_value: str, twofa_type: str
) -> None:
    r = await client.post(
        f"{KITE_BASE}/api/twofa",
        data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": twofa_value,
            "twofa_type": twofa_type,
        },
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise AuthError(f"Kite 2FA failed: {body}")


async def acquire_request_token(
    api_key: str,
    user_id: str,
    password: str,
    *,
    manual_otp: str = "",
    pin: str = "",
    totp_secret: str = "",
) -> str:
    """
    Run the browser-less login dance and return the `request_token` from the
    302 redirect to the registered Kite app callback URL.

    Precedence: manual_otp > pin > totp_secret.
    """
    if manual_otp:
        twofa_value, twofa_type = manual_otp, "totp"
    elif pin:
        twofa_value, twofa_type = pin, "pin"
    elif totp_secret:
        twofa_value, twofa_type = pyotp.TOTP(totp_secret).now(), "totp"
    else:
        raise AuthError("Provide manual_otp, KITE_PIN, or KITE_TOTP_SECRET")

    async with httpx.AsyncClient(follow_redirects=False) as client:
        # Step 1: login
        request_id = await _kite_login_request(client, user_id, password)
        log.info("kite_login_step1_ok", user_id=user_id)

        # Step 2: 2FA (PIN or TOTP)
        await _kite_twofa(client, user_id, request_id, twofa_value, twofa_type)
        log.info("kite_login_step2_ok", twofa_type=twofa_type)

        # Step 3: trigger the OAuth-style redirect
        r = await client.get(f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3")
        # We follow redirects manually until we hit our redirect_url with `request_token=...`
        for _ in range(5):
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location", "")
                if "request_token=" in loc:
                    token = loc.split("request_token=", 1)[1].split("&", 1)[0]
                    return token
                r = await client.get(loc)
            else:
                break

    raise AuthError("Could not extract request_token from Kite login redirects")


def _checksum(api_key: str, request_token: str, api_secret: str) -> str:
    return hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()


async def login_and_store() -> str:
    """End-to-end daily login. Returns the new access_token."""
    api_key = settings.kite_api_key.get_secret_value()
    api_secret = settings.kite_api_secret.get_secret_value()
    user_id = settings.kite_user_id
    password = settings.kite_password.get_secret_value()
    pin = settings.kite_pin.get_secret_value()
    totp_secret = settings.kite_totp_secret.get_secret_value()

    if not all([api_key, api_secret, user_id, password]):
        raise AuthError("Kite credentials missing. Fill in .env (need API key/secret, user_id, password)")

    manual_otp = ""
    if not pin and not totp_secret:
        log.info("kite_login_manual_otp_prompt")
        raw = await asyncio.to_thread(
            input,
            f"\n>>> Enter current 6-digit code from your authenticator app for Zerodha ({user_id}): ",
        )
        manual_otp = raw.strip()
        if not (manual_otp.isdigit() and len(manual_otp) == 6):
            raise AuthError(f"Invalid OTP format: expected 6 digits, got {len(manual_otp)} chars")

    request_token = await acquire_request_token(
        api_key, user_id, password,
        manual_otp=manual_otp, pin=pin, totp_secret=totp_secret,
    )
    log.info("kite_request_token_acquired")

    # Exchange request_token -> access_token (sync SDK call in thread pool)
    KiteConnect = _ensure_kite_imported()
    kite = KiteConnect(api_key=api_key)

    def _generate() -> dict[str, Any]:
        return kite.generate_session(request_token, api_secret=api_secret)

    session = await asyncio.to_thread(_generate)
    access_token: str = session["access_token"]
    public_token: str = session.get("public_token", "")

    await store_token(api_key, user_id, access_token, public_token)
    return access_token


async def ensure_valid_token() -> str:
    """Return a valid token, logging in if necessary."""
    token = await get_active_token()
    if token:
        return token
    log.info("no_active_token_logging_in")
    return await login_and_store()
