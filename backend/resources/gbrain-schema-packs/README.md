# Vendored Wiki Schema compatibility catalog

These manifests are the PuddingClaw-owned runtime copy of the portable
`gbrain-schema-pack-v1` catalog. They let LLM Wiki initialize, edit and validate
its Schema without installing or starting gbrain.

- Upstream: <https://github.com/garrytan/gbrain>
- Pinned source commit: `872c3d6ae4073eb6e77c661d0a72f30b31c4c999`
- Upstream path: `src/core/schema-pack/base/gbrain-*.yaml`

Changing these files is a Schema migration. Update the pinned commit, run the
Brain Schema contract tests, and separately run the optional real-gbrain
compatibility test before publishing a new catalog version.
