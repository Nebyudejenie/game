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
    # CSS/locale/font files stay on the cheaper no-cache/304 pattern --
    # lower severity if briefly stale (a delayed style or translation,
    # not broken application logic), unlike index.html and .js below.
    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    async with httpx.AsyncClient() as client:
        css_response = await client.get(f"{http_base}/css/screens.css")

    assert css_response.status_code == 200
    assert css_response.headers.get("cache-control") == "no-cache", (
        f"{css_response.request.url}: expected a revalidating Cache-Control, "
        f"got {css_response.headers.get('cache-control')!r}"
    )
    # no-cache still allows a cheap 304 via these -- only the "serve a
    # stale copy without asking" behavior is what's being refused here.
    assert css_response.headers.get("etag")
    assert css_response.headers.get("last-modified")


async def test_index_html_and_js_are_never_conditionally_cached(gateway_server):
    # index.html and every .js file are the exception to the no-cache/304
    # pattern above -- a real production incident (a genuinely blank Mini
    # App: index.html served 304, then not a single script or stylesheet
    # request ever followed) traced to some client's own cached body for
    # "/" being stale or empty while its ETag still happened to match, so
    # revalidation "succeeded" against nothing to render. A second real
    # report -- a player's own client still running old application logic
    # the server had already fixed -- showed the identical WebView-cache-
    # corruption pattern applies just as easily to the actual code as to
    # the shell that loads it. no-store, and a repeat request carrying
    # the ETag this same response just handed back must never get a
    # bodyless 304 for either kind of file.
    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    async with httpx.AsyncClient() as client:
        for path in ("/", "/js/app.v6.js"):
            first = await client.get(f"{http_base}{path}")
            assert first.status_code == 200
            assert first.headers.get("cache-control") == "no-store", (
                f"{path}: expected this to never be conditionally cacheable, "
                f"got {first.headers.get('cache-control')!r}"
            )
            assert first.content, f"{path} must always come back with a real body"

            etag = first.headers.get("etag")
            assert etag, f"{path}: FileResponse should still set an ETag even though it's never honored for a 304"
            second = await client.get(f"{http_base}{path}", headers={"If-None-Match": etag})
            assert second.status_code == 200, (
                f"{path}: a repeat request with a matching ETag must still get a full body, "
                "never a 304 -- that's the exact bug this test guards against"
            )
            assert second.content == first.content
