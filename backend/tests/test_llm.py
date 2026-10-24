import pytest

from screener.llm import AnthropicClient, CassetteClient, LLMError, LLMRequest, UserBlock


def _req(**kw):
    base = dict(task="score", model="claude-sonnet-5-5", system="sys", user_blocks=[UserBlock(text="<resume>\nx\n</resume>", cache=True), UserBlock(text="q")], output_schema={"type": "object"}, max_tokens=100, effort="medium", prompt_hash="h")
    return LLMRequest(**{**base, **kw})


def test_api_params_never_send_sampling_parameters():
    params = _req().api_params()
    assert not {"temperature", "top_p", "top_k"} & set(params)
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "medium"}
    assert params["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["system"] == "sys"


def test_sample_index_changes_key_only():
    a, b = _req(sample_index=0), _req(sample_index=1)
    assert a.api_params() == b.api_params() and a.key() != b.key()


class _FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)

        class M:
            def model_dump(self, mode="json"):
                return {"id": "msg_1", "model": kw["model"], "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": '{"level": 2, "evidence": [], "rationale": ""}'}], "stop_reason": "end_turn", "usage": {"input_tokens": 50, "output_tokens": 9, "cache_read_input_tokens": 40}}

        return M()


class _FakeSDK:
    def __init__(self):
        self.messages = _FakeMessages()


def test_anthropic_client_parses_structured_output():
    sdk = _FakeSDK()
    resp = AnthropicClient(client=sdk).complete(_req())
    assert resp.parsed == {"level": 2, "evidence": [], "rationale": ""}
    assert resp.usage.cache_read_input_tokens == 40
    assert "temperature" not in sdk.messages.calls[0]


def test_cassette_record_then_replay(tmp_path):
    sdk = _FakeSDK()
    path = tmp_path / "c.json"
    rec = CassetteClient(path, "record", AnthropicClient(client=sdk))
    first = rec.complete(_req())
    replay = CassetteClient(path, "replay")
    assert replay.complete(_req()).parsed == first.parsed
    assert len(sdk.messages.calls) == 1
    with pytest.raises(LLMError) as e:
        replay.complete(_req(sample_index=7))
    assert e.value.kind == "cassette_miss"
