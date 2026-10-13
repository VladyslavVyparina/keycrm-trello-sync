import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .keycrm_client import Lead
from .trello_client import TrelloCard, extract_lead_ids_from_desc
from .util import normalize_list_name

# Старі маркери — потрібні лише для міграції існуючих описів
_AGENT_BLOCK_RE = re.compile(
    r"<!-- keycrm-sync:start -->.*?<!-- keycrm-sync:end -->",
    re.DOTALL,
)
# Новий формат: агентський блок — перший параграф, що починається з lead_id:
_AGENT_PARA_RE = re.compile(
    r"^lead_id:\d+(?:\n(?![ \t]*\n).+)*",
    re.MULTILINE,
)

_TRELLO_SYNC_PREFIX = "[Trello:"
_KEYCRM_SYNC_PREFIX = "[KeyCRM:"
_TRELLO_NOTE_ID_RE = re.compile(r"\[Trello:action_id=([^\]]+)\]")
_KEYCRM_NOTE_ID_RE = re.compile(r"\[KeyCRM:note_id=([^\]]+)\]")


@dataclass
class SyncError:
    lead_id: str
    reason: str


def _build_agent_block(lead: Lead) -> str:
    """Будує блок з даними KeyCRM (без HTML-маркерів)."""
    lines = [f"lead_id:{lead.lead_id}", f"Назва: {lead.name}"]
    if lead.client_name:
        lines.append(f"Клієнт: {lead.client_name}")
    if lead.phone:
        lines.append(f"Телефон: {lead.phone}")
    if lead.email:
        lines.append(f"Email: {lead.email}")
    if lead.status_name:
        lines.append(f"Статус: {lead.status_name} (id: {lead.status_id})")
    elif lead.status_id:
        lines.append(f"Статус id: {lead.status_id}")
    if lead.manager_name:
        lines.append(f"Менеджер: {lead.manager_name}")
    return "\n".join(lines)


def build_trello_card_description(lead: Lead, existing_desc: str = "") -> str:
    """
    Формує опис картки Trello.
    Агентський блок — перший параграф (до першого порожнього рядка).
    Текст користувача нижче порожнього рядка зберігається.
    """
    agent_block = _build_agent_block(lead)

    # Спочатку видаляємо старий формат з HTML-маркерами (міграція)
    desc = _AGENT_BLOCK_RE.sub("", existing_desc).strip()

    # Замінюємо існуючий агентський параграф (якщо є)
    if _AGENT_PARA_RE.search(desc):
        updated = _AGENT_PARA_RE.sub(agent_block, desc, count=1)
        return updated.strip()

    # Немає агентського блоку — додаємо на початку
    if desc:
        return agent_block + "\n\n" + desc
    return agent_block


def extract_lead_id_from_desc(desc: str) -> Optional[str]:
    """Витягує lead_id з блоку агента або зі старого формату."""
    m = re.search(r"^lead_id:(\d+)\s*$", desc or "", re.MULTILINE)
    return m.group(1) if m else None


