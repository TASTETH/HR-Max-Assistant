"""Worker bridge; inject the project's real HRInput factory/service/normalizer.
No model is implemented or mocked by this module. A dict_row connection is required.
"""
from typing import Callable
from psycopg.types.json import Jsonb


def process_one_analysis(conn, model_version: str, policy_version: str,
                         input_factory: Callable, process_hr_input: Callable,
                         result_normalizer: Callable) -> bool:
    with conn.transaction():
        job = conn.execute("SELECT * FROM korsik.claim_message_analysis(%s,%s)",
                           (model_version,policy_version)).fetchone()
    if job is None:
        return False
    try:
        # No DB transaction is held during service/model execution.
        raw_result = process_hr_input(input_factory(job["request"]))
    except Exception:
        # Do not log exception text: it could contain employee data or secrets.
        explicit = job["request"].get("source") == "hr_request"
        envelope = {"status":"error","requires_hr":explicit,
                    "ticket":{"reason":"explicit_human"} if explicit else None,
                    "priority":"medium","category":"explicit_hr" if explicit else None,
                    "full_result":{"adapter_error":"HR_SERVICE_CALL_FAILED"}}
    else:
        # Map the actual typed HRResult here. Keep its COMPLETE serialized object
        # in full_result, including quotes and model/policy metadata. Preserve
        # ticket even for status=no_data/error. Do not infer escalation from sentiment.
        envelope = result_normalizer(raw_result)
        if not isinstance(envelope,dict) or not isinstance(envelope.get("full_result"),dict):
            raise ValueError("Normalizer must preserve the complete native result object")
    with conn.transaction():
        conn.execute("SELECT korsik.apply_hr_analysis(%s,%s,%s)",
                     (job["id"],job["generation"],Jsonb(envelope)))
    return True
