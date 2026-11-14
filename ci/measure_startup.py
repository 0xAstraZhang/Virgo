"""Offline startup sample; run in fresh processes for baseline/profile comparisons.

Usage: python ci/measure_startup.py [--profile minimal|coding]
The faux provider and temporary home avoid credentials, network and user data.
Schema/prompt sizes are characters, not provider token or quality measurements.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("minimal", "coding"))
    args = parser.parse_args()
    start = time.perf_counter()
    from virgo.harness.session import AgentSession

    imported = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="virgo-measure-") as directory:
        root = Path(directory)
        session = AgentSession(root, home=root / "home", model="faux/measure", session=None,
                               settings={"profile": args.profile} if args.profile else {},
                               extensions=False, refresh_models=False)
        ready = time.perf_counter()
        payload = {
            "profile": args.profile or "baseline-default",
            "import_ms": round((imported - start) * 1000, 3),
            "construct_ms": round((ready - imported) * 1000, 3),
            "total_ms": round((ready - start) * 1000, 3),
            "modules": len(sys.modules),
            "virgo_modules": len([name for name in sys.modules if name.startswith("virgo.")]),
            "threads": len(threading.enumerate()),
            "tools": len(session.main.tools),
            "tool_names": [tool.name for tool in session.main.tools],
            "schema_chars": len(json.dumps([asdict(t.spec()) for t in session.main.tools], ensure_ascii=False)),
            "system_chars": len(session.main.system),
        }
        try:
            import resource

            payload["max_rss_platform_units"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            pass
        session.shutdown()
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
