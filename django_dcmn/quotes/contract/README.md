# `quote.v3` contract (frozen)

`POST /api/quote-requests/` (DESIGN §3). The hub keeps a byte-identical copy of the schema and the fixtures at
`sprints/sprint-14/quote/contract/` (WP8); `quotes/tests/test_contract_sync.py` compares the two.

- `quote.v3.schema.json`: DESIGN §3.9 verbatim, JSON Schema draft 2020-12, normative for the client.
  The server is more lenient than the schema (DESIGN §3.3): bad answers are dropped, never a 400.
- `fixtures/*.json`: the executable contract. Each file is

  ```json
  {
    "description": "what the case shows",
    "headers": {"Idempotency-Key": "…"},        // optional, extra request headers
    "body": { … },                              // exactly what the page POSTs
    "expect": {
      "status": 201,
      "body": {"ok": true},                     // subset of the response body (equal values)
      "body_keys": ["reference", "received_at"],// keys that must be present (values vary per run)
      "rows": {"submissions": 1, "lines": 2, "rejections": 0},
      "submission": {"primary_service": "apostille"},   // optional, subset of the saved QuoteSubmission
      "lines": [{"service_key": "apostille"}],          // optional, subsets of the QuoteLine rows by position
      "rejection": {"reason": "honeypot"}               // optional, subset of the QuoteRejection row
    }
  }
  ```

  The server replays each fixture with `QUOTE_V3_ENABLED=True`, `QUOTE_V3_TEST_ONLY=False`, no Turnstile
  secret (verdict `skipped`) and every other flag at its default.

Error paths use 0-based indexes: `services[1].service` is the second element of `services`.
