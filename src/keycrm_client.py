import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib import parse, request
from urllib.error import HTTPError

from .util import normalize_list_name


@dataclass
class Lead:
    """Картка воронки KeyCRM; lead_id = id картки з /pipelines/cards."""

    lead_id: str
    name: str
    phone: str
    status_id: str
    email: str = ""
    client_name: str = ""
    status_name: str = ""
    manager_name: str = ""
    contact_id: str = ""  # id контакту в KeyCRM для оновлення телефону/email/імені


@dataclass
class KeyCRMNote:
    note_id: str
    text: str
    author: str
    created_at: str


class KeyCRMClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        pipeline_id: int,
        status_id_to_list_name: Dict[str, str],
        timeout_sec: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.pipeline_id = pipeline_id
        self.timeout_sec = timeout_sec
        self._label_to_status_id = {
            normalize_list_name(label): sid.strip()
            for sid, label in status_id_to_list_name.items()
            if label
        }

    def fetch_leads(self) -> List[Lead]:
        """Отримує картки воронки (KeyCRM OpenAPI: GET /pipelines/cards)."""
        url = f"{self.base_url}/pipelines/cards"
        limit = 50
        page = 1
        max_pages = 500
        leads: List[Lead] = []

        while page <= max_pages:
            query: Dict[str, str] = {
                "limit": str(limit),
                "page": str(page),
                "include": "contact,contact.client,status,manager",
                "filter[pipeline_id]": str(self.pipeline_id),
            }
            payload = self._get_json(url, query)
            items = payload.get("data", payload if isinstance(payload, list) else [])
            if not isinstance(items, list):
                items = []

            for row in items:
                if not self._belongs_to_pipeline(row):
                    continue
                lead = self._row_to_lead(row)
                if lead:
                    leads.append(lead)

            if len(items) < limit:
                break
            page += 1

        return leads

    def update_lead_status(self, lead_id: str, new_status_id: str) -> None:
        """Оновлює статус картки воронки в KeyCRM (KeyCRM підтримує лише PUT)."""
        url = f"{self.base_url}/pipelines/cards/{lead_id}"
        self._put_json(url, {"status_id": int(new_status_id)})

    def update_lead_title(self, lead_id: str, title: str) -> None:
        """Оновлює назву картки воронки в KeyCRM."""
        url = f"{self.base_url}/pipelines/cards/{lead_id}"
        self._put_json(url, {"title": title})

    def update_lead_contact(self, lead_id: str, **fields) -> bool:
        """
        Оновлює дані контакту через endpoint pipeline card.
        PUT /pipelines/cards/{lead_id} with {"contact": {...}}
        Підтримувані поля: full_name, email, phone.
        """
        if not lead_id:
            return False
        contact_payload: Dict[str, Any] = {}
        if "phone" in fields and fields["phone"]:
            phone = str(fields["phone"]).strip()
            # Додаємо "+" якщо рядок з лише цифр (нормалізований Trello number field)
            if phone.isdigit():
                phone = "+" + phone
            contact_payload["phone"] = phone
        if "email" in fields and fields["email"]:
            contact_payload["email"] = str(fields["email"]).strip()
        if "full_name" in fields and fields["full_name"]:
            contact_payload["full_name"] = str(fields["full_name"]).strip()
        if not contact_payload:
            return False
        url = f"{self.base_url}/pipelines/cards/{lead_id}"
        try:
            self._put_json(url, {"contact": contact_payload})
            return True
        except HTTPError:
            return False

    def fetch_lead_notes(self, lead_id: str) -> List[KeyCRMNote]:
        """Отримує нотатки (коментарі) картки воронки."""
        url = f"{self.base_url}/pipelines/cards/{lead_id}/notes"
        try:
            payload = self._get_json(url)
        except HTTPError:
            url2 = f"{self.base_url}/notes"
            try:
                payload = self._get_json(url2, {
                    "filter[resource_type]": "lead",
                    "filter[resource_id]": str(lead_id),
                })
            except HTTPError:
                return []
        items = payload.get("data", payload if isinstance(payload, list) else [])
        if not isinstance(items, list):
            return []
        notes = []
        for row in items:
            note_id = str(row.get("id", "")).strip()
            text = str(row.get("body") or row.get("text") or row.get("content") or "").strip()
            created_at = str(row.get("created_at") or "").strip()
            author = ""
            mgr = row.get("manager") or row.get("author") or row.get("user")
            if isinstance(mgr, dict):
                parts = [
                    str(mgr.get("first_name") or "").strip(),
                    str(mgr.get("last_name") or "").strip(),
                ]
                author = " ".join(p for p in parts if p) or str(mgr.get("name") or mgr.get("full_name") or "")
            elif isinstance(mgr, str):
                author = mgr
            if note_id and text:
                notes.append(KeyCRMNote(
                    note_id=note_id,
                    text=text,
                    author=author,
                    created_at=created_at,
                ))
        return notes

    def add_lead_note(self, lead_id: str, text: str) -> Optional[str]:
        """Додає нотатку до картки воронки. Повертає note_id або None."""
        url = f"{self.base_url}/pipelines/cards/{lead_id}/notes"
        try:
            result = self._post_json(url, {"body": text})
        except HTTPError:
            url2 = f"{self.base_url}/notes"
            try:
                result = self._post_json(url2, {
                    "resource_type": "lead",
                    "resource_id": int(lead_id),
                    "body": text,
                })
            except HTTPError:
                return None
        return str(result.get("id", "")).strip() or None

    def _belongs_to_pipeline(self, row: Dict[str, Any]) -> bool:
        pid = _extract_pipeline_id(row)
        if pid is None:
            return True
        return int(pid) == int(self.pipeline_id)

    def _row_to_lead(self, row: Dict[str, Any]) -> Optional[Lead]:
        lead_id = str(row.get("id", "")).strip()
        name = str(row.get("title") or row.get("name") or "").strip()
        phone = _extract_phone(row)
        email, client_name = _extract_email_and_client_name(row)
        status_id = _extract_status_id(row)
        status_name = _extract_status_label(row)
        manager_name = _extract_manager_name(row)
        contact_id = _extract_contact_id(row)
        if not status_id:
            label = _extract_status_label(row)
            if label:
                status_id = self._label_to_status_id.get(normalize_list_name(label), "")

        if not lead_id:
            return None
        return Lead(
            lead_id=lead_id,
            name=name,
            phone=phone,
            email=email,
            client_name=client_name,
            status_id=str(status_id).strip(),
            status_name=status_name,
            manager_name=manager_name,
            contact_id=contact_id,
        )

    def _get_json(self, url: str, query: Optional[Dict[str, str]] = None) -> Dict:
        query = dict(query or {})
        if query:
            url = f"{url}?{parse.urlencode(query)}"
        req = request.Request(
            url=url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_token}",
            },
        )
        with request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}

    def _put_json(self, url: str, data: Dict) -> Dict:
        body_bytes = json.dumps(data).encode("utf-8")
        req = request.Request(
            url=url,
            method="PUT",
            data=body_bytes,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_token}",
            },
        )
        with request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}

    def _post_json(self, url: str, data: Dict) -> Dict:
        body_bytes = json.dumps(data).encode("utf-8")
        req = request.Request(
            url=url,
            method="POST",
            data=body_bytes,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_token}",
            },
        )
        with request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}


