import logging
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

from config import settings
from amo_client import AmoClient
from ai_agent import process_message, transcribe_voice, download_attachment
from db import ConversationDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="EliteDent Bot")
db = ConversationDB()
security = HTTPBasic()


def _require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest(credentials.password.encode(), settings.admin_password.encode())
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            headers={"WWW-Authenticate": "Basic"})
    return credentials


@app.get("/health")
async def health():
    return {"status": "ok"}


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EliteDent Bot — Інструкції</title>
<style>
  body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }}
  h1 {{ font-size: 1.4rem; color: #333; }}
  textarea {{ width: 100%; height: 500px; font-family: monospace; font-size: 14px; padding: 12px;
             border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; resize: vertical; }}
  button {{ margin-top: 12px; padding: 10px 28px; background: #2563eb; color: white;
            border: none; border-radius: 6px; font-size: 15px; cursor: pointer; }}
  button:hover {{ background: #1d4ed8; }}
  .msg {{ margin-top: 10px; padding: 8px 14px; border-radius: 5px; font-size: 14px; }}
  .ok {{ background: #d1fae5; color: #065f46; }}
  .err {{ background: #fee2e2; color: #991b1b; }}
</style>
</head>
<body>
<h1>EliteDent Bot — Інструкції для асистента</h1>
<form method="post" action="scenario">
  <textarea name="content">{content}</textarea>
  <br><button type="submit">Зберегти</button>
</form>
{msg}
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_get(_=Depends(_require_auth)):
    content = Path(settings.scenario_file).read_text(encoding="utf-8") \
        if Path(settings.scenario_file).exists() else ""
    return _ADMIN_HTML.format(content=content.replace("{", "{{").replace("}", "}}"), msg="")


@app.post("/admin/scenario", response_class=HTMLResponse)
async def admin_save(request: Request, _=Depends(_require_auth)):
    form = await request.form()
    content = form.get("content", "")
    try:
        Path(settings.scenario_file).write_text(content, encoding="utf-8")
        msg = '<div class="msg ok">✅ Збережено. Бот використовує нові інструкції.</div>'
    except Exception as e:
        msg = f'<div class="msg err">❌ Помилка: {e}</div>'
        content = ""
    return _ADMIN_HTML.format(content=content.replace("{", "{{").replace("}", "}}"), msg=msg)


@app.post("/webhook/chat")
async def webhook_chat(request: Request, background_tasks: BackgroundTasks):
    """amoCRM sends form-urlencoded POST when a new chat message arrives."""
    body = await request.form()
    data = dict(body)

    # Respond immediately — amoCRM expects fast reply
    background_tasks.add_task(_handle, data)
    return JSONResponse({"status": "ok"})


async def _handle(data: dict):
    entity_id_raw = data.get("message[add][0][entity_id]") or data.get("message[add][0][element_id]")
    if not entity_id_raw:
        return

    entity_id = int(entity_id_raw)
    entity_type = data.get("message[add][0][entity_type]", "lead")
    author_type = data.get("message[add][0][author][type]", "")
    chat_id = data.get("message[add][0][chat_id]", "")
    talk_id = data.get("message[add][0][talk_id]", "")
    contact_id = data.get("message[add][0][contact_id]", "")
    author_id = data.get("message[add][0][author][id]", "")
    element_type = data.get("message[add][0][element_type]", "")
    account_url = data.get("account[_links][self]", "").rstrip("/")
    account_id = data.get("account[id]", "")
    text = data.get("message[add][0][text]", "") or ""
    attachment_type = data.get("message[add][0][attachment][type]", "")
    attachment_link = data.get("message[add][0][attachment][link]", "")

    # Skip bot's own messages to prevent loops
    if author_type in ("bot", "user"):
        return

    if not account_url:
        logger.warning("No account URL in webhook payload")
        return

    amo = AmoClient(account_url)

    try:
        # Check robot_stop tag — if set, bot is silenced for this lead
        entity = await amo.get_entity(entity_type, entity_id)
        tags = entity.get("_embedded", {}).get("tags", [])
        if any(t.get("name") == "robot_stop" for t in tags):
            logger.info("robot_stop tag found for entity %s, skipping", entity_id)
            return

        # Transcribe voice message
        if attachment_type == "voice" and attachment_link:
            try:
                text = await transcribe_voice(attachment_link)
                logger.info("Transcribed voice for entity %s: %s", entity_id, text[:80])
            except Exception as e:
                logger.error("Voice transcription failed: %s", e)

        # Download image / file attachment
        image_data = None
        if attachment_link and attachment_type not in ("voice",):
            try:
                image_data = await download_attachment(attachment_link)
            except Exception as e:
                logger.error("Attachment download failed: %s", e)

        if not text and not image_data:
            return

        # Build conversation history
        history = db.get_history(entity_id)

        # Run AI agent
        result = await process_message(
            amo=amo,
            entity_id=entity_id,
            entity_type=entity_type,
            text=text,
            image_data=image_data,
            history=history,
        )

        if result is None:
            # Bot was stopped (need_help called) or AI returned nothing
            return

        message_to_client = result.get("messageToClient", "")
        pipeline_stage = result.get("pipeline_stage")
        is_spam = result.get("spam", False)

        # Save to conversation history
        db.add(entity_id, "user", text or "[вложение]")
        if message_to_client:
            db.add(entity_id, "assistant", message_to_client)

        if is_spam:
            await amo.update_lead(
                entity_id,
                {
                    "pipeline_id": settings.amo_pipeline_id,
                    "status_id": settings.amo_spam_status_id,
                    "loss_reason_id": settings.amo_spam_loss_reason_id,
                },
            )
            logger.info("Lead %s marked as spam", entity_id)
            return

        # Send reply to patient
        if message_to_client:
            await amo.send_message(
                chat_id=chat_id,
                talk_id=talk_id,
                contact_id=contact_id,
                author_id=author_id,
                entity_id=entity_id,
                element_type=element_type,
                account_id=account_id,
                text=message_to_client,
            )

        # Update pipeline stage
        if pipeline_stage:
            await amo.update_lead(
                entity_id,
                {
                    "pipeline_id": settings.amo_pipeline_id,
                    "status_id": int(pipeline_stage),
                },
            )

        logger.info(
            "Processed entity %s | stage=%s | reply=%s",
            entity_id, pipeline_stage, message_to_client[:60] if message_to_client else "",
        )

    except Exception:
        logger.exception("Unhandled error for entity %s", entity_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=False)
