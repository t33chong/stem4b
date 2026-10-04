import json
import logging
from types import SimpleNamespace

import pytest
from conftest import Reply
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI

from technical_audiobook.api import NarrationError, TruncatedResponse, client_options, generate_json
from technical_audiobook.audio import synthesize_speech
from technical_audiobook.cli import main
from technical_audiobook.config import LLMConfig, TTSConfig, load_config
from technical_audiobook.models import Review


def completion(content='{"approved":true}', finish="stop", **fields):
    return Reply(
        {
            "id": "test",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            **fields,
        }
    )


def review(client, config, workspace):
    return generate_json(
        client, config, workspace, [{"role": "system", "content": "Test"}], Review, "test"
    )


def test_independent_credentials_base_urls_and_optional_fields(monkeypatch, workspace, sdk_server):
    monkeypatch.setenv("LLM_TEST_KEY", "llm-secret")
    monkeypatch.setenv("TTS_TEST_KEY", "tts-secret")
    calls = []

    def handle(request):
        calls.append(request)
        return Reply(b"audio") if request.path.endswith("speech") else completion()

    base_url = sdk_server(handle).removesuffix("/v1")
    llm = LLMConfig(
        base_url=base_url + "/llm/api/v1",
        model="vision",
        api_key_env="LLM_TEST_KEY",
        json_mode="prompt",
        token_parameter="max_completion_tokens",
        extra_body={"reasoning_effort": "low"},
    )
    tts = TTSConfig(
        base_url=base_url + "/speech/v1",
        model="custom-voice-model",
        api_key_env="TTS_TEST_KEY",
        voice="custom-voice",
    )
    with OpenAI(**client_options(llm)) as chat, OpenAI(**client_options(tts)) as speech:
        assert review(chat, llm, workspace).approved
        synthesize_speech(speech, tts, workspace, "Test speech.", workspace / "test.wav")
    assert chat.is_closed() and speech.is_closed()
    assert [r.path for r in calls] == ["/llm/api/v1/chat/completions", "/speech/v1/audio/speech"]
    assert calls[0].headers["authorization"] == "Bearer llm-secret"
    assert calls[1].headers["authorization"] == "Bearer tts-secret"
    body = json.loads(calls[0].content)
    assert "response_format" not in body and "temperature" not in body
    assert body["max_completion_tokens"] == 16000 and "max_tokens" not in body
    assert body["reasoning_effort"] == "low"
    assert "secret" not in (workspace / "requests.jsonl").read_text()


def test_schema_repair_and_flex_retry(workspace, sdk_server, monkeypatch):
    monkeypatch.setattr("technical_audiobook.api.sleep", lambda _: None)
    replies = iter(
        [
            Reply({"error": "busy"}, 429, {"retry-after-ms": "1"}),
            completion("not JSON"),
            completion(),
        ]
    )
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return next(replies)

    config = LLMConfig(model="test", base_url=sdk_server(handle), service_tier="flex")
    with OpenAI(**client_options(config)) as client:
        assert review(client, config, workspace).approved
    assert len(calls) == 3
    assert "Repair the JSON" in calls[-1]["messages"][-1]["content"]
    assert all(body["service_tier"] == "flex" for body in calls)


@pytest.mark.parametrize(
    "reply,expected",
    [
        (Reply({"error": "private provider details"}, 401), APIStatusError),
        (completion("{}", "length"), TruncatedResponse),
        (completion("", "content_filter"), NarrationError),
        (completion(""), NarrationError),
    ],
)
def test_failed_completions_are_never_accepted(reply, expected, workspace, sdk_server):
    calls = []

    def handle(request):
        calls.append(request)
        return reply

    config = LLMConfig(model="test", base_url=sdk_server(handle))
    with OpenAI(**client_options(config)) as client, pytest.raises(expected):
        review(client, config, workspace)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "reply",
    [Reply(), Reply({"error": "failed"}), Reply(b"error", headers={"content-type": "text/html"})],
)
def test_bad_speech_responses_fail(reply, workspace, sdk_server):
    config = TTSConfig(model="test", base_url=sdk_server(lambda _: reply))
    with OpenAI(**client_options(config)) as client, pytest.raises(ValueError):
        synthesize_speech(client, config, workspace, "Hello", workspace / "audio.wav")
    assert not (workspace / "audio.wav").exists()


