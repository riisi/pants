// Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::javascript::import_pattern::{Import, Pattern, StarMatch, imports_from_patterns};
use crate::javascript::{ImportCollector, JavascriptImportInfo, get_dependencies};
use javascript_inference_metadata::ImportPattern;
use protos::gen::pants::cache::{JavascriptInferenceMetadata, javascript_inference_metadata};

fn assert_imports(code: &str, imports: &[&str]) {
    let mut collector = ImportCollector::new(code);
    collector.collect();
    assert_eq!(
        HashSet::from_iter(imports.iter().map(|s| s.to_string())),
        collector.imports.into_iter().collect::<HashSet<_>>()
    );
}

fn given_metadata(
    root: &str,
    pattern_replacements: HashMap<String, Vec<String>>,
    config_root: Option<&str>,
    path_patterns: HashMap<String, Vec<String>>,
) -> JavascriptInferenceMetadata {
    let import_patterns: Vec<ImportPattern> = pattern_replacements
        .iter()
        .map(|(key, value)| ImportPattern {
            pattern: key.clone(),
            replacements: value.clone(),
        })
        .collect();
    let paths = path_patterns
        .iter()
        .map(|(key, value)| ImportPattern {
            pattern: key.clone(),
            replacements: value.clone(),
        })
        .collect();
    JavascriptInferenceMetadata {
        package_root: root.to_string(),
        import_patterns,
        config_root: config_root.map(str::to_string),
        paths,
    }
}

fn assert_dependency_imports<'a>(
    file_path: &str,
    code: &str,
    file_imports: impl IntoIterator<Item = &'a str>,
    package_imports: impl IntoIterator<Item = &'a str>,
    metadata: JavascriptInferenceMetadata,
) {
    let result = get_dependencies(code, PathBuf::from(file_path), metadata).unwrap();
    assert_eq!(
        HashSet::from_iter(file_imports.into_iter().map(|s| s.to_string())),
        result
            .imports
            .values()
            .flat_map(|val| val.file_imports.clone())
            .collect::<HashSet<String>>(),
    );
    assert_eq!(
        HashSet::from_iter(package_imports.into_iter().map(|s| s.to_string())),
        result
            .imports
            .into_values()
            .flat_map(|val| val.package_imports)
            .collect::<HashSet<String>>(),
    );
}

fn assert_dependency_candidates<'a>(
    file_path: &str,
    code: &str,
    expected: impl IntoIterator<Item = (&'a str, JavascriptImportInfo)>,
    metadata: JavascriptInferenceMetadata,
) {
    let result = get_dependencies(code, PathBuf::from(file_path), metadata).unwrap();

    assert_eq!(
        HashMap::from_iter(
            expected
                .into_iter()
                .map(|(import, candidates)| (import.to_string(), candidates))
        ),
        result.imports
    )
}

#[test]
fn simple_imports() {
    assert_imports("import a from 'a'", &["a"]);
    assert_imports("import('c')", &["c"]);
    assert_imports("require('d')", &["d"]);
    assert_imports("import('e');", &["e"]);
    assert_imports("require('f');", &["f"]);
    assert_imports("const g = import('g');", &["g"]);
    assert_imports("const h = require('h');", &["h"]);
}

#[test]
fn type_imports() {
    assert_imports("import type a from 'a'", &["a"]);
}

#[test]
fn await_import() {
    assert_imports("const i = await import('i');", &["i"]);
}

#[test]
fn ignore_imports() {
    assert_imports("import a from 'b'; // pants: no-infer-dep", &[]);
    assert_imports("import a from 'c' // pants: no-infer-dep", &[]);
    assert_imports("import('e') // pants: no-infer-dep", &[]);
    assert_imports("require('f') // pants: no-infer-dep", &[]);
    assert_imports("import('e'); // pants: no-infer-dep", &[]);
    assert_imports("require('f'); // pants: no-infer-dep", &[]);

    // multi-line
    assert_imports(
        "import {
            a
        } from 'ignored'; // pants: no-infer-dep",
        &[],
    );
    // NB. the inference (and thus ignoring) is driven off the 'from'.
    assert_imports(
        "
        import { // pants: no-infer-dep
            a
        } from 'b';
        import {
            c  // pants: no-infer-dep
        } from 'd';",
        &["b", "d"],
    );

    assert_imports(
        "require(
            'ignored'
        ) // pants: no-infer-dep",
        &[],
    );
    // as above, driven off the end of the require()
    assert_imports(
        "require( // pants: no-infer-dep
            'a'
        );
        require(
            'b' // pants: no-infer-dep
        )",
        &["a", "b"],
    );

    assert_imports(
        "import(
            'ignored'
        ) // pants: no-infer-dep",
        &[],
    );
    // as above, driven off the end of the import()
    assert_imports(
        "import( // pants: no-infer-dep
            'a'
        );
        import(
            'b' // pants: no-infer-dep
        )",
        &["a", "b"],
    );
}

