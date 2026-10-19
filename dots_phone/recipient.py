"""Synthetic-only local callback receiver. It is not a live telephony proof."""

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/callback")
async def receive(request: Request):
    value = await request.json()
    return (
        {"challenge": value.get("challenge")}
        if value.get("type") == "verification"
        else {"received": True}
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
