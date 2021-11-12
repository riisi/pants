use std::collections::BTreeMap;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use crate::context::Context;
use crate::externs;
use crate::nodes::{
  lift_directory_digest, task_side_effected, DownloadedFile, MultiPlatformExecuteProcess,
  NodeResult, Paths, RunId, SessionValues, Snapshot,
};
use crate::python::{throw, Key, Value};
use crate::tasks::Intrinsic;
use crate::types::Types;
use crate::Failure;

use cpython::{ObjectProtocol, Python};
use fs::{safe_create_dir_all_ioerror, RelativePath};
use futures::future::{self, BoxFuture, FutureExt, TryFutureExt};
use hashing::{Digest, EMPTY_DIGEST};
use indexmap::IndexMap;
use process_execution::{CacheDest, CacheName, NamedCaches};
use stdio::TryCloneAsFile;
use store::{SnapshotOps, SubsetParams};
use tempfile::TempDir;
use tokio::process;

type IntrinsicFn =
  Box<dyn Fn(Context, Vec<Value>) -> BoxFuture<'static, NodeResult<Value>> + Send + Sync>;

pub struct Intrinsics {
  intrinsics: IndexMap<Intrinsic, IntrinsicFn>,
}

impl Intrinsics {
  pub fn new(types: &Types) -> Intrinsics {
    let mut intrinsics: IndexMap<Intrinsic, IntrinsicFn> = IndexMap::new();
    intrinsics.insert(
      Intrinsic {
        product: types.directory_digest,
        inputs: vec![types.create_digest],
      },
      Box::new(create_digest_to_digest),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.directory_digest,
        inputs: vec![types.path_globs],
      },
      Box::new(path_globs_to_digest),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.paths,
        inputs: vec![types.path_globs],
      },
      Box::new(path_globs_to_paths),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.directory_digest,
        inputs: vec![types.download_file],
      },
      Box::new(download_file_to_digest),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.snapshot,
        inputs: vec![types.directory_digest],
      },
      Box::new(digest_to_snapshot),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.digest_contents,
        inputs: vec![types.directory_digest],
      },
      Box::new(directory_digest_to_digest_contents),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.digest_entries,
        inputs: vec![types.directory_digest],
      },
      Box::new(directory_digest_to_digest_entries),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.directory_digest,
        inputs: vec![types.merge_digests],
      },
      Box::new(merge_digests_request_to_digest),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.directory_digest,
        inputs: vec![types.remove_prefix],
      },
      Box::new(remove_prefix_request_to_digest),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.directory_digest,
        inputs: vec![types.add_prefix],
      },
      Box::new(add_prefix_request_to_digest),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.process_result,
        inputs: vec![types.multi_platform_process, types.platform],
      },
      Box::new(multi_platform_process_request_to_process_result),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.directory_digest,
        inputs: vec![types.digest_subset],
      },
      Box::new(digest_subset_to_digest),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.session_values,
        inputs: vec![],
      },
      Box::new(session_values),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.run_id,
        inputs: vec![],
      },
      Box::new(run_id),
    );
    intrinsics.insert(
      Intrinsic {
        product: types.interactive_process_result,
        inputs: vec![types.interactive_process],
      },
      Box::new(interactive_process),
    );
    Intrinsics { intrinsics }
  }

  pub fn keys(&self) -> impl Iterator<Item = &Intrinsic> {
    self.intrinsics.keys()
  }

  pub async fn run(
    &self,
    intrinsic: Intrinsic,
    context: Context,
    args: Vec<Value>,
  ) -> NodeResult<Value> {
    let function = self
      .intrinsics
      .get(&intrinsic)
      .unwrap_or_else(|| panic!("Unrecognized intrinsic: {:?}", intrinsic));
    function(context, args).await
  }
}

