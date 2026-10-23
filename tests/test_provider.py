import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from isc_agents.agents import OpenAI
from isc_agents.models import REVIEW, Rejected


class ProviderTests(unittest.TestCase):
    def response(self, output, status="completed"):
        return io.BytesIO(json.dumps({"status": status, "output": output}).encode())

    @patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-not-a-real-key"})
    def test_structured_response_and_fixed_request_contract(self):
        result = {"approved": False, "findings": ["Missing business rule"]}
        response = self.response([{"content": [{"type": "output_text", "text": json.dumps(result)}]}])
        with patch("urllib.request.urlopen", return_value=response) as call:
            self.assertEqual(OpenAI("test-model").generate("reviewer", {"prompt": "example"}, REVIEW), result)
            request = call.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
            self.assertFalse(payload["store"])
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertNotIn("tools", payload)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-not-a-real-key"})
    def test_invalid_or_incomplete_output_fails_closed(self):
        cases = [
            self.response([], "incomplete"),
            self.response([{"content": [{"type": "refusal", "refusal": "no"}]}]),
            self.response([{"content": [{"type": "output_text", "text": "not json"}]}]),
            self.response([{"content": [{"type": "output_text", "text": '{"approved":true}'}]}]),
        ]
        for response in cases:
            with self.subTest(response=response), patch("urllib.request.urlopen", return_value=response):
                with self.assertRaises(Rejected):
                    OpenAI("test-model").generate("reviewer", {}, REVIEW)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-not-a-real-key"})
    def test_provider_error_does_not_expose_response_body(self):
        error = HTTPError("https://api.openai.com/v1/responses", 401, "sensitive-detail", {},
                          io.BytesIO(b"sensitive-response-body"))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(Rejected) as caught:
                OpenAI("test-model").generate("reviewer", {}, REVIEW)
            self.assertNotIn("sensitive", str(caught.exception))

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_rejected_before_request(self):
        with self.assertRaises(Rejected):
            OpenAI("test-model")


if __name__ == "__main__":
    unittest.main()
