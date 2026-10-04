import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import Reply
from openai import APIConnectionError, APIStatusError, OpenAI

from stem4b.api import (
    _retry_after_seconds,
    client_options,
    generate_json,
)
from stem4b.config import LLMConfig
from stem4b.models import Review


@pytest.fixture
def waits(monkeypatch):
    delays = []
    monkeypatch.setattr("stem4b.api.sleep", delays.append)
    monkeypatch.setattr("stem4b.api.random.uniform", lambda a, b: 1.0)
    return delays


def completion(content='{"approved":true}', tier="flex"):
    return Reply(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "service_tier": tier,
        }
    )


def run(client, config, work, purpose="test:flex"):
    return generate_json(
        client, config, work, [{"role": "system", "content": "Test"}], Review, purpose
    )


@pytest.mark.parametrize("status", [429, 503])
def test_flex_default_disables_fallback_with_exact_attempt_count(
    workspace,
    sdk_server,
    waits,
    status,
    caplog,
):
    calls = []

    def handle(request):
        calls.append(request)
        return Reply({"error": {"message": "Resource unavailable"}}, status)

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        retries=10,
        flex_max_attempts=3,
    )
    assert config.flex_fallback_to_standard is False
    with OpenAI(**client_options(config)) as client, pytest.raises(APIStatusError) as error:
        run(client, config, workspace)
    assert error.value.status_code == status
    assert len(calls) == 3 and waits == [5, 10]
    assert [request.headers.get("x-stainless-retry-count") for request in calls] == ["0"] * 3
    assert all(json.loads(request.content)["service_tier"] == "flex" for request in calls)
    assert "Resource unavailable" in caplog.text
    assert not (workspace / "requests.jsonl").exists()


def test_flex_recovers_with_exponential_backoff_and_cap(workspace, sdk_server, waits):
    calls = []

    def handle(request):
        calls.append(request)
        return completion() if len(calls) == 6 else Reply({"error": "busy"}, 503)

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_initial_backoff_seconds=2,
        flex_max_backoff_seconds=5,
    )
    with OpenAI(**client_options(config)) as client:
        assert run(client, config, workspace).approved
    assert len(calls) == 6 and waits == [2, 4, 5, 5, 5]
    assert (
        json.loads((workspace / "requests.jsonl").read_text())["requested_service_tier"] == "flex"
    )


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("tier", ["auto", "default"])
def test_fallback_is_explicit_local_and_removes_legacy_extra_tier(
    workspace,
    sdk_server,
    waits,
    status,
    legacy,
    tier,
    caplog,
):
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        return (
            Reply({"error": "capacity unavailable"}, status)
            if body["service_tier"] == "flex"
            else completion(tier="default")
        )

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier=None if legacy else "flex",
        extra_body={"reasoning_effort": "low", **({"service_tier": "flex"} if legacy else {})},
        flex_max_attempts=2,
        flex_fallback_to_standard=True,
        flex_fallback_service_tier=tier,
    )
    original = config.model_dump()
    with OpenAI(**client_options(config)) as client:
        assert run(client, config, workspace).approved
        assert client.max_retries == config.retries and not client.is_closed()
        assert run(client, config, workspace, "test:next-section").approved
    assert [body["service_tier"] for body in calls] == ["flex", "flex", tier] * 2
    assert all(body["reasoning_effort"] == "low" for body in calls)
    assert all(body["messages"] == calls[0]["messages"] for body in calls)
    assert waits == [5, 10, 5, 10] and config.model_dump() == original
    records = [json.loads(line) for line in (workspace / "requests.jsonl").read_text().splitlines()]
    assert all(
        r["configured_service_tier"] == "flex"
        and r["requested_service_tier"] == tier
        and r["service_tier"] == "default"
        for r in records
    )
    assert "fallback is explicitly enabled" in caplog.text


def test_fallback_failure_uses_only_standard_sdk_retry_budget(workspace, sdk_server, waits):
    calls = []

    def handle(request):
        calls.append(request)
        return Reply({"error": "busy"}, 503, {"retry-after-ms": "1"})

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_max_attempts=2,
        flex_fallback_to_standard=True,
        retries=1,
    )
    with OpenAI(**client_options(config)) as client, pytest.raises(APIStatusError):
        run(client, config, workspace)
    assert [json.loads(r.content)["service_tier"] for r in calls] == [
        "flex",
        "flex",
        "auto",
        "auto",
    ]
    assert [r.headers["x-stainless-retry-count"] for r in calls] == ["0", "0", "0", "1"]
    assert waits == [5, 10]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_flex_permanent_errors_never_retry_or_fallback(workspace, sdk_server, waits, status):
    calls = []

    def handle(request):
        calls.append(request)
        return Reply({"error": "fix configuration"}, status)

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_fallback_to_standard=True,
    )
    with OpenAI(**client_options(config)) as client, pytest.raises(APIStatusError):
        run(client, config, workspace)
    assert len(calls) == 1 and not waits


@pytest.mark.parametrize("status", [408, 409, 500])
def test_other_transient_errors_stay_on_flex(workspace, sdk_server, waits, status):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return Reply({"error": "temporary"}, status)

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_max_attempts=2,
        flex_fallback_to_standard=True,
    )
    with OpenAI(**client_options(config)) as client, pytest.raises(APIStatusError):
        run(client, config, workspace)
    assert [r["service_tier"] for r in calls] == ["flex", "flex"] and waits == [5]


