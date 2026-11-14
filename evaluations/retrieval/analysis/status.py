"""Read atomic progress/checkpoints without probing the measured servers."""

import argparse
import json
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("root", type=Path)
args = parser.parse_args()
for folder in sorted(args.root.glob("??-*")):
    final = folder / "result.json"
    if final.exists():
        result = json.loads(final.read_text())
        print(
            json.dumps(
                {
                    "attempt": folder.name,
                    "status": result["status"],
                    "quality_passed": sum(
                        c["status"] == "quality-passed"
                        for c in result.get("cells", {}).values()
                    ),
                    "physical_request_errors": sum(
                        result.get("query_error_types", {}).values()
                    ),
                }
            )
        )
    elif (folder / "progress.json").exists():
        progress = json.loads((folder / "progress.json").read_text())
        checkpoint = folder / "checkpoint.json"
        if checkpoint.exists():
            partial = json.loads(checkpoint.read_text())["result"]
            cells = partial.get("cells", {}).values()
            progress["completed_primary_cases"] = sum(
                "holdout" in cell for cell in cells
            )
            progress["primary_quality_passed"] = sum(
                cell.get("status") == "quality-passed" for cell in cells
            )
            progress["physical_query_errors_so_far"] = sum(
                partial.get("query_error_types", {}).values()
            )
        if progress.get("state") == "request-in-flight":
            progress["seconds_in_request"] = round(
                time.time() - progress["request_started_at_unix"], 1
            )
        print(json.dumps({"attempt": folder.name, **progress}))
    else:
        print(json.dumps({"attempt": folder.name, "status": "starting"}))
if (args.root / "terminal.json").exists():
    print((args.root / "terminal.json").read_text())