fn multi_platform_process_request_to_process_result(
  context: Context,
  args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  async move {
    let process_val = &args[0];
    // TODO: The platform will be used in a followup.
    let _platform_val = &args[1];

    let process_request = MultiPlatformExecuteProcess::lift(process_val).map_err(|str| {
      throw(format!(
        "Error lifting MultiPlatformExecuteProcess: {}",
        str
      ))
    })?;
    let result = context.get(process_request).await?.0;

    let maybe_stdout = context
      .core
      .store()
      .load_file_bytes_with(result.stdout_digest, |bytes: &[u8]| bytes.to_owned())
      .await
      .map_err(throw)?;

    let maybe_stderr = context
      .core
      .store()
      .load_file_bytes_with(result.stderr_digest, |bytes: &[u8]| bytes.to_owned())
      .await
      .map_err(throw)?;

    let stdout_bytes = maybe_stdout.ok_or_else(|| {
      throw(format!(
        "Bytes from stdout Digest {:?} not found in store",
        result.stdout_digest
      ))
    })?;

    let stderr_bytes = maybe_stderr.ok_or_else(|| {
      throw(format!(
        "Bytes from stderr Digest {:?} not found in store",
        result.stderr_digest
      ))
    })?;

    let platform_name: String = result.platform.into();
    let gil = Python::acquire_gil();
    let py = gil.python();
    Ok(externs::unsafe_call(
      py,
      context.core.types.process_result,
      &[
        externs::store_bytes(py, &stdout_bytes),
        Snapshot::store_file_digest(py, &context.core.types, &result.stdout_digest),
        externs::store_bytes(py, &stderr_bytes),
        Snapshot::store_file_digest(py, &context.core.types, &result.stderr_digest),
        externs::store_i64(py, result.exit_code.into()),
        Snapshot::store_directory_digest(py, &result.output_directory).map_err(throw)?,
        externs::unsafe_call(
          py,
          context.core.types.platform,
          &[externs::store_utf8(py, &platform_name)],
        ),
        externs::unsafe_call(
          py,
          context.core.types.process_result_metadata,
          &[
            result
              .metadata
              .total_elapsed
              .map(|d| externs::store_u64(py, Duration::from(d).as_millis() as u64))
              .unwrap_or_else(|| Value::from(py.None())),
            externs::store_utf8(py, result.metadata.source.into()),
            externs::store_u64(py, result.metadata.source_run_id.0.into()),
          ],
        ),
      ],
    ))
  }
  .boxed()
}

fn directory_digest_to_digest_contents(
  context: Context,
  args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  async move {
    let digest = lift_directory_digest(&args[0]).map_err(throw)?;
    let snapshot = context
      .core
      .store()
      .contents_for_directory(digest)
      .await
      .and_then(move |digest_contents| {
        let gil = Python::acquire_gil();
        Snapshot::store_digest_contents(gil.python(), &context, &digest_contents)
      })
      .map_err(throw)?;
    Ok(snapshot)
  }
  .boxed()
}

fn directory_digest_to_digest_entries(
  context: Context,
  args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  async move {
    let digest = lift_directory_digest(&args[0]).map_err(throw)?;
    let snapshot = context
      .core
      .store()
      .entries_for_directory(digest)
      .await
      .and_then(move |digest_entries| {
        let gil = Python::acquire_gil();
        Snapshot::store_digest_entries(gil.python(), &context, &digest_entries)
      })
      .map_err(throw)?;
    Ok(snapshot)
  }
  .boxed()
}

fn remove_prefix_request_to_digest(
  context: Context,
  args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  let core = context.core;
  let store = core.store();

  async move {
    let input_digest =
      lift_directory_digest(&externs::getattr(&args[0], "digest").unwrap()).map_err(throw)?;
    let prefix: String = externs::getattr(&args[0], "prefix").unwrap();
    let prefix = RelativePath::new(PathBuf::from(prefix))
      .map_err(|e| throw(format!("The `prefix` must be relative: {:?}", e)))?;
    let digest = store
      .strip_prefix(input_digest, prefix)
      .await
      .map_err(|e| throw(format!("{:?}", e)))?;
    let gil = Python::acquire_gil();
    Snapshot::store_directory_digest(gil.python(), &digest).map_err(throw)
  }
  .boxed()
}

