"""Explicit inputs and safe exit status at the evaluation command boundary."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .adapters.fixture import FixtureEmbedding, FixtureReranker, FixtureSearch, FixtureTokenCounter
from .adapters.real import real_factories, real_reranker_factory, real_token_counter_factory
from .adapters.resources import ResourceMonitor
from .comparison import selected_candidates, tuning_candidates
from .models import COMPARISON_VERSION, load_suite, record, rows, string, unique, validate_suite
from .ports import (
    EmbeddingFactory,
    RerankerFactory,
    ResourceFactory,
    SearchFactory,
    TokenCounterFactory,
)
from .report import artifact_summary, public_report, validate_manifest, validate_public_report
from .runner import run_evaluation


def main(
    argv: list[str] | None = None,
    *,
    embedding_factory: EmbeddingFactory | None = None,
    search_factory: SearchFactory | None = None,
    resource_factory: ResourceFactory | None = None,
    reranker_factory: RerankerFactory | None = None,
    token_counter_factory: TokenCounterFactory | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="合成データの検索評価")
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--suite-sha256")
    parser.add_argument("--tuning-suite", type=Path)
    parser.add_argument("--tuning-sha256")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--real-config", type=Path)
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--tuning-report", type=Path)
    parser.add_argument("--tuning-report-sha256")
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--mode", choices=("fixture", "real"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--artifact-reference")
    options = parser.parse_args(argv)
    try:
        digest = options.suite_sha256
        if digest is None:
            if options.mode == "real":
                raise ValueError("Real evaluation requires a precommitted suite digest")
            digest = hashlib.sha256(options.suite.read_bytes()).hexdigest()
        suite = load_suite(options.suite, expected_sha256=digest)
        tuning = None
        if options.tune and suite["split"] != "tuning":
            raise ValueError("Only the tuning split may select parameters")
        if options.tuning_suite is not None or options.tuning_sha256 is not None:
            if options.tuning_suite is None or options.tuning_sha256 is None:
                raise ValueError("Pinned tuning suite required")
            tuning = load_suite(options.tuning_suite, expected_sha256=options.tuning_sha256)
            if suite["split"] != "acceptance" or tuning["split"] != "tuning":
                raise ValueError("Explicit acceptance and tuning splits required")
            if suite["protocol"] != tuning["protocol"]:
                raise ValueError("Comparison protocol mismatch")
            validate_suite(suite, tuning_case_ids=unique(rows(tuning["cases"]), "id"))
        elif options.mode == "real" and not options.tune:
            raise ValueError("Real evaluation requires pinned independent tuning inputs")
        candidate_bytes = options.candidates.read_bytes()
        candidates = rows(json.loads(candidate_bytes))
        if options.mode == "real" and record(suite["protocol"]).get("comparison_version") not in (
            COMPARISON_VERSION,
            "memory-v2",
            "memory-v2.1",
        ):
            raise ValueError("Real comparison requires the fixed protocol")
        input_files_sha256 = {
            "suite": digest,
            "candidates": hashlib.sha256(candidate_bytes).hexdigest(),
        }
        if options.tuning_suite is not None:
            input_files_sha256["tuning"] = options.tuning_sha256
        manifest = None
        if options.manifest is not None:
            manifest_bytes = options.manifest.read_bytes()
            manifest = record(json.loads(manifest_bytes))
            validate_manifest(manifest)
            input_files_sha256["manifest"] = hashlib.sha256(manifest_bytes).hexdigest()
        elif options.mode == "real":
            raise ValueError("Real evaluation requires an execution manifest")
        protocol = record(suite["protocol"])
        if protocol.get("comparison_version") == "memory-v2.1" and token_counter_factory is None:
            if options.mode == "real":
                if options.real_config is None:
                    raise ValueError("Explicit tokenizer model configuration required")
                token_counter_factory = real_token_counter_factory(
                    record(json.loads(options.real_config.read_bytes())), candidates
                )
            else:

                def fixture_counter(candidate: dict[str, object]) -> FixtureTokenCounter:
                    return FixtureTokenCounter()

                token_counter_factory = fixture_counter
        if options.tune:
            if options.tuning_report is not None:
                raise ValueError("Tuning cannot consume acceptance or prior selection results")
            candidates = tuning_candidates(candidates, protocol=protocol)
        elif options.tuning_report is not None:
            if tuning is None or options.tuning_report_sha256 is None:
                raise ValueError("Pinned independent tuning report required")
            tuning_bytes = options.tuning_report.read_bytes()
            if hashlib.sha256(tuning_bytes).hexdigest() != options.tuning_report_sha256:
                raise ValueError("Tuning report hash mismatch")
            tuning_report = record(json.loads(tuning_bytes))
            if tuning_report["mode"] != options.mode:
                raise ValueError("Fixture tuning cannot certify real parameters")
            validate_public_report(
                tuning_report,
                suite=tuning,
                candidates=tuning_candidates(candidates, protocol=protocol),
                token_counter_factory=token_counter_factory,
            )
            candidates = selected_candidates({**tuning_report, "suite": tuning}, candidates)
            input_files_sha256["tuning_report"] = options.tuning_report_sha256
        elif options.mode == "real":
            raise ValueError("Acceptance requires parameters selected from pinned tuning evidence")
        if embedding_factory is None and search_factory is None and options.mode == "real":
            if options.real_config is None:
                raise ValueError("Explicit local endpoints and synthetic DB configuration required")
            config = record(json.loads(options.real_config.read_bytes()))
            embedding_factory, search_factory = real_factories(config, candidates)
            reranker_factory = real_reranker_factory(config, candidates)
            monitoring = record(config["resources"])
            model_containers = record(monitoring["model_containers"])
            for candidate in candidates:
                string(model_containers[string(candidate["model_alias"])])
            db_container = string(monitoring["db_container"])
            gpu_device = string(monitoring["gpu_device"])

            def monitor(candidate: dict[str, object]) -> ResourceMonitor:
                return ResourceMonitor(
                    db_container=db_container,
                    model_container=string(model_containers[string(candidate["model_alias"])]),
                    gpu_device=gpu_device,
                )

            resource_factory = monitor
        if embedding_factory is None and search_factory is None and options.mode == "fixture":
            if options.fixture is None:
                raise ValueError("Explicit fixture port data required")
            fixture_bytes = options.fixture.read_bytes()
            fixture = record(json.loads(fixture_bytes))
            input_files_sha256["fixture"] = hashlib.sha256(fixture_bytes).hexdigest()

            def fixture_embedding(candidate: dict[str, object]) -> FixtureEmbedding:
                return FixtureEmbedding(candidate, fixture)

            def fixture_search(candidate: dict[str, object]) -> FixtureSearch:
                return FixtureSearch(fixture)

            embedding_factory = fixture_embedding
            search_factory = fixture_search
            if any("reranker" in c for c in candidates):
                if not all("reranker" in c for c in candidates):
                    raise ValueError("Separate enabled and disabled reranker comparisons required")

                def fixture_reranker(candidate: dict[str, object]) -> FixtureReranker:
                    return FixtureReranker(fixture)

                reranker_factory = fixture_reranker
        if embedding_factory is None or search_factory is None:
            raise ValueError("Explicit embedding and search port factories required")
        report = run_evaluation(
            suite,
            candidates,
            mode=options.mode,
            embedding_factory=embedding_factory,
            search_factory=search_factory,
            resource_factory=resource_factory,
            reranker_factory=reranker_factory,
            token_counter_factory=token_counter_factory,
        )
        if manifest is not None:
            report["manifest"] = manifest
        report["input_files_sha256"] = input_files_sha256
        payload = (
            json.dumps(
                public_report(report, token_counter_factory=token_counter_factory),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            + "\n"
        )
        summary = None
        if options.summary_output is not None or options.artifact_reference is not None:
            if options.summary_output is None or options.artifact_reference is None:
                raise ValueError("Summary output and relative artifact reference required")
            summary = artifact_summary(payload.encode(), options.artifact_reference)
        with options.output.open("x", encoding="utf-8") as output:
            output.write(payload)
        if summary is not None:
            assert options.summary_output is not None
            with options.summary_output.open("x", encoding="utf-8") as output:
                output.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        if any(run["execution_error"] is not None for run in rows(report["runs"])):
            return 2
        return 1 if report["quality_passed"] is False else 0
    except Exception:
        print("評価入力または実行の検証に失敗しました。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