class SyncService:
    def __init__(
        self,
        keycrm_client,
        trello_client,
        logger,
        keycrm_status_map: Dict[str, str],
        require_phone: bool = False,
        auto_create_trello_cards: bool = True,
        sync_bidirectional: bool = True,
        sync_comments: bool = False,
    ):
        self.keycrm_client = keycrm_client
        self.trello_client = trello_client
        self.logger = logger
        self.keycrm_status_map = {str(k).strip(): v for k, v in keycrm_status_map.items()}
        self.require_phone = require_phone
        self.auto_create_trello_cards = auto_create_trello_cards
        self.sync_bidirectional = sync_bidirectional
        self.sync_comments = sync_comments

        # Стан між циклами
        self.prev_keycrm_status: Dict[str, str] = {}
        self.prev_trello_list: Dict[str, str] = {}
        self.prev_trello_name: Dict[str, str] = {}
        self.prev_keycrm_name: Dict[str, str] = {}

        # Для видалення: множина lead_id, які бачили в попередньому циклі
        self.prev_seen_lead_ids: Set[str] = set()

        # Коментарі (якщо sync_comments=True)
        self.synced_trello_action_ids: Dict[str, Set[str]] = {}
        self.synced_keycrm_note_ids: Dict[str, Set[str]] = {}

        # Custom fields Trello ↔ KeyCRM
        self._custom_field_ids: Optional[Dict[str, Any]] = None  # {field_name: {id, type}}
        self._custom_field_type: Dict[str, str] = {}             # {field_id: type}
        self.prev_custom_fields: Dict[str, Dict[str, str]] = {}  # {lead_id: {field_id: value}}
        self.prev_lead_values: Dict[str, Dict[str, str]] = {}    # {lead_id: {attr: value}}
        # Назви Trello custom fields → атрибут Lead (KeyCRM→Trello)
        self.CF_TO_LEAD: Dict[str, str] = {
            "Назва": "name",
            "Ім'я": "client_name",
            "Телефон": "phone",
            "Email": "email",
            "Менеджер": "manager_name",
        }
    def run_once(self) -> List[SyncError]:
        errors: List[SyncError] = []
        self.logger.info("Sync run started")

        try:
            trello_lists = self.trello_client.get_board_list_name_to_id()
        except ValueError as exc:
            self.logger.error("Trello lists resolution failed | reason=%s", exc)
            return [SyncError("", str(exc))]

        # Пряме маппування: status_id -> list_id
        status_id_to_list_id: Dict[str, str] = {}
        for sid, column_name in self.keycrm_status_map.items():
            key = normalize_list_name(column_name)
            if key not in trello_lists:
                msg = (
                    f"Немає колонки Trello для status_id={sid} (очікувана назва: {column_name!r})."
                )
                self.logger.error(msg)
                errors.append(SyncError("", msg))
            else:
                status_id_to_list_id[sid] = trello_lists[key]

        if len(status_id_to_list_id) != len(self.keycrm_status_map):
            self.logger.error(
                "Мапінг KeyCRM -> Trello неповний | resolved=%s | expected=%s",
                len(status_id_to_list_id),
                len(self.keycrm_status_map),
            )

        # Зворотне маппування: list_id -> status_id
        list_id_to_status_id: Dict[str, str] = {}
        if self.sync_bidirectional:
            list_id_map = self.trello_client.get_board_lists_id_to_name()
            norm_name_to_status: Dict[str, str] = {}
            for sid, column_name in self.keycrm_status_map.items():
                key = normalize_list_name(column_name)
                norm_name_to_status[key] = sid  # останній запис виграє → правильний status_id
            for list_id, norm_name in list_id_map.items():
                if norm_name in norm_name_to_status:
                    list_id_to_status_id[list_id] = norm_name_to_status[norm_name]

        leads = self.keycrm_client.fetch_leads()
        cards: List[TrelloCard] = list(self.trello_client.get_board_cards())

        mapped_status_ids = set(self.keycrm_status_map.keys())
        seen_status_ids = {lead.status_id for lead in leads if lead.status_id}
        unknown_status_ids = sorted(
            seen_status_ids - mapped_status_ids,
            key=lambda x: (0, int(x)) if str(x).isdigit() else (1, x),
        )
        if unknown_status_ids:
            self.logger.warning(
                "Є картки зі status_id, яких немає в config/keycrm_status_map.json: %s.",
                unknown_status_ids,
            )

        # ── Видалення: виявити зниклі ліди (були в попередньому циклі, зараз немає) ──
        current_lead_ids = {lead.lead_id for lead in leads}
        if self.prev_seen_lead_ids:
            deleted_ids = self.prev_seen_lead_ids - current_lead_ids
            for deleted_id in deleted_ids:
                self._handle_deleted_lead(deleted_id, cards)
        self.prev_seen_lead_ids = current_lead_ids

        for lead in leads:
            err = self._validate_lead(lead)
            if err:
                errors.append(err)
                self.logger.error(
                    "Lead validation failed | lead_id=%s | reason=%s", err.lead_id, err.reason
                )
                continue

            current_keycrm_status = lead.status_id
            by_id = self.trello_client.find_cards_by_lead_id(cards, lead.lead_id)

            # ─── Картки ще немає в Trello — створити або прив'язати ───────────
            if len(by_id) == 0 and self.auto_create_trello_cards:
                if current_keycrm_status not in status_id_to_list_id:
                    if lead.lead_id not in self.prev_keycrm_status:
                        self.prev_keycrm_status[lead.lead_id] = current_keycrm_status
                        self.logger.info(
                            "Lead observed (немає картки Trello, статус не в мапінгу) | "
                            "lead_id=%s | status_id=%s",
                            lead.lead_id,
                            current_keycrm_status,
                        )
                    continue

                by_name = self.trello_client.find_cards_by_name(cards, lead.name)
                if len(by_name) > 1:
                    errors.append(SyncError(lead.lead_id, f"Duplicate card name '{lead.name}'"))
                    self.logger.error(
                        "Не створено картку: дублікат назви | lead_id=%s | name=%s",
                        lead.lead_id,
                        lead.name,
                    )
                    continue

                if len(by_name) == 1:
                    card = by_name[0]
                    linked_ids = extract_lead_ids_from_desc(card.desc)
                    if linked_ids and lead.lead_id not in linked_ids:
                        self.logger.warning(
                            "Картка Trello %r вже прив'язана до lead_id=%s — "
                            "створюємо нову для lead_id=%s",
                            lead.name, linked_ids, lead.lead_id,
                        )
                    elif lead.lead_id in linked_ids:
                        by_id = [card]
                    else:
                        new_desc = build_trello_card_description(lead, card.desc)
                        try:
                            self.trello_client.update_card_description(card.card_id, new_desc)
                            list_id = status_id_to_list_id[current_keycrm_status]
                            if card.list_id != list_id:
                                self.trello_client.move_card_to_list(card.card_id, list_id)
                        except Exception as exc:
                            errors.append(SyncError(lead.lead_id, f"adopt failed: {exc}"))
                            self.logger.exception("adopt card failed | lead_id=%s", lead.lead_id)
                            continue
                        card.desc = new_desc
                        card.list_id = list_id
                        self.logger.info(
                            "Прив'язано картку Trello до KeyCRM | lead_id=%s | card_id=%s",
                            lead.lead_id, card.card_id,
                        )
                        by_id = [card]

                if len(by_id) == 0:
                    try:
                        list_id = status_id_to_list_id[current_keycrm_status]
                        desc = build_trello_card_description(lead)
                        new_id = self.trello_client.create_card(list_id, lead.name, desc)
                    except Exception as exc:
                        errors.append(SyncError(lead.lead_id, f"create_card failed: {exc}"))
                        self.logger.exception("create_card failed | lead_id=%s", lead.lead_id)
                        continue
                    new_card = TrelloCard(
                        card_id=new_id, name=lead.name, list_id=list_id, desc=desc
                    )
                    cards.append(new_card)
                    self.prev_keycrm_status[lead.lead_id] = current_keycrm_status
                    self.prev_trello_list[lead.lead_id] = list_id
                    self.prev_keycrm_name[lead.lead_id] = lead.name
                    self.prev_trello_name[lead.lead_id] = lead.name
                    self.logger.info(
                        "CREATED: %s | %s | list: %s",
                        new_id, lead.name, current_keycrm_status,
                    )
                    continue

            if len(by_id) == 0 and not self.auto_create_trello_cards:
                if lead.lead_id not in self.prev_keycrm_status:
                    self.prev_keycrm_status[lead.lead_id] = current_keycrm_status
                    self.logger.info(
                        "Lead observed | lead_id=%s | status_id=%s",
                        lead.lead_id, current_keycrm_status,
                    )
                continue

            if len(by_id) != 1:
                errors.append(
                    SyncError(lead.lead_id, f"Duplicate/missing card. Count={len(by_id)}")
                )
                self.logger.error(
                    "Card identity conflict | lead_id=%s | count=%s", lead.lead_id, len(by_id)
                )
                continue

            card = by_id[0]

            # ─── Двостороння синхронізація статусу ────────────────────────────
            prev_keycrm = self.prev_keycrm_status.get(lead.lead_id)
            prev_trello_list = self.prev_trello_list.get(lead.lead_id)
            current_trello_list = card.list_id  # до будь-яких переміщень у цьому циклі

            keycrm_status_changed = (
                prev_keycrm is not None and current_keycrm_status != prev_keycrm
            )
            trello_list_changed = (
                prev_trello_list is not None
                and current_trello_list != prev_trello_list
                and self.sync_bidirectional
            )

            if keycrm_status_changed:
                # KeyCRM змінився → оновити Trello (авторитетне джерело)
                if current_keycrm_status not in status_id_to_list_id:
                    errors.append(SyncError(
                        lead.lead_id,
                        f"Немаплений status_id={current_keycrm_status!r}",
                    ))
                    self.logger.error(
                        "Status mapping failed | lead_id=%s | status_id=%s",
                        lead.lead_id, current_keycrm_status,
                    )
                else:
                    target_list_id = status_id_to_list_id[current_keycrm_status]
                    if card.list_id != target_list_id:
                        try:
                            self.trello_client.move_card_to_list(card.card_id, target_list_id)
                            self.logger.info(
                                "KeyCRM→Trello: переміщено | lead_id=%s | %s→%s",
                                lead.lead_id, prev_keycrm, current_keycrm_status,
                            )
                            card.list_id = target_list_id
                        except Exception as exc:
                            errors.append(SyncError(lead.lead_id, f"Trello move failed: {exc}"))
                            self.logger.exception("move failed | lead_id=%s", lead.lead_id)

            elif trello_list_changed:
                # Trello змінився → оновити KeyCRM
                new_status_id = list_id_to_status_id.get(current_trello_list)
                if new_status_id:
                    try:
                        self.keycrm_client.update_lead_status(lead.lead_id, new_status_id)
                        self.logger.info(
                            "Trello→KeyCRM: статус оновлено | lead_id=%s | %s→%s",
                            lead.lead_id, prev_keycrm, new_status_id,
                        )
                        # Оновлюємо локальний статус щоб уникнути петлі в наступному циклі
                        current_keycrm_status = new_status_id
                    except Exception as exc:
                        errors.append(
                            SyncError(lead.lead_id, f"KeyCRM status update failed: {exc}")
                        )
                        self.logger.exception(
                            "KeyCRM status update failed | lead_id=%s", lead.lead_id
                        )
                        # При помилці — повернути картку Trello назад
                        if prev_trello_list:
                            try:
                                self.trello_client.move_card_to_list(card.card_id, prev_trello_list)
                                card.list_id = prev_trello_list
                            except Exception:
                                pass
                else:
                    # Колонка не в маппінгу — повернути назад
                    if prev_trello_list:
                        try:
                            self.trello_client.move_card_to_list(card.card_id, prev_trello_list)
                            card.list_id = prev_trello_list
                            self.logger.warning(
                                "Trello→KeyCRM: картку повернено — колонка не в маппінгу | "
                                "lead_id=%s", lead.lead_id,
                            )
                        except Exception:
                            pass

            else:
                # Перший запуск або стан стабільний
                if prev_keycrm is None:
                    if current_keycrm_status in status_id_to_list_id:
                        target_list_id = status_id_to_list_id[current_keycrm_status]
                        if card.list_id != target_list_id:
                            try:
                                self.trello_client.move_card_to_list(card.card_id, target_list_id)
                                card.list_id = target_list_id
                                self.logger.info(
                                    "Lead observed (initial sync) | lead_id=%s | %s",
                                    lead.lead_id, current_keycrm_status,
                                )
                            except Exception as exc:
                                errors.append(
                                    SyncError(lead.lead_id, f"Trello move failed: {exc}")
                                )
                        else:
                            self.logger.info(
                                "Lead observed | lead_id=%s | status_id=%s",
                                lead.lead_id, current_keycrm_status,
                            )

            # ─── Синхронізація назви (двостороння) ───────────────────────────
            prev_keycrm_name = self.prev_keycrm_name.get(lead.lead_id)
            prev_trello_name = self.prev_trello_name.get(lead.lead_id)
            current_trello_name = card.name

            keycrm_name_changed = (
                prev_keycrm_name is not None
                and lead.name
                and lead.name != prev_keycrm_name
            )
            trello_name_changed = (
                prev_trello_name is not None
                and current_trello_name
                and current_trello_name != prev_trello_name
                and self.sync_bidirectional
                and not keycrm_name_changed
            )

            if keycrm_name_changed:
                try:
                    self.trello_client.update_card_name(card.card_id, lead.name)
                    card.name = lead.name
                    self.logger.info(
                        "KeyCRM→Trello: назву оновлено | lead_id=%s | %r",
                        lead.lead_id, lead.name,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Не вдалося оновити назву Trello | lead_id=%s | %s", lead.lead_id, exc
                    )

            elif trello_name_changed:
                try:
                    self.keycrm_client.update_lead_title(lead.lead_id, current_trello_name)
                    self.logger.info(
                        "Trello→KeyCRM: назву оновлено | lead_id=%s | %r",
                        lead.lead_id, current_trello_name,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Не вдалося оновити назву KeyCRM | lead_id=%s | %s", lead.lead_id, exc
                    )

            # ─── Оновлення даних KeyCRM в описі Trello ───────────────────────
            # Зберігаємо текст користувача, оновлюємо тільки блок агента
            desired_desc = build_trello_card_description(lead, card.desc)
            if card.desc.strip() != desired_desc.strip():
                try:
                    self.trello_client.update_card_description(card.card_id, desired_desc)
                    card.desc = desired_desc
                    self.logger.info(
                        "KeyCRM→Trello: опис оновлено | lead_id=%s | card_id=%s",
                        lead.lead_id, card.card_id,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Не вдалося оновити опис Trello | lead_id=%s | %s", lead.lead_id, exc
                    )

            # ─── Custom fields Trello ↔ KeyCRM ───────────────────────────────
            self._sync_custom_fields(lead, card)

            # ─── Коментарі (якщо ввімкнено) ──────────────────────────────────
            if self.sync_comments:
                self._sync_comments(lead.lead_id, card.card_id)

            # ─── Зберегти стан для наступного циклу ──────────────────────────
            self.prev_keycrm_status[lead.lead_id] = current_keycrm_status
            self.prev_trello_list[lead.lead_id] = card.list_id
            self.prev_keycrm_name[lead.lead_id] = lead.name
            self.prev_trello_name[lead.lead_id] = card.name

        self.logger.info(
            "Sync run finished | leads=%s | errors=%s", len(leads), len(errors)
        )
        return errors

    @staticmethod
    def _norm_cf(value: str, field_type: str) -> str:
        """Нормалізує значення CF для стабільного порівняння (number → лише цифри)."""
        v = (value or "").strip()
        if field_type == "number":
            return "".join(c for c in v if c.isdigit())
        return v

    @staticmethod
    def _norm_field_name(name: str) -> str:
        """Нормалізує назву поля: видаляє будь-який апостроф і зайві пробіли.
        "Ім'я" → "Імя", "Імя" → "Імя" — збігаються незалежно від апострофа."""
        return (
            (name or "")
            .replace("\u2019", "")   # '  RIGHT SINGLE QUOTATION MARK
            .replace("\u2018", "")   # '  LEFT SINGLE QUOTATION MARK
            .replace("\u0060", "")   # `  backtick
            .replace("\u00b4", "")   # ´  acute accent
            .replace("'", "")        # '  ASCII apostrophe
            .strip()
        )

    def _get_custom_field_ids(self) -> Dict[str, Any]:
        """Кешує та повертає {norm_field_name: {id, type}} для дошки Trello."""
        if self._custom_field_ids is None:
            try:
                raw = self.trello_client.get_board_custom_fields()
                # Нормалізуємо назви: різні варіанти апострофа → ASCII '
                normalized: Dict[str, Any] = {
                    self._norm_field_name(name): info
                    for name, info in raw.items()
                }
                self._custom_field_ids = normalized
                self._custom_field_type = {
                    info["id"]: info.get("type", "text")
                    for info in normalized.values()
                    if isinstance(info, dict)
                }
                if raw:
                    self.logger.info(
                        "Custom fields дошки: %s",
                        {n: f"{v['id']} ({v.get('type','?')})" for n, v in raw.items()},
                    )
                else:
                    self.logger.warning("Custom fields не знайдено на дошці Trello")
            except Exception as exc:
                self.logger.warning("Не вдалося отримати custom fields | %s", exc)
                self._custom_field_ids = {}
        return self._custom_field_ids

    def _sync_custom_fields(self, lead: Lead, card: TrelloCard) -> None:
        """Синхронізує custom fields Trello ↔ KeyCRM."""
        field_infos = self._get_custom_field_ids()  # {name: {id, type}}
        if not field_infos:
            return

        current_cf = card.custom_fields                             # {field_id: value}
        prev_cf = self.prev_custom_fields.get(lead.lead_id)        # None = перший цикл
        prev_kcrm = self.prev_lead_values.get(lead.lead_id) or {}  # попередні KeyCRM значення

        lead_values: Dict[str, str] = {
            "name": lead.name or "",
            "client_name": lead.client_name or "",
            "phone": lead.phone or "",
            "email": lead.email or "",
            "manager_name": lead.manager_name or "",
        }

        for field_name, attr in self.CF_TO_LEAD.items():
            # Нормалізуємо назву поля (апостроф тощо) при пошуку
            info = field_infos.get(self._norm_field_name(field_name))
            if not info or not isinstance(info, dict):
                continue
            field_id = info["id"]
            field_type = info.get("type", "text")

            current_val = self._norm_cf(current_cf.get(field_id, ""), field_type)
            lead_val = self._norm_cf(lead_values.get(attr, ""), field_type)

            if prev_cf is None:
                # ── Перший цикл: заповнюємо поля з KeyCRM (якщо є дані) ──
                if lead_val and current_val != lead_val:
                    try:
                        self.trello_client.set_card_custom_field(
                            card.card_id, field_id, lead_values.get(attr, ""), field_type
                        )
                        card.custom_fields[field_id] = lead_val
                        self.logger.info(
                            "KeyCRM→Trello CF init: %r = %r | lead_id=%s",
                            field_name, lead_val, lead.lead_id,
                        )
                    except Exception as exc:
                        self.logger.warning(
                            "CF init failed: %r | lead_id=%s | err=%s",
                            field_name, lead.lead_id, exc,
                        )
            else:
                prev_trello_val = self._norm_cf(prev_cf.get(field_id, ""), field_type)
                prev_keycrm_val = self._norm_cf(prev_kcrm.get(attr, ""), field_type)

                keycrm_changed = lead_val != prev_keycrm_val

                if keycrm_changed and current_val != lead_val and lead_val:
                    # ── KeyCRM змінився → оновити Trello ──
                    try:
                        self.trello_client.set_card_custom_field(
                            card.card_id, field_id, lead_values.get(attr, ""), field_type
                        )
                        card.custom_fields[field_id] = lead_val
                        self.logger.info(
                            "KeyCRM→Trello CF: %r = %r | lead_id=%s",
                            field_name, lead_val, lead.lead_id,
                        )
                    except Exception as exc:
                        self.logger.warning(
                            "CF update failed: %r | lead_id=%s | %s",
                            field_name, lead.lead_id, exc,
                        )

        # ── Зберігаємо нормалізований стан для наступного циклу ──
        norm_cf: Dict[str, str] = {}
        for fn2, attr2 in self.CF_TO_LEAD.items():
            inf2 = field_infos.get(self._norm_field_name(fn2))
            if not inf2 or not isinstance(inf2, dict):
                continue
            fid2 = inf2["id"]
            ftype2 = inf2.get("type", "text")
            norm_cf[fid2] = self._norm_cf(card.custom_fields.get(fid2, ""), ftype2)
        self.prev_custom_fields[lead.lead_id] = norm_cf
        self.prev_lead_values[lead.lead_id] = {
            a: self._norm_cf(v, "text") for a, v in lead_values.items()
        }

    def _handle_deleted_lead(self, lead_id: str, cards: List[TrelloCard]) -> None:
        """Архівує Trello-картку якщо лід зник з KeyCRM."""
        matching = self.trello_client.find_cards_by_lead_id(cards, lead_id)
        for card in matching:
            try:
                # Архівуємо картку (PUT closed=true)
                self.trello_client._request_json(
                    "PUT",
                    f"{self.trello_client.base_url}/cards/{card.card_id}",
                    {"closed": "true"},
                )
                self.logger.info(
                    "KeyCRM→Trello: Картку архівовано (лід видалено з KeyCRM) | "
                    "lead_id=%s | card_id=%s",
                    lead_id, card.card_id,
                )
            except Exception as exc:
                self.logger.warning(
                    "Не вдалося архівувати картку | lead_id=%s | %s", lead_id, exc
                )

    def _sync_comments(self, lead_id: str, card_id: str) -> None:
        """Двостороння синхронізація коментарів (потребує підтримки API KeyCRM)."""
        if lead_id not in self.synced_trello_action_ids:
            self.synced_trello_action_ids[lead_id] = set()
        if lead_id not in self.synced_keycrm_note_ids:
            self.synced_keycrm_note_ids[lead_id] = set()

        try:
            trello_comments = self.trello_client.get_card_comments(card_id)
        except Exception as exc:
            self.logger.warning(
                "Не вдалося отримати коментарі Trello | lead_id=%s | %s", lead_id, exc
            )
            trello_comments = []

        try:
            keycrm_notes = self.keycrm_client.fetch_lead_notes(lead_id)
        except Exception as exc:
            self.logger.warning(
                "Не вдалося отримати нотатки KeyCRM | lead_id=%s | %s", lead_id, exc
            )
            keycrm_notes = []

        for note in keycrm_notes:
            m = _TRELLO_NOTE_ID_RE.search(note.text)
            if m:
                self.synced_trello_action_ids[lead_id].add(m.group(1))

        for comment in trello_comments:
            m = _KEYCRM_NOTE_ID_RE.search(comment.text)
            if m:
                self.synced_keycrm_note_ids[lead_id].add(m.group(1))

        for comment in trello_comments:
            if comment.text.startswith(_KEYCRM_SYNC_PREFIX):
                continue
            if comment.action_id in self.synced_trello_action_ids[lead_id]:
                continue
            author_part = f" від {comment.author}" if comment.author else ""
            note_text = (
                f"[Trello:action_id={comment.action_id}]"
                f" Коментар Trello{author_part}:\n{comment.text}"
            )
            try:
                result = self.keycrm_client.add_lead_note(lead_id, note_text)
                if result is not None:
                    self.synced_trello_action_ids[lead_id].add(comment.action_id)
                    self.logger.info(
                        "Trello→KeyCRM: коментар | lead_id=%s | action_id=%s",
                        lead_id, comment.action_id,
                    )
                else:
                    self.logger.warning(
                        "KeyCRM notes API недоступне | lead_id=%s", lead_id
                    )
                    return
            except Exception as exc:
                self.logger.warning(
                    "Не вдалося додати нотатку KeyCRM | lead_id=%s | %s", lead_id, exc
                )

        for note in keycrm_notes:
            if note.text.startswith(_TRELLO_SYNC_PREFIX):
                continue
            if note.note_id in self.synced_keycrm_note_ids[lead_id]:
                continue
            author_part = f" від {note.author}" if note.author else ""
            comment_text = (
                f"[KeyCRM:note_id={note.note_id}]"
                f" Нотатка KeyCRM{author_part}:\n{note.text}"
            )
            try:
                self.trello_client.add_card_comment(card_id, comment_text)
                self.synced_keycrm_note_ids[lead_id].add(note.note_id)
                self.logger.info(
                    "KeyCRM→Trello: нотатку | lead_id=%s | note_id=%s",
                    lead_id, note.note_id,
                )
            except Exception as exc:
                self.logger.warning(
                    "Не вдалося додати коментар Trello | lead_id=%s | %s", lead_id, exc
                )

    def _validate_lead(self, lead: Lead) -> Optional[SyncError]:
        if not lead.lead_id:
            return SyncError("", "Missing lead_id")
        if not lead.name:
            return SyncError(lead.lead_id, "Missing name")
        if not lead.phone:
            if self.require_phone:
                return SyncError(lead.lead_id, "Missing phone")
            self.logger.warning(
                "Картка без телефону — продовжується | lead_id=%s | name=%s",
                lead.lead_id, lead.name,
            )
        if not lead.status_id:
            return SyncError(lead.lead_id, "Missing status_id")
        return None