#[test]
fn simple_exports() {
    // https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Statements/export
    assert_imports(r#"export * from "module-name";"#, &["module-name"]);
    assert_imports(r#"export * as name1 from "module-name";"#, &["module-name"]);
    assert_imports(
        r#"export { name1, /* …, */ nameN } from "module-name";"#,
        &["module-name"],
    );
    assert_imports(
        r#"export { import1 as name1, import2 as name2, /* …, */ nameN } from "module-name";"#,
        &["module-name"],
    );
    assert_imports(
        r#"export { default, /* …, */ } from "module-name";"#,
        &["module-name"],
    );
    assert_imports(
        r#"export { default as name1 } from "module-name";"#,
        &["module-name"],
    );
    // just confirm a relative path is preserved
    assert_imports("export * from './b/c'", &["./b/c"]);
}

#[test]
fn type_exports() {
    assert_imports(
        r#"export type { Stuff } from "module-name";"#,
        &["module-name"],
    );
}

#[test]
fn export_without_from() {
    // https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Statements/export
    assert_imports(
        "
// Exporting declarations
export let name1, name2/*, … */; // also var
export const name1 = 1, name2 = 2/*, … */; // also var, let
export function functionName() { /* … */ }
export class ClassName { /* … */ }
export function* generatorFunctionName() { /* … */ }
export const { name1, name2: bar } = o;
export const [ name1, name2 ] = array;

// Export list
export { name1, /* …, */ nameN };
export { variable1 as name1, variable2 as name2, /* …, */ nameN };
export { variable1 as \"string name\" };
export { name1 as default /*, … */ };

// Default exports
export default expression;
export default function functionName() { /* … */ }
export default class ClassName { /* … */ }
export default function* generatorFunctionName() { /* … */ }
export default function () { /* … */ }
export default class { /* … */ }
export default function* () { /* … */ }
",
        &[],
    );
}

#[test]
fn ignore_exports() {
    assert_imports("export * from 'a'; // pants: no-infer-dep", &[]);
    assert_imports("export * as x from './b' // pants: no-infer-dep", &[]);
    assert_imports("export { y } from \"../c\"  // pants: no-infer-dep", &[]);

    // multi-line
    assert_imports(
        "export {
            a
        } from 'ignored'; // pants: no-infer-dep",
        &[],
    );
    // NB. the inference is driven off the 'from'.
    assert_imports(
        "export { // pants: no-infer-dep
            a
        } from 'b';
        export {
            c // pants: no-infer-dep
        } from 'd';",
        &["b", "d"],
    );
}

#[test]
fn still_parses_from_syntax_error() {
    assert_imports("import a from '.'; x=", &["."]);
    assert_imports("export {some nonsense} from '.'", &["."]);
}

#[test]
fn non_string_literals() {
    assert_imports(
        r"
  const a = 5;
  require(a)
  ",
        &[],
    );
}

#[test]
fn constructor_is_not_import() {
    assert_imports(
        r"
  new require('a')
  ",
        &[],
    );
}

#[test]
fn dynamic_scope() {
    assert_imports(
        r"
    await import('some.wasm')
  ",
        &["some.wasm"],
    );
}

#[test]
fn adds_dir_to_file_imports() -> Result<(), Box<dyn std::error::Error>> {
    let result = get_dependencies(
        "import a from './file.js'",
        Path::new("dir/index.js").to_path_buf(),
        Default::default(),
    )?;
    assert_eq!(
        result
            .imports
            .into_values()
            .flat_map(|info| info.file_imports)
            .collect::<HashSet<String>>(),
        HashSet::from_iter(["dir/file.js".to_string()])
    );
    Ok(())
}

#[test]
fn root_level_files_have_no_dir() {
    assert_dependency_imports(
        "index.mjs",
        r#"import a from "./file.js""#,
        ["file.js"],
        [],
        given_metadata(
            Default::default(),
            Default::default(),
            None,
            Default::default(),
        ),
    )
}

#[test]
fn only_walks_one_dir_level_for_curdir() {
    assert_dependency_imports(
        "src/js/index.mjs",
        r#"
    import fs from "fs";
    import { x } from "./xes.mjs";
  "#,
        ["src/js/xes.mjs"],
        ["fs"],
        given_metadata(
            Default::default(),
            Default::default(),
            None,
            Default::default(),
        ),
    )
}

#[test]
fn walks_two_dir_levels_for_pardir() {
    assert_dependency_imports(
        "src/js/a/index.mjs",
        r#"
    import fs from "fs";
    import { x } from "../xes.mjs";
  "#,
        ["src/js/xes.mjs"],
        ["fs"],
        given_metadata(
            Default::default(),
            Default::default(),
            None,
            Default::default(),
        ),
    )
}

#[test]
fn silly_walking() {
    assert_dependency_imports(
        "src/js/a/index.mjs",
        r#"
    import { x } from "././///../../xes.mjs";
  "#,
        ["src/xes.mjs"],
        [],
        given_metadata(
            Default::default(),
            Default::default(),
            None,
            Default::default(),
        ),
    )
}

#[test]
fn imports_outside_of_provided_source_root_are_unchanged() {
    assert_dependency_imports(
        "src/index.mjs",
        r#"
    import { x } from "../../xes.mjs";
  "#,
        ["../../xes.mjs"],
        [],
        given_metadata(
            Default::default(),
            Default::default(),
            None,
            Default::default(),
        ),
    );

    assert_dependency_imports(
        "js/src/lib/index.mjs",
        r#"
    import { x } from "./../../../../lib2/xes.mjs";
  "#,
        ["./../../../../lib2/xes.mjs"],
        [],
        given_metadata(
            Default::default(),
            Default::default(),
            None,
            Default::default(),
        ),
    );
}

#[test]
fn subpath_package_import() {
    assert_dependency_imports(
        "js/src/lib/index.mjs",
        r#"
    import chalk from '#myChalk';
    "#,
        [],
        ["chalk"],
        given_metadata(
            "",
            HashMap::from_iter([("#myChalk".to_string(), vec!["chalk".to_string()])]),
            None,
            Default::default(),
        ),
    );
}

#[test]
fn subpath_file_import() {
    assert_dependency_imports(
        "js/src/lib/index.mjs",
        r#"
    import stuff from '#nested/stuff.mjs';
    "#,
        ["js/src/lib/nested/stuff.mjs"],
        [],
        given_metadata(
            "js",
            HashMap::from_iter([(
                "#nested/*.mjs".to_string(),
                vec!["./src/lib/nested/*.mjs".to_string()],
            )]),
            None,
            Default::default(),
        ),
    );
}

#[test]
fn config_file_import() {
    assert_dependency_imports(
        "js/project/src/lib/index.js",
        r#"
    import stuff from 'lib/stuff';
    "#,
        ["js/project/src/lib/stuff"],
        ["lib/stuff"],
        given_metadata(
            "js",
            Default::default(),
            Some("js/project"),
            HashMap::from_iter([("*".to_string(), vec!["./src/*".to_string()])]),
        ),
    );
}

#[test]
fn config_file_sharing_root_import() {
    assert_dependency_imports(
        "js/project/src/lib/index.js",
        r#"
    import stuff from 'lib/stuff';
    "#,
        ["js/project/src/lib/stuff"],
        ["lib/stuff"],
        given_metadata(
            "js/project",
            Default::default(),
            Some("js/project"),
            HashMap::from_iter([("*".to_string(), vec!["./src/*".to_string()])]),
        ),
    );
}

#[test]
fn config_file_with_at_mapping_imports() {
    assert_dependency_imports(
        "js/project/src/app/index.js",
        r#"
    import { Button } from "@component/lib/button.js";
    "#,
        ["js/project/src/component/lib/button.js"],
        ["@component/lib/button.js"],
        given_metadata(
            "js/project",
            Default::default(),
            Some("js/project"),
            HashMap::from_iter([(
                "@component/*".to_string(),
                vec!["./src/component/*".to_string()],
            )]),
        ),
    );
}

#[test]
fn polyfills() {
    assert_dependency_imports(
        "js/src/index.mjs",
        r#"
    import { ws } from '#websockets';
    "#,
        ["js/websockets-polyfill.js"],
        ["websockets"],
        given_metadata(
            "js",
            HashMap::from_iter([(
                "#websockets".to_string(),
                vec![
                    "websockets".to_string(),
                    "./websockets-polyfill.js".to_string(),
                ],
            )]),
            None,
            Default::default(),
        ),
    );
}

fn assert_matches_with_star<'a>(
    pattern: Pattern,
    matched: impl Into<Option<&'a str>> + std::fmt::Debug,
) {
    let matched = matched.into();
    let is_match = matches!(
      pattern,
      Pattern::Match(_, ref star_match) if star_match.as_ref().map(|StarMatch(string)| *string) == matched
    );
    assert!(
        is_match,
        "pattern = {pattern:?}, expected_match = {matched:?}"
    )
}

