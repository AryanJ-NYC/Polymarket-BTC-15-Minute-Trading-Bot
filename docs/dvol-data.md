# Continuous free BTC volatility observations

`tools.collect_deribit_dvol` archives the public Deribit BTC DVOL minute endpoint.
The existing research collector already records receipt-bound Kraken BTC/USD
trades. Together these sources permit future evaluation of a short-expiry binary
probability model; this collector does not calculate signals or submit orders.

```sh
python -m tools.collect_deribit_dvol --output /path/to/private/dvol-data
```

## Fixed capture contract

One public GET is started every 30 seconds when the preceding request permits;
requests never overlap. Parameters are BTC, resolution60, start at the current
minute floor minus300 seconds and end at request-start milliseconds. Each request
has a15-second total deadline and256KiB response-body cap. Retry/backoff can make
capture sparser and must be retained as such. No pagination follow-up or historical
backfill is performed. All returned revisions, repeats and partial/current minutes
remain in the archive.

Each response retains its parameters, start/completion UTC and monotonic clocks,
HTTP status, selected nonsecret headers and exact bounded raw body. Parsing and
binding status distinguish valid complete windows, pagination/incomplete results,
errors and malformed input. Unfinished/truncated bodies are explicitly incomplete.
The response completion is the earliest availability claimed by this collector;
provider candle labels are not receipt, publication or guaranteed close times.
Do not backdate a later observation or substitute a later candle revision in an
as-of backtest. Archived bytes and parsed fields are evidence, not independent
confirmation of provider completeness.

The shared Archive writer supplies session/sequence identity, exclusive directory
ownership, bounded queue with explicit local gaps, durable gzip records and disk
reserve failure. Status distinguishes accepted observations from durable records.
The process runs until stopped; there is no campaign or trade limit.

Source contracts: [public DVOL endpoint](https://docs.deribit.com/api-reference/market-data/public-get_volatility_index_data),
[DVOL definition](https://insights.deribit.com/exchange-updates/dvol-deribit-implied-volatility-index/).
The annualized percentage index is not a direct probability of a five-minute or
fifteen-minute settlement. Candle-edge/publication/revision assumptions belong in
any downstream model. Historical H33 results are not forward or live-fill proof.

## Isolated deployment

Install a reviewed immutable release using `deploy/install-dvol.sh` on the known
droplet. It creates only `polymarket-deribit-dvol.service`, with dedicated
`polymarket-dvol` user, release `/opt/polymarket-us/dvol-collector/releases/<commit>`,
`current`/`DEPLOYED_REVISION`, and archive `/var/lib/polymarket-deribit-dvol`.
The unit has unlimited runtime, restart on failure,128MiB memory,10%CPU, low
scheduling priority and access only to its own writable archive. Trading credentials,
trading/research/partition data and the quote archive are inaccessible. It does
not use an environment file or authenticated client. No new dependency is added.

Installation manages only this service. Existing collectors and trading keep their
own releases and processes. A repository merge does not update this collector;
manual installation of an exact reviewed release does. Rollback stops this service,
points `current` to a retained release, restores its unit and `DEPLOYED_REVISION`,
then starts this service. To stop permanently, disable this service; retain data.

## AI QA checklist

- Generated HTTP fixtures cover success, repeats/revisions, malformed/binding
  failures, pagination,429/backoff, network/deadline failure and body cap.
- Process fixtures cover cancellation, archive/writer failure and accepted versus
  durable status. Existing collector tests still pass without source changes.
- Lint, format, full test suite, Python compilation, shell syntax and dependency
  checks pass on the exact release. This Python/service change has no UI checks.
- Before activation, capture existing service PIDs/revisions/source hashes and
  archive session identities plus bounded prefix hashes. Verify resource headroom
  and the new service/release/archive identity.
- After activation, independently read the new service's exact revision/source,
  enabled/active/unlimited-runtime status, dedicated identity and limits.
- Observe multiple durable HTTP responses with advancing actual completion clocks,
  matching bounded raw bodies and parse/binding status. Repeated minute labels must
  remain distinct records. A running process alone does not pass this item.
- Compare old service identities/source hashes/archive prefixes and continued writes
  after activation. Test denied credential/other-archive readability in the new
  process's mount namespace without reading their contents.
- Keep capture running indefinitely after QA. Record the exact release and evidence
  paths; distinguish measured short-run health from long-run completeness.
