"""Access gate for the isolated migration review site."""
import base64
import hashlib
import hmac
import os
from urllib.parse import urlsplit
from starlette.responses import PlainTextResponse

async def migration_access(request, call_next):
    if request.method == "GET" and request.url.path == "/api/health":
        return await call_next(request)
    expected = os.getenv("MIGRATION_ACCESS_CREDENTIAL_SHA256", "")
    if os.getenv("MIGRATION_ACCESS_ENABLED") != "1" and not expected:
        return await call_next(request)
    headers = {"Cache-Control": "private, no-store"}
    if len(expected) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected):
        return PlainTextResponse("Access is not configured", 503, headers=headers)
    credential = b""
    try:
        scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
        if scheme.lower() == "basic":
            credential = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        pass
    if b":" not in credential or not hmac.compare_digest(hashlib.sha256(credential).hexdigest(), expected.lower()):
        headers["WWW-Authenticate"] = 'Basic realm="Migration Review", charset="UTF-8"'
        return PlainTextResponse("Authentication required", 401, headers=headers)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin"):
        origin = urlsplit(request.headers["origin"])
        if origin.scheme not in {"http", "https"} or origin.netloc != request.headers.get("host"):
            return PlainTextResponse("Forbidden origin", 403, headers=headers)
    response = await call_next(request)
    response.headers.update(headers)
    return response
