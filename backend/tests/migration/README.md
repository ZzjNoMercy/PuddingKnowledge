# Legacy migration evidence

The tests in this directory replay sanitized source-observer contracts from the
original PuddingClaw checkout. They are not Platform unit or runtime tests and
must not be used as evidence that the independent Platform repository is
self-contained.

The default `pytest` traversal excludes this directory. To run these tests,
provide the original source checkout (or an equivalent reviewed fixture) with
`PUDDINGKNOWLEDGE_LEGACY_SOURCE=/absolute/path/to/PuddingClaw`, then invoke the
directory explicitly with the migration marker, using an environment that has
the original checkout's legacy-only dependencies. The independent target
environment intentionally does not install those dependencies. Without that
variable the tests skip with an explicit fixture message.
The target repository must never copy the legacy `knowledge`, `analytics`,
`catalog_migration`, or Harness runtime modules to make these tests pass.
