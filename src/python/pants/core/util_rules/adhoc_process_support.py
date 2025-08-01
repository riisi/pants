# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from textwrap import dedent  # noqa: PNT20
from typing import TypeVar

from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.build_graph.address import Address
from pants.core.environments.rules import EnvironmentNameRequest, resolve_environment_name
from pants.core.goals.package import (
    EnvironmentAwarePackageRequest,
    PackageFieldSet,
    environment_aware_package,
)
from pants.core.goals.run import RunFieldSet, RunInSandboxRequest
from pants.core.target_types import FileSourceField
from pants.core.util_rules.source_files import SourceFilesRequest, determine_source_files
from pants.core.util_rules.system_binaries import BashBinary
from pants.engine import process
from pants.engine.addresses import Addresses, UnparsedAddressInputs
from pants.engine.env_vars import EnvironmentVarsRequest
from pants.engine.environment import EnvironmentName
from pants.engine.fs import (
    EMPTY_DIGEST,
    CreateDigest,
    Digest,
    DigestSubset,
    Directory,
    FileContent,
    GlobExpansionConjunction,
    MergeDigests,
    PathGlobs,
    PathMetadataRequest,
    Snapshot,
)
from pants.engine.internals.build_files import resolve_address
from pants.engine.internals.graph import (
    find_valid_field_sets,
    resolve_target,
    resolve_targets,
    resolve_unparsed_address_inputs,
    transitive_targets,
)
from pants.engine.internals.native_engine import AddressInput, PathMetadata, RemovePrefix
from pants.engine.internals.platform_rules import environment_vars_subset
from pants.engine.intrinsics import (
    create_digest,
    digest_subset_to_digest,
    digest_to_snapshot,
    execute_process,
    merge_digests,
    path_globs_to_paths,
    path_metadata_request,
    remove_prefix,
)
from pants.engine.process import (
    FallibleProcessResult,
    Process,
    ProcessCacheScope,
    ProcessResult,
    ProductDescription,
    fallible_to_exec_result_or_raise,
)
from pants.engine.rules import Get, collect_rules, concurrently, implicitly, rule
from pants.engine.target import (
    FieldSetsPerTargetRequest,
    SourcesField,
    Target,
    TransitiveTargetsRequest,
    WrappedTargetRequest,
)
from pants.util.frozendict import FrozenDict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdhocProcessRequest:
    description: str
    address: Address
    working_directory: str
    root_output_directory: str
    argv: tuple[str, ...]
    timeout: int | None
    input_digest: Digest
    immutable_input_digests: FrozenDict[str, Digest] | None
    append_only_caches: FrozenDict[str, str] | None
    output_files: tuple[str, ...]
    output_directories: tuple[str, ...]
    env_vars: FrozenDict[str, str]
    log_on_process_errors: FrozenDict[int, str] | None
    log_output: bool
    capture_stdout_file: str | None
    capture_stderr_file: str | None
    workspace_invalidation_globs: PathGlobs | None
    cache_scope: ProcessCacheScope | None = None
    use_working_directory_as_base_for_output_captures: bool = True
    outputs_match_error_behavior: GlobMatchErrorBehavior = GlobMatchErrorBehavior.error
    outputs_match_conjunction: GlobExpansionConjunction | None = GlobExpansionConjunction.all_match


@dataclass(frozen=True)
class PreparedAdhocProcessRequest:
    original_request: AdhocProcessRequest
    process: Process


@dataclass(frozen=True)
class FallibleAdhocProcessResult:
    process_result: FallibleProcessResult
    adjusted_digest: Digest
    description: str


@dataclass(frozen=True)
class AdhocProcessResult:
    process_result: ProcessResult
    adjusted_digest: Digest


@dataclass(frozen=True)
class ResolveExecutionDependenciesRequest:
    address: Address
    execution_dependencies: tuple[str, ...] | None
    runnable_dependencies: tuple[str, ...] | None


@dataclass(frozen=True)
class ResolvedExecutionDependencies:
    digest: Digest
    runnable_dependencies: RunnableDependencies | None


@dataclass(frozen=True)
class RunnableDependencies:
    path_component: str
    immutable_input_digests: Mapping[str, Digest]
    append_only_caches: Mapping[str, str]
    extra_env: Mapping[str, str]


