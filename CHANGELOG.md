# Changelog

## [0.0.1-alpha.22](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.21...v0.0.1-alpha.22) (2026-07-29)


### Documentation

* **spec:** when a fill belongs in the language, and when it is data prep ([#325](https://github.com/FBumann/farkas/issues/325)) ([5f71da8](https://github.com/FBumann/farkas/commit/5f71da8deae76011f6d2afd3af685ddb88196cca))

## [0.0.1-alpha.21](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.20...v0.0.1-alpha.21) (2026-07-29)


### Documentation

* the home page shows the model it sets, as math ([#317](https://github.com/FBumann/farkas/issues/317)) ([4094fb9](https://github.com/FBumann/farkas/commit/4094fb91f30cd9595acbca131882d34e54b301ee))
* **typeset:** say that `symbols=` takes a dict, not only a path ([#323](https://github.com/FBumann/farkas/issues/323)) ([e9f2102](https://github.com/FBumann/farkas/commit/e9f210258732b7bf249e6465b27b0e6c32a6087f))

## [0.0.1-alpha.20](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.19...v0.0.1-alpha.20) (2026-07-29)


### Bug Fixes

* **linopy:** refuse a missing bound at build, with the native lane's message ([#319](https://github.com/FBumann/farkas/issues/319)) ([0e75754](https://github.com/FBumann/farkas/commit/0e75754d21a3092160423364ae2ed64fbd6a5d92))

## [0.0.1-alpha.19](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.18...v0.0.1-alpha.19) (2026-07-29)


### Bug Fixes

* a divisor must be defined where the model divides by it ([#318](https://github.com/FBumann/farkas/issues/318)) ([d7ad809](https://github.com/FBumann/farkas/commit/d7ad80903362fa89a91625ec2d781e4dad0f7621))
* **relational:** absence propagates into a reduction, and laws to hold it there ([#314](https://github.com/FBumann/farkas/issues/314)) ([f4bb63b](https://github.com/FBumann/farkas/commit/f4bb63b7584586d6f87c217216159d600847dc77))

## [0.0.1-alpha.18](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.17...v0.0.1-alpha.18) (2026-07-29)


### Documentation

* typeset every model page from the model it shows ([#280](https://github.com/FBumann/farkas/issues/280)) ([d6eda33](https://github.com/FBumann/farkas/commit/d6eda33eafe5540c2a68cd73a4a050e150c4833d))

## [0.0.1-alpha.17](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.16...v0.0.1-alpha.17) (2026-07-29)


### Features

* typeset a validated model — LaTeX and Typst (spike) ([#269](https://github.com/FBumann/farkas/issues/269)) ([3cbd79a](https://github.com/FBumann/farkas/commit/3cbd79acdd63237f047b548fddc99f834f5f7dcc))


### Bug Fixes

* **ci:** the PR-title check must report on every head, or it blocks the merge ([#308](https://github.com/FBumann/farkas/issues/308)) ([ecbdb13](https://github.com/FBumann/farkas/commit/ecbdb133ac5e13df2150b6f018ab92dc5ebadac2))

## [0.0.1-alpha.16](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.15...v0.0.1-alpha.16) (2026-07-29)


### Documentation

* **releasing:** the alpha stream is 0.0.1, and say what actually holds it ([#306](https://github.com/FBumann/farkas/issues/306)) ([de597a6](https://github.com/FBumann/farkas/commit/de597a6b369c614e75db4752ba697292478e4155))

## [0.0.1-alpha.15](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.14...v0.0.1-alpha.15) (2026-07-29)


### Bug Fixes

* **ci:** check out the manifest the version guard reads ([#303](https://github.com/FBumann/farkas/issues/303)) ([911b9ed](https://github.com/FBumann/farkas/commit/911b9ed29c0c9c1e358d1d9fc861c52f03b621f4))

## [0.0.1-alpha.14](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.13...v0.0.1-alpha.14) (2026-07-29)


### Documentation

* **models:** fold long expressions onto one term per line ([#299](https://github.com/FBumann/farkas/issues/299)) ([826eaaa](https://github.com/FBumann/farkas/commit/826eaaa1958bd6f32967959607af0c9fe7f7d700))

## [0.0.1-alpha.13](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.12...v0.0.1-alpha.13) (2026-07-29)


### ⚠ BREAKING CHANGES

* **linopy:** importing `farkas.linopy` sets `linopy.options['semantics']` to 'v1' process-wide. Models built through this lane change answer wherever a variable is masked or shifted — that is the point, and it is the answer the native engine has always given.
* a bare `shift()` now drops the row at the vacated coordinate instead of contributing zero there, and `shift()` over a variable-free expression is a load error. Add `fill=0` for the previous behaviour.

### Features

* shift() creates absence, as linopy v1 says it does ([#291](https://github.com/FBumann/farkas/issues/291)) ([b394e70](https://github.com/FBumann/farkas/commit/b394e700c2b45b1eb06d434e95a441d64f6255d9))


### Bug Fixes

* **linopy:** the lane selects v1 on import, and fill= takes the identity of its position ([#293](https://github.com/FBumann/farkas/issues/293)) ([451c354](https://github.com/FBumann/farkas/commit/451c3548e6dc88e851d1f51e6c69f9be13a4220e))
* **release:** put the version back on the alpha stream, and make the pin real ([#295](https://github.com/FBumann/farkas/issues/295)) ([8ef60a5](https://github.com/FBumann/farkas/commit/8ef60a57d3a4fecb649bdb913b651a8396916202))

## [0.0.1-alpha.12](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.11...v0.0.1-alpha.12) (2026-07-29)


### Documentation

* **spec:** three behaviours a consumer could only reach by probing ([#288](https://github.com/FBumann/farkas/issues/288)) ([50b087c](https://github.com/FBumann/farkas/commit/50b087c51428770ec476a29349546cbb6f0a948e))

## [0.0.1-alpha.11](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.10...v0.0.1-alpha.11) (2026-07-29)


### Bug Fixes

* **docs:** readable mermaid diagrams in dark mode, and the site icons ([#285](https://github.com/FBumann/farkas/issues/285)) ([b21d5d8](https://github.com/FBumann/farkas/commit/b21d5d8aea094b17713d0cb3547d63c73f89e596))

## [0.0.1-alpha.10](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.9...v0.0.1-alpha.10) (2026-07-28)


### Documentation

* publish the site with mkdocs-material ([#283](https://github.com/FBumann/farkas/issues/283)) ([1ec55ac](https://github.com/FBumann/farkas/commit/1ec55ac5af2467ba0cfec58cafc7d84b6cddc467))

## [0.0.1-alpha.9](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.8...v0.0.1-alpha.9) (2026-07-28)


### Bug Fixes

* **result:** read the solution back in label order ([#278](https://github.com/FBumann/farkas/issues/278)) ([11bf86d](https://github.com/FBumann/farkas/commit/11bf86d8e3c0e5dd02e624b597ae3c4d0aff951b))

## [0.0.1-alpha.8](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.7...v0.0.1-alpha.8) (2026-07-28)


### Bug Fixes

* **piecewise:** emit foreach in declaration order, not set order ([#271](https://github.com/FBumann/farkas/issues/271)) ([c06907b](https://github.com/FBumann/farkas/commit/c06907b0ef5e634542f5f52960a9488271f1bcf7))
* **where:** a declared dimension on a comparison RHS is a load error ([#272](https://github.com/FBumann/farkas/issues/272)) ([5b1784e](https://github.com/FBumann/farkas/commit/5b1784ee1271f0cb88ff1e4657940ff526aee19a))

## [0.0.1-alpha.7](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.6...v0.0.1-alpha.7) (2026-07-28)


### Documentation

* the AST as a narrow waist — one contract, many consumers ([#266](https://github.com/FBumann/farkas/issues/266)) ([688c6df](https://github.com/FBumann/farkas/commit/688c6df1f3d35bcc5487b3910dfa06fdcd191f22))

## [0.0.1-alpha.6](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.5...v0.0.1-alpha.6) (2026-07-28)


### Documentation

* say what examples/ is, and point it at the explained version ([#263](https://github.com/FBumann/farkas/issues/263)) ([aab7617](https://github.com/FBumann/farkas/commit/aab7617b137b9b88f72084142fa5ad558074d6e2))

## [0.0.1-alpha.5](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.4...v0.0.1-alpha.5) (2026-07-28)


### Documentation

* move the three root docs, add a guide, make the index a path ([#260](https://github.com/FBumann/farkas/issues/260)) ([bfbfbec](https://github.com/FBumann/farkas/commit/bfbfbecb7a1291f1bca6dd110b621722806f25fb))

## [0.0.1-alpha.4](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.3...v0.0.1-alpha.4) (2026-07-28)


### Documentation

* a model gallery, with a construct matrix read off the plan ([#257](https://github.com/FBumann/farkas/issues/257)) ([e9cf410](https://github.com/FBumann/farkas/commit/e9cf410b6d81a774d18efa13894e9dc5ad359a43))

## [0.0.1-alpha.3](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.2...v0.0.1-alpha.3) (2026-07-28)


### Documentation

* a CONTRIBUTING.md, and move procedure out of the corpus page ([#255](https://github.com/FBumann/farkas/issues/255)) ([d3d5f96](https://github.com/FBumann/farkas/commit/d3d5f9652944e6915f2be71a73ee7c8e45a070fe))

## [0.0.1-alpha.2](https://github.com/FBumann/farkas/compare/v0.0.1-alpha.1...v0.0.1-alpha.2) (2026-07-28)


### Refactoring

* **bench:** retire the duckdb arm, and the docs it was holding up ([#253](https://github.com/FBumann/farkas/issues/253)) ([6eaef1e](https://github.com/FBumann/farkas/commit/6eaef1e86999bb4047e1215a6c617d641f143dbe))

## [0.0.1-alpha.1](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.33...v0.0.1-alpha.1) (2026-07-28)


### ⚠ BREAKING CHANGES

* rebuild the relational engine on polars ([#189](https://github.com/FBumann/farkas/issues/189))

### Features

* rebuild the relational engine on polars ([#189](https://github.com/FBumann/farkas/issues/189)) ([c11a0dd](https://github.com/FBumann/farkas/commit/c11a0dd3e8ed58bf8c1a8ea9b79961733e381eb1))


### Chores

* re-cut as 0.0.1-alpha.1, and refresh the chart page from the results ([#250](https://github.com/FBumann/farkas/issues/250)) ([f62d434](https://github.com/FBumann/farkas/commit/f62d43446f21267a1655283ae3467fdae420ad51))

## [0.0.0-alpha.33](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.32...v0.0.0-alpha.33) (2026-07-28)


### ⚠ BREAKING CHANGES

* absence propagates and drops the row, plus defined(v) ([#239](https://github.com/FBumann/farkas/issues/239))

### Features

* absence propagates and drops the row, plus defined(v) ([#239](https://github.com/FBumann/farkas/issues/239)) ([5eb5943](https://github.com/FBumann/farkas/commit/5eb5943bc1f83ff4451e1dbfa526d318f7cf7746))

## [0.0.0-alpha.32](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.31...v0.0.0-alpha.32) (2026-07-28)


### Features

* **compat:** answer linopy's v1 convention where the position knows the answer ([#236](https://github.com/FBumann/farkas/issues/236)) ([facee3f](https://github.com/FBumann/farkas/commit/facee3f250096724de20ffd6e0aa215dd8eb6534)), closes [#8](https://github.com/FBumann/farkas/issues/8)

## [0.0.0-alpha.31](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.30...v0.0.0-alpha.31) (2026-07-28)


### Documentation

* alpha.30 credited a perf fix it does not contain ([#230](https://github.com/FBumann/farkas/issues/230)) ([5afb004](https://github.com/FBumann/farkas/commit/5afb004cfb5ede08b0a203bde47f19ec54607476))

## [0.0.0-alpha.30](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.29...v0.0.0-alpha.30) (2026-07-28)


### Documentation

* pin all three label paths, and write the row-major order down ([#152](https://github.com/FBumann/farkas/issues/152)) ([3d8c725](https://github.com/FBumann/farkas/commit/3d8c7251bc210bbc4438382ec58c3b699683624a))

*No functional change: this release is alpha.29 plus tests and a paragraph of
`docs/ARCHITECTURE.md`. The commit subject says `perf:` and claims a speed-up, which
is wrong — that optimisation shipped in alpha.29 as
[#178](https://github.com/FBumann/farkas/issues/178). #152 was retitled after it
had already been merged and released, too late for the squash subject.*

## [0.0.0-alpha.29](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.28...v0.0.0-alpha.29) (2026-07-28)


### Performance

* compute variable labels arithmetically when the mask factors ([#178](https://github.com/FBumann/farkas/issues/178)) ([5d4745f](https://github.com/FBumann/farkas/commit/5d4745ff81179cf5f65afba4ffdc97b46829803f))

## [0.0.0-alpha.28](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.27...v0.0.0-alpha.28) (2026-07-28)


### Bug Fixes

* three ways an objective or a scalar parameter answered quietly ([#223](https://github.com/FBumann/farkas/issues/223)) ([ee08d5d](https://github.com/FBumann/farkas/commit/ee08d5d39dde4682b795d62f623d606d8996979a))

## [0.0.0-alpha.27](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.26...v0.0.0-alpha.27) (2026-07-28)


### Bug Fixes

* refuse a parameter source that carries a coordinate twice ([#201](https://github.com/FBumann/farkas/issues/201)) ([fffee97](https://github.com/FBumann/farkas/commit/fffee9773f259a9b75cf5014f50a02f40c02a3cb))

## [0.0.0-alpha.26](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.25...v0.0.0-alpha.26) (2026-07-27)


### Documentation

* **bench:** correct the label-optimisation numbers, and say how to get them ([#188](https://github.com/FBumann/farkas/issues/188)) ([bf84fd1](https://github.com/FBumann/farkas/commit/bf84fd1c1eb1ad6388d96b26827f83c4de3712c5))

## [0.0.0-alpha.25](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.24...v0.0.0-alpha.25) (2026-07-27)


### Performance

* **sink:** size the hand-off by nonzeros, under one budget and one chunking rule ([#195](https://github.com/FBumann/farkas/issues/195)) ([c31beaf](https://github.com/FBumann/farkas/commit/c31beafdbe95e48a902dd19fa4b27de27b9bc4c0))

## [0.0.0-alpha.24](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.23...v0.0.0-alpha.24) (2026-07-27)


### Performance

* **sink:** hand Arrow to numpy directly, not through to_pydict ([#193](https://github.com/FBumann/farkas/issues/193)) ([ac64f26](https://github.com/FBumann/farkas/commit/ac64f261239326e48a0c441774beedabb908a46f))

## [0.0.0-alpha.23](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.22...v0.0.0-alpha.23) (2026-07-27)


### Performance

* skip the objective's GROUP BY where a column cannot repeat ([#179](https://github.com/FBumann/farkas/issues/179)) ([787b1ce](https://github.com/FBumann/farkas/commit/787b1ce238861acef53049a4ac0135671d850f1a))

## [0.0.0-alpha.22](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.21...v0.0.0-alpha.22) (2026-07-27)


### Performance

* **lp:** render doubles with ::VARCHAR instead of printf('%.17g') ([#190](https://github.com/FBumann/farkas/issues/190)) ([b1df538](https://github.com/FBumann/farkas/commit/b1df538c3c0c4e0195d7d2d5ff2be7fb887608f1))

## [0.0.0-alpha.21](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.20...v0.0.0-alpha.21) (2026-07-27)


### Performance

* **relational:** a label is a position, so compute it instead of counting it ([#186](https://github.com/FBumann/farkas/issues/186)) ([a31645b](https://github.com/FBumann/farkas/commit/a31645bde58974a041f175a18c56e3251bf6d5cd))

## [0.0.0-alpha.20](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.19...v0.0.0-alpha.20) (2026-07-27)


### Bug Fixes

* the HiGHS sink's column ingest was an unbounded global sort ([#181](https://github.com/FBumann/farkas/issues/181)) ([271b0d7](https://github.com/FBumann/farkas/commit/271b0d7254c3cb0b94e740542071185ad0608b8d))

## [0.0.0-alpha.19](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.18...v0.0.0-alpha.19) (2026-07-27)


### Features

* expose duals on the solve path (sol.dual) ([#156](https://github.com/FBumann/farkas/issues/156)) ([284df79](https://github.com/FBumann/farkas/commit/284df7927c6f58064fc691e9b8c2f1b2db2f6a7b))

## [0.0.0-alpha.18](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.17...v0.0.0-alpha.18) (2026-07-27)


### Features

* **bench:** a performance harness the published numbers come from ([#143](https://github.com/FBumann/farkas/issues/143)) ([144713a](https://github.com/FBumann/farkas/commit/144713a6a2b32dc63efae52ce2853ce266d77191))

## [0.0.0-alpha.17](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.16...v0.0.0-alpha.17) (2026-07-27)


### Features

* forward solver_options, and gate reads on an actual incumbent ([#169](https://github.com/FBumann/farkas/issues/169)) ([493a5e6](https://github.com/FBumann/farkas/commit/493a5e6f9d26184e3f1b855d963692343a5bf680))

## [0.0.0-alpha.16](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.15...v0.0.0-alpha.16) (2026-07-27)


### Documentation

* the memory invariant says what it actually guarantees ([#150](https://github.com/FBumann/farkas/issues/150)) ([8ffe339](https://github.com/FBumann/farkas/commit/8ffe33929c29ba28e87ccbd60b4f1005a89a6132))

## [0.0.0-alpha.15](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.14...v0.0.0-alpha.15) (2026-07-27)


### ⚠ BREAKING CHANGES

* documentation only — the suggested alias is now `fk`. `import farkas` was and remains the actual import.

### Chores

* the import alias is fk, because the package is farkas ([#154](https://github.com/FBumann/farkas/issues/154)) ([fac76f0](https://github.com/FBumann/farkas/commit/fac76f04597e38675f136edf2d8f0bd5d74c85a7))

## [0.0.0-alpha.14](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.13...v0.0.0-alpha.14) (2026-07-27)


### ⚠ BREAKING CHANGES

* `Solution` is now `Result`; `status` returns the coarse axis (`ok`) rather than the solver's wording (`Optimal`) — `termination_condition` carries that, and `is_ok` is what most call sites meant. `objective` is `nan` and `primal`/`to_*` raise `NoSolutionError` when the solve produced nothing.

### Features

* a solve result tells you whether it has one ([#148](https://github.com/FBumann/farkas/issues/148)) ([30a91c8](https://github.com/FBumann/farkas/commit/30a91c853913bb7dee9e35323624b26f45ef5a45))

## [0.0.0-alpha.13](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.12...v0.0.0-alpha.13) (2026-07-26)


### Bug Fixes

* quote caller-supplied paths in SQL ([#139](https://github.com/FBumann/farkas/issues/139)) ([61dfe5b](https://github.com/FBumann/farkas/commit/61dfe5b487585fa7acd68cc76ce5d75e4bd42d98))

## [0.0.0-alpha.12](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.11...v0.0.0-alpha.12) (2026-07-26)


### Bug Fixes

* a null coordinate means "no group", not a typo ([#135](https://github.com/FBumann/farkas/issues/135)) ([9a672b4](https://github.com/FBumann/farkas/commit/9a672b42be9753a7e69ff0f6b76948a0352461a0))

## [0.0.0-alpha.11](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.10...v0.0.0-alpha.11) (2026-07-26)


### Documentation

* lead with what the package is; YAML is the format we ship, not the interface ([#132](https://github.com/FBumann/farkas/issues/132)) ([5bd5240](https://github.com/FBumann/farkas/commit/5bd52400a9dd2a9e94eb3f29a2f0ebb6466c78ba))

## [0.0.0-alpha.10](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.9...v0.0.0-alpha.10) (2026-07-26)


### Bug Fixes

* py.typed ships with the package it describes ([#131](https://github.com/FBumann/farkas/issues/131)) ([52529de](https://github.com/FBumann/farkas/commit/52529de80f4dd2c713c2fac2437855eee865f480))

## [0.0.0-alpha.9](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.8...v0.0.0-alpha.9) (2026-07-26)


### ⚠ BREAKING CHANGES

* the import path is now `farkas`; the compat lane is `farkas.linopy` and its extra is `[linopy]`.

### Refactoring

* rename the package to farkas, and compat/ to linopy/ ([#127](https://github.com/FBumann/farkas/issues/127)) ([5fae345](https://github.com/FBumann/farkas/commit/5fae345de3358f227e3a04da91f205982a1af4ce))

## [0.0.0-alpha.8](https://github.com/FBumann/farkas/compare/v0.0.0-alpha.7...v0.0.0-alpha.8) (2026-07-26)


### Refactoring

* one lazy import left, and it is the only real cycle ([#117](https://github.com/FBumann/farkas/issues/117)) ([ecad711](https://github.com/FBumann/farkas/commit/ecad71113f561f231388045dcdbe2b5b85fde416))

## [0.0.0-alpha.7](https://github.com/FBumann/linopy-yaml/compare/v0.0.0-alpha.6...v0.0.0-alpha.7) (2026-07-26)


### Refactoring

* the package moves under src/, so CI tests the artifact ([#118](https://github.com/FBumann/linopy-yaml/issues/118)) ([7eb39a6](https://github.com/FBumann/linopy-yaml/commit/7eb39a6229e86cd446fe476078de7bf8ef13c4ca))

## [0.0.0-alpha.6](https://github.com/FBumann/linopy-yaml/compare/v0.0.0-alpha.5...v0.0.0-alpha.6) (2026-07-26)


### Features

* accept any Arrow-compatible table as a source ([#104](https://github.com/FBumann/linopy-yaml/issues/104)) ([e8a699e](https://github.com/FBumann/linopy-yaml/commit/e8a699ede9cccc4bb688c7ed519c98c38d0992c3))


### Refactoring

* three modules in the engine, one per box in the diagram ([#107](https://github.com/FBumann/linopy-yaml/issues/107)) ([f6b30b2](https://github.com/FBumann/linopy-yaml/commit/f6b30b241cf2d438e7072200dfc169046a0c2d31))


### Documentation

* the seam's level is decided, so stop pointing at an open issue ([#111](https://github.com/FBumann/linopy-yaml/issues/111)) ([7d1a992](https://github.com/FBumann/linopy-yaml/commit/7d1a9929626ba8f48f56fac5bfad12770bed1d6e))

## [0.0.0-alpha.5](https://github.com/FBumann/linopy-yaml/compare/v0.0.0-alpha.4...v0.0.0-alpha.5) (2026-07-26)


### ⚠ BREAKING CHANGES

* a file declaring more than one objective no longer loads.
* a Series or DataArray whose index names are not the declared dims is now a DataError; previously the names were discarded.
* a dimension declares the coordinates its labels carry ([#100](https://github.com/FBumann/linopy-yaml/issues/100))
* every IR, AST and schema class is renamed, and `fk.LanguageError` no longer covers data-binding failures — those are `fk.DataError`. Both remain under `fk.LinopyYamlError`, as does the deprecated `RelationalBuildError` alias.

### Features

* a dimension declares the coordinates its labels carry ([#100](https://github.com/FBumann/linopy-yaml/issues/100)) ([49b790b](https://github.com/FBumann/linopy-yaml/commit/49b790b5bfa36e30dff617db8bda02876ad51757))


### Bug Fixes

* a bool parameter is a mask, on both lanes ([#47](https://github.com/FBumann/linopy-yaml/issues/47)) ([#96](https://github.com/FBumann/linopy-yaml/issues/96)) ([a3f3926](https://github.com/FBumann/linopy-yaml/commit/a3f39261500323329b2b902cf36e7ffbcb59ce5e))
* a declared coordinate must be its declared dtype ([#65](https://github.com/FBumann/linopy-yaml/issues/65)) ([#101](https://github.com/FBumann/linopy-yaml/issues/101)) ([5349991](https://github.com/FBumann/linopy-yaml/commit/5349991cfbdd7f3fe8da302a0a570573314395d2))
* a named index binds by name, not by position ([#91](https://github.com/FBumann/linopy-yaml/issues/91)) ([#98](https://github.com/FBumann/linopy-yaml/issues/98)) ([3e14be8](https://github.com/FBumann/linopy-yaml/commit/3e14be8a445d38d60cb664d04c033ce0dc2ddb42))
* a second objective is a load error, not a silent drop ([#49](https://github.com/FBumann/linopy-yaml/issues/49)) ([#97](https://github.com/FBumann/linopy-yaml/issues/97)) ([24f5849](https://github.com/FBumann/linopy-yaml/commit/24f58494bcf378018aed6c1c3c5f4f2bb96d3c06))
* one place per language rule, and formulations judged like the rest ([#99](https://github.com/FBumann/linopy-yaml/issues/99)) ([3137fbc](https://github.com/FBumann/linopy-yaml/commit/3137fbcc21b176392058c28ec48134b33cae9782))


### Refactoring

* names say what they mean, and errors are one tree ([#94](https://github.com/FBumann/linopy-yaml/issues/94)) ([0c1300c](https://github.com/FBumann/linopy-yaml/commit/0c1300c56f951448f8d765df6e2054c811bf57ac))
* the compat lane is a directory, so the fence is structural ([#95](https://github.com/FBumann/linopy-yaml/issues/95)) ([4015131](https://github.com/FBumann/linopy-yaml/commit/40151315e7dec4e489d0e7f0db03a24b5d1aba95))


### Documentation

* consolidate the doc set and cut it by two thirds ([#87](https://github.com/FBumann/linopy-yaml/issues/87)) ([0a89d7d](https://github.com/FBumann/linopy-yaml/commit/0a89d7d908df6e89bb987a3cf6a799a95ed61fa4))
* runnable architecture walkthrough, one stage at a time ([#54](https://github.com/FBumann/linopy-yaml/issues/54)) ([0f0910f](https://github.com/FBumann/linopy-yaml/commit/0f0910fc90e97eb918b06f53268b79aae6efa0d3))
* say plainly that breaking changes land without a deprecation cycle ([#102](https://github.com/FBumann/linopy-yaml/issues/102)) ([6131342](https://github.com/FBumann/linopy-yaml/commit/6131342ed345776239ba60709a462746e2140f93))
* split sink capability from the expressive ceiling ([#88](https://github.com/FBumann/linopy-yaml/issues/88)) ([4e58227](https://github.com/FBumann/linopy-yaml/commit/4e582271eb826be6dda353ef0e4c3786c8a2a4ab))
* the composition seam exists, and value-only re-solve has a precondition ([#93](https://github.com/FBumann/linopy-yaml/issues/93)) ([2dffd89](https://github.com/FBumann/linopy-yaml/commit/2dffd89250d1a3f68215e398ddca984d72a0705d))

## [0.0.0-alpha.4](https://github.com/FBumann/linopy-yaml/compare/v0.0.0-alpha.3...v0.0.0-alpha.4) (2026-07-25)


### ⚠ BREAKING CHANGES

* unknown YAML keys are an error, not a silent default ([#72](https://github.com/FBumann/linopy-yaml/issues/72))
* sum/roll/shift/group_sum over a dim the operand does not carry, a where dim outside the frame, a bound parameter dim outside foreach, and a constraint whose expression dims differ from its foreach are all load errors. Each previously built a model that solved and was wrong, or larger than the file read as.

### Features

* Result.to_xarray() — the labelled form, one call away ([#75](https://github.com/FBumann/linopy-yaml/issues/75)) ([7df73b4](https://github.com/FBumann/linopy-yaml/commit/7df73b4e75a1d1656b4b1a1d928d0e4bf5814a99))
* static dim checking — the type is a set of dim names ([#68](https://github.com/FBumann/linopy-yaml/issues/68)) ([f96bcb4](https://github.com/FBumann/linopy-yaml/commit/f96bcb4f12797514ef93afd1cd8e771cf8490d0b))


### Bug Fixes

* dim checking runs on both lanes, and binary ops union ([#70](https://github.com/FBumann/linopy-yaml/issues/70)) ([2072cfa](https://github.com/FBumann/linopy-yaml/commit/2072cfae4cfb42c3664d6d4a1e9eb3171e6cfb51))
* read YAML 1.2 booleans, and refuse duplicate keys ([#77](https://github.com/FBumann/linopy-yaml/issues/77)) ([b12af91](https://github.com/FBumann/linopy-yaml/commit/b12af91953f4730e0a9beb74643214bb52dd62d5))
* unknown YAML keys are an error, not a silent default ([#72](https://github.com/FBumann/linopy-yaml/issues/72)) ([909bc4c](https://github.com/FBumann/linopy-yaml/commit/909bc4c120d2a1ca341917971dbfb7851af35553))


### Documentation

* an objective totals its dims, so the examples stop pretending otherwise ([#74](https://github.com/FBumann/linopy-yaml/issues/74)) ([f090c8c](https://github.com/FBumann/linopy-yaml/commit/f090c8c6b214d633b2e14a34fa2aee3c8ff9a1e5))
* the axes the expressiveness taxonomy does not rank ([#76](https://github.com/FBumann/linopy-yaml/issues/76)) ([bcfcdee](https://github.com/FBumann/linopy-yaml/commit/bcfcdeebc6eb7a17e53076c5d75f7d0539d6bbf1))

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

* API polish — check(), write(), LanguageError, Result lifecycle ([#36](https://github.com/FBumann/linopy-yaml/issues/36)) ([fc36af5](https://github.com/FBumann/linopy-yaml/commit/fc36af515eb9baafdf81c036a27ad8ca9431297f))
