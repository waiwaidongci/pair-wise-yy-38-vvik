import tempfile, unittest
from pathlib import Path
from src import rules
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service

GATES=[{"id":"G1","capacity":800},{"id":"G2","capacity":800},{"id":"G3","capacity":500,"restricted":True}]

def make_payload(**kw):
    base={"title":"夜班泄洪调度","description":"夜班录入","severity":"routine",
          "reservoir_level":145.0,"flood_limit":145.0,"inflow":1000.0,
          "rise_rate":0.0,"downstream_alarm":"normal","gates":GATES}
    base.update(kw); return base

class DischargePlanTest(unittest.TestCase):
    def test_severe_downstream_caps_at_half_inflow(self):
        plan=rules.compute_discharge_plan(147,145,1200,0.5,'severe',GATES)
        self.assertLessEqual(plan["planned_discharge"],600.0)
        self.assertTrue(plan["capped_by_downstream"])
    def test_restricted_gates_only_when_needed(self):
        plan=rules.compute_discharge_plan(145,145,1000,0,'normal',GATES)
        self.assertEqual(plan["gate_ids"],["G1","G2"]); self.assertFalse(plan["needs_restricted"])
        big=rules.compute_discharge_plan(148,145,3000,0,'normal',GATES)
        self.assertIn("G3",big["gate_ids"]); self.assertTrue(big["needs_restricted"]); self.assertTrue(big["review_required"])
    def test_rising_level_requires_review(self):
        plan=rules.compute_discharge_plan(146,145,1200,0.3,'normal',GATES)
        self.assertTrue(plan["level_rising"]); self.assertTrue(plan["review_required"])
    def test_gate_validation(self):
        with self.assertRaises(ValidationError): rules.parse_gates([])
        with self.assertRaises(ValidationError): rules.parse_gates([{"id":"G1","capacity":100},{"id":"G1","capacity":200}])
        with self.assertRaises(ValidationError): rules.parse_gates([{"id":"G1","capacity":0}])

class DischargeWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def create(self,**kw):
        return self.service.create_item(make_payload(**kw),"night",'duty_officer')
    def advance(self,item,target,role):
        return self.service.transition(item["id"],target,item["version"],"worker",role)
    def test_authorization_requires_chief_review_when_rising(self):
        item=self.create(rise_rate=0.5)
        self.assertEqual(item["severity"],'attention')
        item=self.advance(item,'checked','duty_officer')
        with self.assertRaises(ConflictError): self.advance(item,'authorized','chief_engineer')
        with self.assertRaises(PermissionDenied):
            self.service.add_record(item["id"],{"kind":"review","detail":"同意"},"night",'duty_officer')
        self.service.add_record(item["id"],{"kind":"review","detail":"同意按方案泄洪"},"chief",'chief_engineer')
        item=self.service.get_item(item["id"],"viewer")
        item=self.advance(item,'authorized','chief_engineer')
        self.assertEqual(item["status"],'authorized')
    def test_calm_plan_authorizes_without_review(self):
        item=self.create()
        self.assertFalse(item["plan"]["review_required"])
        item=self.advance(item,'checked','duty_officer')
        item=self.advance(item,'authorized','chief_engineer')
        self.assertEqual(item["status"],'authorized')
    def test_feedback_within_tolerance_then_close(self):
        item=self.create()
        item=self.advance(item,'checked','duty_officer')
        item=self.advance(item,'authorized','chief_engineer')
        item=self.advance(item,'executed','dispatcher')
        with self.assertRaises(ConflictError): self.advance(item,'closed','chief_engineer')
        item=self.service.feedback(item["id"],1050.0,None,"worker",'dispatcher')
        self.assertEqual(item["status"],'executed'); self.assertAlmostEqual(item["deviation"],0.05)
        item=self.advance(item,'closed','chief_engineer')
        self.assertEqual(item["status"],'closed')
    def test_feedback_deviation_returns_for_review(self):
        item=self.create()
        item=self.advance(item,'checked','duty_officer')
        item=self.advance(item,'authorized','chief_engineer')
        item=self.advance(item,'executed','dispatcher')
        with self.assertRaises(ValidationError): self.service.feedback(item["id"],1500.0,None,"worker",'dispatcher')
        item=self.service.feedback(item["id"],1500.0,"闸门启闭机故障","worker",'dispatcher')
        self.assertEqual(item["status"],'checked')
        with self.assertRaises(ConflictError): self.advance(item,'authorized','chief_engineer')
        self.service.add_record(item["id"],{"kind":"review","detail":"故障已排除，重新授权"},"chief",'chief_engineer')
        item=self.service.get_item(item["id"],"viewer")
        item=self.advance(item,'authorized','chief_engineer')
        item=self.advance(item,'executed','dispatcher')
        item=self.service.feedback(item["id"],1020.0,None,"worker",'dispatcher')
        item=self.advance(item,'closed','chief_engineer')
        self.assertEqual(item["status"],'closed')
        self.assertEqual(self.repo.open_record_count(item["id"]),0)
    def test_executed_cannot_return_via_generic_transition(self):
        item=self.create()
        item=self.advance(item,'checked','duty_officer')
        item=self.advance(item,'authorized','chief_engineer')
        item=self.advance(item,'executed','dispatcher')
        with self.assertRaises(ConflictError): self.advance(item,'checked','duty_officer')
    def test_reserved_record_kinds_rejected(self):
        item=self.create()
        with self.assertRaises(ValidationError):
            self.service.add_record(item["id"],{"kind":"feedback","detail":"伪造反馈"},"worker",'dispatcher')
        with self.assertRaises(ValidationError):
            self.service.add_record(item["id"],{"kind":"deviation","detail":"伪造偏差"},"worker",'dispatcher')
    def test_feedback_roles_and_state(self):
        item=self.create()
        with self.assertRaises(ConflictError): self.service.feedback(item["id"],1000.0,None,"worker",'dispatcher')
        item=self.advance(item,'checked','duty_officer')
        item=self.advance(item,'authorized','chief_engineer')
        item=self.advance(item,'executed','dispatcher')
        with self.assertRaises(PermissionDenied): self.service.feedback(item["id"],1000.0,None,"worker",'viewer')
if __name__=="__main__": unittest.main()
