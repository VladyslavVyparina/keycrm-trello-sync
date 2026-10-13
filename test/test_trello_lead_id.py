import unittest

from src.trello_client import desc_has_lead_id_line, extract_lead_ids_from_desc


class LeadIdLineTest(unittest.TestCase):
    def test_one_does_not_match_ten(self):
        desc = "lead_id:10\nfoo"
        self.assertFalse(desc_has_lead_id_line(desc, "1"))
        self.assertTrue(desc_has_lead_id_line(desc, "10"))

    def test_extract_all_numeric_ids(self):
        desc = "x\nlead_id:1\nlead_id:2\n"
        self.assertEqual(extract_lead_ids_from_desc(desc), ["1", "2"])


if __name__ == "__main__":
    unittest.main()
