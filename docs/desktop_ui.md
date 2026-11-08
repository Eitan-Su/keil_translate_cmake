# Desktop task interaction

The desktop application displays separate parse, generation, CMake configuration,
compilation, linking, and artifact-check phases. Only measured build counters
produce a percentage. Ninja counters describe completed build commands; Make
percentages retain their original unit. Parse, generation, configuration, linking,
and final checks use an activity indicator. A percentage is not an elapsed-time
estimate. Total elapsed time includes parsing and generation; validation elapsed
time starts at CMake configuration.

## Task ownership

Starting a task captures the project, output directory, compiler, target, and
export options. These inputs stay disabled until the worker thread finishes.
The output and VS Code buttons refer to the completed task's directory, even if
the form subsequently changes. A second task cannot start while the previous
thread is releasing resources.

Success, failure, and cancellation produce an inline result with expandable
details. Failure and cancellation allow another run after worker shutdown.
Results contain the total time and validation time; successful builds include
artifact sizes and reported firmware memory use. Closing during a task displays
a reminder to cancel or wait for completion.

## Cancellation contract

`ConversionWorker.cancel()` sets a `threading.Event`; it does not terminate a
Python thread. Parsing finishes its current operation. Generation checks the
event at its existing progress checkpoints, including per-source processing.
`OperationCancelled` propagates through generation while its `finally` block
restores parser state. Cancellation may leave partial generated files; another
conversion regenerates the project. Original SDK sources remain inputs.

`BuildValidator` and `validate_generated_build()` accept optional
`cancel_check`, `activity_callback`, and `log_callback` arguments. Existing
callers and the legacy numeric `progress_callback` remain supported. When
cancellation is requested, the validator stops the current command and its
descendants and drains captured output before returning. Cancellable Windows
commands use a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; POSIX commands
use their process group. A cancelled report has `status="cancelled"` and a
command with `cancelled=true`, which never counts as success. Build reports
retain captured stdout and stderr, including cancellation diagnostics.

`BuildActivity(phase, message, current=0, total=0, unit="steps")` reports explicit
phase information. `total=0` means that no measurable fraction is available;
`unit="percent"` preserves Make's percentage without presenting it as a file
count. Log wording does not control the application's stage or cancellation
state.

## Input and log behavior

The form checks project existence and extension and checks the output directory
before starting. A manually selected output directory survives project changes.
Configuration and build errors remain visible in the result area.

Logs are plain text, retain their original markup characters, and are rendered
in batches every 100 ms. The view retains up to 5,000 lines; complete build output
is retained in `build-validation.json`. Error filtering preserves the full view
history, copying uses the current filtered view, and scrolling upward pauses
automatic following until enabled again.
