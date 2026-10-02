from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from portfolio_ops.api import JsonApplication
from portfolio_ops.clock import FrozenClock
from portfolio_ops.errors import BusinessRuleViolation, Conflict, Forbidden, ValidationFailed
from portfolio_ops.planning import AllocationRequest, RiskPoint, allocate_capacity, latest_streak
from portfolio_ops.service import CollectionLogisticsService
from portfolio_ops.risk import DemandBucket, inventory_coverage, mark_to_risk, traffic_gap


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            RiskPoint("2026-09-18", Decimal("108")),
            RiskPoint("2026-09-19", Decimal("105")),
            RiskPoint("2026-09-20", Decimal("102")),
            RiskPoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["dispatch_id"], "first")
        self.assertEqual(rows[0]["allocated_units"], "70.000")
        self.assertEqual(rows[1]["allocated_units"], "30.000")

    def test_inventory_coverage_and_traffic_gap(self) -> None:
        coverage = inventory_coverage(
            [{"center_id": "receiving-vault", "preservation_resource_kind": "tow-truck", "available_units": "250"}],
            [DemandBucket("receiving-vault", "tow-truck", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = traffic_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["traffic_gap"], "30.000")

    def test_mark_to_risk_groups_deterministically(self) -> None:
        result = mark_to_risk(
            [{"position_id": "p1", "risk_index": "HUMIDITY", "quantity_units": "100", "baseline_value": "105"}],
            {"HUMIDITY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class CollectionLogisticsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"center_id": "collection-east", "name": "北部实验样本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
        self.service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
        self.service.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def risk_record(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": f"2026-09-{day}", "index_value": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_risk_record_revisions_preserve_history(self) -> None:
        first = self.risk_record(23, "98")
        second = self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-23", "index_value": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["risk_record_id"], second["risk_record_id"])
        rows = self.connection.execute("SELECT * FROM risk_index_risk_records ORDER BY risk_record_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_risk_record_id"], rows[0]["risk_record_id"])

    def test_dispatch_request_replay_and_payload_conflict(self) -> None:
        payload = {"dispatch_id": "nom-1", "corridor_id": "transfer-east-1", "specimen_event_id": "herbarium-room", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_dispatch("dispatch", payload)
        self.assertEqual(first, self.service.submit_dispatch("dispatch", payload))
        changed = dict(payload, requested_units="81000")
        with self.assertRaises(Conflict):
            self.service.submit_dispatch("dispatch", changed)

    def test_outage_reduces_allocation_and_deployment_consumes_inventory(self) -> None:
        self.service.announce_restriction("risk", "transfer-east-1", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_dispatch("dispatch", {"dispatch_id": f"nom-{number}", "corridor_id": "transfer-east-1", "specimen_event_id": f"specimen_event-{number}", "duty_date": "2026-09-25", "requested_units": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "transfer-east-1", "2026-09-25")
        self.assertEqual(allocation["available_units"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_units"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        deployment = self.service.dispatch_deployment("dispatch", "deployment-1", "nom-1", "lot-1", 2)
        self.assertEqual(deployment["deployed_units"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_units"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}, "risk_binding": {"risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["metric_adopted"], {
            "risk_index": "HUMIDITY",
            "risk_record_id": 1,
            "duty_date": "2026-09-23",
            "index_value": "98",
            "source_revision": "r-23",
            "observed_at": "2026-09-23T21:00:00Z",
        })

    def test_scenario_requires_explicit_binding(self) -> None:
        self.risk_record(23, "98")
        with self.assertRaisesRegex(ValidationFailed, "risk_binding"):
            self.service.create_scenario("plan", {"scenario_id": "no-bind", "name": "未绑定指标", "risk_index_drop_percent": "9"})

    def test_run_fails_when_bound_series_missing_rather_than_guessing(self) -> None:
        # 同一天登记了两个不同指标系列：拥挤指数 HUMIDITY 与差异化评分 CONGESTION，
        # 后登记的差异化评分曾被“当天最后一条”逻辑错误代入。
        self.risk_record(23, "98")
        self.service.record_risk_record("plan", {"risk_index": "CONGESTION", "duty_date": "2026-09-23", "index_value": "31.5", "source_revision": "diff-23-v1", "observed_at": "2026-09-23T22:30:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "crowded-target", "name": "全球权益拥挤度", "risk_index_drop_percent": "9", "risk_binding": {"risk_index": "HAZMAT", "source_revision": "r-23", "duty_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "crowded-target", 1)
        with self.assertRaisesRegex(BusinessRuleViolation, "HAZMAT"):
            self.service.run_scenario("plan", "crowded-target", "2026-09-23")
        # 系列存在但版本不存在同样报业务错误，不回退到当日其他版本。
        self.service.create_scenario("plan", {"scenario_id": "crowded-version", "name": "全球权益拥挤度版本", "risk_index_drop_percent": "9", "risk_binding": {"risk_index": "HUMIDITY", "source_revision": "r-99", "duty_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "crowded-version", 1)
        with self.assertRaisesRegex(BusinessRuleViolation, "r-99"):
            self.service.run_scenario("plan", "crowded-version", "2026-09-23")

    def test_historical_run_replays_from_snapshot_despite_later_revisions(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "snap", "name": "快照重放", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}, "risk_binding": {"risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "snap", 1)
        first = self.service.run_scenario("plan", "snap", "2026-09-23")
        first_projected = first["projected_risk_index_cny"]
        # 同日补登记一个“更晚”的新版本——旧逻辑会把最后一条当作输入。
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-23", "index_value": "70", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T23:30:00Z"})
        detail = self.service.scenario_run("plan", first["run_id"])
        self.assertEqual(detail["metric_adopted"]["source_revision"], "r-23")
        self.assertEqual(detail["metric_adopted"]["index_value"], "98")
        replayed = self.service.replay_scenario_run("plan", first["run_id"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["projected_risk_index_cny"], first_projected)
        self.assertEqual(replayed["metric_adopted"]["source_revision"], "r-23")
        self.assertEqual(replayed["metric_adopted"]["observed_at"], "2026-09-23T21:00:00Z")
        # 新证据应产生新的运行（绑定到新版本后），旧结论不漂移。
        self.service.create_scenario("plan", {"scenario_id": "snap-2", "name": "快照重放修订", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}, "risk_binding": {"risk_index": "HUMIDITY", "source_revision": "r-23-corrected", "duty_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "snap-2", 1)
        revised = self.service.run_scenario("plan", "snap-2", "2026-09-23")
        self.assertNotEqual(revised["run_id"], first["run_id"])
        self.assertNotEqual(revised["projected_risk_index_cny"], first_projected)
        again = self.service.replay_scenario_run("plan", first["run_id"])
        self.assertEqual(again["projected_risk_index_cny"], first_projected)

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE traffic_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/risk_records/summary/HUMIDITY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