fn add_prefix_request_to_digest(
  context: Context,
  args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  let core = context.core;
  let store = core.store();
  async move {
    let input_digest =
      lift_directory_digest(&externs::getattr(&args[0], "digest").unwrap()).map_err(throw)?;
    let prefix: String = externs::getattr(&args[0], "prefix").unwrap();
    let prefix = RelativePath::new(PathBuf::from(prefix))
      .map_err(|e| throw(format!("The `prefix` must be relative: {:?}", e)))?;
    let digest = store
      .add_prefix(input_digest, prefix)
      .await
      .map_err(|e| throw(format!("{:?}", e)))?;
    let gil = Python::acquire_gil();
    Snapshot::store_directory_digest(gil.python(), &digest).map_err(throw)
  }
  .boxed()
}

fn digest_to_snapshot(context: Context, args: Vec<Value>) -> BoxFuture<'static, NodeResult<Value>> {
  let store = context.core.store();
  async move {
    let digest = lift_directory_digest(&args[0])?;
    let snapshot = store::Snapshot::from_digest(store, digest).await?;
    let gil = Python::acquire_gil();
    Snapshot::store_snapshot(gil.python(), snapshot)
  }
  .map_err(throw)
  .boxed()
}

fn merge_digests_request_to_digest(
  context: Context,
  args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  let core = context.core;
  let store = core.store();
  let digests: Result<Vec<hashing::Digest>, String> =
    externs::getattr::<Vec<Value>>(&args[0], "digests")
      .unwrap()
      .into_iter()
      .map(|val: Value| lift_directory_digest(&val))
      .collect();
  async move {
    let digest = store
      .merge(digests.map_err(throw)?)
      .await
      .map_err(|e| throw(format!("{:?}", e)))?;
    let gil = Python::acquire_gil();
    Snapshot::store_directory_digest(gil.python(), &digest).map_err(throw)
  }
  .boxed()
}

fn download_file_to_digest(
  context: Context,
  mut args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  async move {
    let key = Key::from_value(args.pop().unwrap()).map_err(Failure::from_py_err)?;
    let digest = context.get(DownloadedFile(key)).await?;
    let gil = Python::acquire_gil();
    Snapshot::store_directory_digest(gil.python(), &digest).map_err(throw)
  }
  .boxed()
}

fn path_globs_to_digest(
  context: Context,
  mut args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  async move {
    let val = args.pop().unwrap();
    let path_globs = Snapshot::lift_path_globs(&val)
      .map_err(|e| throw(format!("Failed to parse PathGlobs: {}", e)))?;
    let digest = context.get(Snapshot::from_path_globs(path_globs)).await?;
    let gil = Python::acquire_gil();
    Snapshot::store_directory_digest(gil.python(), &digest).map_err(throw)
  }
  .boxed()
}

fn path_globs_to_paths(
  context: Context,
  mut args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  let core = context.core.clone();
  async move {
    let val = args.pop().unwrap();
    let path_globs = Snapshot::lift_path_globs(&val)
      .map_err(|e| throw(format!("Failed to parse PathGlobs: {}", e)))?;
    let paths = context.get(Paths::from_path_globs(path_globs)).await?;
    let gil = Python::acquire_gil();
    Paths::store_paths(gil.python(), &core, &paths).map_err(throw)
  }
  .boxed()
}

enum CreateDigestItem {
  FileContent(RelativePath, bytes::Bytes, bool),
  FileEntry(RelativePath, Digest, bool),
  Dir(RelativePath),
}