@dataclass(frozen=True)
class ToolRunnerRequest:
    runnable_address_str: str
    args: tuple[str, ...]
    execution_dependencies: tuple[str, ...]
    runnable_dependencies: tuple[str, ...]
    target: Target
    named_caches: FrozenDict[str, str] | None = None


@dataclass(frozen=True)
class ToolRunner:
    digest: Digest
    args: tuple[str, ...]
    extra_env: FrozenDict[str, str]
    extra_paths: tuple[str, ...]
    append_only_caches: FrozenDict[str, str]
    immutable_input_digests: FrozenDict[str, Digest]


#
# Things that need a home
#


@dataclass(frozen=True)
class ExtraSandboxContents:
    digest: Digest
    paths: tuple[str, ...]
    immutable_input_digests: Mapping[str, Digest]
    append_only_caches: Mapping[str, str]
    extra_env: Mapping[str, str]


@dataclass(frozen=True)
class MergeExtraSandboxContents:
    additions: tuple[ExtraSandboxContents, ...]


@rule
async def merge_extra_sandbox_contents(request: MergeExtraSandboxContents) -> ExtraSandboxContents:
    additions = request.additions

    digests: list[Digest] = []
    paths: list[str] = []
    immutable_input_digests: dict[str, Digest] = {}
    append_only_caches: dict[str, str] = {}
    extra_env: dict[str, str] = {}

    for addition in additions:
        digests.append(addition.digest)
        if addition.paths:
            paths.extend(addition.paths)
        _safe_update(immutable_input_digests, addition.immutable_input_digests)
        _safe_update(append_only_caches, addition.append_only_caches)
        _safe_update(extra_env, addition.extra_env)

    digest = await merge_digests(MergeDigests(digests))

    return ExtraSandboxContents(
        digest=digest,
        paths=tuple(paths),
        immutable_input_digests=FrozenDict(immutable_input_digests),
        append_only_caches=FrozenDict(append_only_caches),
        extra_env=FrozenDict(extra_env),
    )


#
# END THINGS THAT NEED A HOME
#


@rule
async def convert_fallible_adhoc_process_result(
    fallible_result: FallibleAdhocProcessResult,
) -> AdhocProcessResult:
    result = await fallible_to_exec_result_or_raise(
        **implicitly(
            {
                fallible_result.process_result: FallibleProcessResult,
                ProductDescription(fallible_result.description): ProductDescription,
            }
        )
    )
    return AdhocProcessResult(
        process_result=result, adjusted_digest=fallible_result.adjusted_digest
    )


async def _resolve_runnable_dependencies(
    bash: BashBinary, deps: tuple[str, ...] | None, owning: Address, origin: str
) -> tuple[Digest, RunnableDependencies | None]:
    if not deps:
        return EMPTY_DIGEST, None

    addresses = await resolve_unparsed_address_inputs(
        UnparsedAddressInputs(
            (dep for dep in deps),
            owning_address=owning,
            description_of_origin=origin,
        ),
        **implicitly(),
    )

    targets = await resolve_targets(**implicitly({addresses: Addresses}))

    fspt = await find_valid_field_sets(
        FieldSetsPerTargetRequest(RunFieldSet, targets), **implicitly()
    )

    for address, field_set in zip(addresses, fspt.collection):
        if not field_set:
            raise ValueError(
                dedent(
                    f"""\
                    Address `{address.spec}` was specified as a runnable dependency, but is not
                    runnable.
                    """
                )
            )

    runnables = await concurrently(
        Get(RunInSandboxRequest, RunFieldSet, field_set[0]) for field_set in fspt.collection
    )

    shims: list[FileContent] = []
    extras: list[ExtraSandboxContents] = []

    for address, runnable in zip(addresses, runnables):
        extras.append(
            ExtraSandboxContents(
                digest=runnable.digest,
                paths=(),
                immutable_input_digests=FrozenDict(runnable.immutable_input_digests or {}),
                append_only_caches=FrozenDict(runnable.append_only_caches or {}),
                extra_env=FrozenDict(),
            )
        )
        shims.append(
            FileContent(
                address.target_name,
                _runnable_dependency_shim(bash.path, runnable.args, runnable.extra_env),
                is_executable=True,
            )
        )

    merged_extras, shim_digest = await concurrently(
        merge_extra_sandbox_contents(MergeExtraSandboxContents(tuple(extras))),
        create_digest(CreateDigest(shims)),
    )

    shim_digest_path = f"_runnable_dependency_shims_{shim_digest.fingerprint}"
    immutable_input_digests = {shim_digest_path: shim_digest}
    _safe_update(immutable_input_digests, merged_extras.immutable_input_digests)

    return (
        merged_extras.digest,
        RunnableDependencies(
            shim_digest_path,
            FrozenDict(immutable_input_digests),
            merged_extras.append_only_caches,
            FrozenDict({"_PANTS_SHIM_ROOT": "{chroot}"}),
        ),
    )


