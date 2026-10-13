import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib import parse, request

from .util import normalize_list_name

_LEAD_ID_LINE = re.compile(r"^lead_id:(\d+)\s*$", re.MULTILINE)


@dataclass
class TrelloCard:
    card_id: str
    name: str
    list_id: str
    desc: str
    custom_fields: Dict[str, str] = field(default_factory=dict)  # {field_id: text_value}


@dataclass
class TrelloComment:
    action_id: str
    text: str
    author: str
    created_at: str


def desc_has_lead_id_line(desc: str, lead_id: str) -> bool:
    """Точний збіг рядка lead_id (не підрядок, щоб 1 не давав 10, 11, …)."""
    marker = f"lead_id:{lead_id}"
    for line in (desc or "").splitlines():
        if line.strip() == marker:
            return True
    return False


def extract_lead_ids_from_desc(desc: str) -> List[str]:
    return [m.group(1) for m in _LEAD_ID_LINE.finditer(desc or "")]


def desc_has_any_lead_id_line(desc: str) -> bool:
    return _LEAD_ID_LINE.search(desc or "") is not None


class TrelloClient:
    def __init__(self, base_url: str, api_key: str, token: str, board_id: str, timeout_sec: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.token = token
        self.board_id = board_id
        self.timeout_sec = timeout_sec

    def get_board_list_name_to_id(self) -> Dict[str, str]:
        url = f"{self.base_url}/boards/{self.board_id}/lists"
        payload = self._request_json("GET", url)
        if not isinstance(payload, list):
            return {}
        mapping: Dict[str, str] = {}
        for row in payload:
            name = str(row.get("name", ""))
            list_id = str(row.get("id", ""))
            if not list_id:
                continue
            key = normalize_list_name(name)
            if not key:
                continue
            if key in mapping and mapping[key] != list_id:
                raise ValueError(
                    f"На дошці є дві колонки з однаковою назвою (після нормалізації): {name!r}"
                )
            mapping[key] = list_id
        return mapping

    def get_board_lists_id_to_name(self) -> Dict[str, str]:
        """Повертає map: list_id -> normalized list name."""
        url = f"{self.base_url}/boards/{self.board_id}/lists"
        payload = self._request_json("GET", url)
        if not isinstance(payload, list):
            return {}
        return {str(row.get("id", "")): normalize_list_name(str(row.get("name", "")))
                for row in payload if row.get("id")}

    def get_board_cards(self) -> List[TrelloCard]:
        url = f"{self.base_url}/boards/{self.board_id}/cards"
        payload = self._request_json("GET", url, {"customFieldItems": "true"})
        cards = []
        for row in payload:
            cf: Dict[str, str] = {}
            for item in (row.get("customFieldItems") or []):
                fid = item.get("idCustomField", "")
                val = item.get("value") or {}
                text = val.get("text") or val.get("number") or ""
                if fid:
                    cf[fid] = str(text) if text else ""
            cards.append(
                TrelloCard(
                    card_id=str(row.get("id", "")),
                    name=str(row.get("name", "")).strip(),
                    list_id=str(row.get("idList", "")),
                    desc=str(row.get("desc", "")),
                    custom_fields=cf,
                )
            )
        return cards

    def get_board_custom_fields(self) -> Dict[str, Dict[str, str]]:
        """Повертає {field_name: {"id": ..., "type": ...}} для всіх custom fields дошки."""
        url = f"{self.base_url}/boards/{self.board_id}/customFields"
        try:
            data = self._request_json("GET", url)
        except Exception:
            return {}
        if not isinstance(data, list):
            return {}
        return {
            item["name"]: {"id": item["id"], "type": item.get("type", "text")}
            for item in data
            if item.get("id") and item.get("name")
        }

    def set_card_custom_field(
        self, card_id: str, field_id: str, value: str, field_type: str = "text"
    ) -> None:
        """Встановлює значення custom field на картці з урахуванням типу поля."""
        url = f"{self.base_url}/cards/{card_id}/customField/{field_id}/item"
        if not value:
            body: Dict[str, Any] = {"value": ""}
        elif field_type == "number":
            # Number-поля Trello не приймають +, пробіли — залишаємо лише цифри
            digits = "".join(c for c in value if c.isdigit())
            body = {"value": {"number": digits}} if digits else {"value": ""}
        else:
            body = {"value": {"text": value}}
        self._request_json_body("PUT", url, body)

    def move_card_to_list(self, card_id: str, list_id: str) -> None:
        url = f"{self.base_url}/cards/{card_id}"
        self._request_json("PUT", url, {"idList": list_id})

    def update_card_description(self, card_id: str, desc: str) -> None:
        url = f"{self.base_url}/cards/{card_id}"
        self._request_json("PUT", url, {"desc": desc})

    def update_card_name(self, card_id: str, name: str) -> None:
        url = f"{self.base_url}/cards/{card_id}"
        self._request_json("PUT", url, {"name": name})

    def create_card(self, list_id: str, name: str, desc: str) -> str:
        url = f"{self.base_url}/cards"
        payload = self._request_json("POST", url, {"idList": list_id, "name": name, "desc": desc})
        card_id = str(payload.get("id", "")).strip()
        if not card_id:
            raise ValueError("Trello API не повернув id нової картки")
        return card_id

    def find_cards_by_lead_id(self, cards: List[TrelloCard], lead_id: str) -> List[TrelloCard]:
        return [c for c in cards if desc_has_lead_id_line(c.desc, lead_id)]

    def find_cards_by_name(self, cards: List[TrelloCard], name: str) -> List[TrelloCard]:
        normalized = name.strip().lower()
        return [c for c in cards if c.name.strip().lower() == normalized and normalized]

    def get_card_comments(self, card_id: str) -> List[TrelloComment]:
        """Отримує коментарі картки."""
        url = f"{self.base_url}/cards/{card_id}/actions"
        try:
            payload = self._request_json("GET", url, {"filter": "commentCard"})
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        comments = []
        for row in payload:
            action_id = str(row.get("id", "")).strip()
            data = row.get("data", {}) or {}
            text = str(data.get("text") or "").strip()
            created_at = str(row.get("date") or "").strip()
            member = row.get("memberCreator") or {}
            author = str(member.get("fullName") or member.get("username") or "").strip()
            if action_id and text:
                comments.append(TrelloComment(
                    action_id=action_id,
                    text=text,
                    author=author,
                    created_at=created_at,
                ))
        return comments

    def add_card_comment(self, card_id: str, text: str) -> None:
        """Додає коментар до картки Trello."""
        url = f"{self.base_url}/cards/{card_id}/actions/comments"
        self._request_json("POST", url, {"text": text})

    def _auth_query(self) -> Dict[str, str]:
        return {"key": self.api_key, "token": self.token}

    def _request_json_body(self, method: str, url: str, data: Dict) -> Dict:
        """Відправляє запит з JSON-тілом (потрібно для custom fields API)."""
        query = self._auth_query()
        url = f"{url}?{parse.urlencode(query)}"
        body_bytes = json.dumps(data).encode("utf-8")
        req = request.Request(
            url=url,
            method=method,
            data=body_bytes,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}

    def _request_json(
        self,
        method: str,
        url: str,
        data: Optional[Dict[str, str]] = None,
    ):
        query = self._auth_query()
        if method == "GET":
            all_query = {**query, **(data or {})}
            url = f"{url}?{parse.urlencode(all_query)}"
            req = request.Request(url=url, method=method, headers={"Accept": "application/json"})
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}

        payload = parse.urlencode({**query, **(data or {})}).encode("utf-8")
        req = request.Request(
            url=url,
            method=method,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
