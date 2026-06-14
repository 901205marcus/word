from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import InboxMessage, MessageStatus
from .service import LineMVPService

BASE_DIR = Path(__file__).resolve().parents[2]
OUTPUT_DIR = BASE_DIR / "outputs"
service = LineMVPService(BASE_DIR)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="LINE Schedule MVP")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    messages = [_serialize_message(message) for message in service.list_messages()]
    return templates.TemplateResponse(
        request,
        "line_review.html",
        {
            "request": request,
            "messages": messages,
            "default_docx_path": os.getenv("SCHEDULE_DOCX_PATH", ""),
        },
    )


@app.post("/messages/create")
async def create_message(
    raw_text: str = Form(""),
    attachment: UploadFile | None = File(default=None),
):
    text = raw_text.strip()
    if attachment and attachment.filename:
        content = await attachment.read()
        if content:
            if text:
                item = service.create_manual_message(text)
                service.append_attachment_text(item.id, attachment.filename, content)
            else:
                service.create_message_from_attachment(attachment.filename, content)
            return RedirectResponse("/", status_code=303)
    if not text:
        raise HTTPException(status_code=400, detail="請輸入文字或上傳附件。")
    service.create_manual_message(text)
    return RedirectResponse("/", status_code=303)


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


@app.post("/review/{message_id}/attachment")
async def append_attachment(message_id: str, attachment: UploadFile = File(...)):
    if not attachment.filename:
        raise HTTPException(status_code=400, detail="請選擇附件。")
    content = await attachment.read()
    if not content:
        raise HTTPException(status_code=400, detail="附件是空的。")
    service.append_attachment_text(message_id, attachment.filename, content)
    return RedirectResponse("/", status_code=303)


@app.post("/review/{message_id}/apply")
async def apply_to_word(
    message_id: str,
    docx_path: str = Form(""),
    docx_file: UploadFile | None = File(default=None),
):
    uploaded_name = None
    uploaded_content = None
    if docx_file and docx_file.filename:
        uploaded_name = docx_file.filename
        uploaded_content = await docx_file.read()
    service.apply_to_word(
        message_id,
        docx_path=docx_path.strip() or None,
        uploaded_filename=uploaded_name,
        uploaded_content=uploaded_content,
    )
    return RedirectResponse("/", status_code=303)


def _serialize_message(message: InboxMessage) -> dict:
    raw = asdict(message)
    raw["status"] = message.status.value
    raw["actions"] = []
    for action in message.actions:
        action_raw = asdict(action)
        action_raw["action"] = action.action.value
        raw["actions"].append(action_raw)
    raw["output_url"] = _to_output_url(message.output_path)
    raw["preview_urls"] = [_to_output_url(path) for path in message.preview_paths if _to_output_url(path)]
    return raw


def _to_output_url(path: str) -> str:
    if not path:
        return ""
    candidate = Path(path)
    try:
        relative = candidate.relative_to(OUTPUT_DIR)
    except ValueError:
        return ""
    return f"/files/{relative.as_posix()}"
