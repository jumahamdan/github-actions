"""Always-reported required checks with explicit applicability, never missing passes."""

import argparse
import json

from .common import PipelineError, atomic_json, cli_error, load_json, strict_keys


def evaluate(config, results):
    strict_keys(config, {"schema", "checks"}, {"schema", "checks"})
    if config["schema"] != 1 or not isinstance(config["checks"], dict) or not config["checks"]:
        raise PipelineError("Required-check policy is empty")
    if not isinstance(results, dict):
        raise PipelineError("Job results must be a JSON object")
    decisions = {}
    for job, applicable in config["checks"].items():
        if not isinstance(applicable, bool) or job not in results:
            raise PipelineError("Required-check applicability or job result is missing")
        result = results[job].get("result") if isinstance(results[job], dict) else results[job]
        if applicable and result != "success":
            raise PipelineError(f"Applicable check did not succeed: {job}")
        if applicable and isinstance(results[job],dict) and "status" in results[job].get("outputs",{}):
            if results[job]["outputs"]["status"] != "passed":
                raise PipelineError(f"Required operation remains pending or advisory: {job}")
        if not applicable and result not in ("success", "skipped"):
            raise PipelineError(f"Unexpected failure of an inapplicable check: {job}")
        decisions[job] = "passed" if applicable else "not-applicable"
    if set(results) - set(config["checks"]):
        raise PipelineError("Job is absent from the applicability policy")
    return {"schema": 1, "status": "passed", "checks": decisions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    atomic_json(args.output, evaluate(load_json(args.config), json.loads(args.results)))


if __name__ == "__main__":
    cli_error(main)
