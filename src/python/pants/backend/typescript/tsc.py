# Copyright 2025 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""
Subsystem for the tsc tool, used for TypeScript typechecking.
"""
from pants.backend.javascript.subsystems.nodejs_tool import NodeJSToolBase

class TscTool(NodeJSToolBase):
    options_scope = "tsc"
    name = "tsc"
    default_version = "typescript@5.4.5"  # Default, can be overridden per resolve
