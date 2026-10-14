"""Shared helpers for the department mock services.

Generates a FastAPI app exposing generic CRUD over a set of seeded SQLite tables.
Each dept service stays a thin module: schema + seed data + port.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings


def _init_schema(conn: sqlite3.Connection, schema: dict[str, list[str]], seed: dict[str, list[dict]]) -> None:
    for table, cols in schema.items():
        ddl = []
        for col in cols:
            if col == "id":
                ddl.append('"id" INTEGER PRIMARY KEY')
            else:
                ddl.append(f'"{col}"')
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(ddl)})')
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if count == 0:
            for row in seed.get(table, []):
                keys = [k for k in row if k in cols]
                qcols = ", ".join(f'"{k}"' for k in keys)
                placeholders = ", ".join("?" * len(keys))
                conn.execute(
                    f'INSERT INTO "{table}" ({qcols}) VALUES ({placeholders})',
                    [row[k] for k in keys],
                )
    conn.commit()


def _connect(db_path: Path, schema: dict[str, list[str]], seed: dict[str, list[dict]]) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _init_schema(conn, schema, seed)
    return conn


def make_app(
    service_name: str,
    schema: dict[str, list[str]],
    seed: dict[str, list[dict]],
    db_path: Path | None = None,
) -> FastAPI:
    if db_path is None:
        slug = service_name.lower().replace(" ", "_")
        db_path = get_settings().services.data_dir / f"{slug}.sqlite"

    app = FastAPI(title=service_name, version="0.1.0")

    @app.middleware("http")
    async def _require_service_auth(request: Request, call_next):
        """Mk18 Phase C: dept services accept only the operator's own JWT.

        `/health` stays open (container healthchecks); every table route needs
        a valid, active-member Bearer token — the same JWT the gateway verified,
        forwarded by `ToolExecutor`. Anyone reaching the port directly without
        a token gets 401, never data.
        """
        if request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or not auth.split(" ", 1)[1].strip():
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)
        token = auth.split(" ", 1)[1].strip()
        try:
            from app.auth.jwt import verify_token

            claims = verify_token(
                token, get_settings().jwt.secret, get_settings().jwt.algorithm
            )
        except Exception:  # noqa: BLE001
            return JSONResponse({"detail": "invalid token"}, status_code=401)
        try:
            from app.auth.users import get_store

            member = get_store().get(str(claims.get("sub", "")))
        except Exception:  # noqa: BLE001
            member = None
        if member is None or not member.active:
            return JSONResponse({"detail": "account disabled or not a member"}, status_code=403)
        request.state.claims = claims
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": service_name}

    for table, cols in schema.items():
        _add_table_routes(app, table, cols, db_path, schema, seed)

    return app


def _add_table_routes(
    app: FastAPI,
    table: str,
    cols: list[str],
    db_path: Path,
    schema: dict[str, list[str]],
    seed: dict[str, list[dict]],
) -> None:
    """Register GET/POST/DELETE CRUD routes for one table.

    `table`/`cols` are closure variables so they never appear as FastAPI
    endpoint parameters (a bare `list[str]` parameter is misread by FastAPI
    as a body parameter).
    """

    searchable = [c for c in cols if c != "id"]

    @app.get(f"/{table}", name=f"list_{table}")
    def list_rows(q: str | None = None, limit: int = 50) -> list[dict]:
        conn = _connect(db_path, schema, seed)
        try:
            if q and searchable:
                like = f"%{q}%"
                where = " OR ".join(f'CAST("{c}" AS TEXT) LIKE ?' for c in searchable)
                params: list[Any] = [like] * len(searchable) + [limit]
                rows = conn.execute(
                    f'SELECT * FROM "{table}" WHERE {where} ORDER BY id LIMIT ?', params
                ).fetchall()
            else:
                rows = conn.execute(
                    f'SELECT * FROM "{table}" ORDER BY id LIMIT ?', (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @app.get(f"/{table}/{{row_id}}", name=f"get_{table}")
    def get_row(row_id: int) -> dict:
        conn = _connect(db_path, schema, seed)
        try:
            row = conn.execute(
                f'SELECT * FROM "{table}" WHERE id = ?', (row_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail=f"{table} {row_id} not found")
        return dict(row)

    @app.post(f"/{table}", name=f"create_{table}")
    async def create_row(request: Request) -> dict:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        allowed = [c for c in cols if c != "id"]
        bad = [k for k in body if k not in allowed]
        if bad:
            raise HTTPException(status_code=422, detail=f"unknown fields: {bad}")
        data = {k: body[k] for k in allowed if k in body}
        conn = _connect(db_path, schema, seed)
        try:
            keys = list(data.keys())
            qcols = ", ".join(f'"{k}"' for k in keys)
            placeholders = ", ".join("?" * len(keys))
            cur = conn.execute(
                f'INSERT INTO "{table}" ({qcols}) VALUES ({placeholders})',
                [data[k] for k in keys],
            )
            conn.commit()
            row = conn.execute(
                f'SELECT * FROM "{table}" WHERE id = ?', (cur.lastrowid,)
            ).fetchone()
        finally:
            conn.close()
        return dict(row)

    @app.delete(f"/{table}/{{row_id}}", name=f"delete_{table}")
    def delete_row(row_id: int) -> dict:
        conn = _connect(db_path, schema, seed)
        try:
            cur = conn.execute(f'DELETE FROM "{table}" WHERE id = ?', (row_id,))
            conn.commit()
        finally:
            conn.close()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"{table} {row_id} not found")
        return {"deleted": True, "id": row_id}
