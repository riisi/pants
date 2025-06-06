# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.core.environments.target_types import EnvironmentField
from pants.core.util_rules.adhoc_process_support import PathEnvModifyMode
from pants.engine.env_vars import EXTRA_ENV_VARS_USAGE_HELP
from pants.engine.fs import GlobExpansionConjunction
from pants.engine.process import ProcessCacheScope
from pants.engine.target import (
    COMMON_TARGET_FIELDS,
    BoolField,
    Dependencies,
    DictStringToStringField,
    IntField,
    MultipleSourcesField,
    SpecialCasedDependencies,
    StringField,
    StringSequenceField,
    Target,
    ValidNumbers,
)
from pants.util.docutil import bin_name
from pants.util.strutil import help_text


class AdhocToolDependenciesField(Dependencies):
    pass


class AdhocToolRunnableField(StringField):
    alias: ClassVar[str] = "runnable"
    required = True
    help = help_text(
        lambda: f"""
        Address to a target that can be invoked by the `run` goal (and does not set
        `run_in_sandbox_behavior=NOT_SUPPORTED`). This will be executed along with any arguments
        specified by `{AdhocToolArgumentsField.alias}`, in a sandbox with that target's transitive
        dependencies, along with the transitive dependencies specified by
        `{AdhocToolExecutionDependenciesField.alias}`.
        """
    )


class AdhocToolOutputFilesField(StringSequenceField):
    alias: ClassVar[str] = "output_files"
    required = False
    default = ()
    help = help_text(
        lambda: f"""
        Specify the output files to capture, relative to the value of
        `{AdhocToolWorkdirField.alias}`.

        For directories, use `{AdhocToolOutputDirectoriesField.alias}`. At least one of
        `{AdhocToolOutputFilesField.alias}` and `{AdhocToolOutputDirectoriesField.alias}` must be
        specified.

        Relative paths (including `..`) may be used, as long as the path does not ascend further
        than the build root.
        """
    )


class AdhocToolOutputDirectoriesField(StringSequenceField):
    alias: ClassVar[str] = "output_directories"
    required = False
    default = ()
    help = help_text(
        lambda: f"""
        Specify full directories (including recursive descendants) of output to capture, relative
        to the value of `{AdhocToolWorkdirField.alias}`.

        For individual files, use `{AdhocToolOutputFilesField.alias}`. At least one of
        `{AdhocToolOutputFilesField.alias}` and `{AdhocToolOutputDirectoriesField.alias}` must be
        specified.

        Relative paths (including `..`) may be used, as long as the path does not ascend further
        than the build root.
        """
    )


class AdhocToolOutputDependenciesField(AdhocToolDependenciesField):
    supports_transitive_excludes = True
    alias: ClassVar[str] = "output_dependencies"

    help = help_text(
        lambda: f"""
        Any dependencies that need to be present (as transitive dependencies) whenever the outputs
        of this target are consumed (including as dependencies).

        See also `{AdhocToolExecutionDependenciesField.alias}` and
        `{AdhocToolRunnableDependenciesField.alias}`.
        """
    )


class AdhocToolExecutionDependenciesField(SpecialCasedDependencies):
    alias: ClassVar[str] = "execution_dependencies"
    required = False
    default = None

    help = help_text(
        lambda: f"""
        The execution dependencies for this command.

        Dependencies specified here are those required to make the command complete successfully
        (e.g. file inputs, packages compiled from other targets, etc), but NOT required to make
        the outputs of the command useful. Dependencies that are required to use the outputs
        produced by this command should be specified using the
        `{AdhocToolOutputDependenciesField.alias}` field.

        If this field is specified, dependencies from `{AdhocToolOutputDependenciesField.alias}`
        will not be added to the execution sandbox.

        See also `{AdhocToolOutputDependenciesField.alias}` and
        `{AdhocToolRunnableDependenciesField.alias}`.
        """
    )


class AdhocToolRunnableDependenciesField(SpecialCasedDependencies):
    alias: ClassVar[str] = "runnable_dependencies"
    required = False
    default = None

    help = help_text(
        lambda: f"""
        The runnable dependencies for this command.

        Dependencies specified here are those required to exist on the `PATH` to make the command
        complete successfully (interpreters specified in a `#!` command, etc). Note that these
        dependencies will be made available on the `PATH` with the name of the target.

        See also `{AdhocToolOutputDependenciesField.alias}` and
        `{AdhocToolExecutionDependenciesField.alias}`.
        """
    )