def test_config_environment_overrides_and_relative_paths(tmp_path, monkeypatch):
    file = tmp_path / "config.toml"
    file.write_text('[llm]\nmodel="original"\n[narration]\ninstructions_file="policy.md"\n')
    monkeypatch.setenv("LLM_MODEL", "selected")
    monkeypatch.setenv("TTS_BASE_URL", "http://localhost:9000/v1")
    monkeypatch.setenv("LLM_SERVICE_TIER", "flex")
    config = load_config(file)
    assert config.llm.model == "selected"
    assert config.tts.base_url == "http://localhost:9000/v1"
    assert config.llm.service_tier == "flex"
    assert config.narration.instructions_file == str(tmp_path / "policy.md")
    with pytest.raises(ValueError, match="core request"):
        LLMConfig(extra_body={"messages": []})


@pytest.mark.parametrize("tier", [None, "flex", "priority", "provider-specific-tier"])
def test_service_tier_is_optional_and_recorded(workspace, sdk_server, tier):
    calls = []

    def handle(request):
        calls.append(request)
        return completion(service_tier="default")

    config = LLMConfig(
        model="test", base_url=sdk_server(handle), service_tier=tier, temperature=0.2
    )
    with OpenAI(**client_options(config)) as client:
        assert review(client, config, workspace).approved
    request = json.loads(calls[0].content)
    assert request.get("service_tier") == tier
    if tier is None:
        assert "service_tier" not in request
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0.2 and request["max_tokens"] == 16000
    assert "max_completion_tokens" not in request and "stream" not in request
    assert calls[0].headers["user-agent"].startswith("OpenAI/Python")
    record = json.loads((workspace / "requests.jsonl").read_text())
    assert record.get("requested_service_tier") == tier
    assert record["service_tier"] == "default"


def test_service_tier_config_validation_and_legacy_extra_body():
    with pytest.raises(ValueError):
        LLMConfig(service_tier="  ")
    with pytest.raises(ValueError, match="not both"):
        LLMConfig(service_tier="flex", extra_body={"service_tier": "priority"})
    assert LLMConfig(extra_body={"service_tier": "flex"}).service_tier is None
    with pytest.raises(ValueError):
        TTSConfig(service_tier="flex")


def test_pre_sdk_llm_cache_serialization_is_unchanged():
    expected = {
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "api_key_env": "LLM_API_KEY",
        "timeout_seconds": 300,
        "retries": 3,
        "extra_body": {},
        "max_output_tokens": 16000,
        "token_parameter": "max_tokens",
        "json_mode": "json_object",
        "temperature": None,
    }
    for tier in (None, "flex", "priority"):
        assert LLMConfig(service_tier=tier).cache_options() == expected


@pytest.mark.parametrize("kind", ["chat", "speech"])
@pytest.mark.parametrize("has_key", [False, True])
def test_sdk_never_inherits_global_credentials_or_custom_headers(
    monkeypatch, workspace, sdk_server, kind, has_key
):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-global-secret")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "unrelated-admin-secret")
    monkeypatch.setenv("OPENAI_ORG_ID", "unrelated-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "unrelated-project")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.example/v1")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS", "authorization: leaked-secret\nX-Other-Key: leaked-secret"
    )
    monkeypatch.delenv("TEST_ENDPOINT_KEY", raising=False)
    if has_key:
        monkeypatch.setenv("TEST_ENDPOINT_KEY", "selected-key")
    calls = []

    def handle(request):
        calls.append(request)
        return Reply(b"audio") if kind == "speech" else completion()

    config_type = LLMConfig if kind == "chat" else TTSConfig
    config = config_type(model="test", base_url=sdk_server(handle), api_key_env="TEST_ENDPOINT_KEY")
    with OpenAI(**client_options(config)) as client:
        if kind == "chat":
            review(client, config, workspace)
        else:
            synthesize_speech(client, config, workspace, "Test", workspace / "test.wav")
    assert calls[0].headers.get("authorization") == ("Bearer selected-key" if has_key else None)
    assert all(
        calls[0].headers.get(h) is None
        for h in ("openai-project", "openai-organization", "x-other-key")
    )
    assert "leaked-secret" not in str(calls[0].headers)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503])