#[test]
fn pattern_matches_trailing_star() {
    let pattern = Pattern::matches("#lib/*", "#lib/something/index.js");
    assert_matches_with_star(pattern, "something/index.js")
}

#[test]
fn pattern_matches_star() {
    let pattern = Pattern::matches("#lib/*/index.js", "#lib/something/index.js");
    assert_matches_with_star(pattern, "something")
}

#[test]
fn pattern_matches_star_with_extension() {
    let pattern = Pattern::matches("#internal/*.js", "#internal/z.js");
    assert_matches_with_star(pattern, "z")
}

#[test]
fn pattern_without_star_matches() {
    let pattern = Pattern::matches("#some-lib", "#some-lib");
    assert_matches_with_star(pattern, None)
}

#[test]
fn static_pattern_mismatch() {
    let pattern = Pattern::matches("#some-lib", "#some-other-lib");
    assert_eq!(pattern, Pattern::NoMatch)
}

#[test]
fn mismatch_after_star_pattern() {
    let pattern = Pattern::matches("#some-lib/*.mjs", "#some-lib/a.js");
    assert_eq!(pattern, Pattern::NoMatch)
}

#[test]
fn mismatch_before_star_pattern() {
    let pattern = Pattern::matches("#other-lib/*.js", "#some-lib/a.js");
    assert_eq!(pattern, Pattern::NoMatch)
}

