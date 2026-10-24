import json
import unittest
from unittest.mock import patch

from app.hr.analyzer import OllamaAnalyzer
from app.hr.schemas import HRInput, LLMAnalysis
from app.hr.validator import InvalidLLMResponse, validate_analysis


class OllamaSchemaTests(unittest.TestCase):
    def test_request_uses_compatible_grammar_and_keeps_full_prompt_schema(self):
        data = HRInput(
            event_id="test-event", employee_id="test-employee",
            source="message", text="Нет доступа к CRM",
        )
        content = json.dumps({"sentiment": "negative", "summary": "Нет доступа", "signals": []})
        with patch("app.hr.analyzer.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({
                "done": True, "message": {"content": content},
            }).encode()
            self.assertEqual(OllamaAnalyzer(model="test-model")._request(data), content)
            payload = json.loads(urlopen.call_args.args[0].data)

        format_schema = payload["format"]
        quote = format_schema["$defs"]["Signal"]["properties"]["quote"]
        self.assertNotIn("maxLength", quote)
        self.assertNotIn("maxLength", format_schema["properties"]["summary"])
        self.assertEqual(quote["minLength"], 1)
        self.assertEqual(format_schema["properties"]["signals"]["maxItems"], 9)
        self.assertEqual(
            format_schema["$defs"]["Signal"]["properties"]["category"],
            LLMAnalysis.model_json_schema()["$defs"]["Signal"]["properties"]["category"],
        )
        prompt_schema = json.loads(payload["messages"][0]["content"].split("\nSchema: ", 1)[1])
        self.assertEqual(prompt_schema, LLMAnalysis.model_json_schema())

    def test_response_validation_still_rejects_oversized_strings(self):
        data = HRInput(
            event_id="test-event", employee_id="test-employee",
            source="message", text="Нет доступа к CRM",
        )
        for analysis in (
            {"sentiment": "negative", "summary": "x" * 501, "signals": []},
            {"sentiment": "negative", "summary": "Нет доступа", "signals": [
                {"category": "access_problem", "quote": "x" * 3001},
            ]},
        ):
            with self.subTest(analysis=analysis), self.assertRaises(InvalidLLMResponse):
                validate_analysis(json.dumps(analysis), data)

    def test_response_validation_still_rejects_invented_quotes(self):
        data = HRInput(
            event_id="test-event", employee_id="test-employee",
            source="message", text="Нет доступа к CRM",
        )
        raw = json.dumps({
            "sentiment": "negative", "summary": "Нет доступа", "signals": [
                {"category": "access_problem", "quote": "Компьютер сломан"},
            ],
        })
        with self.assertRaises(InvalidLLMResponse):
            validate_analysis(raw, data)


if __name__ == "__main__":
    unittest.main()
