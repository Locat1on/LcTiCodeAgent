# Context compaction benchmark — 2026-08-31

## Setup

- Scenario: `buggy_average_multiturn_v1`
- Model: `google/gemini-3.7-flash`
- Logical context budget: 4,096 tokens
- One isolated workspace copy per strategy
- Three read-only evidence-building turns, one manual compaction point, then the same repair task
- Independent post-run `unittest` verification
- One run per strategy; this is a preliminary functional comparison, not a statistical claim

## Results

| Strategy | Task | Compression ratio | Prompt tokens | Re-reads | Recoverable events | Time |
|---|---:|---:|---:|---:|---:|---:|
| none | pass | N/A | 42,634 | 2 | 0 | 29.419 s |
| drop_oldest | pass | 0.4496 | 36,876 | 2 | 4 | 28.768 s |
| plain_summary | pass | 0.4028 | 32,980 | 2 | 3 | 43.025 s |
| validated | pass | **0.2315** | 43,017 | 4 | **5** | 39.758 s |

All four runs preserved the test files, required constraints and identifiers, and all four independently passed the final tests. Test-evidence accuracy was 1.0 after normalizing common English and Chinese success wording.

`compression_ratio` is the after/before ratio of the single compaction event that removed the most tokens. This avoids diluting a major structured-summary rewrite with small deterministic pruning events. `total_tokens_removed` remains available in the JSON artifact.

## Interpretation

The validated method produced the strongest single reduction (`3585 → 830`, ratio `0.2315`) and retained five recoverable event pointers. It was not the cheapest run: its first manual structured summary invented an event ID and was rejected by the local factuality validator; a later threshold-triggered summary passed. That rejection, plus additional reads, raised total prompt tokens above the baselines.

The result supports two bounded claims:

1. Evidence-validated summarization can compress more aggressively while preserving task success and recoverability in this scenario.
2. Validation has a measurable cost when the model emits an invalid summary; the current implementation favors safety over minimum token use.

It does **not** support a general claim that the proposed method always reduces total cost or improves task success. More tasks and repeated runs would be required for that conclusion.

Machine-readable results: `docs/evaluation/context-benchmark-20260831.json`.
