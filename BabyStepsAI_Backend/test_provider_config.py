import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import provider_config


class ProviderConfigTests(unittest.TestCase):
    def test_reads_only_allowlisted_groq_values_from_private_env_file(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file=Path(folder)/".env"
            env_file.write_text("GROQ_API_KEY=synthetic-secret\nGROQ_MODEL=openai/gpt-oss-20b\nUNRELATED_SECRET=must-not-load\n")
            env_file.chmod(0o600)
            with patch.object(provider_config,"PROJECT_ENV",env_file),patch.dict(os.environ,{"GROQ_API_KEY":"","GROQ_MODEL":""}):
                self.assertEqual(provider_config.groq_api_key(),"")
                os.environ.pop("GROQ_API_KEY")
                os.environ.pop("GROQ_MODEL")
                self.assertEqual(provider_config.groq_api_key(),"synthetic-secret")
                self.assertEqual(provider_config.configured_groq_model(),"openai/gpt-oss-20b")
                self.assertNotIn("UNRELATED_SECRET",os.environ)

    def test_rejects_insecure_env_file_and_unapproved_paid_model(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file=Path(folder)/".env"
            env_file.write_text("GROQ_API_KEY=synthetic-secret\n")
            env_file.chmod(0o644)
            with patch.object(provider_config,"PROJECT_ENV",env_file),patch.dict(os.environ,{"GROQ_API_KEY":"","GROQ_MODEL":""}):
                self.assertEqual(provider_config.groq_api_key(),"")
        self.assertTrue(provider_config.groq_model_allowed("openai/gpt-oss-20b"))
        self.assertFalse(provider_config.groq_model_allowed("paid/unknown-model"))


if __name__ == "__main__":
    unittest.main()
