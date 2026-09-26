"""
Regression tests for where the upload parser gets its LLM settings.

The question this answers: does ``web/app.py`` still *set* the LLM interface,
or does the parser read it from ``.env`` like every other part of the agent?

``web/app.py`` used to hardcode ``LLMPatientParser(use_llm=True)``, which meant
the switch lived in application code and ignored ``AGENT_LLM_PROVIDER``. A
deployment that turned the model off in ``.env`` still had the upload path
reach for it, and one with no credential paid for a doomed call on every
upload. These tests pin the switch to the environment instead.
"""

import importlib

import pytest

from utils.llm_parser import LLMPatientParser

LLM_VARS = (
    "AGENT_LLM_PROVIDER",
    "AGENT_LLM_API_KEY",
    "AGENT_LLM_MODEL",
    "AGENT_LLM_BASE_URL",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Start every case from an environment with no LLM settings at all."""
    for name in LLM_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestTheSwitchComesFromTheEnvironment:
    def test_no_credential_means_the_rules_decide(self, clean_env):
        """An unconfigured deployment must not try to call a model."""
        parser = LLMPatientParser.from_environment()

        assert parser.llm_ready is False

    def test_a_credential_turns_the_model_on(self, clean_env):
        clean_env.setenv("AGENT_LLM_API_KEY", "sk-test-key")

        assert LLMPatientParser.from_environment().llm_ready is True

    def test_disabled_in_the_env_turns_the_model_off(self, clean_env):
        clean_env.setenv("AGENT_LLM_PROVIDER", "disabled")
        clean_env.setenv("AGENT_LLM_API_KEY", "sk-test-key")

        parser = LLMPatientParser.from_environment()

        assert parser.provider == "disabled"
        assert parser.use_llm is False
        assert parser.llm_ready is False

    @pytest.mark.parametrize("alias", ["none", "off", "rules", "DISABLED"])
    def test_the_documented_disabled_aliases_all_hold(self, clean_env, alias):
        """``_normalize_kind`` documents these; the parser must honour them."""
        clean_env.setenv("AGENT_LLM_PROVIDER", alias)
        clean_env.setenv("AGENT_LLM_API_KEY", "sk-test-key")

        assert LLMPatientParser.from_environment().use_llm is False

    def test_an_explicit_false_still_wins_for_tests(self, clean_env):
        """The existing suite passes ``use_llm=False``; that must keep working."""
        clean_env.setenv("AGENT_LLM_API_KEY", "sk-test-key")

        assert LLMPatientParser(use_llm=False).llm_ready is False
        assert LLMPatientParser(use_llm=True).use_llm is True

    def test_it_agrees_with_the_provider_layer(self, clean_env):
        """The parser must not answer differently from the rest of the agent."""
        from tools.llm_providers import LlmProviderConfig

        clean_env.setenv("AGENT_LLM_PROVIDER", "disabled")
        clean_env.setenv("AGENT_LLM_API_KEY", "sk-test-key")
        config = LlmProviderConfig.from_env()

        assert LLMPatientParser.from_environment().use_llm is not config.is_disabled


class TestTheEndpointAlsoComesFromTheEnvironment:
    def test_it_reads_the_configured_model_and_base_url(self, clean_env):
        clean_env.setenv("AGENT_LLM_MODEL", "local-model")
        clean_env.setenv("AGENT_LLM_BASE_URL", "http://127.0.0.1:11434/v1")

        parser = LLMPatientParser.from_environment()

        assert parser.model == "local-model"
        assert parser.base_url == "http://127.0.0.1:11434/v1"

    def test_the_old_fallback_order_for_the_key_survives(self, clean_env):
        clean_env.setenv("LLM_API_KEY", "legacy-key")

        assert LLMPatientParser.from_environment().api_key == "legacy-key"

    def test_an_override_beats_the_environment(self, clean_env):
        clean_env.setenv("AGENT_LLM_MODEL", "from-env")

        parser = LLMPatientParser.from_environment(model="from-caller")

        assert parser.model == "from-caller"

    def test_parsing_falls_back_to_rules_when_disabled(self, clean_env):
        """End to end: a disabled provider still returns usable records."""
        clean_env.setenv("AGENT_LLM_PROVIDER", "disabled")
        clean_env.setenv("AGENT_LLM_API_KEY", "sk-test-key")

        rows = LLMPatientParser.from_environment().parse_file(
            b"Patient ID,Name,Phone,Email,Last Visit,Recall Interval\n"
            b"4001,Never Called,91234567,n@example.com,2024-01-15,180\n",
            "patients.csv",
        )

        assert [row["patient_id"] for row in rows] == ["4001"]


class TestTheWebAppDoesNotSetIt:
    def test_the_upload_parser_is_built_from_the_environment(self):
        """Importing the app must not pin ``use_llm`` to a literal."""
        from tools.llm_providers import LlmProviderConfig

        app_module = importlib.import_module("web.app")
        expected = not LlmProviderConfig.from_env().is_disabled

        assert app_module.llm_parser.__class__ is LLMPatientParser
        assert app_module.llm_parser.use_llm is expected

    def test_the_module_source_has_no_hardcoded_llm_switch(self):
        """Guard the actual line the clinic complained about."""
        with open("web/app.py", encoding="utf-8") as handle:
            source = handle.read()

        assert "LLMPatientParser(use_llm=True)" not in source
        assert "LLMPatientParser.from_environment()" in source
