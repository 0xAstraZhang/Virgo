# Serial and parallel suite validation (2026-11-13)


[Manual validation run](https://github.com/0xAstraZhang/Virgo/actions/runs/43390383788)
compared both modes at commit `cb30ecd` on the same runner per platform:

| Platform | Serial | Parallel | Reduction | Identical outcomes |
| --- | ---: | ---: | ---: | --- |
| Ubuntu | 364.08 s | 297.60 s | 18.3% | 1273 passed, 6 skipped |
| Windows | 429.69 s | 362.88 s | 15.5% | 1252 passed, 27 skipped |
| macOS | 451.41 s | 356.03 s | 21.1% | 1279 passed, 6 skipped |

These are complete runner command wall times, not total workflow durations.
Each pair had identical individual normalized test identities/statuses and no
report-integrity problems. All three platforms also passed the injected-failure
CLI acceptance and PowerShell cache isolation acceptance with 48 real launches
per platform. This is one cloud pair per OS; runner load and timing variability
mean the percentages are measurements, not guarantees. Two earlier local macOS
trials reduced 155.09 s to 103.95/103.51 s, illustrating why local timings alone
were insufficient to estimate hosted-runner gains.
