# Configuration

[Back to README](../README.md) · [Workflow](usage.md) · [Troubleshooting](troubleshooting.md)

Run `stem4b init` for a commented configuration and a blank `.env`. The
[packaged template](../src/stem4b/templates/stem4b.toml) lists the main settings;
omitted fields use defaults. Unknown fields fail validation so typos do not silently
change behavior. Set real model and voice names before converting.

`-c PATH` selects a TOML file. Otherwise stem4b discovers `stem4b.toml` in the current
directory, or uses defaults and environment overrides if absent. `.env` is loaded
beside an explicitly selected configuration, otherwise from the current directory.
`--env-file PATH` overrides that location. Existing shell variables win over `.env`;
model/endpoint environment variables win over TOML. API keys are read only from the
configured `api_key_env` variables, never from `OPENAI_API_KEY` implicitly.

Relative `book.cover` and `narration.instructions_file` paths are resolved against
the configuration's directory. Source, output and workspace CLI paths are resolved
against the current directory. Never place secrets in endpoint URLs.

## Settings at a glance

| Table | Main controls |
| --- | --- |
| `[llm]` | Vision model, endpoint, token budget, compatibility, retry policy |
| `[tts]` | Speech model, endpoint, voice, format, clip length and workers |
| `[extraction]` | PDF page range, rendering resolution, EPUB non-linear content |
| `[narration]` | Book/paper policy, review, revision budget, chapter workers and instructions |
| `[navigation]` | Source-TOC reconciliation, chapter prefix and front matter |
| `[audio]` | Sample rate, AAC bitrate, heading/chapter pauses, TOC depth |
| `[book]` | Optional title, author and cover overrides |
| `[pronunciations]` | Exact, case-sensitive whole-term speech substitutions |

Use `stem4b doctor` to check locally, or `--stage extract` / `--stage narrate` to
check only the required settings. It never tests credentials, contacts providers or
spends credits. An absent API key is a warning because local servers can be unauthenticated.

## Provider compatibility and audio controls

