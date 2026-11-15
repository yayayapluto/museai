"""Admin API: account pool management, status and tasks.

A web console (and a browser extension for one-click cookie import) are planned
on top of these endpoints; see docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends

from ... import __version__
from ...accounts.keepalive import renew_account
from ...accounts.model import Account, AccountStatus
from ...core.cookies import CookieParseError, parse_any
from ...errors import InvalidRequest, NotFound
from ...services.container import Services
from ...upstream.muse import missing_session_cookies
from ..deps import get_services, key_hash, require_admin_key
from ..schemas import AccountCreate, AccountUpdate, KeyCreate

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_key)])


def _get(svc: Services, account_id: str) -> Account:
    acc = svc.pool.get(account_id)
    if acc is None:
        raise NotFound(f"account '{account_id}' not found")
    return acc


@router.get("/status")
async def status(svc: Services = Depends(get_services)) -> dict:
    return {
        "version": __version__,
        "driver": await svc.driver.health(),
        "capabilities": svc.driver.capabilities.__dict__,
        "accounts": svc.pool.stats(),
    }


def _cookies(raw: str | None, given: dict[str, str]) -> dict[str, str]:
    """Accept either parsed cookies or any raw browser export."""
    cookies = {k: v for k, v in given.items() if v}
    if raw:
        try:
            cookies.update(parse_any(raw))
        except CookieParseError as exc:
            raise InvalidRequest(str(exc)) from exc
    return cookies


@router.get("/accounts")
async def list_accounts(svc: Services = Depends(get_services)) -> dict:
    return {"data": [a.public() for a in svc.pool.all()]}


@router.post("/accounts")
async def create_account(body: AccountCreate, svc: Services = Depends(get_services)) -> dict:
    cookies = _cookies(body.cookies_raw, body.cookies)
    if not cookies:
        raise InvalidRequest("cookies must not be empty")
    acc = Account(label=body.label, cookies=cookies, cookie_expires=body.cookie_expires,
                  enabled=body.enabled)
    await svc.pool.upsert(acc)
    return {"account": acc.public(), "missing_cookies": missing_session_cookies(cookies)}


@router.patch("/accounts/{account_id}")
async def update_account(account_id: str, body: AccountUpdate,
                         svc: Services = Depends(get_services)) -> dict:
    acc = _get(svc, account_id)
    if body.label is not None:
        acc.label = body.label
    if body.enabled is not None:
        acc.enabled = body.enabled
    if body.cookies is not None or body.cookies_raw is not None:
        acc.cookies = _cookies(body.cookies_raw, body.cookies or {})
        # Fresh cookies give an invalid account another chance.
        acc.status = AccountStatus.ACTIVE
        acc.last_error = None
    if body.cookie_expires is not None:
        acc.cookie_expires = body.cookie_expires
    await svc.pool.upsert(acc)
    return {"account": acc.public()}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, svc: Services = Depends(get_services)) -> dict:
    if not await svc.pool.remove(account_id):
        raise NotFound(f"account '{account_id}' not found")
    return {"deleted": account_id}


@router.post("/accounts/{account_id}/reset")
async def reset_account(account_id: str, svc: Services = Depends(get_services)) -> dict:
    acc = _get(svc, account_id)
    acc.status, acc.cooldown_until, acc.last_error = AccountStatus.ACTIVE, 0.0, None
    await svc.pool.upsert(acc)
    return {"account": acc.public()}


@router.post("/accounts/{account_id}/renew")
async def renew(account_id: str, svc: Services = Depends(get_services)) -> dict:
    _get(svc, account_id)
    return await renew_account(svc.pool, svc.driver, account_id)


@router.get("/tasks")
async def list_tasks(limit: int = 100, svc: Services = Depends(get_services)) -> dict:
    return {"data": [t.model_dump(mode="json") for t in svc.tasks.list(limit)]}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, svc: Services = Depends(get_services)) -> dict:
    return {"cancelled": await svc.tasks.cancel(task_id)}


@router.get("/usage")
async def usage(since: float | None = None, svc: Services = Depends(get_services)) -> dict:
    if svc.usage is None:
        raise InvalidRequest("usage tracking needs MUSE2API_ACCOUNT_STORE=sqlite")
    return await svc.usage.summary(since)


@router.post("/keys")
async def create_key(body: KeyCreate, svc: Services = Depends(get_services)) -> dict:
    if svc.keys is None:
        raise InvalidRequest("key management needs MUSE2API_ACCOUNT_STORE=sqlite")
    key = "m2k-" + secrets.token_urlsafe(24)
    await svc.keys.add(key_hash(key), body.label)
    return {"key": key, "label": body.label}


@router.get("/keys")
async def list_keys(svc: Services = Depends(get_services)) -> dict:
    if svc.keys is None:
        raise InvalidRequest("key management needs MUSE2API_ACCOUNT_STORE=sqlite")
    return {"data": await svc.keys.list()}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str, svc: Services = Depends(get_services)) -> dict:
    if svc.keys is None:
        raise InvalidRequest("key management needs MUSE2API_ACCOUNT_STORE=sqlite")
    revoked = await svc.keys.revoke(key_id)
    if not revoked:
        raise NotFound(f"key '{key_id}' not found")
    return {"revoked": key_id}
