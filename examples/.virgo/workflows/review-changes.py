"""Example workflow: review the working-tree diff along several dimensions in parallel, then verify."""

META = {"name": "review-changes", "description": "Parallel multi-dimension review of the uncommitted diff"}

FINDINGS = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "location": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["title", "severity"],
            },
        }
    },
    "required": ["findings"],
}
VERDICT = {
    "type": "object",
    "properties": {"real": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["real"],
}


def run(ctx, args):
    dims = args.get("dimensions") or ["correctness", "security", "tests"]
    ctx.phase("collect")
    diff = ctx.agent(
        "Run `git diff` and return the diff verbatim (truncate files over 400 lines).",
        tools="read-only",
        label="diff",
    )
    ctx.phase("review")
    reviews = ctx.map(
        dims,
        lambda d: ctx.agent(
            f"Review this diff only for {d} issues:\n\n{diff}", schema=FINDINGS, label=f"review-{d}"
        ),
    )
    findings = [f for r in reviews for f in r["findings"]]
    ctx.phase("verify")
    verdicts = ctx.map(
        findings,
        lambda f: ctx.agent(
            f"Is this review finding real? Check the code.\nFinding: {f}\n\nDiff:\n{diff}",
            schema=VERDICT,
            tools="read-only",
            label=f"verify-{f['title']}",
        ),
    )
    order = {"high": 0, "medium": 1, "low": 2}
    confirmed = [
        f | {"reason": v.get("reason", "")} for f, v in zip(findings, verdicts, strict=True) if v["real"]
    ]
    return {
        "confirmed": sorted(confirmed, key=lambda f: order[f["severity"]]),
        "rejected": len(findings) - len(confirmed),
    }
