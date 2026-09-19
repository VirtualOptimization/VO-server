"""Convert a RoomPlan JSON file into normalized, problem, and optimized outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

def main() -> None:
    load_dotenv()
    print("pipeline: startup", flush=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to raw RoomPlan JSON")
    parser.add_argument(
        "--normalized-out",
        default="workers/optimizer/examples/generated/roomplan.normalized.json",
        help="Path to save normalized scan JSON",
    )
    parser.add_argument(
        "--problem-out",
        default="workers/optimizer/examples/generated/roomplan.problem.json",
        help="Path to save layout problem JSON",
    )
    parser.add_argument(
        "--optimized-out",
        default="workers/optimizer/examples/generated/roomplan.optimized.json",
        help="Path to save optimized output JSON",
    )
    parser.add_argument(
        "--plot-out",
        default="workers/optimizer/examples/generated/roomplan.optimized.svg",
        help="Path to save before/after comparison plot",
    )
    parser.add_argument("--global-maxiter", type=int, default=40, help="DE max iterations")
    parser.add_argument("--global-popsize", type=int, default=10, help="DE population size")
    parser.add_argument("--local-maxiter", type=int, default=200, help="SLSQP max iterations")
    parser.add_argument(
        "--no-global",
        action="store_true",
        help="Skip differential evolution and use deterministic initial candidates plus SLSQP",
    )
    args = parser.parse_args()

    print("pipeline: importing optimizer modules", flush=True)
    print("pipeline: importing transform", flush=True)
    from server.services.transform import build_layout_problem, convert_roomplan_to_optimizer_payload
    from workers.optimizer.neufert_rules import validate_neufert_rules
    print("pipeline: transform imported", flush=True)
    print("pipeline: importing ai constraints", flush=True)
    from workers.optimizer.ai_constraints import maybe_apply_ai_constraints
    print("pipeline: ai constraints imported", flush=True)
    print("pipeline: importing layout", flush=True)
    from workers.optimizer.layout import CanonicalLayoutOptimizer
    print("pipeline: layout imported", flush=True)
    print("pipeline: optimizer modules imported", flush=True)

    src_path = Path(args.input).expanduser().resolve()
    normalized_out = Path(args.normalized_out)
    problem_out = Path(args.problem_out)
    optimized_out = Path(args.optimized_out)
    plot_out = Path(args.plot_out)

    raw_payload = json.loads(src_path.read_text(encoding="utf-8"))
    print("pipeline: input loaded", flush=True)
    normalized = convert_roomplan_to_optimizer_payload(raw_payload)
    print("pipeline: roomplan normalized", flush=True)
    problem = build_layout_problem(normalized)
    print(
        f"pipeline: problem built (items={len(problem.get('movable_items', []))}, "
        f"neufert={problem.get('neufert_rules_attached', 0)})",
        flush=True,
    )
    problem = maybe_apply_ai_constraints(problem)
    print("pipeline: optional AI constraints applied", flush=True)

    normalized_out.parent.mkdir(parents=True, exist_ok=True)
    normalized_out.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    problem_out.parent.mkdir(parents=True, exist_ok=True)
    problem_out.write_text(
        json.dumps(problem, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    optimizer = CanonicalLayoutOptimizer(problem)
    print("pipeline: optimizer created", flush=True)
    optimized = optimizer.optimize(
        global_maxiter=args.global_maxiter,
        global_popsize=args.global_popsize,
        local_maxiter=args.local_maxiter,
        use_global=not args.no_global,
    )
    print("pipeline: optimizer finished", flush=True)
    optimized["ai_used"] = problem.get("ai_used", False)
    optimized["ai_error"] = problem.get("ai_error")
    if problem.get("ai_constraints"):
        optimized["ai_constraints"] = problem["ai_constraints"]
    validate_neufert_rules(problem, optimized)
    print(
        "pipeline: Neufert validation "
        f"{optimized['neufert_validation']['summary']}",
        flush=True,
    )

    optimized_out.parent.mkdir(parents=True, exist_ok=True)
    optimized_out.write_text(
        json.dumps(optimized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    optimizer.save_comparison_svg(optimized, plot_out)

    print(f"Normalized JSON saved to: {normalized_out}")
    print(f"Layout problem JSON saved to: {problem_out}")
    print(f"Optimized JSON saved to: {optimized_out}")
    print(f"Comparison plot saved to: {plot_out}")


if __name__ == "__main__":
    main()
