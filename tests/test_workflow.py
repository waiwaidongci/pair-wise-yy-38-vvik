import tempfile
import unittest
from pathlib import Path

from src.repository import Repository
from src.service import Service


def gates():
    return [{"name": "1号", "state": "available", "capacity": 300.0},
            {"name": "2号", "state": "restricted", "capacity": 300.0}]


def payload(**overrides):
    data = {"level": 94.5, "flood_limit": 95.0, "inflow": 200.0, "rise": 0.0,
            "downstream": "normal", "construction_limit": "", "gates": gates()}
    data.update(overrides)
    return data


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _authorize(self, order):
        self.service.add_record(order["id"], {"kind": "review",
                                "detail": "复核水情与闸门方案，同意下泄"},
                                "张总工", "chief_engineer")
        return self.service.authorize(order["id"], order["version"],
                                      "张总工", "chief_engineer")

    def test_complete_workflow_and_audit(self):
        order = self.service.create_order(payload(), "李值班", "duty_officer")
        self.assertEqual(order["status"], "draft")
        self.assertEqual(order["order_no"], "XF-0001")
        self.assertEqual(order["planned_flow"], 200)
        self.assertEqual(order["gate_count"], 1)

        authorized = self._authorize(order)
        self.assertEqual(authorized["status"], "authorized")

        executed = self.service.execute(
            authorized["id"], {"actual_flow": 205, "expected_version": authorized["version"]},
            "王调度", "dispatcher")
        self.assertEqual(executed["status"], "executed")
        self.assertTrue(executed["within_tolerance"])

        closed = self.service.close_order(
            executed["id"], executed["version"], "张总工", "chief_engineer")
        self.assertEqual(closed["status"], "closed")

        events = self.service.audit("viewer", order["id"])
        actions = [e["action"] for e in events]
        self.assertEqual(actions, ["create", "record", "authorize", "execute", "close"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_deviation_over_ten_percent_returns_to_recheck(self):
        order = self.service.create_order(
            payload(rise=0.2), "李值班", "duty_officer")
        self.assertTrue(order["chief_review_required"])
        order = self._authorize(order)

        # 偏差超过一成且未说明原因 → 拒绝
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            self.service.execute(order["id"],
                                 {"actual_flow": 250, "expected_version": order["version"]},
                                 "王调度", "dispatcher")

        # 说明原因后退回待复核
        recheck = self.service.execute(
            order["id"],
            {"actual_flow": 250, "deviation_reason": "下游施工段临时压减开度",
             "expected_version": order["version"]},
            "王调度", "dispatcher")
        self.assertEqual(recheck["status"], "recheck")
        self.assertGreater(recheck["deviation_ratio"], 0.10)
        self.assertFalse(recheck["within_tolerance"])

        # 旧复核意见已随授权闭环，重新授权必须补充新复核
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.authorize(recheck["id"], recheck["version"],
                                   "张总工", "chief_engineer")
        recheck = self.service.get_order(recheck["id"], "viewer")
        recheck = self._authorize(recheck)
        self.assertEqual(recheck["status"], "authorized")

    def test_preview_does_not_persist(self):
        result = self.service.preview(
            payload(downstream="severe", level=96.0, rise=0.5), "viewer")
        self.assertEqual(result["plan"]["planned_flow"], 100)
        self.assertTrue(result["plan"]["severe_cap_applied"])
        self.assertEqual(self.service.list_orders("viewer"), [])


if __name__ == "__main__":
    unittest.main()
