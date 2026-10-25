"""Install only the personal-plan handler and a small hook in the existing dialog.py."""
import argparse
import ast
import shutil
from datetime import datetime, timezone
from pathlib import Path


HOOK = '''    # Employee's own plan is a global file request; keep survey/comment state intact.
    from .personal_plan import handle_plan_request
    if handle_plan_request(c, emp, text, mid, rid):
        return "personal_plan"
'''


def patch_dialog(text):
    tree = ast.parse(text)
    route = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "route"), None)
    if route is None:
        raise ValueError("route function not found")
    if any(isinstance(n, ast.ImportFrom) and n.module == "personal_plan" for n in ast.walk(route)):
        return text
    # Hook after the HR ownership branch, before pending-input and waiting_hr routing.
    guard = next((n for n in route.body if isinstance(n, ast.If) and any(
        isinstance(x, ast.Constant) and x.value == "hr_active" for x in ast.walk(n.test))), None)
    if guard is None or not any(isinstance(n, ast.Return) for n in ast.walk(guard)):
        raise ValueError("HR ownership guard not found; nothing changed")
    lines = text.splitlines(keepends=True)
    lines.insert(guard.end_lineno, HOOK)
    result = "".join(lines)
    compile(result, "dialog.py", "exec")
    return result


def install(root):
    root = Path(root).resolve()
    module = root / "backend/korsik/personal_plan.py"
    dialog = root / "backend/korsik/dialog.py"
    if not module.is_file() or not dialog.is_file():
        raise ValueError("Copy backend and tools into your existing korsik_v2 first")
    compile(module.read_text(encoding="utf-8-sig"), str(module), "exec")
    original = dialog.read_bytes()
    text = original.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    patched = patch_dialog(text.replace("\r\n", "\n"))
    if newline == "\r\n":
        patched = patched.replace("\n", "\r\n")
    encoded = patched.encode("utf-8")
    if encoded == original:
        print("Plan handler already installed.")
        return
    folder = root / "data/update-backups" / datetime.now(timezone.utc).strftime("personal_plan_%Y%m%dT%H%M%S_%fZ")
    folder.mkdir(parents=True)
    (folder / "dialog.py").write_bytes(original)
    tmp = dialog.with_suffix(".py.plan-tmp")
    tmp.write_bytes(encoded)
    shutil.copymode(dialog, tmp)
    tmp.replace(dialog)
    print("Installed personal plan delivery. Backup:", folder.relative_to(root))
    print("Rebuild app and worker to activate the change.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    install(args.root)
