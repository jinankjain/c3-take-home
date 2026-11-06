"""C3 replay/check interface; the checker imports no candidate source."""
import argparse
import json
from pathlib import Path
import sys

import yaml


def scenario_path(value):
    direct = Path(value)
    if direct.is_file():
        return direct.resolve()
    for suffix in (".yaml", ".yml", ".json"):
        candidate = Path("scenarios") / (value + suffix)
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError("scenario file does not exist")


def load_scenario(value, faults=None):
    path = scenario_path(value)
    scenario = yaml.safe_load(path.read_text())
    if not isinstance(scenario, dict) or not isinstance(scenario.get("name"), str):
        raise ValueError("scenario requires a name")
    if not isinstance(scenario.get("actions"), list) or not 1 <= len(scenario["actions"]) <= 16:
        raise ValueError("scenario requires between 1 and 16 actions")
    if any(key in scenario for key in ("include", "extends", "$include")):
        raise ValueError("file includes are unsupported")
    if not isinstance(scenario.get("world", {}), dict) or not isinstance(scenario.get("params", {}), dict):
        raise ValueError("world and params must be mappings")
    selected = faults if faults is not None else scenario.get("faults")
    if isinstance(selected, str):
        candidate = Path(selected)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate if faults is not None else path.parent / candidate
        selected = yaml.safe_load(candidate.read_text())
        if isinstance(selected, dict) and "faults" in selected:
            selected = selected["faults"]
    if selected is None:
        selected = {}
    if not isinstance(selected, dict):
        raise ValueError("faults must be a mapping or a fault-file path")
    scenario["faults"] = selected
    return scenario


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--scenario", required=True)
    replay.add_argument("--seed", type=int, required=True)
    replay.add_argument("--out", required=True)
    replay.add_argument("--faults")
    check = sub.add_parser("check")
    check.add_argument("--trace", required=True)
    check.add_argument("--reference-messages", type=int)
    args = parser.parse_args()
    if args.mode == "check":
        from .checks import check_trace
        result = check_trace(Path(args.trace), args.reference_messages)
        print(json.dumps(result, sort_keys=True))
        return 1 if result["violations"] else 0
    try:
        scenario = load_scenario(args.scenario, args.faults)
        from .engine import run_scenario
        summary = run_scenario(scenario, args.seed, Path(args.out))
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        return 3
    print(json.dumps(summary, sort_keys=True))
    return 3 if summary["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