@rule
async def resolve_execution_environment(
    request: ResolveExecutionDependenciesRequest,
    bash: BashBinary,
) -> ResolvedExecutionDependencies:
    target_address = request.address
    raw_execution_dependencies = request.execution_dependencies

    # Always include the execution dependencies that were specified
    if raw_execution_dependencies is not None:
        _descr = f"the `execution_dependencies` from the target {target_address}"
        execution_dependencies = await resolve_unparsed_address_inputs(
            UnparsedAddressInputs(
                raw_execution_dependencies,
                owning_address=target_address,
                description_of_origin=_descr,
            ),
            **implicitly(),
        )
    else:
        execution_dependencies = Addresses(())

    transitive = await transitive_targets(
        TransitiveTargetsRequest(execution_dependencies), **implicitly()
    )

    all_dependencies = (
        *(i for i in transitive.roots if i.address is not target_address),
        *transitive.dependencies,
    )

    sources, pkgs_per_target = await concurrently(
        determine_source_files(
            SourceFilesRequest(
                sources_fields=[tgt.get(SourcesField) for tgt in all_dependencies],
                for_sources_types=(SourcesField, FileSourceField),
                enable_codegen=True,
            )
        ),
        find_valid_field_sets(
            FieldSetsPerTargetRequest(PackageFieldSet, all_dependencies), **implicitly()
        ),
    )

    packages = await concurrently(
        environment_aware_package(EnvironmentAwarePackageRequest(field_set))
        for field_set in pkgs_per_target.field_sets
    )

    _descr = f"the `runnable_dependencies` from the target {target_address}"
    runnables_digest, runnable_dependencies = await _resolve_runnable_dependencies(
        bash, request.runnable_dependencies, target_address, _descr
    )

    dependencies_digest = await merge_digests(
        MergeDigests([sources.snapshot.digest, runnables_digest, *(pkg.digest for pkg in packages)])
    )

    return ResolvedExecutionDependencies(dependencies_digest, runnable_dependencies)


K = TypeVar("K")
V = TypeVar("V")


def _safe_update(d1: dict[K, V], d2: Mapping[K, V]) -> dict[K, V]:
    """Updates `d1` with the values from `d2`, raising an exception if a key exists in both
    dictionaries, but with a different value."""

    for k, v in d2.items():
        if k in d1 and d1[k] != v:
            raise ValueError(f"Key {k} was specified in both dictionaries with different values.")
        d1[k] = v
    return d1


def _runnable_dependency_shim(
    bash: str, args: Iterable[str], extra_env: Mapping[str, str]
) -> bytes:
    """The binary shim script to be placed in the output directory for the digest."""

    def _quote(s: str) -> str:
        quoted = shlex.quote(s)
        return quoted.replace("{chroot}", "'${_PANTS_SHIM_ROOT}'")

    binary = " ".join(_quote(arg) for arg in args)
    env_str = "\n".join(
        f"export {shlex.quote(key)}={_quote(value)}" for (key, value) in extra_env.items()
    )
    return dedent(
        f"""\
        #!{bash}
        {env_str}
        exec {binary} "$@"
        """
    ).encode()


