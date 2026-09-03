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
        js_response = await client.get(f"{http_base}/js/app.v6.js")

    assert js_response.status_code == 200
    assert js_response.headers.get("cache-control") == "no-cache", (
        f"{js_response.request.url}: expected a revalidating Cache-Control, "
        f"got {js_response.headers.get('cache-control')!r}"
    )
    # no-cache still allows a cheap 304 via these -- only the "serve a
    # stale copy without asking" behavior is what's being refused here.
    assert js_response.headers.get("etag")
    assert js_response.headers.get("last-modified")


async def test_index_html_is_never_conditionally_cached(gateway_server):
    # index.html is the one exception to the no-cache/304 pattern above --
    # a real production incident (a genuinely blank Mini App: index.html
    # served 304, then not a single script or stylesheet request ever
    # followed) traced to some client's own cached body for "/" being
    # stale or empty while its ETag still happened to match, so
    # revalidation "succeeded" against nothing to render. no-store, and a
    # repeat request carrying the ETag this same response just handed
    # back must never get a bodyless 304 for it.
    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    async with httpx.AsyncClient() as client:
        first = await client.get(f"{http_base}/")
        assert first.status_code == 200
        assert first.headers.get("cache-control") == "no-store", (
            f"expected index.html to never be conditionally cacheable, "
            f"got {first.headers.get('cache-control')!r}"
        )
        assert first.content, "index.html must always come back with a real body"

        etag = first.headers.get("etag")
        assert etag, "FileResponse should still set an ETag even though it's never honored for a 304"
        second = await client.get(f"{http_base}/", headers={"If-None-Match": etag})
        assert second.status_code == 200, (
            "a repeat request with a matching ETag must still get a full body, never a 304 -- "
            "that's the exact bug this test guards against"
        )
        assert second.content == first.content