class AdhocToolSourcesField(MultipleSourcesField):
    # We solely register this field for codegen to work.
    alias: ClassVar[str] = "_sources"
    uses_source_roots = False
    expected_num_files = 0


class AdhocToolArgumentsField(StringSequenceField):
    alias: ClassVar[str] = "args"
    default = ()
    help = help_text(
        lambda: f"Extra arguments to pass into the `{AdhocToolRunnableField.alias}` field."
    )


class AdhocToolStdoutFilenameField(StringField):
    alias: ClassVar[str] = "stdout"
    default = None
    help = help_text(
        lambda: f"""
        A filename to capture the contents of `stdout` to. Relative paths are
        relative to the value of `{AdhocToolWorkdirField.alias}`, absolute paths
        start at the build root.
        """
    )


class AdhocToolStderrFilenameField(StringField):
    alias: ClassVar[str] = "stderr"
    default = None
    help = help_text(
        lambda: f"""
        A filename to capture the contents of `stderr` to. Relative paths are
        relative to the value of `{AdhocToolWorkdirField.alias}`, absolute paths
        start at the build root.
        """
    )


class AdhocToolTimeoutField(IntField):
    alias: ClassVar[str] = "timeout"
    default = 30
    help = "Command execution timeout (in seconds)."
    valid_numbers = ValidNumbers.positive_only


class AdhocToolExtraEnvVarsField(StringSequenceField):
    alias: ClassVar[str] = "extra_env_vars"
    help = help_text(
        f"""
        Additional environment variables to provide to the process.

        {EXTRA_ENV_VARS_USAGE_HELP}
        """
    )


class AdhocToolLogOutputField(BoolField):
    alias: ClassVar[str] = "log_output"
    default = False
    help = "Set to true if you want the output logged to the console."


class AdhocToolWorkdirField(StringField):
    alias: ClassVar[str] = "workdir"
    default = "."
    help = help_text(
        """
        Sets the working directory for the process.

        Values are relative to the build root, except in the following cases:

        * `.` specifies the location of the `BUILD` file.
        * Values beginning with `./` are relative to the location of the `BUILD` file.
        * `/` or the empty string specifies the build root.
        * Values beginning with `/` are also relative to the build root.
        """
    )


class AdhocToolNamedCachesField(DictStringToStringField):
    alias = "experimental_named_caches"
    help = help_text(
        """
        Named caches to construct for the execution.
        See https://www.pantsbuild.org/docs/reference-global#named_caches_dir.

        The keys of the mapping are the directory name to be created in the named caches dir.
        The values are the name of the symlink (relative to the sandbox root) in the sandbox which
        points to the subdirectory in the named caches dir

        NOTE: The named caches MUST be handled with great care. Processes accessing the named caches
        can be run in parallel, and can be cancelled at any point in their execution (and
        potentially restarted). That means that _every_ operation modifying the contents of the cache
        MUST be concurrency and cancellation safe.
        """
    )


class AdhocToolOutputRootDirField(StringField):
    alias: ClassVar[str] = "root_output_directory"
    default = "/"
    help = help_text(
        """
        Adjusts the location of files output by this target, when consumed as a dependency.

        Values are relative to the build root, except in the following cases:

          * `.` specifies the location of the `BUILD` file.
          * Values beginning with `./` are relative to the location of the `BUILD` file.
          * `/` or the empty string specifies the build root.
          * Values beginning with `/` are also relative to the build root.
        """
    )


class AdhocToolWorkspaceInvalidationSourcesField(StringSequenceField):
    alias: ClassVar[str] = "workspace_invalidation_sources"
    help = help_text(
        """
        Path globs for source files on which this target depends and for which any changes should cause
        this target's process to be re-executed. Unlike ordinary dependencies, the files referenced by
        `workspace_invalidation_sources` globs are not materialized into any execution sandbox
        and are referenced solely for cache invalidation purposes.

        Note: This field is intended to work with the in-workspace execution environment configured by
        the `workspace_environment` target type. It should only be used when the configured
        environment for a target is a `workspace_environment`.

        Implementation: Pants computes a digest of all of the files referenced by the provided globs
        and injects that digest into the process as an environment variable. Since environment variables
        are part of the cache key for a process's execution, any changes to the referenced files will
        change the digest and thus force re-exection of the process.
        """
    )