@rule
async def create_tool_runner(
    request: ToolRunnerRequest,
) -> ToolRunner:
    runnable_address = await resolve_address(
        **implicitly(
            {
                AddressInput.parse(
                    request.runnable_address_str,
                    relative_to=request.target.address.spec_path,
                    description_of_origin=f"Runnable target for {request.target.address.spec_path}",
                ): AddressInput
            }
        )
    )

    addresses = Addresses((runnable_address,))
    addresses.expect_single()

    runnable_targets = await resolve_targets(**implicitly({addresses: Addresses}))

    run_field_sets, environment_name, execution_environment = await concurrently(
        find_valid_field_sets(
            FieldSetsPerTargetRequest(RunFieldSet, runnable_targets), **implicitly()
        ),
        resolve_environment_name(
            EnvironmentNameRequest.from_target(request.target), **implicitly()
        ),
        resolve_execution_environment(
            ResolveExecutionDependenciesRequest(
                address=request.target.address,
                execution_dependencies=request.execution_dependencies,
                runnable_dependencies=request.runnable_dependencies,
            ),
            **implicitly(),
        ),
    )

    run_field_set: RunFieldSet = run_field_sets.field_sets[0]

    # Must be run in target environment so that the binaries/envvars match the execution
    # environment when we actually run the process.
    run_request = await Get(
        RunInSandboxRequest, {environment_name: EnvironmentName, run_field_set: RunFieldSet}
    )

    dependencies_digest = execution_environment.digest
    runnable_dependencies = execution_environment.runnable_dependencies

    extra_env: dict[str, str] = dict(run_request.extra_env or {})
    extra_path = extra_env.pop("PATH", None)

    extra_sandbox_contents = []

    extra_sandbox_contents.append(
        ExtraSandboxContents(
            digest=EMPTY_DIGEST,
            paths=(extra_path,) if extra_path else (),
            immutable_input_digests=run_request.immutable_input_digests or FrozenDict(),
            append_only_caches=run_request.append_only_caches or FrozenDict(),
            extra_env=run_request.extra_env or FrozenDict(),
        )
    )

    if runnable_dependencies:
        extra_sandbox_contents.append(
            ExtraSandboxContents(
                digest=EMPTY_DIGEST,
                paths=(f"{{chroot}}/{runnable_dependencies.path_component}",),
                immutable_input_digests=runnable_dependencies.immutable_input_digests,
                append_only_caches=runnable_dependencies.append_only_caches,
                extra_env=runnable_dependencies.extra_env,
            )
        )

    merged_extras, main_digest = await concurrently(
        merge_extra_sandbox_contents(MergeExtraSandboxContents(tuple(extra_sandbox_contents))),
        merge_digests(MergeDigests((dependencies_digest, run_request.digest))),
    )

    extra_env = dict(merged_extras.extra_env)

    append_only_caches = {
        **merged_extras.append_only_caches,
        **(request.named_caches or {}),  # type: ignore[dict-item]
    }

    return ToolRunner(
        digest=main_digest,
        args=run_request.args + tuple(request.args),
        extra_env=FrozenDict(extra_env),
        extra_paths=merged_extras.paths,
        append_only_caches=FrozenDict(append_only_caches),
        immutable_input_digests=FrozenDict(merged_extras.immutable_input_digests),
    )


