import json
import re
import base64
import tempfile
import os
import logging
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from config import settings
from amo_client import AmoClient

logger = logging.getLogger(__name__)

_openai = AsyncOpenAI(
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
)

_scenario = (
    Path(settings.scenario_file).read_text(encoding="utf-8")
    if Path(settings.scenario_file).exists()
    else ""
)

SYSTEM_PROMPT = f"""Ты — ИИ-ассистент стоматологической клиники EliteDent (Тбилиси).
Квалифицируешь пациентов, собираешь данные, записываешь в amoCRM.

## Сценарий квалификации:
{_scenario}

## Статусы воронки (pipeline_stage):
- 73422594 — Неквалифицированный (новый лид)
- 66764618 — Квалифицирован (интерес + целевая услуга + снимок есть)
- 73197942 — Делает снимок (интерес + целевая услуга + снимка нет)
- 82386522 — Снимок получен (клиент загрузил файл)
- 66764678 — Билеты получены (даты прилета известны)
- 66764674 — Прогрев
- 81626199 — Закрыто (отказ или нецелевой)

## Правила:
1. Строго следуй сценарию квалификации
2. Жди полного ответа перед следующим вопросом
3. Определи язык по первому сообщению (русский или иврит)
4. При получении фото паспорта — сразу вызови инструмент сохранения
5. Если не можешь помочь или пациент просит оператора — вызови need_help

## ОБЯЗАТЕЛЬНО: финальный ответ ТОЛЬКО в JSON:
{{
  "pipeline_stage": "номер стадии",
  "messageToClient": "сообщение пациенту на его языке",
  "whatdoyouwant": "что хочет пациент",
  "howmuch": "бюджет если озвучен иначе пусто",
  "spam": false
}}
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_passport_id",
            "description": "Сохраняет номер паспорта / теудат зеут пациента в amoCRM",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "Номер паспорта или теудат зеут"},
                },
                "required": ["value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_passport_full",
            "description": "Сохраняет полные паспортные данные пациента в amoCRM",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "Все паспортные данные одной строкой"},
                },
                "required": ["value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "need_help",
            "description": "Передаёт пациента живому менеджеру и останавливает бота. "
                           "Вызывай только когда пациент явно просит оператора или бот не может помочь.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Причина передачи"},
                },
                "required": ["reason"],
            },
        },
    },
]


async def transcribe_voice(url: str) -> str:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        audio = r.content

    suffix = ".ogg"
    if ".mp3" in url:
        suffix = ".mp3"
    elif ".m4a" in url:
        suffix = ".m4a"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio)
        tmp = f.name

    try:
        with open(tmp, "rb") as f:
            result = await _openai.audio.transcriptions.create(
                model=settings.whisper_model,
                file=f,
                language="ru",
            )
        return result.text
    finally:
        os.unlink(tmp)


async def download_attachment(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    # Strip markdown code fences
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except Exception:
                pass
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


async def _run_tool(
    name: str,
    args: dict,
    amo: AmoClient,
    entity_id: int,
    entity_type: str,
) -> tuple[str, bool]:
    """Returns (result_text, stop_bot)."""
    try:
        if name == "save_passport_id":
            await amo.update_custom_fields(
                entity_type, entity_id,
                [{"field_id": 1168511, "value": args["value"]}],
            )
            return "Номер паспорта сохранен", False

        if name == "save_passport_full":
            await amo.update_custom_fields(
                entity_type, entity_id,
                [{"field_id": 1168513, "value": args["value"]}],
            )
            return "Паспортные данные сохранены", False

        if name == "need_help":
            await amo.create_task(entity_type, entity_id, f"Нужна помощь менеджера: {args['reason']}")
            await amo.add_tag(entity_type, entity_id, "robot_stop")
            return "Менеджер уведомлён, бот остановлен", True

    except Exception as e:
        logger.error("Tool %s error: %s", name, e)
        return f"Ошибка: {e}", False

    return "OK", False


async def process_message(
    *,
    amo: AmoClient,
    entity_id: int,
    entity_type: str,
    text: str,
    image_data: bytes | None,
    history: list[dict],
) -> dict | None:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)

    if image_data:
        b64 = base64.b64encode(image_data).decode()
        user_content = [
            {"type": "text", "text": text or "Распознай документ"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    else:
        user_content = text

    messages.append({"role": "user", "content": user_content})

    for _ in range(8):
        response = await _openai.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            tools=TOOLS,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        choice = response.choices[0]
        msg = choice.message

        # Append assistant message to context
        assistant_entry: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_entry["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        messages.append(assistant_entry)

        if choice.finish_reason == "tool_calls" and msg.tool_calls:
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result, stop = await _run_tool(tc.function.name, args, amo, entity_id, entity_type)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
                if stop:
                    return None
            continue

        # finish_reason == "stop" → parse JSON from content
        if msg.content:
            parsed = _parse_json(msg.content)
            if parsed:
                return parsed
            # Fallback: AI returned plain text — send it as-is
            logger.warning("Could not parse AI response as JSON, using as plain text: %s", msg.content[:300])
            return {"messageToClient": msg.content, "pipeline_stage": None, "spam": False}

        break

    return None