- Chat Completions and speech use separate base URLs and credentials.
  OpenAI-hosted models are not required. The
  [official SDK](https://developers.openai.com/api/reference/python) handles requests and
  ordinary retries; Flex uses the explicit retry/fallback policy described below.
- `llm.json_mode = "prompt"` omits `response_format` for servers without JSON mode;
  responses are still schema-validated. JSON mode is the default.
- Set `llm.token_parameter = "max_completion_tokens"` when required by your model.
  No temperature is sent unless configured. Increase `max_output_tokens` for verbose
  mathematical/code sections or models whose reasoning uses the same token budget.
- `llm.extra_body` and `tts.extra_body` pass provider-specific fields without overriding
  the core request, using the SDK's `extra_body` option. Function calling is not required.
- Optional `llm.service_tier = "flex"` selects Flex processing on supported models/endpoints;
  omit it to send no tier parameter. Other provider-supported tier strings pass through.
  This applies to every LLM call (boundaries, narration, reviews, repairs and front-matter
  checks), not to `/audio/speech`. It can also be set through `LLM_SERVICE_TIER`.
  [OpenAI's Flex guide](https://developers.openai.com/api/docs/guides/flex-processing)
  recommends allowing for slower responses and occasional unavailable capacity. Consider
  a longer `llm.timeout_seconds`, such as `900`, when configuring a new conversion.
  By default retries keep the requested tier; standard fallback requires explicit opt-in.
  `requests.jsonl` records the tier actually requested and the returned tier when available;
  after a fallback it also records `configured_service_tier = "flex"`.
  Changing only `llm.service_tier` or the Flex controls below preserves narration,
  boundary and front-matter caches. Other LLM settings, including timeout/retry
  settings, participate in cache identities and can require regeneration.
  An existing `llm.extra_body.service_tier` is still supported, but do not set both forms.
- `LLM_BASE_URL`, `LLM_MODEL`, `LLM_SERVICE_TIER`, `TTS_BASE_URL`, `TTS_MODEL`, and `TTS_VOICE` override TOML.
  Shell variables take precedence over `.env`. File references are relative to the TOML.
- `tts.max_chars` is enforced after pronunciation replacement, splitting at paragraphs,
  sentences or words. Choose a limit your endpoint supports. Speech requests run with
  bounded `tts.workers`; `narration.workers` independently controls chapter concurrency.
  Sections within each chapter stay sequential.
- WAV, MP3, FLAC, Opus and AAC responses are normalized to mono 16-bit PCM. Raw `pcm`
  responses are assumed little-endian signed 16-bit mono at `tts.pcm_sample_rate`.
- Set `tts.instructions` only when your model supports it. `audio.bitrate`,
  heading/chapter pauses and table-of-contents depth control final assembly. Optional
  `book.title`, `book.author`, and `book.cover` override source metadata. EPUB covers and
  the first physical PDF page are used automatically, independently of narrated page selection.
- `llm.retries` / `tts.retries` configure the SDK's `max_retries` for ordinary requests
  (including standard fallback), and each endpoint's `timeout_seconds` configures its SDK
  timeout per attempt. Flex overrides SDK retries with its own bounded policy; the two
  loops are never nested. Authentication and unsupported-parameter failures stop immediately.
  Speech downloads are received one bounded clip at a time before being atomically saved,
  so the SDK can also retry interrupted body reads. Memory use scales with clip size and
  `tts.workers`, not book length. Partial downloads never replace complete clips.
  Invalid audio is kept as `.invalid` for diagnosis; rerunning requests a replacement.
- Provider HTTP failures log the full response body, HTTP status, endpoint path and request
  ID (when supplied) to stderr, without needing `--verbose`. This is the final response
  after configured retries; connection failures also log their underlying cause. Text/JSON
  returned by a speech provider in place of audio is included in the error too. Request
  headers and request payloads are not logged, and successful audio is never dumped to the
  console. Error bodies are not redacted, so review logs before sharing them if a provider
  echoes input or credentials. `requests.jsonl` remains a usage log, not an error log.

### Flex retries and optional standard fallback

These settings apply only when the effective `service_tier` is `"flex"`, whether configured
in `[llm]`, `LLM_SERVICE_TIER`, or the legacy `llm.extra_body.service_tier` field:

```toml
[llm]
service_tier = "flex"
flex_max_attempts = 6              # Total attempts, INCLUDING the initial request
flex_initial_backoff_seconds = 5
flex_max_backoff_seconds = 60
flex_fallback_to_standard = false  # Default: NEVER switch tiers
flex_fallback_service_tier = "auto"
```

Flex retries HTTP **429 and 503**, covering OpenAI resource-unavailable responses and
the 503 capacity responses from Gemini's OpenAI-compatible endpoint. Backoff approximately
doubles after each failure, capped at `flex_max_backoff_seconds`, with 0–25% downward jitter
to avoid synchronizing workers. Valid `Retry-After` (seconds or HTTP date) and
`retry-after-ms` hints are minimum delays. A hint above the configured maximum stops the
request without retrying early or switching tiers; increase the maximum or resume later.

Set `flex_fallback_to_standard = true` to opt into potentially higher costs. For example,
with `flex_max_attempts = 3`, three unsuccessful Flex attempts ending in HTTP 429/503 are
followed by a request using `flex_fallback_service_tier`. `"auto"` uses the provider/project's
normal routing, as recommended by the
[OpenAI Flex guide](https://developers.openai.com/api/docs/guides/flex-processing), and is
not a guarantee of a particular project tier. Use `"default"` to explicitly request
[OpenAI's standard pricing and performance](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Choose the value supported by your compatible endpoint. No provider, model, credentials,
prompt or generation settings change. The fallback uses the ordinary `llm.retries` budget;
it does not switch back to Flex or create another fallback loop.

Timeouts, connection failures and other transient HTTP errors (408, 409, 5xx) also retry
within the Flex budget, but do not trigger fallback unless the final failure is 429/503.
Authentication, bad-parameter errors, recognized quota/billing errors, explicit provider
`x-should-retry: false`, and cancellation do not trigger retries or tier fallback.
Invalid JSON, refusals, source-review rejections and truncation are content problems, not
capacity failures; they never directly trigger tier fallback.

The budget is per LLM call, including boundaries, reviews and repairs. Each new call starts
on Flex again, including JSON-repair calls; a successful fallback never changes the shared
configuration or another worker's tier. Retry warnings show the purpose, attempt count,
delay and provider error body, and fallback warnings make the pricing change explicit.
The default maximum is six Flex attempts; `llm.retries` does not multiply that count.
TTS requests are unaffected.

Clips are concatenated in source order as uniform PCM and encoded to AAC **once**. Chapter
times are computed from sample counts, including pauses, rather than rounded per-clip
timestamps. The completed M4B is checked with ffprobe for its audio stream, duration,
chapter boundaries and requested cover artwork, then atomically installed at the output path.
