import json
import time
import logging
from pathlib import Path

import httpx

from config import settings

logger = logging.getLogger(__name__)


class AmoClient:
    """amoCRM REST API + amojo chat client with automatic token refresh."""

    def __init__(self, account_url: str):
        self.account_url = account_url.rstrip("/")
        self._tokens = self._load_tokens()

    # ── Token management ────────────────────────────────────────────────────

    def _load_tokens(self) -> dict:
        p = Path(settings.tokens_file)
        if p.exists():
            return json.loads(p.read_text())
        return {}

    def _save_tokens(self, tokens: dict):
        Path(settings.tokens_file).write_text(json.dumps(tokens, indent=2))
        self._tokens = tokens

    async def _refresh(self):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self.account_url}/oauth2/access_token",
                json={
                    "client_id": settings.amo_client_id,
                    "client_secret": settings.amo_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self._tokens.get("refresh_token"),
                    "redirect_uri": settings.amo_redirect_uri,
                },
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            self._save_tokens(r.json())
            logger.info("amoCRM token refreshed")

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._tokens.get('access_token')}"}

    # ── Generic HTTP helpers with auto-refresh ───────────────────────────────

    async def _get(self, path: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.account_url}{path}", headers=self._auth())
            if r.status_code == 401:
                await self._refresh()
                r = await c.get(f"{self.account_url}{path}", headers=self._auth())
            r.raise_for_status()
            return r.json()

    async def _patch(self, path: str, body: list | dict) -> dict:
        headers = {**self._auth(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.patch(f"{self.account_url}{path}", json=body, headers=headers)
            if r.status_code == 401:
                await self._refresh()
                headers = {**self._auth(), "Content-Type": "application/json"}
                r = await c.patch(f"{self.account_url}{path}", json=body, headers=headers)
            r.raise_for_status()
            return r.json() if r.content else {}

    async def _post_json(self, path: str, body: list | dict) -> dict:
        headers = {**self._auth(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.account_url}{path}", json=body, headers=headers)
            if r.status_code == 401:
                await self._refresh()
                headers = {**self._auth(), "Content-Type": "application/json"}
                r = await c.post(f"{self.account_url}{path}", json=body, headers=headers)
            r.raise_for_status()
            return r.json() if r.content else {}

    # ── amoCRM REST API ──────────────────────────────────────────────────────

    async def get_entity(self, entity_type: str, entity_id: int) -> dict:
        return await self._get(f"/api/v4/{entity_type}s/{entity_id}")

    async def update_lead(self, lead_id: int, data: dict):
        await self._patch("/api/v4/leads", [{"id": lead_id, **data}])

    async def update_custom_fields(self, entity_type: str, entity_id: int, fields: list[dict]):
        """fields = [{"field_id": 123, "value": "text"}]"""
        await self._patch(
            f"/api/v4/{entity_type}s",
            [{
                "id": entity_id,
                "custom_fields_values": [
                    {"field_id": f["field_id"], "values": [{"value": f["value"]}]}
                    for f in fields
                ],
            }],
        )

    async def add_tag(self, entity_type: str, entity_id: int, tag: str):
        await self._patch(
            f"/api/v4/{entity_type}s",
            [{"id": entity_id, "tags_to_add": [{"name": tag}]}],
        )

    async def create_task(self, entity_type: str, entity_id: int, text: str):
        await self._post_json(
            "/api/v4/tasks",
            [{
                "text": text,
                "complete_till": int(time.time()),
                "entity_type": f"{entity_type}s",
                "entity_id": entity_id,
            }],
        )

    # ── amojo chat (send message) ────────────────────────────────────────────

    async def get_chat_session(self) -> dict:
        """Returns amojo session dict with access_token and account.id."""
        headers = {
            **self._auth(),
            "X-Requested-With": "XMLHttpRequest",
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self.account_url}/ajax/v1/chats/session",
                data={"request[chats][session][action]": "create"},
                headers=headers,
            )
            if r.status_code == 401:
                await self._refresh()
                headers = {**self._auth(), "X-Requested-With": "XMLHttpRequest"}
                r = await c.post(
                    f"{self.account_url}/ajax/v1/chats/session",
                    data={"request[chats][session][action]": "create"},
                    headers=headers,
                )
            r.raise_for_status()
            return r.json()["response"]["chats"]["session"]

    async def send_message(
        self,
        *,
        chat_id: str,
        talk_id: str,
        contact_id: str,
        author_id: str,
        entity_id: int,
        element_type: str,
        account_id: str,
        text: str,
    ):
        session = await self.get_chat_session()
        amojo_account_id = session["account"]["id"]
        chat_token = session["access_token"]
        persona_name = session["user"]["name"]

        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://amojo.amocrm.ru/v1/chats/{amojo_account_id}/{chat_id}/messages",
                data={
                    "silent": "false",
                    "priority": "low",
                    "crm_entity[id]": str(entity_id),
                    "crm_entity[type]": element_type,
                    "persona_name": persona_name,
                    "persona_avatar": "",
                    "text": text,
                    "recipient_id": author_id,
                    "crm_dialog_id": talk_id,
                    "crm_contact_id": contact_id,
                    "crm_account_id": account_id,
                    "skip_link_shortener": "false",
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Auth-Token": chat_token,
                    "chatId": chat_id,
                },
            )
            r.raise_for_status()
