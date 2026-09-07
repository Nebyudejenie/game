"""Delivery-node authentication: a completely separate credential space
from admin sessions (services/admin/auth.py) -- every node (a MacroDroid
device today, a future native agent or gateway tomorrow) is untrusted
external infrastructure, never handed an admin bearer token. See
packages/core/sms/nodes.py for why a per-node hashed credential replaces
the single shared MACRODROID_INGEST_TOKEN the existing (narrower) Telebirr
ingestion route uses.
"""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import Depends, Header, HTTPException, Request

from packages.core.sms.nodes import DeliveryNode, authenticate_node


async def current_node(request: Request, authorization: str = Header(default="")) -> DeliveryNode:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer node token")
    token = authorization[len("Bearer ") :]
    pool: asyncpg.Pool = request.app.state.pool
    node = await authenticate_node(pool, raw_token=token)
    if node is None:
        raise HTTPException(status_code=401, detail="invalid or revoked node credential")
    return node


CurrentNode = Annotated[DeliveryNode, Depends(current_node)]
