import unittest

from src.keycrm_client import Lead
from src.sync_service import SyncService
from src.trello_client import TrelloCard, desc_has_lead_id_line
from src.util import normalize_list_name


class FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeKeyCRM:
    def __init__(self, batches):
        self._batches = batches
        self._idx = 0

    def fetch_leads(self):
        value = self._batches[self._idx]
        if self._idx < len(self._batches) - 1:
            self._idx += 1
        return value


class FakeTrello:
    def __init__(self, cards, list_name_to_id):
        self.cards = list(cards)
        self._list_name_to_id = {normalize_list_name(k): v for k, v in list_name_to_id.items()}
        self.moved = []
        self._next_new = 1

    def get_board_list_name_to_id(self):
        return dict(self._list_name_to_id)

    def get_board_cards(self):
        return self.cards

    def create_card(self, list_id, name, desc):
        cid = f"CNEW{self._next_new}"
        self._next_new += 1
        self.cards.append(TrelloCard(card_id=cid, name=name, list_id=list_id, desc=desc))
        return cid

    def move_card_to_list(self, card_id, list_id):
        self.moved.append((card_id, list_id))

    def find_cards_by_lead_id(self, cards, lead_id):
        return [c for c in cards if desc_has_lead_id_line(c.desc, lead_id)]

    def update_card_description(self, card_id, desc):
        for c in self.cards:
            if c.card_id == card_id:
                c.desc = desc
                return

    def find_cards_by_name(self, cards, name):
        normalized = name.strip().lower()
        return [c for c in cards if c.name.strip().lower() == normalized and normalized]


class SyncServiceTest(unittest.TestCase):
    def setUp(self):
        self.keycrm_status_map = {"1": "Новий лід", "2": "В роботі", "3": "Успішно"}
        self.list_map = {"Новий лід": "L1", "В роботі": "L2", "Успішно": "L3"}

    def test_moves_card_when_status_changes(self):
        lead_a = Lead(lead_id="1001", name="Ivan", phone="+380111", status_id="1")
        lead_b = Lead(lead_id="1001", name="Ivan", phone="+380111", status_id="2")
        kcrm = FakeKeyCRM([[lead_a], [lead_b]])
        trello = FakeTrello([TrelloCard(card_id="C1", name="Ivan", list_id="L1", desc="lead_id:1001")], self.list_map)

        service = SyncService(
            kcrm,
            trello,
            FakeLogger(),
            self.keycrm_status_map,
            require_phone=False,
            auto_create_trello_cards=False,
        )
        first = service.run_once()
        second = service.run_once()

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(trello.moved, [("C1", "L2")])

    def test_duplicate_by_lead_id_blocks_move(self):
        lead_a = Lead(lead_id="2002", name="Oleh", phone="+380222", status_id="1")
        lead_b = Lead(lead_id="2002", name="Oleh", phone="+380222", status_id="2")
        kcrm = FakeKeyCRM([[lead_a], [lead_b]])
        trello = FakeTrello(
            [
                TrelloCard(card_id="C1", name="Oleh", list_id="L1", desc="lead_id:2002"),
                TrelloCard(card_id="C2", name="Oleh-dup", list_id="L1", desc="lead_id:2002"),
            ],
            self.list_map,
        )

        service = SyncService(
            kcrm,
            trello,
            FakeLogger(),
            self.keycrm_status_map,
            require_phone=False,
            auto_create_trello_cards=False,
        )
        service.run_once()
        errors = service.run_once()

        self.assertEqual(len(errors), 1)
        self.assertIn("Duplicate/missing card by lead_id", errors[0].reason)
        self.assertEqual(trello.moved, [])

    def test_same_title_two_cards_moves_by_lead_id_only(self):
        """Дві картки з однаковою назвою, різні lead_id — переміщується та, що з потрібним маркером."""
        lead_a = Lead(lead_id="3003", name="Maria", phone="+380333", status_id="1")
        lead_b = Lead(lead_id="3003", name="Maria", phone="+380333", status_id="2")
        kcrm = FakeKeyCRM([[lead_a], [lead_b]])
        trello = FakeTrello(
            [
                TrelloCard(card_id="C1", name="Maria", list_id="L1", desc="lead_id:3003"),
                TrelloCard(card_id="C2", name="Maria", list_id="L1", desc="lead_id:9999"),
            ],
            self.list_map,
        )

        service = SyncService(
            kcrm,
            trello,
            FakeLogger(),
            self.keycrm_status_map,
            require_phone=False,
            auto_create_trello_cards=False,
        )
        service.run_once()
        errors = service.run_once()

        self.assertEqual(errors, [])
        self.assertEqual(trello.moved, [("C1", "L2")])

    def test_validation_error_for_missing_phone_when_required(self):
        lead = Lead(lead_id="4004", name="Petro", phone="", status_id="1")
        kcrm = FakeKeyCRM([[lead]])
        trello = FakeTrello([], self.list_map)

        service = SyncService(
            kcrm,
            trello,
            FakeLogger(),
            self.keycrm_status_map,
            require_phone=True,
            auto_create_trello_cards=False,
        )
        errors = service.run_once()

        self.assertEqual(len(errors), 1)
        self.assertIn("Missing phone", errors[0].reason)

    def test_missing_phone_allowed_when_not_required(self):
        lead = Lead(lead_id="4004", name="Petro", phone="", status_id="1")
        kcrm = FakeKeyCRM([[lead]])
        trello = FakeTrello([], self.list_map)

        service = SyncService(
            kcrm,
            trello,
            FakeLogger(),
            self.keycrm_status_map,
            require_phone=False,
            auto_create_trello_cards=False,
        )
        errors = service.run_once()

        self.assertEqual(errors, [])

    def test_missing_trello_column_reports_error(self):
        lead = Lead(lead_id="5005", name="Anna", phone="+380555", status_id="1")
        kcrm = FakeKeyCRM([[lead]])
        trello = FakeTrello([], {"В роботі": "L2"})

        service = SyncService(
            kcrm,
            trello,
            FakeLogger(),
            self.keycrm_status_map,
            require_phone=False,
            auto_create_trello_cards=False,
        )
        errors = service.run_once()

        self.assertTrue(any("Немає колонки Trello" in e.reason for e in errors))

    def test_creates_trello_card_when_missing(self):
        lead = Lead(lead_id="7007", name="Новий тест", phone="+380707", status_id="2")
        kcrm = FakeKeyCRM([[lead]])
        trello = FakeTrello([], self.list_map)

        service = SyncService(
            kcrm,
            trello,
            FakeLogger(),
            self.keycrm_status_map,
            require_phone=False,
            auto_create_trello_cards=True,
        )
        errors = service.run_once()

        self.assertEqual(errors, [])
        self.assertEqual(len(trello.cards), 1)
        self.assertIn("lead_id:7007", trello.cards[0].desc)
        self.assertEqual(trello.cards[0].list_id, "L2")


if __name__ == "__main__":
    unittest.main()
