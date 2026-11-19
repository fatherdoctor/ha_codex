"""Schedule time edges, durable reservations, owner authority and agent boundaries."""

import concurrent.futures
import copy
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_server import server
import schedule_api
import scheduling
import scheduling_mcp
import tool_threads


def setUpModule():
    """These tests cover the scheduling code, which is switched off in this series; turn it on for them."""
    global _saved_switch
    _saved_switch = scheduling.SCHEDULED_RUNS
    scheduling.SCHEDULED_RUNS = True


def tearDownModule():
    scheduling.SCHEDULED_RUNS = _saved_switch


NOW = datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()


def proposal(**changes):
    return {
        "name": "Sample inspection",
        "prompt": "Inspect the sample configuration.",
        "mode": "default",
        "spec": {"kind": "interval", "seconds": 3600},
        **changes,
    }


THREAD = "11111111-2222-4333-8444-555555555555"


def open_exchange(test, task_id):
    """Begin the task's running exchange and bind the chat's thread to it; return the thread id a request names."""
    server.ha_data.begin(task_id)
    tool_threads.CHATS.bind(task_id, THREAD)
    test.addCleanup(tool_threads.CHATS.release, task_id)
    test.addCleanup(server.ha_data.end, task_id)
    return THREAD


class FakeWorker:
    def __init__(self):
        self.tasks = {}
        self.sent = []
        self.blocked = False

    def busy(self):
        return self.blocked

    def snapshot(self, task_id):
        return self.tasks.get(task_id)

    def submit(self, task_id, prompt, mode, name, schedule_id):
        self.sent.append((task_id, prompt, mode, name, schedule_id))
        self.tasks[task_id] = {"status": "queued"}
        self.blocked = True


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "schedules.sqlite3"
        self.store = scheduling.Store(self.path)
        self.worker = FakeWorker()
        self.engine = scheduling.Engine(self.store, self.worker)

    def save(self, **kwargs):
        return self.store.save(proposal(**kwargs), owner=True, now=NOW)

    def test_interval_has_minimum_and_rejects_nonfinite_and_bool(self):
        for value in (0, 59, True, 60.5, float("nan"), 366 * 86400 + 1):
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaises(ValueError),
            ):
                self.save(spec={"kind": "interval", "seconds": value})
        self.assertEqual(
            self.save(spec={"kind": "interval", "seconds": 60})["next_due"], NOW + 60
        )

    def test_unsupported_modes_fields_and_oversized_prompts_are_refused(self):
        for payload in (
            proposal(mode="danger-full-access"),
            proposal(approved=True),
            proposal(prompt="x" * 16001),
            proposal(spec={"kind": "shell", "command": "false"}),
        ):
            with self.assertRaises(ValueError):
                self.store.save(payload, owner=True, now=NOW)

    def test_once_requires_future_time_with_timezone(self):
        for at in (NOW, "not-a-date", float("inf"), True):
            with self.assertRaises(ValueError):
                self.save(spec={"kind": "once", "at": at})
        value = self.save(spec={"kind": "once", "at": "2027-03-02T01:00:00+01:00"})
        self.assertEqual(value["next_due"], NOW + 86400)

    def test_cron_steps_ranges_lists_and_sunday_alias(self):
        value = self.save(spec={"kind": "cron", "expression": "*/15 0-2,23 * * 0,7"})
        self.assertEqual(
            datetime.fromtimestamp(value["next_due"], timezone.utc).isoformat(),
            "2027-03-07T00:00:00+00:00",
        )
        spec = {"kind": "cron", "expression": "*/15 * * * *"}
        self.assertEqual(scheduling.next_due(spec, NOW + 59), NOW + 900)
        self.assertEqual(scheduling.next_due(spec, NOW + 900), NOW + 1800)

    def test_cron_day_fields_use_or_and_wildcard_uses_other_field(self):
        spec = {"kind": "cron", "expression": "0 0 2 * 3"}
        self.assertEqual(scheduling.next_due(spec, NOW), NOW + 86400)
        self.assertEqual(
            scheduling.next_due({"kind": "cron", "expression": "0 0 * * 3"}, NOW),
            NOW + 2 * 86400,
        )
        self.assertEqual(
            scheduling.next_due({"kind": "cron", "expression": "0 0 */2 * 1"}, NOW),
            NOW + 14 * 86400,
        )

    def test_cron_midnight_month_boundary_and_impossible_day(self):
        date = datetime(2027, 3, 31, 23, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            scheduling.next_due({"kind": "cron", "expression": "0 0 1 * *"}, date),
            date + 60,
        )
        for expression in (
            "0 0 31 2 *",
            "60 * * * *",
            "*/0 * * * *",
            "* * * *",
            "0 0 * JAN *",
            "0 0 * * 8",
            "* * * * * extra",
        ):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.save(spec={"kind": "cron", "expression": expression})

    def test_agent_proposal_cannot_self_approve_or_be_edited_by_the_agent(self):
        value = self.store.save(proposal(), owner=False, author="codex", now=NOW)
        self.assertFalse(value["approved"])
        self.assertFalse(value["enabled"])
        self.engine.tick(NOW + 3601)
        self.assertEqual(self.worker.sent, [])
        with self.assertRaises(LookupError):
            self.store.save(
                proposal(prompt="Inspect another sample."),
                owner=False,
                author="codex",
                now=NOW,
                schedule_id=value["id"],
            )
        with self.assertRaises(ValueError):
            self.store.action(value["id"], "approve", NOW, "stale-revision")
        self.store.action(value["id"], "approve", NOW, value["revision"])
        with self.assertRaises(LookupError):
            self.store.save(
                proposal(),
                owner=False,
                author="codex",
                now=NOW,
                schedule_id=value["id"],
            )
        self.assertTrue(self.store.schedules()[0]["enabled"])

    def test_pause_resume_delete_and_expired_once(self):
        value = self.save()
        self.store.action(value["id"], "pause", NOW)
        self.engine.tick(NOW + 4000)
        self.assertFalse(self.worker.sent)
        self.store.action(value["id"], "resume", NOW + 4000)
        self.assertEqual(self.store.schedules()[0]["next_due"], NOW + 7600)
        self.store.action(value["id"], "delete", NOW)
        self.assertEqual(self.store.schedules(), [])
        once = self.save(spec={"kind": "once", "at": NOW + 60})
        with self.assertRaises(ValueError):
            self.store.action(once["id"], "resume", NOW + 61)

    def test_restart_persists_schedule_and_coalesces_missed_intervals(self):
        self.save()
        self.store = scheduling.Store(self.path)
        self.engine = scheduling.Engine(self.store, self.worker)
        self.engine.tick(NOW + 5 * 3600)
        self.assertEqual(len(self.worker.sent), 1)
        self.assertEqual(self.store.schedules()[0]["next_due"], NOW + 6 * 3600)
        self.engine.tick(NOW + 5 * 3600)
        self.assertEqual(len(self.worker.sent), 1)

    def test_crash_between_reservation_and_task_save_is_not_retried(self):
        value = self.save()
        claim = self.store.reserve(NOW + 3600, value["id"])
        self.engine = scheduling.Engine(scheduling.Store(self.path), self.worker)
        self.engine.tick(NOW + 3601)
        self.assertEqual(self.store.runs()[0]["status"], "interrupted")
        self.assertFalse(self.worker.sent)
        self.assertIn("not retried", self.store.runs()[0]["error"])

    def test_concurrent_reservations_only_claim_one_occurrence(self):
        value = self.save()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: scheduling.Store(self.path).reserve(
                        NOW + 3600, value["id"]
                    ),
                    range(8),
                )
            )
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_reservation_uses_current_approved_payload_after_edit_race(self):
        value = self.save()
        original = self.store.reserve

        def edited(now, schedule_id=None):
            self.store.save(
                proposal(
                    prompt="Current approved sample.",
                    spec={"kind": "once", "at": NOW + 1},
                ),
                owner=True,
                now=NOW,
                schedule_id=value["id"],
            )
            return original(now, schedule_id)

        with patch.object(self.store, "reserve", side_effect=edited):
            self.engine.tick(NOW + 3600)
        self.assertEqual(self.worker.sent[0][1], "Current approved sample.")

    def test_native_request_is_parked_and_never_auto_decided(self):
        self.save(mode="read-only")
        self.engine.tick(NOW + 3600)
        task_id = self.worker.sent[0][0]
        self.worker.tasks[task_id] = {
            "status": "running",
            "approvals": [{"status": "pending"}],
        }
        self.engine.tick(NOW + 7200)
        self.assertEqual(self.store.runs()[0]["status"], "waiting_for_approval")
        self.assertEqual(len(self.worker.sent), 1)
        self.assertEqual(self.worker.sent[0][2], "read-only")
        self.worker.tasks[task_id] = {
            "status": "failed",
            "approvals": [{"status": "expired"}],
        }
        self.engine.reconcile()
        self.assertEqual(self.store.runs()[0]["status"], "failed")

    def test_once_disables_after_claim_and_busy_skips(self):
        self.save(spec={"kind": "once", "at": NOW + 60})
        self.worker.blocked = True
        self.engine.tick(NOW + 61)
        self.assertFalse(self.worker.sent)
        self.assertFalse(self.store.runs())
        self.worker.blocked = False
        self.engine.tick(NOW + 62)
        self.assertFalse(self.store.schedules()[0]["enabled"])
        self.assertIsNone(self.store.schedules()[0]["next_due"])

    def test_daily_per_schedule_and_global_limits(self):
        value = self.save(spec={"kind": "interval", "seconds": 60})
        for i in range(scheduling.MAX_SCHEDULE_DAILY_RUNS):
            run = self.store.reserve(NOW + 60 * (i + 1), value["id"])
            self.assertIsNotNone(run)
            self.store.finish(run["id"], "completed")
        self.assertIsNone(self.store.reserve(NOW + 5000, value["id"]))
        self.assertIsNotNone(self.store.reserve(NOW + 86460, value["id"]))
        for i in range(scheduling.MAX_DAILY_RUNS - scheduling.MAX_SCHEDULE_DAILY_RUNS):
            other = self.save(name=f"Other {i}", spec={"kind": "once", "at": NOW + 60})
            self.assertIsNotNone(self.store.reserve(NOW + 5000, other["id"]))
        last = self.save(name="Last", spec={"kind": "once", "at": NOW + 60})
        self.assertIsNone(self.store.reserve(NOW + 5000, last["id"]))

    def test_only_completed_run_history_is_pruned(self):
        with patch.object(scheduling, "MAX_RUNS", 2):
            first = self.save(name="Pending", spec={"kind": "once", "at": NOW + 60})
            pending = self.store.reserve(NOW + 60, first["id"])
            for i in range(4):
                item = self.save(
                    name=f"Done {i}", spec={"kind": "once", "at": NOW + 60}
                )
                value = self.store.reserve(NOW + 60, item["id"])
                self.store.finish(value["id"], "completed")
            runs = self.store.runs()
            self.assertEqual(len(runs), 3)
            self.assertIn(pending["id"], {r["id"] for r in runs})

    def test_database_is_private_and_a_lead_author_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.save(proposal(), owner=False, author="lead", now=NOW)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = self.path.parent / "linked.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            scheduling.Store(link)


class SchedulingApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(server, "schedule_timezone", return_value="UTC"),
            patch.object(server, "DATA_ROOT", self.root),
            patch.object(server, "TASK_STATE_FILE", self.root / "tasks.json"),
            patch.object(server, "task_root", return_value=self.root / "tasks"),
            patch.object(
                server,
                "read_options",
                return_value={
                    **server.DEFAULT_OPTIONS,
                    "codex_sandbox": "workspace-write",
                },
            ),
            patch.object(
                server, "api_token", return_value="synthetic-worker-credential"
            ),
            patch.object(
                server,
                "lookup_ha_users",
                return_value=[
                    {
                        "id": "synthetic-admin",
                        "is_active": True,
                        "group_ids": ["system-admin"],
                    }
                ],
            ),
            patch.object(server, "start_background_task"),
            patch.object(server, "notify_permission_async"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        server.tasks.clear()
        server.active_task_runners.clear()
        server.running_processes.clear()
        server.message_queue.clear()
        server.admin_cache.clear()
        self.addCleanup(server.tasks.clear)
        self.addCleanup(server.active_task_runners.clear)
        self.addCleanup(server.running_processes.clear)
        self.client = server.app.test_client()
        self.service = server.schedule_service
        self.owner = {
            "X-Ingress-Path": "/sample",
            "X-Remote-User-Id": "synthetic-admin",
        }
        self.owner_env = {"REMOTE_ADDR": server.INGRESS_PROXY_IP}

    def request_owner(self, method, path, data=None):
        return self.client.open(
            path,
            method=method,
            json=data,
            headers=self.owner,
            environ_overrides=self.owner_env,
        )

    def test_management_requires_real_ingress_administrator(self):
        with patch.object(server, "lookup_ha_users", return_value=[]):
            self.assertEqual(
                self.request_owner("POST", "/schedules", proposal()).status_code, 403
            )
        server.admin_cache.clear()
        result = self.request_owner("POST", "/schedules", proposal())
        self.assertEqual(result.status_code, 201)
        self.assertTrue(result.json["schedule"]["approved"])
        self.assertEqual(result.headers["Cache-Control"], "no-store")

    def agent(self):
        """Open a running turn and return a function that sends one Codex socket request."""
        task_id = "synthetic-task"
        server.tasks[task_id] = {
            "status": "running",
            "current_turn_id": "synthetic-turn",
        }
        thread = open_exchange(self, task_id)
        return lambda op, args: self.service.dispatch(
            {"operation": op, "arguments": args, "thread_id": thread}
        )

    def test_agent_proposal_cannot_activate_and_edit_invalidates_owner_approval(self):
        call = self.agent()
        result = call("propose_schedule", proposal())
        schedule = self.service.engine().store.schedules()[0]
        self.assertEqual(result["schedule_id"], schedule["id"])
        self.assertFalse(schedule["enabled"])
        self.assertFalse(schedule["approved"])
        endpoint = "/schedules/" + schedule["id"] + "/approve"
        self.assertEqual(
            self.client.post(
                endpoint, json={"revision": schedule["revision"]}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.request_owner(
                "POST", endpoint, {"revision": schedule["revision"]}
            ).status_code,
            200,
        )
        with self.assertRaises(LookupError):
            self.service.engine().store.save(
                proposal(prompt="Changed sample."),
                owner=False,
                author="codex",
                now=NOW,
                schedule_id=schedule["id"],
            )
        self.assertTrue(self.service.engine().store.schedules()[0]["approved"])
        with self.assertRaises(ValueError):
            call("propose_schedule", proposal(approved=True))

    def test_redacted_agent_prompt_cannot_be_blindly_approved(self):
        self.agent()(
            "propose_schedule", proposal(prompt="A sample that must be hidden.")
        )
        value = self.service.engine().store.schedules()[0]
        with patch.object(
            server,
            "redact",
            side_effect=lambda text: "[redacted]" if "must be hidden" in text else text,
        ):
            result = self.request_owner(
                "POST",
                "/schedules/" + value["id"] + "/approve",
                {"revision": value["revision"]},
            )
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.service.engine().store.schedules()[0]["approved"])

    def test_owner_can_edit_pause_resume_and_delete(self):
        value = self.request_owner("POST", "/schedules", proposal()).json["schedule"]
        route = "/schedules/" + value["id"]
        self.assertEqual(
            self.request_owner("PATCH", route, proposal(mode="read-only")).status_code,
            200,
        )
        self.assertEqual(
            self.request_owner("POST", route + "/pause", {}).status_code, 200
        )
        self.assertFalse(
            self.request_owner("GET", "/schedules").json["schedules"][0]["enabled"]
        )
        self.assertEqual(
            self.request_owner("POST", route + "/resume", {}).status_code, 200
        )
        self.assertEqual(
            self.request_owner("POST", route + "/delete", {}).status_code, 200
        )
        self.assertEqual(self.request_owner("GET", "/schedules").json["schedules"], [])

    def test_agent_cannot_answer_native_approval_or_create_bypass_fields(self):
        call = self.agent()
        self.assertEqual(
            self.client.post(
                "/tasks/sample/approvals/sample", json={"decision": "allow"}
            ).status_code,
            401,
        )
        with self.assertRaises(ValueError):
            call("answer_approval", {"decision": "allow"})
        for data in ({"mode": "danger-full-access"}, {"approval_policy": "never"}):
            with self.assertRaises(ValueError):
                call("propose_schedule", proposal(**data))
        self.assertEqual(self.service.engine().store.schedules(), [])
        server.start_background_task.assert_not_called()

    def test_saved_readonly_task_still_refuses_an_invalid_global_policy(self):
        task_id = "synthetic-task"
        server.tasks[task_id] = {
            "status": "queued",
            "task_source": "schedule",
            "background_mode": "read-only",
        }
        with (
            patch.object(
                server,
                "read_options",
                return_value={
                    **server.DEFAULT_OPTIONS,
                    "codex_sandbox": "danger-full-access",
                },
            ),
            patch.object(server, "fail_task_launch") as refused,
            patch.object(server, "record_baseline") as baseline,
        ):
            server.run_task(task_id, "Inspect the sample configuration.")
        refused.assert_called_once()
        baseline.assert_not_called()

    def test_policy_refusal_is_not_bypassed_by_a_due_schedule(self):
        store = self.service.engine().store
        store.save(proposal(), owner=True, now=NOW)
        with patch.object(
            server.protected_policy,
            "policy_problem",
            return_value="Synthetic policy refusal.",
        ):
            with self.assertRaises(ValueError):
                self.service.engine().tick(NOW + 3601)
        server.start_background_task.assert_not_called()
        self.assertEqual(store.runs()[0]["status"], "failed")

    def test_oversized_or_malformed_json_do_not_launch(self):
        self.assertEqual(
            self.client.post(
                "/schedules",
                data="not json",
                content_type="application/json",
                headers=self.owner,
                environ_overrides=self.owner_env,
            ).status_code,
            400,
        )
        self.assertEqual(
            self.request_owner(
                "POST", "/schedules", proposal(prompt="x" * 70000)
            ).status_code,
            400,
        )
        self.assertEqual(self.service.engine().store.schedules(), [])
        server.start_background_task.assert_not_called()

    def test_proposal_tool_requires_active_turn_and_has_no_approval_operation(self):
        task_id = "synthetic-task"
        call = self.agent()
        value = call("propose_schedule", proposal())
        self.assertFalse(value["approved"])
        self.assertFalse(value["enabled"])
        self.assertEqual(len(call("list_schedules", {})["items"]), 1)
        for operation in (
            "read_mailbox",
            "post_mailbox",
        ):  # removed; they are not operations any more
            with self.assertRaises(ValueError):
                call(operation, {})
        with self.assertRaises(ValueError):
            call("approve_schedule", {"id": value["schedule_id"]})
        server.tasks[task_id]["current_turn_id"] = "different-turn"
        with self.assertRaises(ValueError):
            call("propose_schedule", proposal())
        server.start_background_task.assert_not_called()

    def test_tool_pages_are_bounded_and_reject_invalid_limits(self):
        call = self.agent()
        for i in range(3):
            self.service.engine().store.save(
                proposal(name="Sample " + str(i)),
                owner=False,
                author="codex",
                now=NOW + i,
            )
        result = call("list_schedules", {"offset": 1, "limit": 1})
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["next_offset"], 2)
        for limit in (
            True,
            "1",
        ):  # 0 and 21 are clamped now (tests/test_schedule_limit.py)
            with self.assertRaises(ValueError):
                call("list_schedules", {"limit": limit})

    def test_tool_transport_roundtrip_and_ended_exchange(self):
        # The HA pytest harness blocks sockets; allow only this local Unix
        # transport, keeping all IP networking blocked for this test.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            import pytest_socket

            pytest_socket.socket_allow_hosts([], allow_unix_socket=True)
        task_id = "synthetic-task"
        server.tasks[task_id] = {
            "status": "running",
            "current_turn_id": "synthetic-turn",
        }
        thread = open_exchange(self, task_id)
        path = self.root / "schedules.sock"
        listener = self.service.serve(path)
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        with patch.dict("os.environ", {"CODEX_SCHEDULE_SOCKET": str(path)}):
            response = scheduling_mcp.fetch("propose_schedule", proposal(), thread)
            self.assertTrue(response["ok"])
            self.assertFalse(response["enabled"])
            server.ha_data.end(task_id)
            self.assertFalse(scheduling_mcp.fetch("list_schedules", {}, thread)["ok"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_native_session_gets_actual_readonly_sandbox_and_on_request_approvals(self):
        task_id = "synthetic-task"
        server.tasks[task_id] = {
            "status": "running",
            "task_source": "schedule",
            "background_mode": "read-only",
            "current_turn_id": "synthetic-turn",
        }
        proc = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
        with patch.object(server, "AppServerSession") as constructor:
            server.create_approval_session(
                task_id,
                proc,
                "Sample prompt",
                self.root / "final.json",
                None,
                {},
                server.task_options(task_id),
            )
        kwargs = constructor.call_args.kwargs
        self.assertEqual(kwargs["thread_params"]["sandbox"], "read-only")
        self.assertEqual(
            kwargs["turn_params"]["sandboxPolicy"],
            {"type": "readOnly", "networkAccess": False},
        )
        self.assertEqual(kwargs["turn_params"]["approvalPolicy"], "on-request")
        args = server.build_app_server_args()
        self.assertTrue(any("scheduling_mcp.py" in a for a in args))
