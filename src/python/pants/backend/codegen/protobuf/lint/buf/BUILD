# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_sources()
python_tests(
    name="tests",
    overrides={
        "lint_rules_integration_test.py": {
            "tags": ["platform_specific_behavior"],
        }
    },
)