@pytest.mark.parametrize("kind", ["chat", "speech"])
def test_sdk_owns_non_flex_and_speech_retries(workspace, sdk_server, status, kind):
    calls = []

    def handle(request):
        calls.append(request)
        return Reply({"error": "private details"}, status, {"retry-after-ms": "1"})

    config_type = LLMConfig if kind == "chat" else TTSConfig
    config = config_type(model="test", retries=2, timeout_seconds=900, base_url=sdk_server(handle))
    if kind == "chat":
        config.service_tier = "default"
    with OpenAI(**client_options(config)) as client, pytest.raises(APIStatusError) as caught:
        assert client.max_retries == 2 and client.timeout == 900
        if kind == "chat":
            review(client, config, workspace)
        else:
            synthesize_speech(client, config, workspace, "Test", workspace / "test.wav")
    assert caught.value.status_code == status and len(calls) == 3
    assert [call.headers.get("x-stainless-retry-count") for call in calls] == ["0", "1", "2"]
    for call in calls:
        body = json.loads(call.content)
        assert body.get("service_tier") == ("default" if kind == "chat" else None)
    assert not (workspace / "requests.jsonl").exists()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_sdk_permanent_errors_are_not_retried(workspace, sdk_server, status):
    calls = []

    def handle(request):
        calls.append(request)
        return Reply({"error": "failed"}, status)

    config = LLMConfig(model="test", base_url=sdk_server(handle))
    with OpenAI(**client_options(config)) as client, pytest.raises(APIStatusError):
        review(client, config, workspace)
    assert len(calls) == 1


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("kind", ["chat", "speech"])
def test_sdk_retries_interrupted_downloads_without_partial_output(
    workspace, sdk_server, recover, kind
):
    calls = []

    def handle(request):
        calls.append(request)
        if recover and len(calls) == 2:
            return Reply(b"complete-audio") if kind == "speech" else completion()
        return Reply(b"partial", truncated=True)

    config_type = LLMConfig if kind == "chat" else TTSConfig
    config = config_type(model="test", base_url=sdk_server(handle), retries=1)
    destination = workspace / "speech.wav"
    destination.write_bytes(b"previous-good-audio")

    def run(client):
        if kind == "chat":
            return review(client, config, workspace)
        return synthesize_speech(client, config, workspace, "Test", destination)

    with OpenAI(**client_options(config)) as client:
        if recover:
            run(client)
        else:
            with pytest.raises(APIConnectionError):
                run(client)
    assert destination.read_bytes() == (
        b"complete-audio" if recover and kind == "speech" else b"previous-good-audio"
    )
    assert len(calls) == 2
    assert not list(workspace.glob(".speech.wav.*"))


def test_speech_options_and_provider_fields(workspace, sdk_server):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return Reply(b"audio", headers={"content-type": "audio/flac"})

    config = TTSConfig(
        model="custom-tts",
        voice="custom-voice",
        response_format="flac",
        instructions="Unhurried technical narration.",
        base_url=sdk_server(handle),
        extra_body={"speed": 0.9, "vendor_option": True},
    )
    with OpenAI(**client_options(config)) as client:
        synthesize_speech(
            client, config, workspace, "Exact canonical script text.", workspace / "speech.flac"
        )
    assert calls == [
        {
            "model": "custom-tts",
            "voice": "custom-voice",
            "response_format": "flac",
            "instructions": "Unhurried technical narration.",
            "speed": 0.9,
            "vendor_option": True,
            "input": "Exact canonical script text.",
        }
    ]


