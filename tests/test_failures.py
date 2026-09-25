import tempfile
import unittest
from pathlib import Path

from src.domain import (ConflictError, PermissionDenied, ValidationError)
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


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_role_matrix(self):
        order = self.service.create_order(payload(), "李值班", "duty_officer")
        # 观摩者不能授权
        with self.assertRaises(PermissionDenied):
            self.service.authorize(order["id"], 1, "路人", "viewer")
        # 值班员不能授权
        with self.assertRaises(PermissionDenied):
            self.service.authorize(order["id"], 1, "李值班", "duty_officer")
        # 非总工不能登记复核意见
        with self.assertRaises(PermissionDenied):
            self.service.add_record(order["id"], {"kind": "review", "detail": "同意"},
                                    "李值班", "duty_officer")
        # 观摩者不能录单
        with self.assertRaises(PermissionDenied):
            self.service.create_order(payload(), "路人", "viewer")

    def test_authorization_requires_review_record(self):
        order = self.service.create_order(payload(), "李值班", "duty_officer")
        with self.assertRaises(ConflictError):
            self.service.authorize(order["id"], 1, "张总工", "chief_engineer")

    def test_version_conflict(self):
        order = self.service.create_order(payload(), "李值班", "duty_officer")
        self.service.add_record(order["id"], {"kind": "review", "detail": "同意"},
                                "张总工", "chief_engineer")
        with self.assertRaises(ConflictError):
            self.service.authorize(order["id"], 99, "张总工", "chief_engineer")

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.service.create_order(payload(level="高"), "李值班", "duty_officer")
        with self.assertRaises(ValidationError):
            self.service.create_order(payload(downstream="flood"), "李值班", "duty_officer")
        with self.assertRaises(ValidationError):
            # 空闸门清单
            self.service.create_order(payload(gates=[]), "李值班", "duty_officer")
        with self.assertRaises(ValidationError):
            # 闸门名称重复
            self.service.create_order(
                payload(gates=[{"name": "1号", "state": "available", "capacity": 100},
                               {"name": "1号", "state": "available", "capacity": 100}]),
                "李值班", "duty_officer")

    def test_cannot_close_with_open_records(self):
        order = self.service.create_order(payload(), "李值班", "duty_officer")
        self.service.add_record(order["id"], {"kind": "review", "detail": "同意"},
                                "张总工", "chief_engineer")
        order = self.service.authorize(order["id"], order["version"],
                                       "张总工", "chief_engineer")
        order = self.service.execute(
            order["id"], {"actual_flow": 200, "expected_version": order["version"]},
            "王调度", "dispatcher")
        self.service.add_record(order["id"], {"kind": "note", "detail": "现场待确认"},
                                "王调度", "dispatcher")
        with self.assertRaises(ConflictError):
            self.service.close_order(order["id"], order["version"],
                                     "张总工", "chief_engineer")

    def test_illegal_transitions(self):
        order = self.service.create_order(payload(), "李值班", "duty_officer")
        # 未授权不能执行
        with self.assertRaises(ConflictError):
            self.service.execute(
                order["id"], {"actual_flow": 200, "expected_version": 1},
                "王调度", "dispatcher")


if __name__ == "__main__":
    unittest.main()
