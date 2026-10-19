"""Loopback-only human credential entry. Never expose through the public tunnel."""

import hmac
import json
import secrets
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from dots_phone.bws import private_write
from dots_phone.oauth import HASHER

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
csrf = secrets.token_urlsafe(32)


@app.get("/")
async def page():
    return HTMLResponse(
        f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Dots Phone secure local setup</title>
    <h1>Dots Phone secure local setup</h1><p>This loopback page stores the approved BWS runtime token in an owner-only file and creates the two application-secret import entries. Save your new owner password in your password manager. Secret values are never shown in responses.</p>
    <form method="post"><input type="hidden" name="csrf" value="{csrf}">
    <p><label>BWS dots-phone read-only access token <input name="token" type="password" required autocomplete="off"></label></p>
    <p><label>New owner password (20+ characters) <input name="password" type="password" minlength="20" required autocomplete="new-password"></label></p>
    <p><label>Repeat owner password <input name="repeat" type="password" required autocomplete="new-password"></label></p>
    <button>Save approved local setup</button></form></html>''',
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        },
    )


@app.post("/")
async def save(request: Request):
    if request.headers.get("origin") != "http://127.0.0.1:8765" or len(await request.body()) > 8192:
        return Response(status_code=403)
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf", "")), csrf):
        return Response(status_code=403)
    password, token = str(form.get("password", "")), str(form.get("token", "")).strip()
    if len(password) < 20 or password != form.get("repeat") or not token:
        return HTMLResponse("Password mismatch, short password, or missing token.", status_code=400)
    target = Path(".secrets/application-import.json")
    if target.exists() or Path(".secrets/bws-access-token").exists():
        return HTMLResponse(
            "Setup files already exist; no overwrite or rotation performed.", status_code=409
        )
    payload = {
        "projects": [],
        "secrets": [
            {
                "key": "DOTS_PHONE_DATA_ENCRYPTION_KEY",
                "value": Fernet.generate_key().decode(),
                "note": "Fernet: 32 random bytes, URL-safe base64. Preserve for existing data.",
                "projectIds": [],
            },
            {
                "key": "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH",
                "value": HASHER.hash(password),
                "note": "Argon2id owner password hash; plaintext password stays in your password manager.",
                "projectIds": [],
            },
        ],
    }
    for item in payload["secrets"]:
        item["id"] = str(uuid4())
    private_write(target, json.dumps(payload))
    private_write(".secrets/bws-access-token", token)
    return HTMLResponse(
        "Saved owner-only setup files. Reply “setup saved” in Codex. Do not send any secret values.",
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        access_log=False,
        log_level="critical",
        proxy_headers=False,
    )
