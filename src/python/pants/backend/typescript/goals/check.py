# Copyright 2025 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""
TypeScript typechecking goal for Pants.
Runs `tsc --build --no-emit` per NodeJS resolve (project), using the NodeJS tool subsystem.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePath

from pants.backend.javascript.nodejs_project import AllNodeJSProjects
from pants.backend.javascript.package_json import PackageJsonSourceField
from pants.backend.javascript.subsystems.nodejs_tool import (
    NodeJSToolRequest,
)
from pants.core.goals.check import (
    CheckRequest,
    CheckResult,
    CheckResults,
    CheckSubsystem,
)
from pants.engine.process import FallibleProcessResult
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.target import FieldSet, Targets, SourcesField
from pants.core.util_rules.source_files import (
    determine_source_files,
    SourceFilesRequest,
)
from pants.engine.unions import UnionRule
from pants.core.target_types import FileSourceField
from pants.backend.typescript.target_types import TypeScriptSourceField
from pants.backend.typescript.tsc import TscTool
from pants.util.logging import LogLevel


# Subsystem for configuring the typecheck goal
class TypeScriptCheckSubsystem(CheckSubsystem):
    name = "tsc"
    help = "Typecheck TypeScript code using tsc. Runs per NodeJS resolve."


# FieldSet for TypeScript sources
@dataclass(frozen=True)
class TypeScriptCheckFieldSet(FieldSet):
    required_fields = (TypeScriptSourceField,)
    sources: TypeScriptSourceField


# CheckRequest for TypeScript typechecking
class TypeScriptCheckRequest(CheckRequest):
    field_set_type = TypeScriptCheckFieldSet
    tool_name = "tsc"


@rule(desc="Typecheck TypeScript with tsc", level=LogLevel.INFO)
async def typecheck_typescript(
    request: TypeScriptCheckRequest,
    all_projects: AllNodeJSProjects,
    tsc: TscTool,
    subsystem: TypeScriptCheckSubsystem,
    # Add the ability to get all targets in the project
    all_targets: Targets,
) -> CheckResults:
    # Find the set of NodeJS projects relevant to the incoming sources
    project_dirs = set()
    for field_set in request.field_sets:
        spec_path = PurePath(field_set.sources.address.spec_path)
        try:
            project = all_projects.project_for_directory(str(spec_path))
            project_dirs.add(project.root_dir)
        except Exception:
            continue
    results = []
    for root_dir in project_dirs:
        project = all_projects.project_for_directory(root_dir)
        # Find all targets under this project root
        project_targets = [
            tgt
            for tgt in all_targets
            if tgt.address.spec_path.startswith(project.root_dir)
        ]
        # Filter for targets with a SourcesField (TypeScript sources, tests, and file targets)
        sources_fields = [
            tgt[SourcesField] for tgt in project_targets if tgt.has_field(SourcesField)
        ]

        # Use determine_source_files to hydrate and merge all sources
        source_files = await determine_source_files(
            SourceFilesRequest(
                sources_fields,
                # Accept all sources fields, including FileSourceField for tsconfig.json
                for_sources_types=(
                    TypeScriptSourceField,
                    FileSourceField,
                    PackageJsonSourceField,
                ),
                enable_codegen=True,
            )
        )
        project_digest = source_files.snapshot.digest
        tsc_req = tsc.request(
            args=("--build", "--no-emit"),
            input_digest=project_digest,
            description=f"Typechecking TypeScript in resolve {project.default_resolve_name}",
            level=LogLevel.INFO,
        )
        tsc_req = dataclass_replace(tsc_req, resolve=project.default_resolve_name)
        proc = await Get(FallibleProcessResult, NodeJSToolRequest, tsc_req)
        results.append(
            CheckResult.from_fallible_process_result(
                proc,
                partition_description=f"TypeScript resolve: {project.default_resolve_name}",
            )
        )
    return CheckResults(results, checker_name=TypeScriptCheckRequest.tool_name)


# Helper to replace fields in a frozen dataclass
def dataclass_replace(obj, **kwargs):
    return type(obj)(**{**obj.__dict__, **kwargs})


def rules() -> Iterable:
    return [*collect_rules(), UnionRule(CheckRequest, TypeScriptCheckRequest)]