def test_connection_failure_retries_but_never_switches_tier(workspace, sdk_server, waits):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return Reply(b"interrupted", truncated=True)

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_max_attempts=2,
        flex_fallback_to_standard=True,
    )
    with OpenAI(**client_options(config)) as client, pytest.raises(APIConnectionError):
        run(client, config, workspace)
    assert [r["service_tier"] for r in calls] == ["flex", "flex"] and waits == [5]


@pytest.mark.parametrize(
    "reply",
    [
        Reply({"error": {"message": "No credits", "code": "insufficient_quota"}}, 429),
        Reply({"error": "defer"}, 503, {"x-should-retry": "false"}),
    ],
)
def test_provider_non_retryable_errors_do_not_fallback(workspace, sdk_server, waits, reply):
    calls = []

    def handle(request):
        calls.append(request)
        return reply

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_fallback_to_standard=True,
    )
    with OpenAI(**client_options(config)) as client, pytest.raises(APIStatusError):
        run(client, config, workspace)
    assert len(calls) == 1 and not waits


@pytest.mark.parametrize("hint,expected", [("0", 5), ("12", 12), ("60", 60), ("nonsense", 5)])
def test_retry_after_is_a_minimum(workspace, sdk_server, waits, hint, expected):
    replies = iter([Reply({"error": "busy"}, 429, {"retry-after": hint}), completion()])
    config = LLMConfig(
        model="test", base_url=sdk_server(lambda _: next(replies)), service_tier="flex"
    )
    with OpenAI(**client_options(config)) as client:
        assert run(client, config, workspace).approved
    assert waits == [expected]


@pytest.mark.parametrize("budget", [1, 3])
def test_long_retry_after_never_retries_early_or_falls_back(workspace, sdk_server, waits, budget):
    calls = []

    def handle(request):
        calls.append(request)
        return Reply({"error": "busy"}, 503, {"retry-after": "120"})

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_max_attempts=budget,
        flex_fallback_to_standard=True,
    )
    with OpenAI(**client_options(config)) as client, pytest.raises(APIStatusError):
        run(client, config, workspace)
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"retry-after-ms": "12500"}, 12.5),
        ({"retry-after-ms": "bad", "retry-after": "7.5"}, 7.5),
        ({"retry-after": "Thu, 01 Jan 1970 00:01:50 GMT"}, 10),
        ({"retry-after": "Thu, 01 Jan 1970 00:00:50 GMT"}, 0),
        ({"retry-after": "nan"}, None),
        ({"retry-after": "inf"}, None),
        ({"retry-after": "-2"}, None),
        ({}, None),
    ],
)
def test_retry_after_formats(monkeypatch, headers, expected):
    monkeypatch.setattr("stem4b.api.time", lambda: 100)
    assert _retry_after_seconds(headers) == expected


def test_schema_repair_starts_a_new_flex_budget(workspace, sdk_server, waits):
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["service_tier"] == "flex":
            return Reply({"error": "busy"}, 429)
        return completion("not JSON" if len(calls) == 2 else '{"approved":true}', tier="default")

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_max_attempts=1,
        flex_fallback_to_standard=True,
    )
    with OpenAI(**client_options(config)) as client:
        assert run(client, config, workspace).approved
    assert [r["service_tier"] for r in calls] == ["flex", "auto", "flex", "auto"]
    assert "Repair the JSON" in calls[-1]["messages"][-1]["content"]


def test_flex_jitter_and_interrupt_propagation(workspace, sdk_server, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return Reply({"error": "busy"}, 503)

    monkeypatch.setattr("stem4b.api.random.uniform", lambda a, b: 0.8)

    def interrupted(delay):
        assert delay == 4
        raise KeyboardInterrupt

    monkeypatch.setattr("stem4b.api.sleep", interrupted)
    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_fallback_to_standard=True,
    )
    with OpenAI(**client_options(config)) as client, pytest.raises(KeyboardInterrupt):
        run(client, config, workspace)
    assert len(calls) == 1


def test_parallel_fallback_does_not_mutate_shared_client_or_config(workspace, sdk_server, waits):
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        return (
            Reply({"error": "busy"}, 503)
            if body["service_tier"] == "flex"
            else completion(tier="default")
        )

    config = LLMConfig(
        model="test",
        base_url=sdk_server(handle),
        service_tier="flex",
        flex_max_attempts=1,
        flex_fallback_to_standard=True,
    )
    with OpenAI(**client_options(config)) as client, ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, client, config, workspace, f"review:{i}") for i in range(2)]
        assert all(f.result().approved for f in futures)
        assert client.max_retries == config.retries and config.service_tier == "flex"
        assert not client.is_closed()
    assert [r["service_tier"] for r in calls].count("flex") == 2
    assert [r["service_tier"] for r in calls].count("auto") == 2


@pytest.mark.parametrize(
    "options",
    [
        {"flex_max_attempts": 0},
        {"flex_max_attempts": -1},
        {"flex_initial_backoff_seconds": 0},
        {"flex_max_backoff_seconds": 2},
        {"flex_initial_backoff_seconds": float("nan")},
        {"flex_max_backoff_seconds": float("inf")},
        {"flex_fallback_service_tier": "flex"},
        {"flex_fallback_service_tier": "priority"},
    ],
)
def test_flex_config_rejects_invalid_values(options):
    with pytest.raises(ValueError):
        LLMConfig(**options)


def test_flex_settings_do_not_invalidate_accepted_work():
    baseline = LLMConfig().cache_options()
    config = LLMConfig(
        service_tier="flex",
        flex_max_attempts=30,
        flex_initial_backoff_seconds=15,
        flex_max_backoff_seconds=300,
        flex_fallback_to_standard=True,
        flex_fallback_service_tier="default",
    )
    assert config.cache_options() == baseline