async def check_outputs(
    output_digest: Digest,
    output_files: Iterable[str],
    output_directories: Iterable[str],
    outputs_match_error_behavior: GlobMatchErrorBehavior,
    outputs_match_mode: GlobExpansionConjunction | None,
    address: Address,
) -> None:
    """Check an output digest from adhoc/shell backends to ensure that the outputs expected by the
    user do in fact exist."""

    if outputs_match_mode is None:
        return

    filtered_output_files_digests = await concurrently(
        digest_subset_to_digest(
            DigestSubset(
                output_digest,
                PathGlobs(
                    [output_file],
                    glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
                ),
            ),
        )
        for output_file in output_files
    )

    filtered_output_directory_digests = await concurrently(
        digest_subset_to_digest(
            DigestSubset(
                output_digest,
                PathGlobs(
                    [output_directory, os.path.join(output_directory, "**")],
                    glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
                ),
            ),
        )
        for output_directory in output_directories
    )

    filtered_output_files = tuple(zip(output_files, filtered_output_files_digests))
    filtered_output_directories = tuple(zip(output_directories, filtered_output_directory_digests))

    unused_output_files = tuple(
        f"{output_file} (from `output_files` field)"
        for output_file, digest in filtered_output_files
        if digest == EMPTY_DIGEST
    )
    unused_output_directories = tuple(
        f"{output_directory} (from `output_directories` field)"
        for output_directory, digest in filtered_output_directories
        if digest == EMPTY_DIGEST
    )

    def warn_or_raise(message: str, snapshot: Snapshot) -> None:
        unused_globs_str = ", ".join([*unused_output_files, *unused_output_directories])
        message = f"{message}\n\nThe following output globs were unused: {unused_globs_str}"

        if snapshot.dirs:
            message += f"\n\nDirectories in output ({len(snapshot.dirs)} total):"
            dirs = sorted(snapshot.dirs, key=lambda x: x.count(os.pathsep))
            if len(dirs) > 15:
                message += f" {', '.join(dirs[0:15])}, ... (trimmed for brevity)"
            else:
                message += f" {', '.join(dirs)}"

        if snapshot.files:
            message += f"\n\nFiles in output ({len(snapshot.files)} total):"
            files = sorted(snapshot.files, key=lambda x: x.count(os.pathsep))
            if len(files) > 15:
                message += f" {', '.join(files[0:15])}, ... (trimmed for brevity)"
            else:
                message += f" {', '.join(files)}"

        if outputs_match_error_behavior == GlobMatchErrorBehavior.error:
            raise ValueError(message)
        else:
            logger.warning(message)

    if outputs_match_mode == GlobExpansionConjunction.all_match:
        if not unused_output_files and not unused_output_directories:
            return

        snapshot, wrapped_tgt = await concurrently(
            digest_to_snapshot(output_digest),
            resolve_target(
                WrappedTargetRequest(address, "adhoc_process_support rule"), **implicitly()
            ),
        )
        warn_or_raise(
            f"The `{wrapped_tgt.target.alias}` target at `{address}` is configured with `outputs_match_mode` set to `all` "
            "which requires all output globs to actually match an output.",
            snapshot,
        )

    # Otherwise it is `GlobExpansionConjunction.any_match` which means only at least one glob must match.
    total_count = len(filtered_output_files) + len(filtered_output_directories)
    unused_count = len(unused_output_files) + len(unused_output_directories)
    if total_count == 0 or unused_count < total_count:
        return
    snapshot, wrapped_tgt = await concurrently(
        digest_to_snapshot(output_digest),
        resolve_target(WrappedTargetRequest(address, "adhoc_process_support rule"), **implicitly()),
    )
    warn_or_raise(
        f"The `{wrapped_tgt.target.alias}` target at `{address}` is configured with `outputs_match_mode` set to `any` "
        "which requires at least one output glob to actually match an output.",
        snapshot,
    )


@rule
async def run_prepared_adhoc_process(
    prepared_request: PreparedAdhocProcessRequest,
) -> FallibleAdhocProcessResult:
    request = prepared_request.original_request

    result = await execute_process(prepared_request.process, **implicitly())

    log_on_errors = request.log_on_process_errors or FrozenDict()
    error_to_log = log_on_errors.get(result.exit_code, None)
    if error_to_log:
        logger.error(error_to_log)

    if request.log_output:
        if result.stdout:
            logger.info(result.stdout.decode())
        if result.stderr:
            logger.warning(result.stderr.decode())

    working_directory = parse_relative_directory(request.working_directory, request.address)

    root_output_directory: str | None = None
    if request.use_working_directory_as_base_for_output_captures:
        root_output_directory = parse_relative_directory(
            request.root_output_directory, working_directory
        )

    extras = (
        (request.capture_stdout_file, result.stdout),
        (request.capture_stderr_file, result.stderr),
    )
    extra_contents = {i: j for i, j in extras if i}

    # Check the outputs (if configured) to ensure any required glob matches in fact occurred.
    output_digest = result.output_digest
    output_files: list[str] = list(request.output_files)
    output_directories: list[str] = list(request.output_directories)
    if request.use_working_directory_as_base_for_output_captures:
        output_files = [
            os.path.normpath(os.path.join(working_directory, of)) for of in output_files
        ]
        output_directories = [
            os.path.normpath(os.path.join(working_directory, od)) for od in output_directories
        ]
    await check_outputs(
        output_digest=output_digest,
        output_files=output_files,
        output_directories=output_directories,
        outputs_match_error_behavior=request.outputs_match_error_behavior,
        outputs_match_mode=request.outputs_match_conjunction,
        address=request.address,
    )

    if extra_contents:
        if request.use_working_directory_as_base_for_output_captures:
            extra_digest = await create_digest(
                CreateDigest(
                    FileContent(_parse_relative_file(name, working_directory), content)
                    for name, content in extra_contents.items()
                )
            )
        else:
            extra_digest = await create_digest(
                CreateDigest(FileContent(name, content) for name, content in extra_contents.items())
            )

        output_digest = await merge_digests(MergeDigests((output_digest, extra_digest)))

    adjusted: Digest = output_digest
    if root_output_directory is not None:
        adjusted = await remove_prefix(RemovePrefix(output_digest, root_output_directory))

    return FallibleAdhocProcessResult(
        process_result=result,
        adjusted_digest=adjusted,
        description=request.description,
    )


