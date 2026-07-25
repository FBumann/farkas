# Changelog

## [0.0.0-alpha.3](https://github.com/FBumann/linopy-yaml/compare/v0.0.0-alpha.2...v0.0.0-alpha.3) (2026-07-25)


### ⚠ BREAKING CHANGES

* `where: "<dimension>"` is a load error. It never did anything except in the case where it broke.
* an unknown name in a where string is a load error rather than a False mask; parameter-vs-parameter where comparisons are rejected; and names may no longer collide across kinds. Each was a way to build a model that solved and was silently wrong.
* a variable declared without `bounds.lower` was silently non-negative; it is now unbounded below, matching linopy's `add_variables(lower=-inf)`. Models relying on the implicit `>= 0` must write `lower: 0`. An LP that was bounded only by that implicit constraint will now report as unbounded.

### Bug Fixes

* a bare dimension name in a where is a load error ([#64](https://github.com/FBumann/linopy-yaml/issues/64)) ([1a89fef](https://github.com/FBumann/linopy-yaml/commit/1a89fef19e9d301141e8bd459c89ef2e2255b554))
* both lanes agree on where-comparisons over dimensions, and on `**` ([#52](https://github.com/FBumann/linopy-yaml/issues/52)) ([7bec431](https://github.com/FBumann/linopy-yaml/commit/7bec431c6d46e5d2eda96cf23e9fde7712825adb))
* check() enforces degree 1; README stops promising a fallback ([#55](https://github.com/FBumann/linopy-yaml/issues/55)) ([d0b008c](https://github.com/FBumann/linopy-yaml/commit/d0b008ca5b8f4dc1b3bbeee460392c2ded32469b))


### Refactoring

* cut back the accumulated surface ([#56](https://github.com/FBumann/linopy-yaml/issues/56)) ([1802dd0](https://github.com/FBumann/linopy-yaml/commit/1802dd00feeeda727c67a6a36c415ddc6caf5a21))
* finish the lane split — where_parser keeps grammar, not evaluation ([#59](https://github.com/FBumann/linopy-yaml/issues/59)) ([a30f7ec](https://github.com/FBumann/linopy-yaml/commit/a30f7ec7dc511ea7a59ded6434facc6e90405a23))
* let the annotations say what the parsers already guarantee ([#61](https://github.com/FBumann/linopy-yaml/issues/61)) ([49387d4](https://github.com/FBumann/linopy-yaml/commit/49387d4c993df817da8ac3e5fd3f5f8f27becb72))
* name resolution is a pass, not a backend detail ([#62](https://github.com/FBumann/linopy-yaml/issues/62)) ([8622fa6](https://github.com/FBumann/linopy-yaml/commit/8622fa6d0063ae32fbecdffe51f9e28f70969452))


### Documentation

* cross-language vocabulary map, and a procedure for the ceiling ([#63](https://github.com/FBumann/linopy-yaml/issues/63)) ([7f535cd](https://github.com/FBumann/linopy-yaml/commit/7f535cde9127079e7e6dcaf5754daeadb659e7af))
* SPEC catches up with the code it describes ([#57](https://github.com/FBumann/linopy-yaml/issues/57)) ([50edfb6](https://github.com/FBumann/linopy-yaml/commit/50edfb61f991ac852e8aace2be79f93506a28a47))

## [0.0.0-alpha.2](https://github.com/FBumann/linopy-yaml/compare/v0.0.0-alpha.1...v0.0.0-alpha.2) (2026-07-24)


### Bug Fixes

* name-check dimension kwargs at load time; restore docs lost at merge ([#48](https://github.com/FBumann/linopy-yaml/issues/48)) ([4c6bfc9](https://github.com/FBumann/linopy-yaml/commit/4c6bfc97919e84db71bdc4d0c82d46a20f866b2d))

## 0.0.0-alpha.1 (2026-07-24)


### Features

* API polish — check(), write(), LanguageError, Solution lifecycle ([#36](https://github.com/FBumann/linopy-yaml/issues/36)) ([fc36af5](https://github.com/FBumann/linopy-yaml/commit/fc36af515eb9baafdf81c036a27ad8ca9431297f))
