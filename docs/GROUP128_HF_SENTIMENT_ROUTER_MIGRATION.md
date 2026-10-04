# group128 (2026-10-04) - Hugging Face sentiment moved off the retired endpoint

Cumulative on group127. Run `bash run_tests.sh` in analysis-intelligence-service on the VM.

## What the boot log showed
`HF API call failed: [Errno -5] No address associated with hostname` on news analysis. `news/main.py::_score_headline` posted to `api-inference.huggingface.co`, which Hugging Face has retired (it no longer resolves). Every headline therefore got the neutral 0.0 fallback, and each call still waited on a failing connection.

## Change (`analysis-intelligence-service/news/main.py`)
- Calls the Inference Providers router: `https://router.huggingface.co/v1/chat/completions` (OpenAI-style: `model`, `messages`, `max_tokens`, `temperature`). The answer is read from `choices[0].message.content`; "positive" -> 0.8, "negative" -> -0.8, anything else -> 0.0, as before.
- New env vars, all optional: `HF_MODEL` (default `mistralai/Mistral-7B-Instruct-v0.2`), `HF_API_URL` (default the router URL). `HF_API_KEY` is unchanged. Documented in `.env.oracle.example`.
- After a network-level failure (DNS, connect, timeout) the call is skipped for 300 s (`HF_FAILURE_BACKOFF_SEC`) rather than retried for every headline. Status-code errors and malformed payloads do not trigger the back-off. The 429/503 rate-limit reporting is unchanged.

## NOT verified (needs your VM)
I could not call Hugging Face from the sandbox. Whether the default model is served to your token through Inference Providers is unconfirmed; the router only serves models offered by an enabled provider. If the log shows `HF API error: 400/404/410`, set `HF_MODEL` to a model your HF account lists under Inference Providers and make sure the token has that permission. Until then the safe behaviour is unchanged: sentiment falls back to neutral.

## Tests
`tests/test_news_main.py::TestScoreHeadline`: the classification test now uses the new request/response shape; new tests for the default endpoint, the back-off after a transport error, back-off expiry, and no back-off on a malformed payload. Sandbox has no httpx/pytest: the real function body was run against a stub httpx (all scenarios pass); the test file compiles, not run under pytest.