fn create_digest_to_digest(
  context: Context,
  args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  let items: Vec<CreateDigestItem> = {
    let gil = Python::acquire_gil();
    let py = gil.python();
    externs::collect_iterable(&args[0])
      .unwrap()
      .into_iter()
      .map(|obj| {
        let raw_path: String = externs::getattr(&obj, "path").unwrap();
        let path = RelativePath::new(PathBuf::from(raw_path)).unwrap();
        if obj.hasattr(py, "content").unwrap() {
          let bytes = bytes::Bytes::from(externs::getattr::<Vec<u8>>(&obj, "content").unwrap());
          let is_executable: bool = externs::getattr(&obj, "is_executable").unwrap();
          CreateDigestItem::FileContent(path, bytes, is_executable)
        } else if obj.hasattr(py, "file_digest").unwrap() {
          let py_digest = externs::getattr(&obj, "file_digest").unwrap();
          let digest = Snapshot::lift_file_digest(&py_digest).unwrap();
          let is_executable: bool = externs::getattr(&obj, "is_executable").unwrap();
          CreateDigestItem::FileEntry(path, digest, is_executable)
        } else {
          CreateDigestItem::Dir(path)
        }
      })
      .collect()
  };

  let digest_futures: Vec<_> = items
    .into_iter()
    .map(|item| {
      let store = context.core.store();
      async move {
        match item {
          CreateDigestItem::FileContent(path, bytes, is_executable) => {
            let digest = store.store_file_bytes(bytes, true).await?;
            let snapshot = store
              .snapshot_of_one_file(path, digest, is_executable)
              .await?;
            let res: Result<_, String> = Ok(snapshot.digest);
            res
          }
          CreateDigestItem::FileEntry(path, digest, is_executable) => {
            let snapshot = store
              .snapshot_of_one_file(path, digest, is_executable)
              .await?;
            let res: Result<_, String> = Ok(snapshot.digest);
            res
          }
          CreateDigestItem::Dir(path) => store
            .create_empty_dir(path)
            .await
            .map_err(|e| format!("{:?}", e)),
        }
      }
    })
    .collect();

  let store = context.core.store();
  async move {
    let digests = future::try_join_all(digest_futures).await.map_err(throw)?;
    let digest = store
      .merge(digests)
      .await
      .map_err(|e| throw(format!("{:?}", e)))?;
    let gil = Python::acquire_gil();
    Snapshot::store_directory_digest(gil.python(), &digest).map_err(throw)
  }
  .boxed()
}

fn digest_subset_to_digest(
  context: Context,
  args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  let globs = externs::getattr(&args[0], "globs").unwrap();
  let store = context.core.store();

  async move {
    let path_globs = Snapshot::lift_prepared_path_globs(&globs).map_err(throw)?;
    let original_digest =
      lift_directory_digest(&externs::getattr(&args[0], "digest").unwrap()).map_err(throw)?;
    let subset_params = SubsetParams { globs: path_globs };
    let digest = store
      .subset(original_digest, subset_params)
      .await
      .map_err(|e| throw(format!("{:?}", e)))?;
    let gil = Python::acquire_gil();
    Snapshot::store_directory_digest(gil.python(), &digest).map_err(throw)
  }
  .boxed()
}

fn session_values(context: Context, _args: Vec<Value>) -> BoxFuture<'static, NodeResult<Value>> {
  async move { context.get(SessionValues).await }.boxed()
}

fn run_id(context: Context, _args: Vec<Value>) -> BoxFuture<'static, NodeResult<Value>> {
  async move { context.get(RunId).await }.boxed()
}