def _extract_contact_id(row: Dict[str, Any]) -> str:
    contact = row.get("contact")
    if isinstance(contact, dict):
        cid = contact.get("id")
        if cid is not None:
            return str(cid).strip()
    return ""


def _extract_pipeline_id(row: Dict[str, Any]):
    pipeline = row.get("pipeline")
    if isinstance(pipeline, dict):
        return pipeline.get("id")
    return row.get("pipeline_id") or row.get("funnel_id")


def _first_non_empty_str(*candidates) -> str:
    for c in candidates:
        if c is None:
            continue
        s = str(c).strip()
        if s:
            return s
    return ""


def _phone_from_phones_list(raw: Any) -> str:
    if not isinstance(raw, list):
        return ""
    for item in raw:
        if isinstance(item, str):
            found = _first_non_empty_str(item)
            if found:
                return found
        if isinstance(item, dict):
            found = _first_non_empty_str(
                item.get("phone"),
                item.get("number"),
                item.get("value"),
                item.get("msisdn"),
            )
            if found:
                return found
    return ""


def _extract_phone(row: Dict[str, Any]) -> str:
    contact = row.get("contact")
    if isinstance(contact, dict):
        found = _phone_from_phones_list(contact.get("phones"))
        if found:
            return found
        for key in (
            "phone",
            "mobile",
            "telephone",
            "client_phone",
            "msisdn",
            "phone_number",
            "main_phone",
            "full_phone",
        ):
            val = contact.get(key)
            if val:
                return str(val).strip()
        client = contact.get("client")
        if isinstance(client, dict):
            found = _phone_from_phones_list(client.get("phones"))
            if found:
                return found
            for key in ("phone", "mobile", "telephone", "msisdn", "phone_number", "full_phone"):
                val = client.get(key)
                if val:
                    return str(val).strip()
    for key in ("phone", "client_phone"):
        val = row.get(key)
        if val:
            return str(val).strip()
    return ""


def _extract_email_and_client_name(row: Dict[str, Any]):
    email = ""
    client_name = ""
    contact = row.get("contact")
    if isinstance(contact, dict):
        email = _first_non_empty_str(contact.get("email"), contact.get("e_mail"))
        client_name = _first_non_empty_str(
            contact.get("full_name"),
            contact.get("name"),
            contact.get("title"),
        )
        client = contact.get("client")
        if isinstance(client, dict):
            if not email:
                email = _first_non_empty_str(client.get("email"), client.get("e_mail"))
            if not client_name:
                client_name = _first_non_empty_str(
                    client.get("full_name"),
                    client.get("name"),
                    " ".join(
                        p
                        for p in (
                            str(client.get("first_name") or "").strip(),
                            str(client.get("last_name") or "").strip(),
                        )
                        if p
                    ),
                )
    return email, client_name


def _extract_manager_name(row: Dict[str, Any]) -> str:
    mgr = row.get("manager")
    if isinstance(mgr, dict):
        parts = [
            str(mgr.get("first_name") or "").strip(),
            str(mgr.get("last_name") or "").strip(),
        ]
        joined = " ".join(p for p in parts if p)
        return _first_non_empty_str(mgr.get("full_name"), mgr.get("name"), joined)
    return ""


def _extract_status_id(row: Dict[str, Any]) -> str:
    raw = row.get("status_id")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    ps = row.get("pipeline_status") or row.get("pipeline_stage") or row.get("status")
    if isinstance(ps, dict):
        rid = ps.get("id")
        return str(rid).strip() if rid is not None else ""
    raw = row.get("pipeline_status_id")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return ""


def _extract_status_label(row: Dict[str, Any]) -> str:
    ps = row.get("pipeline_status") or row.get("pipeline_stage") or row.get("status")
    if isinstance(ps, dict):
        return str(ps.get("title") or ps.get("name") or "").strip()
    if isinstance(row.get("status"), str):
        return str(row.get("status")).strip()
    return ""
