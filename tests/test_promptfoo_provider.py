"""promptfoo/market-pipeline/provider.py: call_api wiring around llm_extract.extract_structured (stubbed)."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PROVIDER = Path(__file__).resolve().parent.parent / "promptfoo" / "market-pipeline" / "provider.py"


@pytest.fixture
def provider(monkeypatch):
    calls = []
    fake = types.ModuleType("llm_extract")

    def extract_structured(text, schema, **kw):
        calls.append((text, schema, kw))
        if "boom" in text:
            raise RuntimeError("provider down")
        return {"echo": text, "schema_keys": sorted(schema)}

    fake.extract_structured = extract_structured
    monkeypatch.setitem(sys.modules, "llm_extract", fake)
    monkeypatch.setattr(sys, "path", list(sys.path))                    # the module inserts into sys.path
    spec = importlib.util.spec_from_file_location("promptfoo_provider", PROVIDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.calls = calls
    return mod


def ctx(cfg):
    return {"vars": {"extract_config": cfg}}


def test_call_api_passes_defaults_and_returns_json_output(provider):
    out = provider.call_api("doc text", {}, ctx(json.dumps({"schema": {"a": 1, "b": 2}})))
    assert json.loads(out["output"]) == {"echo": "doc text", "schema_keys": ["a", "b"]}
    assert "error" not in out
    text, schema, kw = provider.calls[0]
    assert (text, schema) == ("doc text", {"a": 1, "b": 2})
    assert kw == {"instructions": "", "provider": "groq", "fallback_provider": "gemini", "require_citations": True}


def test_call_api_honours_every_config_override(provider):
    cfg = {"schema": {"x": 1}, "instructions": "be brief", "provider": "gemini", "fallback_provider": "groq",
           "require_citations": False}
    provider.call_api("t", {}, ctx(cfg))                                # dict config (not a JSON string)
    assert provider.calls[0][2] == {"instructions": "be brief", "provider": "gemini", "fallback_provider": "groq",
                                    "require_citations": False}


def test_call_api_converts_extraction_errors_into_error_payload(provider):
    out = provider.call_api("boom now", {}, ctx({"schema": {"x": 1}}))
    assert out == {"output": "", "error": "provider down"}


def test_call_api_without_schema_raises_keyerror_before_calling_the_model(provider):
    with pytest.raises(KeyError):
        provider.call_api("t", {}, ctx("{}"))
    assert provider.calls == []


@pytest.mark.parametrize("context", [None, {}, {"vars": {}}])
def test_call_api_missing_config_is_a_keyerror(provider, context):
    with pytest.raises(KeyError):
        provider.call_api("t", {}, context)


def test_invalid_json_config_raises(provider):
    with pytest.raises(ValueError):
        provider.call_api("t", {}, ctx("{not json"))


def test_module_puts_its_parent_directory_on_sys_path(provider):
    assert str(PROVIDER.resolve().parent.parent) in sys.path