# Compute a stable bytes value for a `PathMetadata` consisting of the values to be hashed.
# Access time is not included to avoid having mere access to a file invalidating an execution.
def _path_metadata_to_bytes(m: PathMetadata | None) -> bytes:
    if m is None:
        return b""

    def dt_fmt(dt: datetime | None) -> str | None:
        if dt is not None:
            return dt.isoformat()
        return None

    d = {
        "path": m.path,
        "kind": str(m.kind),
        "length": m.length,
        "is_executable": m.is_executable,
        "unix_mode": m.unix_mode,
        "created": dt_fmt(m.created),
        "modified": dt_fmt(m.modified),
        "symlink_target": m.symlink_target,
    }

    return json.dumps(d, sort_keys=True).encode()


async def compute_workspace_invalidation_hash(path_globs: PathGlobs) -> str:
    raw_paths = await path_globs_to_paths(path_globs)
    paths = sorted([*raw_paths.files, *raw_paths.dirs])
    metadata_results = await concurrently(
        path_metadata_request(PathMetadataRequest(path)) for path in paths
    )

    # Compute a stable hash of all of the metadatas since the hash value should be stable
    # when used outside the process (for example, in the cache). (The `__hash__` dunder method
    # computes an unstable hash which can and does vary across different process invocations.)
    #
    # While it could be more of an intellectual correctness point than a necessity, It does matter,
    # however, for a single user to see the same behavior across process invocations if pantsd restarts.
    #
    # Note: This could probbaly use a non-cryptographic hash (e.g., Murmur), but that would require
    # a third party dependency.
    h = hashlib.sha256()
    for mr in metadata_results:
        h.update(_path_metadata_to_bytes(mr.metadata))
    return h.hexdigest()


@rule
async def prepare_adhoc_process(
    request: AdhocProcessRequest,
    bash: BashBinary,
) -> PreparedAdhocProcessRequest:
    description = request.description
    address = request.address
    working_directory = parse_relative_directory(request.working_directory or "", address)
    argv = request.argv
    timeout: int | None = request.timeout
    output_files = request.output_files
    output_directories = request.output_directories
    append_only_caches = request.append_only_caches or FrozenDict()
    immutable_input_digests = request.immutable_input_digests or FrozenDict()

    command_env: dict[str, str] = dict(request.env_vars)

    # Compute the hash for any workspace invalidation sources and put the hash into the environment as a dummy variable
    # so that the process produced by this rule will be invalidated if any of the referenced files change.
    if request.workspace_invalidation_globs is not None:
        workspace_invalidation_hash = await compute_workspace_invalidation_hash(
            request.workspace_invalidation_globs
        )
        command_env["__PANTS_WORKSPACE_INVALIDATION_SOURCES_HASH"] = workspace_invalidation_hash

    input_snapshot = await digest_to_snapshot(request.input_digest)

    if not working_directory or working_directory in input_snapshot.dirs:
        # Needed to ensure that underlying filesystem does not change during run
        work_dir = EMPTY_DIGEST
    else:
        work_dir = await create_digest(CreateDigest([Directory(working_directory)]))

    input_digest = await merge_digests(MergeDigests([request.input_digest, work_dir]))

    process = Process(
        argv=argv,
        description=f"Running {description}",
        env=command_env,
        input_digest=input_digest,
        output_directories=output_directories,
        output_files=output_files,
        timeout_seconds=timeout,
        working_directory=working_directory,
        append_only_caches=append_only_caches,
        immutable_input_digests=immutable_input_digests,
        cache_scope=request.cache_scope or ProcessCacheScope.SUCCESSFUL,
    )

    if request.use_working_directory_as_base_for_output_captures:
        process = _output_at_build_root(process, bash)

    return PreparedAdhocProcessRequest(
        original_request=request,
        process=process,
    )


