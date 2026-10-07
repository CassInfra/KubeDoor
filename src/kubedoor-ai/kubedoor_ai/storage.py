from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import asyncpg
from psycopg.conninfo import make_conninfo

from .domain import AIError, Identity, redact


def plain(row):
    if row is None:
        return None
    if isinstance(row, (dict, asyncpg.Record)):
        return {k: plain(v) for k, v in dict(row).items()}
    if isinstance(row, (list, tuple)):
        return [plain(v) for v in row]
    if isinstance(row, uuid.UUID):
        return str(row)
    if isinstance(row, datetime):
        return row.isoformat()
    return row


def pg_options() -> dict:
    return {"host": os.getenv("PG_HOST", "localhost"), "port": int(os.getenv("PG_PORT", "5432")),
            "user": os.getenv("PG_USER", "kubedoor"), "password": os.getenv("PG_PASSWORD", ""),
            "database": os.getenv("PG_DATABASE", "kubedoor")}


def pg_dsn() -> str:
    options = pg_options()
    options["dbname"] = options.pop("database")
    return make_conninfo(**options)


class Store:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def open(cls):
        async def codec(conn):
            await conn.set_type_codec("jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads)
        pool = await asyncpg.create_pool(**pg_options(), min_size=1, max_size=10, command_timeout=30, init=codec)
        store = cls(pool)
        await pool.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        return store

    async def fetch(self, sql, *args):
        return plain(await self.pool.fetch(sql, *args))

    async def one(self, sql, *args):
        return plain(await self.pool.fetchrow(sql, *args))

    async def execute(self, sql, *args):
        return await self.pool.execute(sql, *args)

    async def session(self, sid: str, identity: Identity):
        row = await self.one("SELECT * FROM kubedoor_ai_sessions WHERE id=$1::uuid AND username=$2", sid, identity.username)
        if not row:
            raise AIError("会话不存在", 404, "not_found")
        return row

    async def run(self, rid: str, identity: Identity):
        row = await self.one("SELECT * FROM kubedoor_ai_runs WHERE id=$1::uuid AND username=$2", rid, identity.username)
        if not row:
            raise AIError("任务不存在", 404, "not_found")
        return row

    async def event(self, rid: str, kind: str, data: dict):
        async with self.pool.acquire() as conn, conn.transaction():
            seq = await conn.fetchval("UPDATE kubedoor_ai_runs SET event_seq=event_seq+1,updated_at=now() WHERE id=$1::uuid RETURNING event_seq", rid)
            if seq is None:
                return None
            clean = redact(data)
            await conn.execute("INSERT INTO kubedoor_ai_events(run_id,seq,type,data) VALUES($1::uuid,$2,$3,$4)", rid, seq, kind, clean)
            return {"seq": seq, "type": kind, "data": clean}

    async def status(self, rid: str, status: str, error: str | None = None):
        await self.execute("UPDATE kubedoor_ai_runs SET status=$2,error=$3,updated_at=now() WHERE id=$1::uuid", rid, status, error)
        await self.event(rid, "status", {"status": status, "error": error})

    async def message(self, run: dict, role: str, content: str, tools=None, memories=None):
        if tools:
            tools = [{**item, "preview": {k: v for k, v in item.get("preview", {}).items() if k != "credential_fingerprint"}} for item in tools]
        tools, content, memories = redact(tools), redact(content), redact(memories or [])
        row = await self.one("INSERT INTO kubedoor_ai_messages(id,session_id,run_id,role,content,scope,tools,memories) VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8) RETURNING *", str(uuid.uuid4()), run["session_id"], run["id"], role, content, run["scope"], tools, memories)
        await self.execute("UPDATE kubedoor_ai_sessions SET updated_at=now() WHERE id=$1::uuid", run["session_id"])
        return row

    async def connection(self, env):
        return await self.one("SELECT * FROM kubedoor_ai_connections WHERE env=$1", env)

    async def summaries(self):
        return await self.fetch("SELECT env,true AS configured,context,test_status,tested_at,revision FROM kubedoor_ai_connections ORDER BY env")

    async def pending(self, rid):
        rows = await self.fetch("SELECT * FROM kubedoor_ai_actions WHERE run_id=$1::uuid AND state='pending' ORDER BY created_at", rid)
        return redact([{"action_id": r["id"], "name": r["operation"], "tool": r["operation"], "source": r["source"], "arguments": r["arguments"], "preview": {k: v for k, v in r["preview"].items() if k != "credential_fingerprint"}, "diff": r["preview"].get("diff"), "command": r["preview"].get("command"), "expires_at": r["expires_at"]} for r in rows])

    async def recover(self):
        """A dispatched mutation is uncertain after a crash, never replay it."""
        await self.execute("UPDATE kubedoor_ai_actions SET state='unknown',result=$1,updated_at=now() WHERE state='dispatching'", {"success": False, "error": {"code": "outcome_unknown", "message": "服务重启时请求已经发出，请查询实际资源状态；不会自动重试。"}})
        rows = await self.fetch("UPDATE kubedoor_ai_runs SET status='failed',error='服务重启，执行结果需要重新核验',updated_at=now() WHERE status='running' RETURNING id")
        for row in rows:
            await self.event(row["id"], "error", {"code": "runtime_restarted", "message": "服务重启，任务已停止；已提交修改请重新核验。"})
            await self.event(row["id"], "done", {"status": "failed"})

    async def close(self):
        await self.pool.close()
