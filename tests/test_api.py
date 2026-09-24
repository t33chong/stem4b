import json

import httpx
import pytest

from technical_audiobook.api import APIError, LLMClient, SpeechClient, TruncatedResponse
from technical_audiobook.config import LLMConfig, TTSConfig, load_config
from technical_audiobook.models import Review


def completion(content, finish="stop"):
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": finish, "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        },
    )


def test_independent_credentials_base_urls_and_optional_fields(monkeypatch, workspace):
    monkeypatch.setenv("LLM_TEST_KEY", "llm-secret")
    monkeypatch.setenv("TTS_TEST_KEY", "tts-secret")
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("speech"):
            return httpx.Response(200, content=b"audio", headers={"content-type": "audio/wav"})
        return completion('{"approved": true, "findings": []}')

    transport = httpx.MockTransport(handle)
    llm = LLMClient(
        LLMConfig(
            base_url="https://llm.example/api/v1",
            model="vision",
            api_key_env="LLM_TEST_KEY",
            json_mode="prompt",
            token_parameter="max_completion_tokens",
            extra_body={"reasoning_effort": "low"},
        ),
        workspace,
        transport,
    )
    tts = SpeechClient(
        TTSConfig(
            base_url="https://speech.example/v1",
            model="voice-model",
            api_key_env="TTS_TEST_KEY",
            voice="speaker",
        ),
        workspace,
        transport,
    )
    try:
        assert llm.generate([{"role": "system", "content": "Review"}], Review, "test").approved
        tts.synthesize("Test speech.", workspace / "test.wav")
    finally:
        llm.close()
        tts.close()
    assert str(calls[0].url) == "https://llm.example/api/v1/chat/completions"
    assert str(calls[1].url) == "https://speech.example/v1/audio/speech"
    assert calls[0].headers["authorization"] == "Bearer llm-secret"
    assert calls[1].headers["authorization"] == "Bearer tts-secret"
    body = json.loads(calls[0].content)
    assert "response_format" not in body and "temperature" not in body
    assert body["max_completion_tokens"] == 16000
    assert body["reasoning_effort"] == "low"
    assert "secret" not in (workspace / "requests.jsonl").read_text()


def test_schema_repair_and_retry(monkeypatch, workspace):
    monkeypatch.setattr("technical_audiobook.api.time.sleep", lambda _: None)
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "1"}),
            completion("not JSON"),
            completion('{"approved": true, "findings": []}'),
        ]
    )
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return next(responses)

    client = LLMClient(LLMConfig(model="vision"), workspace, httpx.MockTransport(handle))
    try:
        assert client.generate([{"role": "system", "content": "Test"}], Review, "test").approved
    finally:
        client.close()
    assert len(calls) == 3
    assert "Repair the JSON" in calls[-1]["messages"][-1]["content"]


@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx.Response(401, json={"error": "private provider details"}), APIError),
        (completion("{}", "length"), TruncatedResponse),
        (completion("", "content_filter"), APIError),
        (completion(""), APIError),
    ],
)
def test_failed_completions_are_never_accepted(response, expected, workspace):
    calls = []

    def handle(request):
        calls.append(request)
        return response

    client = LLMClient(LLMConfig(model="vision"), workspace, httpx.MockTransport(handle))
    try:
        with pytest.raises(expected):
            client.generate([{"role": "system", "content": "Test"}], Review, "test")
    finally:
        client.close()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"", headers={"content-type": "audio/wav"}),
        httpx.Response(200, json={"error": "failed"}),
    ],
)
def test_bad_speech_responses_fail(response, workspace):
    client = SpeechClient(
        TTSConfig(model="tts"), workspace, httpx.MockTransport(lambda _: response)
    )
    try:
        with pytest.raises(APIError):
            client.synthesize("Hello", workspace / "audio.wav")
    finally:
        client.close()
    assert not (workspace / "audio.wav").exists()


def test_config_environment_overrides_and_relative_paths(tmp_path, monkeypatch):
    file = tmp_path / "config.toml"
    file.write_text('[llm]\nmodel="original"\n[narration]\ninstructions_file="policy.md"\n')
    monkeypatch.setenv("LLM_MODEL", "selected")
    monkeypatch.setenv("TTS_BASE_URL", "http://localhost:9000/v1")
    config = load_config(file)
    assert config.llm.model == "selected"
    assert config.tts.base_url == "http://localhost:9000/v1"
    assert config.narration.instructions_file == str(tmp_path / "policy.md")
    with pytest.raises(ValueError, match="core request"):
        LLMConfig(extra_body={"messages": []})