fn interactive_process(
  context: Context,
  mut args: Vec<Value>,
) -> BoxFuture<'static, NodeResult<Value>> {
  async move {
    let types = &context.core.types;
    let interactive_process_result = types.interactive_process_result;

    let value: Value = args.pop().unwrap();

    let argv: Vec<String> = externs::getattr(&value, "argv").unwrap();
    if argv.is_empty() {
      return Err("Empty argv list not permitted".to_owned().into());
    }

    let run_in_workspace: bool = externs::getattr(&value, "run_in_workspace").unwrap();
    let restartable: bool = externs::getattr(&value, "restartable").unwrap();
    let input_digest_value: Value = externs::getattr(&value, "input_digest").unwrap();
    let input_digest: Digest = lift_directory_digest(&input_digest_value)?;
    let env = externs::getattr_from_str_frozendict(&value, "env");
    let append_only_caches = externs::getattr_from_str_frozendict(&value, "append_only_caches")
        .into_iter()
        .map(|(name, dest)| Ok((CacheName::new(name).unwrap(), CacheDest::new(dest).unwrap())))
        .collect::<Result<BTreeMap<_, _>, String>>()?;
    let session = context.session;

    if !append_only_caches.is_empty() && run_in_workspace {
      return Err("Local interactive process cannot use append-only caches when run in workspace.".to_owned().into());
    }

    if !restartable {
        task_side_effected()?;
    }

    let maybe_tempdir = if run_in_workspace {
      None
    } else {
      Some(TempDir::new().map_err(|err| format!("Error creating tempdir: {}", err))?)
    };

    if input_digest != EMPTY_DIGEST {
      if run_in_workspace {
        return Err(
          "Local interactive process should not attempt to materialize files when run in workspace.".to_owned().into()
        );
      }

      let destination = match maybe_tempdir {
        Some(ref dir) => dir.path().to_path_buf(),
        None => unreachable!(),
      };

      context
        .core
        .store()
        .materialize_directory(destination, input_digest)
        .await?;
    }

    if !append_only_caches.is_empty() {
      let named_caches = NamedCaches::new(context.core.named_caches_dir.clone());
      let named_cache_symlinks = named_caches
          .local_paths(&append_only_caches)
          .collect::<Vec<_>>();

      let destination = match maybe_tempdir {
        Some(ref dir) => dir.path().to_path_buf(),
        None => unreachable!(),
      };

      for named_cache_symlink in named_cache_symlinks {
        safe_create_dir_all_ioerror(&named_cache_symlink.src).map_err(|err| {
          format!(
            "Error making {} for local execution: {:?}",
            named_cache_symlink.src.display(),
            err
          )
        })?;

        let dst = destination.join(&named_cache_symlink.dst);
        if let Some(dir) = dst.parent() {
          safe_create_dir_all_ioerror(dir).map_err(|err| {
            format!(
              "Error making {} for local execution: {:?}", dir.display(), err
            )
          })?;
        }
        symlink(&named_cache_symlink.src, &dst).map_err(|err| {
          format!(
            "Error linking {} -> {} for local execution: {:?}",
            named_cache_symlink.src.display(),
            dst.display(),
            err
          )
        })?;
      }
    }

    let p = Path::new(&argv[0]);
    let program_name = match maybe_tempdir {
      Some(ref tempdir) if p.is_relative() => {
        let mut buf = PathBuf::new();
        buf.push(tempdir);
        buf.push(p);
        buf
      }
      _ => p.to_path_buf(),
    };

    let mut command = process::Command::new(program_name);
    for arg in argv[1..].iter() {
      command.arg(arg);
    }

    if let Some(ref tempdir) = maybe_tempdir {
      command.current_dir(tempdir.path());
    }

    command.env_clear();
    command.envs(env);

    command.kill_on_drop(true);

    let exit_status = session.clone()
      .with_console_ui_disabled(async move {
        // Once any UI is torn down, grab exclusive access to the console.
        let (term_stdin, term_stdout, term_stderr) =
          stdio::get_destination().exclusive_start(Box::new(|_| {
            // A stdio handler that will immediately trigger logging.
            Err(())
          }))?;
        // NB: Command's stdio methods take ownership of a file-like to use, so we use
        // `TryCloneAsFile` here to `dup` our thread-local stdio.
        command
          .stdin(Stdio::from(
            term_stdin
              .try_clone_as_file()
              .map_err(|e| format!("Couldn't clone stdin: {}", e))?,
          ))
          .stdout(Stdio::from(
            term_stdout
              .try_clone_as_file()
              .map_err(|e| format!("Couldn't clone stdout: {}", e))?,
          ))
          .stderr(Stdio::from(
            term_stderr
              .try_clone_as_file()
              .map_err(|e| format!("Couldn't clone stderr: {}", e))?,
          ));
        let mut subprocess = command
          .spawn()
          .map_err(|e| format!("Error executing interactive process: {}", e))?;
        tokio::select! {
          _ = session.cancelled() => {
            // The Session was cancelled: kill the process, and then wait for it to exit (to avoid
            // zombies).
            subprocess.kill().map_err(|e| format!("Failed to interrupt child process: {}", e)).await?;
            subprocess.wait().await.map_err(|e| e.to_string())
          }
          exit_status = subprocess.wait() => {
            // The process exited.
            exit_status.map_err(|e| e.to_string())
          }
        }
      })
      .await?;

    let code = exit_status.code().unwrap_or(-1);
    let result = {
      let gil = Python::acquire_gil();
      let py = gil.python();
      externs::unsafe_call(
        py,
        interactive_process_result,
        &[externs::store_i64(py, i64::from(code))],
      )
    };
    Ok(result)
  }.boxed()
}
