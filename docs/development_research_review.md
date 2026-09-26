# Development research review v1

`qsl.development_research_review.v1` carries one completed development study for
read-only review. It is a separate evidence category from
`qsl.research_task.v1`; it is not a P3 observation, and this producer cannot
convert it into a research task.

## Producer and provenance

Run `python3 scripts/build_development_research_review.py --summary <A-summary>
--output <message.json>`. The producer reads the caller-selected file once and
requires its exact bytes to match the frozen A summary SHA-256
`7c4a2dcf03c2becb19c012e015f86c5a1f5b6f845afd7d88516a2c515462da91`. It then
checks the complete aggregate shape, fixed candidate identity, matched recovery,
source assurance, non-certified PIT state, and upstream inputs. It does not
rerun the study.

The message includes a 25-entry upstream input index covering the source
manifest, R6 manifests/materialization, future manifest/pages/license record,
R7/R8 engines and policies, capital and settlement policies, runner, and TQQQ
contract. Output ledgers are not inputs and are not copied into the message.
The source summary byte digest and the digest of the normalized result are
separate fields. Result projection contains session count, scales, four selected
metrics per path, and the signed numeric comparisons; negative values are kept.

The summary has no separate strategy revision or portfolio key. The message
therefore uses its fixed candidate identity for the candidate, portfolio, and
strategy identity, and binds the strategy revision to the reported R8 engine
digest. This is an explicit source limitation, not an independently verified
portfolio registry identity. `producer_revision_sha256` is computed from the
current `service/development_research_review.py` source bytes and checked again
by the producer validator. It identifies those source bytes; it is not a
signature or an authenticated build attestation.

## Authority and validation

The exact authority is `review_disposition=advisory`, `mode=read_only`,
`no_order=true`, and `adoption`, `codegen`, `experiment`, `notification`, and
`trade` all false. The validator rejects extra fields, privacy/path/raw/account/
credential keys, source assurance or PIT upgrades, authority escalation, old
P3/task schema labels, and digest mismatches. `duplicate_key` is derived from
study/candidate/portfolio, producer/runner, source index, policy, settlement,
cost, and message version; `created_at` and the result digest are excluded so a
second result for the same economic identity can be detected as a conflict. A
local `DuplicateReviewRegistry` treats a repeated key and result digest as an
idempotent read and rejects the same key with another result digest.

The SHA-256 fields detect accidental mutation; they are not signatures and do
not authenticate the producer. The evidence remains single-source structural
research. Retrospective corporate-action process dates mean strict point-in-time
certification is false. No paper, shadow, live, deployment, account, adoption,
experiment, notification, or trading action is authorized or performed.
