import unittest

from src import rules
from src.domain import ConflictError, ValidationError


def gate(name, state="available", capacity=300.0):
    return {"name": name, "state": state, "capacity": capacity}


class RulesTest(unittest.TestCase):
    def test_balance_when_calm(self):
        # 库位低于汛限且未上涨：维持进出平衡，1扇可用闸门即可
        plan = rules.plan_order(200, 0.0, 94.0, 95.0, "normal",
                                [gate("1号"), gate("2号")])
        self.assertEqual(plan["planned_flow"], 200)
        self.assertEqual(plan["gate_count"], 1)
        self.assertFalse(plan["chief_review_required"])
        self.assertFalse(plan["uses_restricted_gates"])

    def test_level_above_limit_increases_discharge(self):
        # 超汛限0.5米：增加 0.5*30 = 15
        plan = rules.plan_order(200, 0.0, 95.5, 95.0, "normal",
                                [gate("1号"), gate("2号")])
        self.assertEqual(plan["planned_flow"], 215)

    def test_rising_level_pre_release_and_requires_chief(self):
        # 上涨0.2米/小时：预泄6，且必须总工复核
        plan = rules.plan_order(200, 0.2, 94.0, 95.0, "normal",
                                [gate("1号"), gate("2号")])
        self.assertEqual(plan["planned_flow"], 206)
        self.assertTrue(plan["level_rising"])
        self.assertTrue(plan["chief_review_required"])

    def test_severe_downstream_caps_at_half_inflow(self):
        # 严重告警：即便超汛限，下泄也不得超过入库一半(200)
        plan = rules.plan_order(400, 0.5, 96.0, 95.0, "severe",
                                [gate("1号"), gate("2号")])
        self.assertEqual(plan["planned_flow"], 200)
        self.assertTrue(plan["severe_cap_applied"])

    def test_maintenance_gate_never_selected(self):
        gates = [gate("1号", "maintenance", 300), gate("2号", "available", 250)]
        plan = rules.plan_order(300, 0.0, 94.0, 95.0, "normal", gates)
        self.assertNotIn("1号", plan["selected_gates"])
        self.assertIn("2号", plan["selected_gates"])

    def test_restricted_gate_only_when_needed_and_flags_review(self):
        gates = [gate("1号", "available", 200), gate("2号", "restricted", 300)]
        plan = rules.plan_order(400, 0.0, 94.0, 95.0, "normal", gates)
        # 可用闸门不足，必须启用受限闸门
        self.assertTrue(plan["uses_restricted_gates"])
        self.assertTrue(plan["chief_review_required"])
        self.assertEqual(plan["gate_count"], 2)

    def test_restricted_gate_not_used_when_available_capacity_sufficient(self):
        gates = [gate("1号", "available", 500), gate("2号", "restricted", 300)]
        plan = rules.plan_order(300, 0.0, 94.0, 95.0, "normal", gates)
        self.assertFalse(plan["uses_restricted_gates"])
        self.assertEqual(plan["selected_gates"], ["1号"])

    def test_capacity_shortfall_flag(self):
        gates = [gate("1号", "available", 100), gate("2号", "restricted", 100)]
        plan = rules.plan_order(500, 0.0, 94.0, 95.0, "normal", gates)
        self.assertTrue(plan["capacity_shortfall"])

    def test_zero_flow_opens_no_gate(self):
        plan = rules.plan_order(0, 0.0, 94.0, 95.0, "normal", [gate("1号")])
        self.assertEqual(plan["planned_flow"], 0)
        self.assertEqual(plan["gate_count"], 0)
        self.assertEqual(plan["selected_gates"], [])

    def test_deviation_threshold(self):
        self.assertTrue(rules.deviation_within_tolerance(100, 110))
        self.assertTrue(rules.deviation_within_tolerance(100, 90))
        self.assertFalse(rules.deviation_within_tolerance(100, 111))
        self.assertFalse(rules.deviation_within_tolerance(100, 89))

    def test_transition_guards(self):
        self.assertTrue(rules.can_transition("draft", "authorized"))
        self.assertFalse(rules.can_transition("draft", "executed"))
        with self.assertRaises(ConflictError):
            rules.validate_transition("draft", "closed")

    def test_severity_and_priority(self):
        calm = rules.compute_severity(94.0, 95.0, 0.0, "normal")
        severe = rules.compute_severity(95.2, 95.0, 0.3, "severe")
        self.assertEqual(calm, "routine")
        self.assertEqual(severe, "emergency")
        with self.assertRaises(ValidationError):
            rules.priority_score("not-a-severity")


if __name__ == "__main__":
    unittest.main()
