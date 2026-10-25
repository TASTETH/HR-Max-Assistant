"""Offline regression tests. No network, DB changes, or MAX messages."""
import ast
import copy
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("plan_install", ROOT / "tools/install_personal_plan.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Connection:
    def __init__(self, rows=()):
        self.rows, self.queries = list(rows), []

    def execute(self, sql, params):
        assert "employee_id=%s" in sql and "AND active" in sql and "OR" not in sql.replace("ORDER", "")
        assert params == (7,)
        self.queries.append(sql)
        rows = [m for m in self.rows if m.get("employee_id") == params[0] and m.get("active", True)]
        return types.SimpleNamespace(fetchone=lambda: max(rows, key=lambda m: m["id"]) if rows else None)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.sent, self.cases, self.attached = [], [], []
        package = "personal_plan_regression"
        p = types.ModuleType(package); p.__path__ = []
        analysis = types.ModuleType(package + ".analysis")
        analysis.rule_signals = lambda text: [{"type": "leave"}] if "хочу уволиться" in text.lower() else []
        cases = types.ModuleType(package + ".cases")
        def open_case(c, emp, **kwargs):
            self.cases.append(kwargs)
            return {"id": 88}
        cases.open_case = open_case
        outbound = types.ModuleType(package + ".outbound")
        outbound.personalize = lambda emp, ty, vy: vy if emp.get("address_form") == "vy" else ty
        outbound.queue_to_employee = lambda c, emp, text, **kwargs: self.sent.append({"text": text, **kwargs})
        sys.modules.update({package: p, analysis.__name__: analysis, cases.__name__: cases,
                            outbound.__name__: outbound})
        name = package + ".personal_plan"
        spec = importlib.util.spec_from_file_location(name, ROOT / "backend/korsik/personal_plan.py")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.module
        spec.loader.exec_module(self.module)
        self.emp = {"id": 7, "dialog_mode": "auto", "dialog_revision": 2,
                    "address_form": "ty", "pending_input": None}
        self.own = {"id": 31, "employee_id": 7, "kind": "file", "active": True}
        self.foreign = {"id": 99, "employee_id": 8, "kind": "file", "active": True}

    def handle(self, text="Пришли мой план адаптации", rows=None):
        return self.module.handle_plan_request(Connection([self.own] if rows is None else rows),
                                               self.emp, text, 42, "test")

    def test_natural_requests_and_commands(self):
        for text in ("/plan", "/myplan", "Пришли мой план адаптации", "Где мой адаптационный план?",
                     "Можно получить мой персональный план?", "Пришлите, пожалуйста, план адаптации"):
            with self.subTest(text=text):
                self.assertTrue(self.module.is_plan_request(text))

    def test_other_questions_not_intercepted(self):
        for text in ("Можно ли принимать денежные подарки?", "Что такое план адаптации?",
                     "Какие задачи в моём плане адаптации?", "Где столовая?", "Как подключить VPN?"):
            with self.subTest(text=text):
                self.assertFalse(self.module.is_plan_request(text))

    def test_only_own_latest_active_plan(self):
        self.assertTrue(self.handle(rows=[self.foreign, self.own,
                                         {**self.own, "id": 33}, {**self.own, "id": 34, "active": False}]))
        self.assertEqual(self.sent[0]["material_ids"], [33])
        self.assertFalse(self.cases)

    def test_no_common_or_foreign_fallback(self):
        self.handle(rows=[self.foreign, {**self.own, "id": 100, "employee_id": None}])
        self.assertFalse(self.sent[0].get("material_ids"))
        self.assertEqual(self.cases[0]["reason"], "material_missing")

    def test_missing_plan_registers_case_without_consuming_survey(self):
        self.emp["pending_input"] = {"type": "survey_comment", "run": 15}
        pending = copy.deepcopy(self.emp["pending_input"])
        self.handle(rows=[])
        self.assertEqual(self.emp["pending_input"], pending)
        self.assertIs(self.cases[0]["set_waiting"], False)
        self.assertEqual(self.cases[0]["root_message_id"], 42)

    def test_wrong_owner_from_storage_fails_closed(self):
        connection = types.SimpleNamespace(execute=lambda *a: types.SimpleNamespace(fetchone=lambda: self.foreign))
        with self.assertRaisesRegex(ValueError, "owner_mismatch"):
            self.module.handle_plan_request(connection, self.emp, "/plan", 42, "test")
        self.assertFalse(self.sent)

    def test_explicit_foreign_request_not_delivered(self):
        self.assertTrue(self.handle("Пришли мне план адаптации другого сотрудника"))
        self.assertFalse(self.sent[0].get("material_ids"))
        self.assertFalse(self.cases)

    def test_hr_ownership_respected(self):
        self.emp["dialog_mode"] = "hr_active"
        self.assertFalse(self.handle())
        self.assertFalse(self.sent)

    def test_critical_signal_not_swallowed_by_file_request(self):
        self.assertFalse(self.handle("Я хочу уволиться, пришли мой план адаптации"))
        self.assertFalse(self.sent)

    def test_address_form_respected(self):
        self.emp["address_form"] = "vy"
        self.handle()
        self.assertIn("ваш персональный", self.sent[0]["text"])

    def test_link_and_text_materials(self):
        for kind in ("link", "text"):
            self.sent.clear()
            self.handle(rows=[{**self.own, "kind": kind, "url": "https://example.com/plan", "body": "Учебный план"}])
            self.assertEqual(self.sent[0]["material_ids"], [])
            self.assertIn("https://example.com/plan" if kind == "link" else "Учебный план", self.sent[0]["text"])

    def load_route(self):
        source = (ROOT / "backend/korsik/dialog.py").read_text(encoding="utf-8-sig")
        patched = installer.patch_dialog(source)
        route = next(n for n in ast.parse(patched).body if isinstance(n, ast.FunctionDef) and n.name == "route")
        namespace = {"__package__": self.module.__package__,
                     "attach_to_case": lambda c, emp, mid: self.attached.append(mid)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])), "route", "exec"), namespace)
        return namespace["route"], patched

    def test_real_route_global_request_preserves_pending_input(self):
        route, _ = self.load_route()
        self.emp.update(dialog_mode="waiting_hr", active_case_id=3,
                        pending_input={"type": "survey_text", "run": 15})
        before = copy.deepcopy(self.emp)
        self.assertEqual(route(Connection([self.own]), self.emp, "Пришли мой план адаптации", 42, "test"), "personal_plan")
        self.assertEqual(self.emp, before)
        self.assertFalse(self.attached)

    def test_real_route_hr_guard_runs_before_plan(self):
        route, _ = self.load_route()
        self.emp["dialog_mode"] = "hr_active"
        self.assertEqual(route(Connection([self.own]), self.emp, "/plan", 42, "test"), "to_hr")
        self.assertEqual(self.attached, [42])
        self.assertFalse(self.sent)

    def test_hook_idempotent_and_original_route_preserved(self):
        original = (ROOT / "backend/korsik/dialog.py").read_text(encoding="utf-8-sig")
        patched = installer.patch_dialog(original)
        self.assertEqual(installer.patch_dialog(patched), patched)
        self.assertEqual(patched.replace(installer.HOOK, ""), original.replace(installer.HOOK, ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