class AdhocToolPathEnvModifyModeField(StringField):
    alias = "path_env_modify"
    default = PathEnvModifyMode.PREPEND.value
    help = help_text(
        """
        When executing the command of an `adhoc_tool`, `shell_command`, or `test_shell_command` target,
        Pants may augment the `PATH` environment variable with the location of any binary shims created for
        `tools` and any runnable dependencies.

        Modification of the `PATH` environment variable can be configured as follows:

        - `prepend`: Prepend the extra path components to any existing `PATH` value.

        - `append`: Append the extra path componenets to any existing `PATH` value.

        - `off`: Do not modify the existing `PATH` value.
        """
    )
    valid_choices = PathEnvModifyMode

    @property
    def enum_value(self) -> PathEnvModifyMode:
        return PathEnvModifyMode(self.value)


class OutputsMatchMode(Enum):
    """The different types of output checks for adhoc_tool / shell_command targets."""

    ALL = "all"
    ALL_WARN = "all_warn"
    AT_LEAST_ONE = "at_least_one"
    AT_LEAST_ONE_WARN = "at_least_one_warn"
    ALLOW_EMPTY = "allow_empty"

    @property
    def glob_match_error_behavior(self) -> GlobMatchErrorBehavior:
        if self in (OutputsMatchMode.ALL, OutputsMatchMode.AT_LEAST_ONE):
            return GlobMatchErrorBehavior.error
        else:
            return GlobMatchErrorBehavior.warn

    @property
    def glob_expansion_conjunction(self) -> GlobExpansionConjunction | None:
        if self in (OutputsMatchMode.ALL, OutputsMatchMode.ALL_WARN):
            return GlobExpansionConjunction.all_match
        elif self in (OutputsMatchMode.AT_LEAST_ONE, OutputsMatchMode.AT_LEAST_ONE_WARN):
            return GlobExpansionConjunction.any_match
        else:
            return None


class AdhocToolOutputsMatchMode(StringField):
    alias = "outputs_match_mode"
    default = OutputsMatchMode.ALL_WARN.value
    help = help_text(
        """
        Configure whether all, or some, of the values in the `output_files` and `output_directories` fields must actually match
        the outputs generated by the invoked process. These values are called "globs". Outputs may be matched by more than one
        glob.

        Valid values are:

        - `all_warn`: Log a warning if any glob fails to match an output. (In other words, all globs must match to avoid a
        warning.) This is the default value.

        - `all`: Ensure all globs match an output or else raise an error.

        - `at_least_one_warn`: Log a warning if none of the globs match an output.

        - `at_least_one`: Ensure at least one glob matches an output or else raise an error.

        - `allow_empty`: Allow empty digests (which means nothing was captured). This disables checking that globs match outputs.
        """
    )
    valid_choices = OutputsMatchMode

    @property
    def enum_value(self) -> OutputsMatchMode:
        return OutputsMatchMode(self.value)


class AdhocToolCacheScopeField(StringField):
    alias = "cache_scope"
    default = "from_environment"
    help = help_text(
        f"""
        Set the "cache scope" of the executed process to provided value. The cache scope determines for how long
        Pants will cache the result of the process execution (assuming no changes to files or dependencies
        invalidate the result in the meantime).

        The valid values are:

        - `from_environment`: Use the default cache scope for the applicable environment in which the process will execute.
        This is `success` for all environments except for `experimental_workspace_environment`, in which case `session`
        cache scope will be used.

        - `success`: Cache successful executions of the process.

        - `success_per_pantsd_restart`: Cache successful executions of the process for the life of the
         applicable pantsd process.

        - `session`: Only cache the result for a single Pants session. This will usually be a single invocation of the
        `{bin_name()}` tool.
        """
    )
    valid_choices = ("from_environment", "success", "success_per_pantsd_restart", "session")

    @property
    def enum_value(self) -> ProcessCacheScope | None:
        value = self.value
        if value == "success":
            return ProcessCacheScope.SUCCESSFUL
        elif value == "success_per_pantsd_restart":
            return ProcessCacheScope.PER_RESTART_SUCCESSFUL
        elif value == "session":
            return ProcessCacheScope.PER_SESSION
        else:
            # Default case `from_environment`
            return None