#[test]
fn trailing_star_pattern_mismatch() {
    let pattern = Pattern::matches("#some-lib/*", "#some-other-lib");
    assert_eq!(pattern, Pattern::NoMatch)
}

#[test]
fn star_only_pattern() {
    let pattern = Pattern::matches("*", "some-other-lib");
    assert_matches_with_star(pattern, "some-other-lib")
}

#[test]
fn empty_pattern_does_not_match_import() {
    let pattern = Pattern::matches("", "#some-other-lib");
    assert_eq!(pattern, Pattern::NoMatch)
}

#[test]
fn empty_import() {
    let pattern = Pattern::matches("", "");
    assert_eq!(pattern, Pattern::NoMatch)
}

#[test]
fn empty_import_and_star_pattern() {
    let pattern = Pattern::matches("*", "");
    assert_eq!(pattern, Pattern::NoMatch)
}

#[test]
fn unicode_shenanigans() {
    assert_matches_with_star(Pattern::matches("#🔥*🔥", "#🔥asd🔥"), "asd");
}

#[test]
fn more_unicode_shenanigans() {
    assert_matches_with_star(
        Pattern::matches("#我的氣墊船充滿了鱔魚/*.js", "#我的氣墊船充滿了鱔魚/asd.js"),
        "asd",
    );
}

#[test]
fn matching_unicode_shenanigans() {
    assert_matches_with_star(
        Pattern::matches("#*/stuff.js", "#🔥asd🔥/stuff.js"),
        "🔥asd🔥",
    );
}

#[test]
fn unicode_shenanigans_with_equal_start_byte() {
    assert_matches_with_star(Pattern::matches("#á/*é.js", "#á/asdáé.js"), "asdá");
}

#[test]
fn replaces_groups() {
    let mut patterns = HashMap::default();
    patterns.insert(
        "#internal/*.js".to_string(),
        vec!["./src/internal/*.js".to_string()],
    );
    let imports = imports_from_patterns("dir", &patterns, "#internal/z.js");

    assert_eq!(
        imports,
        HashSet::from_iter([Import::Matched("dir/src/internal/z.js".to_string())])
    )
}

#[test]
fn longest_prefix_wins() {
    let mut patterns = HashMap::default();

    patterns.insert(
        "#internal/stuff/*.js".to_string(),
        vec!["./src/stuff/*.js".to_string()],
    );
    patterns.insert(
        "#internal/*.js".to_string(),
        vec!["./src/things/*.js".to_string()],
    );

    let imports = imports_from_patterns("dir", &patterns, "#internal/stuff/index.js");

    assert_eq!(
        imports,
        HashSet::from_iter([Import::Matched("dir/src/stuff/index.js".to_string())])
    )
}

#[test]
fn candidate_imports_from_config_and_imports() {
    assert_dependency_candidates(
        "js/project/src/lib/index.js",
        r#"
    import { relative, or, package } from 'lib/stuff';
    import { subpath } from '#nested/index.js';
    "#,
        [
            (
                "lib/stuff",
                JavascriptImportInfo::new(["js/project/src/lib/stuff"], ["lib/stuff"]),
            ),
            (
                "#nested/index.js",
                JavascriptImportInfo::new(["js/project/src/lib/nested/index.js"], [] as [&str; 0]),
            ),
        ],
        given_metadata(
            "js/project",
            HashMap::from_iter([(
                "#nested/*".to_string(),
                vec!["./src/lib/nested/*".to_string()],
            )]),
            Some("js/project"),
            HashMap::from_iter([("*".to_string(), vec!["./src/*".to_string()])]),
        ),
    );
}