class PathEnvModifyMode(Enum):
    """How the PATH environment variable should be augmented with extra path elements."""

    PREPEND = "prepend"
    APPEND = "append"
    OFF = "off"


async def prepare_env_vars(
    existing_env_vars: Mapping[str, str],
    env_vars_templates: tuple[str, ...],
    *,
    extra_paths: tuple[str, ...] = (),
    path_env_modify_mode: PathEnvModifyMode = PathEnvModifyMode.PREPEND,
    description_of_origin: str,
) -> FrozenDict[str, str]:
    env_vars: dict[str, str] = dict(existing_env_vars)

    to_fetch: set[str] = set()
    duplicate_keys: set[str] = set()
    for env_var in env_vars_templates:
        parts = env_var.split("=", 1)
        if parts[0] in env_vars:
            duplicate_keys.add(parts[0])

        if len(parts) == 2:
            env_vars[parts[0]] = parts[1]
        else:
            to_fetch.add(parts[0])

    if duplicate_keys:
        dups_as_str = ", ".join(sorted(duplicate_keys))
        raise ValueError(
            f"The following environment variables referenced in {description_of_origin} are defined multiple times: {dups_as_str}"
        )

    if to_fetch:
        fetched_env_vars = await environment_vars_subset(
            EnvironmentVarsRequest(tuple(sorted(to_fetch))), **implicitly()
        )
        env_vars.update(fetched_env_vars)

    def path_env_join(left: str | None, right: str | None) -> str | None:
        if not left and not right:
            return None
        if left and not right:
            return left
        if not left and right:
            return right
        return f"{left}:{right}"

    if extra_paths:
        existing_path_env = env_vars.get("PATH")
        extra_paths_as_str = ":".join(extra_paths)

        new_path_env: str | None = None
        if path_env_modify_mode == PathEnvModifyMode.PREPEND:
            new_path_env = path_env_join(extra_paths_as_str, existing_path_env)
        elif path_env_modify_mode == PathEnvModifyMode.APPEND:
            new_path_env = path_env_join(existing_path_env, extra_paths_as_str)

        if new_path_env:
            env_vars["PATH"] = new_path_env

    return FrozenDict(env_vars)


def _output_at_build_root(process: Process, bash: BashBinary) -> Process:
    working_directory = process.working_directory or ""

    output_directories = process.output_directories
    output_files = process.output_files
    if working_directory:
        output_directories = tuple(os.path.join(working_directory, d) for d in output_directories)
        output_files = tuple(os.path.join(working_directory, d) for d in output_files)

    cd = f"cd {shlex.quote(working_directory)} && " if working_directory else ""
    shlexed_argv = " ".join(shlex.quote(arg) for arg in process.argv)
    new_argv = (bash.path, "-c", f"{cd}{shlexed_argv}")

    return dataclasses.replace(
        process,
        argv=new_argv,
        working_directory=None,
        output_directories=output_directories,
        output_files=output_files,
    )


def parse_relative_directory(workdir_in: str, relative_to: Address | str) -> str:
    """Convert the `workdir` field into something that can be understood by `Process`."""

    if isinstance(relative_to, Address):
        reldir = relative_to.spec_path
    else:
        reldir = relative_to

    if workdir_in == ".":
        return reldir
    elif workdir_in.startswith("./"):
        return os.path.join(reldir, workdir_in[2:])
    elif workdir_in.startswith("/"):
        return workdir_in[1:]
    else:
        return workdir_in


def _parse_relative_file(file_in: str, relative_to: str) -> str:
    """Convert the `capture_std..._file` fields into something that can be understood by
    `Process`."""

    if file_in.startswith("/"):
        return file_in[1:]

    return os.path.join(relative_to, file_in)


def rules():
    return (
        *collect_rules(),
        *process.rules(),
    )
