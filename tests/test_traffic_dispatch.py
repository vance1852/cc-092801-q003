from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from portfolio_ops.api import JsonApplication
from portfolio_ops.clock import FrozenClock
from portfolio_ops.errors import Conflict, Forbidden, MetricSeriesUnavailable, ValidationFailed
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

    def test_scenario_requires_explicit_metric_binding(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index_drop_percent": "9"})

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}, "metric_binding": {"metric_series": "HUMIDITY", "source_revision": "r-23", "effective_date": "2026-09-23"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["adopted_metric"]["metric_series"], "HUMIDITY")
        self.assertEqual(first["adopted_metric"]["source_revision"], "r-23")
        self.assertEqual(first["adopted_metric"]["duty_date"], "2026-09-23")
        self.assertEqual(first["adopted_metric"]["observed_at"], "2026-09-23T21:00:00Z")

    def test_scenario_does_not_pick_another_series_registered_same_day(self) -> None:
        # 同一天先登记拥挤指数，再登记临床差异化（CONGESTION）——旧逻辑会取最后一条 CONGESTION。
        humidity = self.risk_record(23, "98")
        self.service.record_risk_record("plan", {"risk_index": "CONGESTION", "duty_date": "2026-09-23", "index_value": "72", "source_revision": "diff-23", "observed_at": "2026-09-23T22:30:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "humidity-only", "name": "湿度情景", "risk_index_drop_percent": "9", "metric_binding": {"metric_series": "HUMIDITY", "source_revision": "r-23", "effective_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "humidity-only", 1)
        run = self.service.run_scenario("plan", "humidity-only", "2026-09-23")
        self.assertEqual(run["adopted_metric"]["risk_record_id"], humidity["risk_record_id"])
        self.assertEqual(run["adopted_metric"]["index_value"], "98")
        self.assertEqual(run["adopted_metric"]["metric_series"], "HUMIDITY")
        self.assertEqual(run["projected_risk_index_cny"], "89.18")

    def test_missing_bound_series_or_revision_is_business_error_not_guess(self) -> None:
        self.risk_record(23, "98")
        self.service.create_scenario("plan", {"scenario_id": "missing-series", "name": "缺系列", "metric_binding": {"metric_series": "CONGESTION", "source_revision": "c-23", "effective_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "missing-series", 1)
        with self.assertRaises(MetricSeriesUnavailable):
            self.service.run_scenario("plan", "missing-series", "2026-09-23")
        self.service.create_scenario("plan", {"scenario_id": "missing-rev", "name": "缺版本", "metric_binding": {"metric_series": "HUMIDITY", "source_revision": "r-23-recalled", "effective_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "missing-rev", 1)
        with self.assertRaises(MetricSeriesUnavailable):
            self.service.run_scenario("plan", "missing-rev", "2026-09-23")
        # 失败不得留下任何运行记录
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM response_scenario_runs").fetchone()[0],
            0,
        )

    def test_historical_run_replays_input_snapshot_and_resists_revisions(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "pinned", "name": "钉住版本", "risk_index_drop_percent": "9", "metric_binding": {"metric_series": "HUMIDITY", "source_revision": "r-23", "effective_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "pinned", 1)
        first = self.service.run_scenario("plan", "pinned", "2026-09-23")
        # 事后补登同一日期的修订版本：绑定 r-23 的情景重放时仍采用原证据
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-23", "index_value": "55", "source_revision": "r-23-corrected", "observed_at": "2026-09-24T02:00:00Z"})
        again = self.service.run_scenario("plan", "pinned", "2026-09-23")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["run_id"], first["run_id"])
        self.assertEqual(again["adopted_metric"]["source_revision"], "r-23")
        self.assertEqual(again["projected_risk_index_cny"], "89.18")
        detail = self.service.get_scenario_run("audit", first["run_id"])
        self.assertEqual(detail["input_snapshot"]["adopted_metric"]["source_revision"], "r-23")
        self.assertEqual(detail["input_snapshot"]["adopted_metric"]["index_value"], "98")
        # 即使运营状态（库存）后续变化，旧运行保存的输入快照与结论不漂移
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-2", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "90000", "unit_cost_cny": "88", "received_at": "2026-09-24T07:00:00Z"})
        old_detail = self.service.get_scenario_run("audit", first["run_id"])
        self.assertEqual(old_detail["projected_risk_index_cny"], "89.18")
        self.assertEqual(old_detail["input_snapshot"]["inventory"][0]["available_units"], 60000.0)
        # 绑定到新修订的情景采用新证据，得到独立运行而不改动旧结论
        self.service.create_scenario("plan", {"scenario_id": "pinned-v2", "name": "修订版本", "risk_index_drop_percent": "9", "metric_binding": {"metric_series": "HUMIDITY", "source_revision": "r-23-corrected", "effective_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "pinned-v2", 1)
        revised = self.service.run_scenario("plan", "pinned-v2", "2026-09-23")
        self.assertFalse(revised["replayed"])
        self.assertEqual(revised["projected_risk_index_cny"], "50.05")
        self.assertEqual(self.service.get_scenario_run("audit", first["run_id"])["projected_risk_index_cny"], "89.18")

    def test_run_detail_api_and_audit_event_show_metric_provenance(self) -> None:
        self.risk_record(23, "98")
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "metric_binding": {"metric_series": "HUMIDITY", "source_revision": "r-23", "effective_date": "2026-09-23"}})
        self.service.approve_scenario("risk", "restart", 1)
        run = self.service.run_scenario("plan", "restart", "2026-09-23")
        app = JsonApplication(self.service)
        response = app.handle("GET", f"/scenario_runs/{run['run_id']}", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["adopted_metric"]["metric_series"], "HUMIDITY")
        self.assertEqual(response.body["adopted_metric"]["observed_at"], "2026-09-23T21:00:00Z")
        self.assertEqual(response.body["input_snapshot"]["adopted_metric"]["source_revision"], "r-23")
        event = self.connection.execute(
            "SELECT payload_json FROM traffic_audit_events WHERE event_type='scenario.executed' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["adopted_metric"]["metric_series"], "HUMIDITY")
        self.assertEqual(payload["adopted_metric"]["source_revision"], "r-23")
        self.assertEqual(payload["adopted_metric"]["observed_at"], "2026-09-23T21:00:00Z")


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
