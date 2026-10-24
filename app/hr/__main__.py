"""CLI: python -m app.hr examples/input_problem.json [--model installed-model]"""
import argparse
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import ValidationError
from .service import process_hr_input


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    p = argparse.ArgumentParser()
    p.add_argument("file", type=Path)
    p.add_argument("--model", help="Installed Ollama model name")
    args = p.parse_args()
    if args.model:
        os.environ["OLLAMA_MODEL"] = args.model
    try:
        import json
        payload = json.loads(args.file.read_text(encoding="utf-8-sig"))
        result = asyncio.run(process_hr_input(payload))
    except (OSError, ValueError, ValidationError) as exc:
        p.error(f"Cannot parse input file: {type(exc).__name__}")
    print(result.model_dump_json(indent=2))
    return 0 if result.status != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
