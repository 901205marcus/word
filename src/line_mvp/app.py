from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .models import MessageStatus
from .service import LineMVPService

BASE_DIR = Path(__file__).resolve().parents[2]
service = LineMVPService(BASE_DIR)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="LINE Schedule MVP")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    messages = service.list_messages()
    return templates.TemplateResponse(
        request,
        "line_review.html",
        {
            "request": request,
            "messages": [asdict(message) for message in messages],
            "default_docx_path": os.getenv("SCHEDULE_DOCX_PATH", ""),
        },
    )


@app.post("/webhook/line")
async def line_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not service.verify_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    payload = json.loads(body.decode("utf-8"))
    created = service.ingest_webhook(payload)
    return JSONResponse({"created": [message.id for message in created]})


@app.post("/review/{message_id}/refresh")
def refresh_parse(message_id: str):
    service.refresh_parse(message_id)
    return RedirectResponse("/", status_code=303)


@app.post("/review/{message_id}/status")
def update_status(message_id: str, status: str = Form(...)):
    try:
        service.mark_status(message_id, MessageStatus(status))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/", status_code=303)


@app.post("/review/{message_id}/apply")
def apply_to_word(message_id: str, docx_path: str = Form(...)):
    if not docx_path.strip():
        raise HTTPException(status_code=400, detail="docx_path is required")
    service.apply_to_word(message_id, docx_path.strip())
    return RedirectResponse("/", status_code=303)
