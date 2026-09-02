"""Cache-Control on the Mini App's static bundle (services/gateway/app.py's
_RevalidateStaticFiles) -- a real production incident, not a hypothetical:
plain StaticFiles sends no Cache-Control at all, and Cloudflare was
observed live inventing its own default (`max-age=14400`) in that gap,
meaning a returning player's browser could sit on a stale JS/CSS bundle
for up to 4 hours after a deploy with nothing in the origin's own logs
showing anything wrong. See DECISIONS.md's 2026-09-02 entry.

This only needs to prove the header the origin sends -- Cloudflare's own
edge behavior is out of reach from here, but an explicit origin
Cache-Control is what makes Cloudflare respect it instead of inventing
its own default.
"""

import httpx


async def test_static_miniapp_assets_are_served_with_a_revalidating_cache_header(gateway_server):
    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    async with httpx.AsyncClient() as client:
        index_response = await client.get(f"{http_base}/")
        js_response = await client.get(f"{http_base}/js/app.v6.js")

    for response in (index_response, js_response):
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-cache", (
            f"{response.request.url}: expected a revalidating Cache-Control, "
            f"got {response.headers.get('cache-control')!r}"
        )
        # no-cache still allows a cheap 304 via these -- only the "serve a
        # stale copy without asking" behavior is what's being refused here.
        assert response.headers.get("etag")
        assert response.headers.get("last-modified")
