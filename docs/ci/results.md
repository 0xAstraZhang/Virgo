# CI results and comparison policy

[CI overview](../CI.md)

## Reading results

The comparison job's summary and `comparison` artifact contain the outcome table,
new failures, existing failures, fixed failures, changed skips, and missing/new tests.
Each `results-<platform>-<revision>` artifact contains pytest output, JUnit XML,
normalized results, interpreter information, and the tested SHA.

Comparison identifies tests by function/method name and parameter ID, ignoring the
module path so moving a test does not manufacture a new failure. Duplicate keys
are rejected as ambiguous. The two renamed parameter IDs in PR #9's
`test_compaction_does_not_enable_native_search` are explicitly mapped to their
previous names. The Harness simplification's four renamed Todo, question and
sub-agent cases also map to their previous names so changed expectations remain
comparable; other renames are reported as added/removed tests.
PR #18's unreadable-thought and inline-activity replacement cases are also mapped
to their original names. PR #16's startup refresh, picker cancellation, and two
parameterized metadata-precedence replacements are mapped likewise. Their new
expectations reflect the feature change: refresh on restart with cached fallback,
Esc closing only the picker, and explicit configuration overriding fetched fields.
PR #34's three parameterized folding cases and web-search result case map to their
previous names: headers now toggle immediately, while body and padding clicks
keep details open for text selection. These replacements retain their baseline
identities for failure, skip, and missing-test comparisons.
The comparison CLI check covers passing and failing replacements, unknown names,
and collisions between old and new names. An approved replacement can still fail as a regression;
an unknown rename or an alias collision still fails the comparison.

A failed PR test is classified as a regression only if the baseline passed it.
Failures that were skipped or absent in the baseline are listed separately as
newly exposed failures, and also fail the comparison. Existing failures remain
visible but do not fail the comparison. An unapproved disappearing executed baseline test,
an ambiguous result, an incomplete run, or a missing artifact fails the comparison.
A previously executed baseline case becoming skipped also fails the comparison.
A newly added test that is platform-skipped is listed as added, not a regression.
Module-level collection skips are reported separately from individual test skips.
Main's single-revision mode fails on every failed/error test; it does not apply the
PR allowance for existing failures.

### Reviewed duplicate retirements

Intentional duplicate removal uses an explicit entry in
`ci/pipeline/reports.py::APPROVED_RETIREMENTS`, scoped to the exact reviewed
baseline SHA, test identity and original module. It is recognized only when the
removed case passed in that baseline and every named retained scenario passed
in both baseline and head. A failing baseline case, missing/skipped/failing
retained coverage, another baseline or an unrelated same-name case cannot use
the exemption. A retained old case still receives the ordinary failure/skip checks.

The removed identity remains in `removed`; `retired` records its retained coverage,
also listed in the readable summary. Unregistered deletions still fail. This is
separate from identity aliases and does not reduce expected counts or omit tests
from execution. See the [retirement record](../testing/test-retirements.md) for
the rationale behind each entry. If the PR base changes before merge, review
the mapping against that base and update the exact SHA explicitly. Once merged,
later baselines no longer contain these cases and the old exception cannot apply.

The summary validates upstream job outcomes and every expected report. Invalid
JSON (including duplicate keys), unexpected/duplicate matrix records, empty test
sets, incomplete execution, invalid statuses/counts, mismatched exit codes and
wrong SHAs fail with a readable summary. Missing or invalid targets also fail.
The producer's diagnostics remain visible even when its report is rejected.

`ci/compare_tests.py` exposes `resolve`, `classify`, `run`, and `compare` commands.
`classify --target target.json [--project .]` adds the full-test decision and reason.
`compare` retains the default base/head behavior and accepts `--mode head` for a
single strict revision; workflow targets select the mode automatically. Workflow
calls also pass `--resolve-result`, `--checks-result`, and `--test-result`, so
skipped/failed prerequisites cannot produce a successful final check.