class AdhocToolTarget(Target):
    alias: ClassVar[str] = "adhoc_tool"
    core_fields = (
        *COMMON_TARGET_FIELDS,
        AdhocToolRunnableField,
        AdhocToolArgumentsField,
        AdhocToolExecutionDependenciesField,
        AdhocToolOutputDependenciesField,
        AdhocToolRunnableDependenciesField,
        AdhocToolLogOutputField,
        AdhocToolOutputFilesField,
        AdhocToolOutputDirectoriesField,
        AdhocToolSourcesField,
        AdhocToolTimeoutField,
        AdhocToolExtraEnvVarsField,
        AdhocToolWorkdirField,
        AdhocToolOutputRootDirField,
        AdhocToolStdoutFilenameField,
        AdhocToolStderrFilenameField,
        AdhocToolWorkspaceInvalidationSourcesField,
        AdhocToolPathEnvModifyModeField,
        AdhocToolOutputsMatchMode,
        AdhocToolCacheScopeField,
        EnvironmentField,
    )
    help = help_text(
        lambda: f"""
        Execute any runnable target for its side effects.

        Example BUILD file:

            {AdhocToolTarget.alias}(
                {AdhocToolRunnableField.alias}=":python_source",
                {AdhocToolArgumentsField.alias}=[""],
                {AdhocToolExecutionDependenciesField.alias}=[":scripts"],
                {AdhocToolOutputDirectoriesField.alias}=["results/"],
                {AdhocToolOutputFilesField.alias}=["logs/my-script.log"],
            )

            shell_sources(name="scripts")
        """
    )


# ---
# `system_binary` target
# ---


class SystemBinaryNameField(StringField):
    alias: ClassVar[str] = "binary_name"
    required = True
    help = "The name of the binary to find."


class SystemBinaryExtraSearchPathsField(StringSequenceField):
    alias: ClassVar[str] = "extra_search_paths"
    default = ()
    help = help_text(
        """
        Extra search paths to look for the binary. These take priority over Pants' default
        search paths.
        """
    )


class SystemBinaryFingerprintPattern(StringField):
    alias: ClassVar[str] = "fingerprint"
    required = False
    default = None
    help = help_text(
        """
        A regular expression which will be used to match the fingerprint outputs from
        candidate binaries found during the search process.
        """
    )


class SystemBinaryFingerprintArgsField(StringSequenceField):
    alias: ClassVar[str] = "fingerprint_args"
    default = ()
    help = help_text(
        "Specifies arguments that will be used to run the binary during the search process."
    )


class SystemBinaryFingerprintDependenciesField(AdhocToolRunnableDependenciesField):
    alias: ClassVar[str] = "fingerprint_dependencies"
    help = help_text(
        """
        Specifies any runnable dependencies that need to be available on the `PATH` when the binary
        is run, so that the search process may complete successfully. The name of the target must
        be the name of the runnable dependency that is called by this binary.
        """
    )


class SystemBinaryLogFingerprintingErrorsField(BoolField):
    alias = "log_fingerprinting_errors"
    default = True
    help = help_text(
        """
        If True, then any errors encountered while fingerprinting candidate binaries will be logged as a warning.
        """
    )


class SystemBinaryTarget(Target):
    alias: ClassVar[str] = "system_binary"
    core_fields = (
        *COMMON_TARGET_FIELDS,
        SystemBinaryNameField,
        SystemBinaryExtraSearchPathsField,
        SystemBinaryFingerprintPattern,
        SystemBinaryFingerprintArgsField,
        SystemBinaryFingerprintDependenciesField,
        SystemBinaryLogFingerprintingErrorsField,
    )
    help = help_text(
        lambda: f"""
        A system binary that can be run with `pants run` or consumed by `{AdhocToolTarget.alias}`.

        Pants will search for binaries with name `{SystemBinaryNameField.alias}` in the search
        paths provided, as well as default search paths. If
        `{SystemBinaryFingerprintPattern.alias}` is specified, each binary that is located will be
        executed with the arguments from `{SystemBinaryFingerprintArgsField.alias}`. Any binaries
        whose output does not match the pattern will be excluded.

        The first non-excluded binary will be the one that is resolved.
        """
    )