@pytest.mark.parametrize(
    "payload", [b"not-json", b"[]", b"{}", b'{"choices":[]}', b'{"choices":[{"message":null}]}']
)
def test_malformed_chat_responses_fail_safely(workspace, sdk_server, payload):
    config = LLMConfig(
        model="test",
        base_url=sdk_server(lambda _: Reply(payload, headers={"content-type": "application/json"})),
    )
    with OpenAI(**client_options(config)) as client, pytest.raises(NarrationError):
        review(client, config, workspace)


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("kind", ["chat", "speech"])
@pytest.mark.parametrize(
    "reply",
    [
        Reply(
            {"error": {"message": "Unsupported service_tier: flex", "code": "invalid_parameter"}},
            400,
            {"x-request-id": "req-diagnostic"},
        ),
        Reply(
            b"<html>Gateway unavailable</html>\n" + b"details " * 1000 + b"end of response",
            502,
            {"content-type": "text/html"},
        ),
        Reply(b"", 401),
    ],
)
def test_cli_logs_provider_errors_without_request_payloads(
    monkeypatch, workspace, sdk_server, caplog, verbose, kind, reply
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(logging.getLogger("openai"), "level", logging.DEBUG)
    monkeypatch.setenv("TEST_ERROR_KEY", "request-authorization-secret")
    config_type = LLMConfig if kind == "chat" else TTSConfig
    config = config_type(
        model="test",
        api_key_env="TEST_ERROR_KEY",
        retries=0,
        base_url=sdk_server(lambda _: reply),
    )
    with OpenAI(**client_options(config)) as client:

        def fail(*args, **kwargs):
            if kind == "chat":
                generate_json(
                    client,
                    config,
                    workspace,
                    [{"role": "system", "content": "private-book-content"}],
                    Review,
                    "test",
                )
            else:
                synthesize_speech(
                    client, config, workspace, "private-book-content", workspace / "speech.wav"
                )

        monkeypatch.setattr("technical_audiobook.cli.convert", fail)
        args = ["convert", "unused.pdf", "-o", str(workspace / "unused.m4b")]
        assert main(args + (["--verbose"] if verbose else [])) == 1
    body = json.dumps(reply.body) if isinstance(reply.body, dict) else reply.body.decode()
    assert f"Provider response body:\n{body or '<empty body>'}" in caplog.text
    assert "private-book-content" not in caplog.text
    assert "request-authorization-secret" not in caplog.text
    assert f"HTTP {reply.status}" in caplog.text
    assert f"request ID: {reply.headers.get('x-request-id', 'unavailable')}" in caplog.text
    endpoint = "/v1/chat/completions" if kind == "chat" else "/v1/audio/speech"
    assert f"POST {endpoint}" in caplog.text
    assert not (workspace / "speech.wav").exists()


@pytest.mark.parametrize("timeout", [False, True])
def test_cli_logs_connection_error_details(monkeypatch, workspace, caplog, timeout):
    def fail(*args, **kwargs):
        request = SimpleNamespace()
        if timeout:
            raise APITimeoutError(request=request) from TimeoutError("provider read timed out")
        raise APIConnectionError(request=request) from OSError("DNS lookup failed")

    monkeypatch.setattr("technical_audiobook.cli.convert", fail)
    assert main(["convert", "unused.pdf", "-o", str(workspace / "unused.m4b")]) == 1
    assert "configured retries" in caplog.text
    assert ("APITimeoutError" if timeout else "APIConnectionError") in caplog.text
    assert ("provider read timed out" if timeout else "DNS lookup failed") in caplog.text
    assert "Provider response body" not in caplog.text


@pytest.mark.parametrize("body", [{"error": "Unexpected provider format"}, "Unexpected text"])
def test_cli_logs_other_sdk_error_details(monkeypatch, workspace, caplog, body):
    def fail(*args, **kwargs):
        raise APIError("Provider parsing failed", request=SimpleNamespace(), body=body)

    monkeypatch.setattr("technical_audiobook.cli.convert", fail)
    assert main(["convert", "unused.pdf", "-o", str(workspace / "unused.m4b")]) == 1
    assert "Provider parsing failed" in caplog.text
    assert (json.dumps(body) if isinstance(body, dict) else body) in caplog.text


def test_cli_logs_json_returned_instead_of_audio(monkeypatch, workspace, sdk_server, caplog):
    reply = Reply({"error": "Unknown voice"}, headers={"x-request-id": "req-speech-error"})
    config = TTSConfig(model="test", base_url=sdk_server(lambda _: reply))
    with OpenAI(**client_options(config)) as client:

        def fail(*args, **kwargs):
            synthesize_speech(client, config, workspace, "Test", workspace / "speech.wav")

        monkeypatch.setattr("technical_audiobook.cli.convert", fail)
        assert main(["convert", "unused.pdf", "-o", str(workspace / "unused.m4b")]) == 1
    assert "instead of audio bytes" in caplog.text
    assert "Unknown voice" in caplog.text
    assert "req-speech-error" in caplog.text
    assert not (workspace / "speech.wav").exists()
