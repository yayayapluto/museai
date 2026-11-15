"""Multi-tenant storage and rate limiting: SQLite stores, DB-backed keys, usage."""

from __future__ import annotations

import asyncio

import pytest

from museai.accounts.model import Account
from museai.accounts.sqlite_store import SqliteAccountStore, SqliteKeyStore, SqliteUsageStore


def run(coro):
    return asyncio.run(coro)


def test_account_store_roundtrip(tmp_path):
    store = SqliteAccountStore(tmp_path / "museai.db")
    accounts = [Account(label="one", cookies={"hatch_sess": "a"}), Account(label="two")]
    run(store.save(accounts))

    loaded = {a.label: a for a in run(store.load())}
    assert set(loaded) == {"one", "two"}
    assert loaded["one"].cookies == {"hatch_sess": "a"}

    run(store.save(accounts[:1]))
    assert len(run(store.load())) == 1


def test_key_store_revokes(tmp_path):
    store = SqliteKeyStore(tmp_path / "museai.db")
    run(store.add("hash-1", "laptop"))
    assert run(store.exists("hash-1")) is True
    listed = run(store.list())
    assert listed[0]["label"] == "laptop" and listed[0]["revoked"] is False

    assert run(store.revoke("hash-1")) is True
    assert run(store.exists("hash-1")) is False
    assert run(store.revoke("missing")) is False


def test_usage_summary_aggregates(tmp_path):
    store = SqliteUsageStore(tmp_path / "museai.db")
    for _ in range(2):
        run(store.record(key_hash="k" * 64, route="/v1/chat/completions", model="muse-chat",
                         prompt_tokens=5, completion_tokens=7))
    run(store.record(key_hash="k" * 64, route="/v1/responses", account_id="acc_1",
                     prompt_tokens=3, completion_tokens=1))

    summary = run(store.summary())
    assert summary["requests"] == 3
    assert summary["prompt_tokens"] == 13
    assert summary["completion_tokens"] == 15
    assert summary["by_key"][0]["requests"] == 3
    assert {row["account_id"] for row in summary["by_account"]} == {"acc_1", "-"}


def test_rate_limit_bucket_rejects_over_limit():
    from museai.api import deps
    from museai.errors import Muse2APIError

    deps._buckets.clear()
    for _ in range(3):
        deps._limit("bucket-test", 3)
    with pytest.raises(Muse2APIError) as exc:
        deps._limit("bucket-test", 3)
    assert exc.value.status_code == 429
    deps._buckets.clear()
