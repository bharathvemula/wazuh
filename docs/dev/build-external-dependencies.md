# Build External Dependencies

This document describes the GitHub Actions workflow that rebuilds Wazuh's vendored external dependencies (curl, openssl, rocksdb, …) across every platform we ship, and produces the consolidated `externals-all.tar.gz` blob that gets published to `packages.wazuh.com/deps/<DEPS_VERSION>/`.

Workflow file: `.github/workflows/5_builderpackage_externals.yml`

Supporting scripts under `packages/externals/`:

| File | Purpose |
|------|---------|
| `dependencies.json` | Inventory: version, revision, URL, sha256, archive format, patches, targets, platforms, CPE, purl and license of every `EXTERNAL_RES` dependency. |
| `deps.py` | Inventory tool: `check` (schema and names against `make print-EXTERNAL_RES`), `flatten` (bash arrays for the builder images; with `--seed`, entries unchanged since the `DEPS_VERSION` set point at that set's sources: same content fields, same hash of each patch and same overlay files, so editing, adding or deleting an overlay file rebuilds the entry), `readme` (README table), `sbom` (CycloneDX), `manifest` (set `manifest.json`), `drift` (inventory against the published manifest). |
| `build_external.sh` | Container-side build script. Downloads every dependency of the leg from the inventory, checks its sha256, keeps its `subdir`, copies its overlay, applies its patches, and runs the per-leg build; `libbpf-bootstrap` comes from the `build-ebpf` job and the embedded Python from its own set, see [Caveats](#caveats). |
| `generate_external.sh` | Wrapper that flattens the inventory with `--seed`, runs `build_external.sh` and packs the result into `externals-<leg>.tar.gz` with the S3 layout `make deps` expects. |
| `ebpf/build_ebpf.sh` | Builds `libbpf-bootstrap.tar.gz` (`modern.bpf.o` + `libbpf.so`) for amd64, aarch64, arm32, i386 and ppc64le on one x86_64 host, with clang 20 and Zig. See [libbpf-bootstrap](#libbpf-bootstrap-is-built-by-the-build-ebpf-job). |
| `smoke_build.sh` | Sanity check: builds the agent/manager from source against the freshly built consolidated tree to confirm the precompiled tarballs are actually consumable. |

## When to use it

- You changed `packages/externals/dependencies.json` (a version bump, a new dependency, a patch).
- You need to rebuild the whole set (e.g. after a toolchain change that affects how everything compiles).

The output of a successful run is the artifact you upload to `packages.wazuh.com/deps/<new-DEPS_VERSION>/`; the same PR then bumps `DEPS_VERSION` in `src/Makefile`, see [Publishing](#publishing-a-new-deps_version--the-safe-order).

## Running the workflow

From the Actions UI: pick **5.X - Package - Build external dependencies**, click **Run workflow**, choose the branch.

Or from the CLI:

```bash
# Build every dependency of the branch's dependencies.json for the set that will be published as 5/externals/<N>.
gh workflow run 5_builderpackage_externals.yml --ref <branch> -f deps_version=5/externals/<N>
```

### Inputs

| Input | Purpose | Default |
|-------|---------|---------|
| `deps_version` | Set this build will be published as, `5/externals/<number>` (e.g. `5/externals/2`); written into `manifest.json`. | `""` (`unassigned`) |
| `docker_image_tag` | GHCR builder image tag for the Linux legs. `auto` derives it from `VERSION.json`; `developer` uses the branch name; anything else is a literal tag. | `auto` |

There is intentionally no per-leg dispatch input. A deps release is whole-or-nothing — partial output would publish a tarball that breaks `make deps` on any platform whose leg is missing. To re-run a single failed leg, use GitHub's **Re-run failed jobs** on the workflow run.

## What runs

The `build-externals` matrix is fixed at 7 entries:

| Leg | Target | Runner | Notes |
|-----|--------|--------|-------|
| `rpm-amd64` | agent | `wz-linux-amd64` | CentOS 6 agent builder image (glibc 2.12 baseline). |
| `rpm-arm64` | agent | `wz-linux-arm64` | CentOS 6 agent builder image. |
| `macos-intel64` | agent | `macos-14-large` | Native macOS build. Agent-only. |
| `macos-arm64` | agent | `macos-14` | Native macOS build. Agent-only. |
| `windows-i686` | agent | `wz-linux-amd64` | MinGW cross-compile inside the `compile_windows_agent` image (ubuntu:22.04, same one the official windows agent build uses). Agent-only. |
| `rpm-amd64` | manager | `wz-linux-amd64` | CentOS 7 manager builder image (glibc 2.17). |
| `rpm-arm64` | manager | `wz-linux-arm64` | CentOS 7 manager builder image. |

Why each Linux arch runs twice: the manager image (CentOS 7) can build a couple of deps the agent image (CentOS 6) can't (newer toolchain), but the agent image's older glibc is the safe baseline for everything else. Both legs build their full dep set; where both compiled a dep, the `consolidate` job ships the agent-image copy, so we ship glibc-2.12-compatible binaries wherever possible.

We don't build separate `deb` legs because rpm glibc is forward-compatible with deb's.

A separate `build-ebpf` job runs `ebpf/build_ebpf.sh` on `ubuntu-24.04` for every Linux arch.

## Jobs

```
build-externals (matrix, 7 jobs) ─┐
                                  ├─► consolidate ──► smoke-build (5 jobs)
build-ebpf ───────────────────────┘
```

- **`build-externals`** — each leg takes the dependencies unchanged since the `DEPS_VERSION` set from that set's `libraries/sources/` (sha256 from its `manifest.json`) and downloads the new or changed ones from their inventory URL (sha256 checked, overlay copied and patches applied), runs the build, and uploads `externals-<leg>-<target>.tar.gz`. A download, checksum, `subdir`, overlay or patch failure fails the leg.
- **`build-ebpf`**: installs clang 20 (`apt.llvm.org`) and the Zig version pinned in `build_ebpf.sh`, runs it, and uploads `libbpf-bootstrap` (`<arch>/libbpf-bootstrap.tar.gz`).
- **`consolidate`** — downloads every per-leg tarball, merges into the canonical `libraries/{linux,darwin,windows,sources}/` layout, places the `build-ebpf` tarballs under `libraries/linux/<arch>/`, writes `manifest.json` (the inventory entries plus the sha256 of every file) with `deps.py manifest`, and uploads `externals-all.tar.gz` (the artifact you publish to `packages.wazuh.com/deps/<version>/`).
- **`smoke-build`** — for each of the 4 Linux combinations (amd64/arm64 × agent/manager) plus a windows-i686 leg, pulls `externals-all.tar.gz`, points `make deps RESOURCES_URL=file://…` at the local tree, then runs the real Wazuh build inside the matching builder image (`pkg_rpm_<target>_builder_<arch>` for Linux, `compile_windows_agent` for windows). Confirms the precompiled tarballs you just packed actually get consumed. Emits `::warning::` for any dep that fell back to source compile — that means the binary was packed at a path `src/external/CMakeLists.txt` doesn't expect. The windows leg is what catches host-side tools shipped in `libraries/windows/<dep>.tar.gz` (e.g. flatbuffers' `flatc`, invoked during `make TARGET=winagent` schema codegen) that were built against a newer glibc/libstdc++ than the consumer image — without it, that mismatch only surfaces downstream when the windows agent build runs.

## Output

Every job stores its output with the `upload_s3_artifact` action, in the CI's internal S3 bucket (`<repository>/<workflow>/<run_id>/<name>.zip`), not as a GitHub Actions artifact. Per-leg artifacts (`externals-rpm-amd64-agent`, `externals-macos-arm64-agent`, …) are kept there for debugging.

The artifact to publish is `externals-all` → `externals-all.tar.gz`. Its layout matches the S3 directory it gets uploaded into:

```
manifest.json                            ← what the set contains (deps.py manifest)
libraries/
├── linux/{amd64,aarch64}/<dep>.tar.gz   ← precompiled binaries
├── linux/{arm32,i386,ppc64le}/libbpf-bootstrap.tar.gz
├── darwin/{amd64,aarch64}/<dep>.tar.gz
├── windows/<dep>.tar.gz                 ← no arch subdir for MinGW
└── sources/<dep>.tar.gz                 ← source trees (the `geo_db` data snapshot and the trees the sets already published)
```

`make deps` walks this exact tree; the path layout is not negotiable. If you change `generate_external.sh`'s `S3_PATH` mapping, you must update `PRECOMPILED_RES` in `src/Makefile` to match (and vice versa).

Smoke build logs (`smoke-build-<target>-<arch>`) are uploaded the same way and are the first place to look when a downstream build starts pulling deps from source unexpectedly.

## Dependency matrix

Which dependency each platform/target actually builds and links, and how it is
published. The download set per target lives in `EXTERNAL_RES` (`src/Makefile`)
and the build/link guards in `src/external/CMakeLists.txt`. A dep is downloaded
for the targets that compile it, with three exceptions that are downloaded more
widely for their headers: bzip2 (every target), audit-userspace and procps (every
Linux target, manager included), and the test frameworks (every target).

Legend: ✔ built & linked · — not used. Targets: **La** Linux agent · **Lm**
Linux manager/server · **Ma** macOS agent · **Wa** Windows agent (MinGW).

### Universal (every platform and target)

| Dependency | La | Lm | Ma | Wa | Published as |
|------------|----|----|----|----|--------------|
| cJSON | ✔ | ✔ | ✔ | ✔ | precompiled `.a` (source-buildable fallback) |
| openssl | ✔ | ✔ | ✔ | ✔ | precompiled `.a` |
| zlib | ✔ | ✔ | ✔ | ✔ | precompiled `.a` (bundles minizip on non-Windows) |
| sqlite | ✔ | ✔ | ✔ | ✔ | precompiled `.a` (source-buildable fallback) |
| libyaml | ✔ | ✔ | ✔ | ✔ | precompiled `.a` |
| curl | ✔ | ✔ | ✔ | ✔ | precompiled `.a` |
| libpcre2 | ✔ | ✔ | ✔ | ✔ | precompiled `.a` |
| flatbuffers | ✔ | ✔ | ✔ | ✔ | precompiled `.a` + `flatc` |
| nlohmann | ✔ | ✔ | ✔ | ✔ | **source-only (header)** |
| zstd | ✔ | ✔ | ✔ | ✔ | precompiled `.a` |
| jwt-cpp | ✔ | ✔ | ✔ | ✔ | **source-only (header)** |
| rapidjson | ✔ | ✔ | ✔ | ✔ | **source-only (header)** |

### Shared build-time dependency (downloaded on all targets, linked non-Windows)

`shared.h` pulls in `shared/include/bzip2_op.h` → `<bzlib.h>` on every target (and
the bzip2 unit-test wrapper needs the header too), so the source is downloaded
everywhere — including the Windows agent. Only non-Windows targets link `libbz2`
(`external/CMakeLists.txt` builds `ext_bzip2` under `NOT IS_WINDOWS`).

| Dependency | La | Lm | Ma | Wa | Published as |
|------------|----|----|----|----|--------------|
| bzip2 | ✔ | ✔ | ✔ | ✔ | precompiled `.a` (linked on non-Windows only). Agent/server link it via `shared/src/bzip2_op.c`→`libwazuhext`; server also builds rocksdb (`WITH_BZ2`). |

### Linux agent only

Consumers are `data_provider`/sysinfo, `syscheckd` (whodata), `rootcheck` — all
agent-only subdirectories. The server's `wazuh_modules` builds
inventory_sync_server/keystore_server/vulnerability_scanner instead, so it links none of these.
audit-userspace and procps are nevertheless downloaded for the Linux manager too: the
unit-test wrappers under `src/unit_tests/wrappers/externals/` include `<libaudit.h>` and
`procps/readproc.h` on every Linux target, so only their headers are used there.

| Dependency | La | Lm | Ma | Wa | Published as |
|------------|----|----|----|----|--------------|
| audit-userspace | ✔ | — | — | — | precompiled `.a` (gated by `ENABLE_AUDIT`, agent-only) |
| procps | ✔ | — | — | — | precompiled `.a` (source-buildable fallback) |
| libdb | ✔ | — | — | — | precompiled `.a` |
| popt | ✔ | — | — | — | precompiled `.a` (rpm dependency) |
| lua | ✔ | — | — | — | precompiled `.a` (rpm dependency) |
| rpm | ✔ | — | — | — | precompiled `.a` |
| dbus | ✔ | — | — | — | precompiled `.a` |
| libbpf-bootstrap | ✔ | — | — | — | precompiled `.o` + `.so` (`build-ebpf` job) |

### macOS agent only

| Dependency | La | Lm | Ma | Wa | Published as |
|------------|----|----|----|----|--------------|
| libplist | — | — | ✔ | — | precompiled `.a` |

### Linux server (manager) only

| Dependency | La | Lm | Ma | Wa | Published as |
|------------|----|----|----|----|--------------|
| cpython | — | ✔ | — | — | own set (`5_builderpackage_embedded-python.yml`, `PYTHON_DEPS_VERSION`) |
| libffi | — | ✔ | — | — | precompiled `.a` (cpython/ctypes) |
| jemalloc | — | ✔ | — | — | precompiled `.so` |
| rocksdb | — | ✔ | — | — | precompiled `.so` |
| simdjson | — | ✔ | — | — | precompiled `.a` |
| abseil-cpp | — | ✔ | — | — | precompiled `.a` |
| re2 | — | ✔ | — | — | precompiled `.a` (needs abseil) |
| spdlog | — | ✔ | — | — | precompiled `.a` |
| yaml-cpp | — | ✔ | — | — | precompiled `.a` |
| pugixml | — | ✔ | — | — | precompiled `.a` |
| libmaxminddb | — | ✔ | — | — | precompiled `.a` |
| protobuf | — | ✔ | — | — | precompiled `.a` |
| date | — | ✔ | — | — | precompiled `.a` (needs curl) |
| fmt | — | ✔ | — | — | precompiled `.a` |
| minizip | — | ✔ | — | — | precompiled `.a` — **lives in the zlib tree**, built on the non-Windows legs (incl. agent) so it ships inside `zlib.tar.gz` |
| asio | — | ✔ | — | — | **source-only (header)** |
| expected-lite | — | ✔ | — | — | **source-only (header)** |
| llhttp | — | ✔ | — | — | precompiled `.a` |
| restinio | — | ✔ | — | — | **source-only (header)** |
| RxCpp | — | ✔ | — | — | **source-only (header)** |
| taskflow | — | ✔ | — | — | **source-only (header)** |
| concurrentqueue | — | ✔ | — | — | **source-only (header)** |
| fast_float | — | ✔ | — | — | **source-only (header)** |
| cpp-httplib | — | ✔ | — | — | **source-only (header)** |
| geo_db | — | ✔ | — | — | data blob (MaxMind GeoLite2), sources bucket |
| tzdata | — | ✔ | — | — | data (IANA tz), sources bucket |

### Test frameworks (downloaded on all targets, compiled only into test binaries)

These are *compiled* only when `UNIT_TEST`/`WAZUH_ENGINE_TEST` is set, but they are
*downloaded* unconditionally: the deps step (`make deps TARGET=…`) does not pass
`TEST=1`, and the unit-test CI consumes the same per-(os,arch) bundle, so gating
the download behind a flag drops them from the bundle and breaks every test build.

| Dependency | Downloaded for | Compiled into | Published as |
|------------|----------------|---------------|--------------|
| googletest | all targets | agent + server tests | precompiled `.a` |
| benchmark | all targets | server tests | precompiled `.a` |

> **Header-only deps** (nlohmann, jwt-cpp, cpp-httplib, rapidjson, RxCpp, taskflow,
> concurrentqueue, fast_float, asio, expected-lite, restinio) carry no compiled artifact — they belong only in
> `libraries/sources/`. The generation snapshot still copies their (binary-free)
> trees into `libraries/<os>/<arch>/`; pruning those redundant per-arch copies is
> tracked as follow-up work for #36247.

## Publishing a new DEPS_VERSION — the safe order

5.x publishes two kinds of sets, numbered from 1: `deps/5/externals/<n>` (this workflow, the C/C++ libraries) and `deps/5/python/<n>` (`5_builderpackage_embedded-python.yml`, the embedded Python and its wheels). `src/Makefile` points at one of each: `DEPS_VERSION = 5/externals/<n>` and `PYTHON_DEPS_VERSION = 5/python/<n>`. A set may be rebuilt while no merged branch points at it and the bucket policy does not cover it yet; after that it is never modified, and a change is the next number. The 4.x sets (`55`, `4.10.4`) and older ones (`54`, `99-37702`, …) stay where they are.

1. Open a branch and edit `packages/externals/dependencies.json` (version, revision, `url`, sha256 of the downloaded archive, patches). Run `python3 packages/externals/deps.py check` and `python3 packages/externals/deps.py readme`, and commit both files.
2. Take the next number (`aws s3 ls s3://…/deps/5/externals/`) and dispatch the workflow with `deps_version=5/externals/<N>`. Keep `DEPS_VERSION` at the currently published set while it runs, see the caveat below.
3. Wait for `build-externals`, `build-ebpf`, `consolidate`, and all `smoke-build` jobs to go green.
4. Download `externals-all.tar.gz` and upload its contents (`manifest.json` and `libraries/…`) to `s3://…/deps/5/externals/<N>/`, never over an existing key (`aws s3api put-object --if-none-match '*'`).
5. Set `DEPS_VERSION = 5/externals/<N>` in `src/Makefile` in the same PR. The drift check (`5_codequality_externals-drift.yml`) then compares the inventory with `deps/5/externals/<N>/manifest.json`.
6. Rebuild and publish the embedded Python against the new set in the same PR (see below): the drift check fails while the Python set was built against another `DEPS_VERSION`.

## Inventory fields

`deps.py check` is the only definition of the schema; this is what each field means. Entries are sorted by `name`.

| Field | Meaning |
|-------|---------|
| `name` | The name in `EXTERNAL_RES` and the directory under `src/external/`. |
| `version` | Public release whose code the content matches, verified by content: build files often carry a stale version. Failing that (rapidjson, date), the closest earlier release; geo_db, which is data, carries its date. |
| `revision` | Exact upstream tag or commit; for procps, which has no tags for 3.2.x, the release tarball name, and for geo_db the CTI export it came from. |
| `source` | `upstream` (the project's own release archive) or `snapshot` (a published tarball is the source; only `geo_db`, which is data, uses it, see [Dependencies with Wazuh changes](#dependencies-with-wazuh-changes)). |
| `url` | Download URL. Placeholders: `{version}`, `{version_us}` (dots to underscores), `{version_dash}` (dots to dashes), `{version_concat}` (sqlite's form, `3.51.1` → `3510100`) and `{revision}`. |
| `upstream_sha256` / `snapshot_sha256` | sha256 of the downloaded file, required by `source`: `curl -fsSL <url> \| sha256sum`. |
| `format`, `strip` | Archive format (`tar.gz`, `tar.bz2`, `tar.xz`, `zip`, or `file` for a single file) and the leading directories to strip. |
| `subdir` | Optional. Only this subdirectory of the extracted archive is kept (after `strip`); procps uses `proc`. A relative path without `..`. |
| `overlay` | Optional. Name of a directory under `packages/externals/overlay/`, copied over the tree. It must exist and hold at least one file. |
| `patches` | Files under `packages/externals/patches/`, applied in order with `git apply -p1`. |
| `targets`, `platforms` | `agent`/`manager` and `linux`/`darwin`/`windows`; `check` compares them with `make print-EXTERNAL_RES` for each target and platform. |
| `cpe` | Versionless CPE 2.3 (`cpe:2.3:a:<vendor>:<product>:*:…`), a list of them, or `null` together with `scan: false`. The version always comes from `version`. |
| `purl` | `pkg:github/<owner>/<repo>@<revision>` or `pkg:generic/<name>@<version>`, with a `/` in the version percent-encoded. |
| `scan`, `reason` | `scan: false` needs a `reason`, usually "no CPE in NVD". |
| `license`, `author`, `homepage` | Columns of the README table. |
| `notes`, `prebuilt` | Informational; the build does not read them. |

`build_external.sh` applies them in this order: extract, `subdir`, `overlay`, `patches`.

The `libbpf-bootstrap` `revision` must match `LIBBPF_TAG` in `ebpf/build_ebpf.sh`, and the `cpython` `version` must match `framework/.python-version`.

## Dependencies with Wazuh changes

Every dependency is built from its upstream release. What Wazuh changes lives in the repository, in one of four forms; pick the smallest that expresses the change.

**Build option.** A flag set in the dependency's rule in `src/external/CMakeLists.txt`: lua builds with an explicit `CFLAGS` without `-march=native`, libplist configures with `--without-cython`. To add one, edit the `BUILD_COMMAND`, `CONFIGURE_COMMAND` or `CMAKE_ARGS` of the rule; to remove it, delete the flag. The inventory does not change.

**Overlay.** A file of our own that replaces or completes one of upstream: rpm and popt ship their `CMakeLists.txt` this way. Put it at `packages/externals/overlay/<dir>/<path>` and set `overlay` to `<dir>` in the entry; the build copies the directory over the extracted tree. To remove it, delete the directory and the field.

**Patch.** A change to upstream code, in `packages/externals/patches/<lib>/NNNN-<name>.patch`, listed in `patches`: RxCpp, rpm (ndb), procps and date. The file is relative to the tree after `subdir`, and its header must carry `Why:`, `Upstream:` and `Remove when:` with a value each, before the first `diff`. To remove it, delete the file and its `patches` item, typically when `Remove when:` holds, for instance after a bump to a release that includes the change.

**Nothing.** libpcre2, flatbuffers, bzip2, audit-userspace and libyaml build as upstream ships them.

Only `geo_db`, which is data, stays a `source: snapshot`. `deps.py check` requires a snapshot to point under `packages.wazuh.com/deps/`, where its published file lives, and a snapshot is never the way to carry a code change.

When you bump a dependency, check that its overlay and patches still apply; `build_external.sh` fails the leg if a patch does not.

## Pull request checks

`5_codequality_externals-drift.yml` runs on pull requests that touch `packages/externals/**`, `src/Makefile`, `README.md`, `framework/requirements.txt`, `framework/.python-version` or the workflow itself:

1. The `deps.py` tests.
2. `deps.py check`, with MinGW installed so the Windows list is evaluated.
3. `deps.py readme --check`: the README table must be the one generated from the inventory.
4. `deps.py drift`: the inventory against `deps/$DEPS_VERSION/manifest.json`, which must name that set, on the fields that define the content (version, revision, source, URL, sha256, patches, overlay, subdir, targets, platforms), the hash of each patch and, per entry, the files of its overlay, and its `cpython` entry and `framework/requirements.txt` against `deps/$PYTHON_DEPS_VERSION/manifest.json`, whose `built_against` must be `DEPS_VERSION`. License, CPE or notes fixes need no new set. `drift` compares manifests, not the published files: `files` in each manifest holds their sha256 for an audit of the set.

In a pull request that bumps a dependency, `drift` fails until the new set is published and `DEPS_VERSION` points at it. That is what keeps a bump from merging before its set exists.

## Weekly vulnerability scan

`5_codeanalysis_externals-vulns.yml` runs on Mondays at 12:17 UTC, after the daily Grype database is published, and on demand, from the default branch. The weekly operational report reads its results on Tuesday at 06:00 UTC; if the Monday run fails, dispatch it again before then. For every versioned branch (`main`, `5.0.x`, `4.14.x`, …) with commits in the last 120 days, it builds the SBOM of the dependencies `make deps TARGET=manager` downloads (`deps.py sbom --target manager`, which also counts header-only and test dependencies, plus the wheels of `framework/requirements.txt`): from the branch's `dependencies.json`, or, on branches without one (4.x), from the `manifest.json` of the set its `DEPS_VERSION` points at; a branch whose set has no manifest is skipped with a notice. Grype scans it and the SARIF goes to code scanning under the category `externals-vulns-<branch>`. Agent-only dependencies are not scanned.

Grype applies no verdicts here: CVEs that do not affect a dependency are recorded and discounted by the weekly report that reads these results. A CVE that is fixed in a later version is not a verdict: bump the dependency instead.

## Caveats

### Bump `DEPS_VERSION` only after the new set is uploaded

The inventory decides every dependency source, but `DEPS_VERSION` (defined in `src/Makefile`) still decides what the workflow takes from the currently published set: the sources of every dependency unchanged since that set (`deps.py flatten --seed` compares the inventory with its `manifest.json`), and the `libbpf-bootstrap` source tree, fetched with `make EXTERNAL_SRC_ONLY=yes <goals>` together with the embedded Python (from `PYTHON_DEPS_VERSION`) and the other prerequisites of `make deps` (http-request, indexer templates, WCS file and credentials library, from GitHub).

If your branch points `DEPS_VERSION` at the set you're trying to *produce*, those fetches fail and the run fails. Dispatch the workflow while `DEPS_VERSION` points at the *currently published* set, and bump it once the new set is uploaded.

### `libbpf-bootstrap` is built by the `build-ebpf` job

`libbpf-bootstrap` needs clang ≥ 7 with the BPF backend and Linux UAPI headers ≥ 4.13 (`linux/bpf_perf_event.h`), and the legacy agent builder image (CentOS 6 / Debian wheezy era, glibc 2.12) has neither. There is no from-source fallback in `src/external/CMakeLists.txt`: the agent loads `libbpf.so` and `modern.bpf.o` at runtime, so its build does not need them, and `consolidate` ships the copy `build-ebpf` produced.

`ebpf/build_ebpf.sh` compiles `modern.bpf.o` from `src/syscheckd/src/ebpf/src/modern.bpf.c` of the dispatched branch with clang 20, and cross-compiles libbpf `v1.7.0` with Zig against glibc 2.17 (2.19 on ppc64le). Pinned versions (clang, Zig, libbpf tag, `libbpf/vmlinux.h` commit) live at the top of the script; the job reads the Zig version from there. The output is reproducible, so a rebuild of an unchanged source gives the same binaries.

To build it locally on `ubuntu:24.04` with `libelf-dev zlib1g-dev`, `apt.llvm.org/llvm.sh 20` and the pinned Zig:

```bash
bash packages/externals/ebpf/build_ebpf.sh   # writes ./output/<arch>/libbpf-bootstrap.tar.gz
```

### The embedded Python is its own set

`cpython` and the wheels of `framework/requirements.txt` are built by `5_builderpackage_embedded-python.yml` (`framework/cpython/compile.sh`) against the libraries of `DEPS_VERSION`, and published as `deps/5/python/<n>` (`libraries/sources/cpython_<arch>.tar.gz` and `libraries/linux/<arch>/cpython.tar.gz`). `make deps` downloads them from `PYTHON_RESOURCES_URL` (`PYTHON_DEPS_VERSION`), so a `RESOURCES_URL` override (a local mirror, `smoke_build.sh`) does not affect them: set `PYTHON_RESOURCES_URL` too to take Python from elsewhere. Manager only; the agent `EXTERNAL_RES` has no `$(CPYTHON)`. The externals workflow only fetches the Python tree for its configure step and does not ship it. Its inventory entry describes the upstream release that is scanned. To publish a new one (a wheel bump, a new Python, or new libraries):

1. Run `5_builderpackage_embedded-python.yml` with `python_deps_version=5/python/<N>`, with `DEPS_VERSION` already at the externals set it must link against.
2. Upload the contents of its `python-all.tar.gz` (`manifest.json`, with `built_against` and the wheels, and `libraries/…`) to `s3://…/deps/5/python/<N>/`, never over an existing key.
3. Set `PYTHON_DEPS_VERSION = 5/python/<N>` in `src/Makefile`. The drift check compares the inventory's `cpython` entry and `framework/requirements.txt` with that manifest, and its `built_against` with `DEPS_VERSION`.

### macOS and Windows are agent-only

There are no manager builder images for darwin or windows. The matrix reflects this; do not add manager entries for those systems.

### Source-rebuild fallbacks are silent

The precompiled-fetch rules in `src/Makefile` (`external-precompiled/%.tar.gz` and its cpython counterpart) are `-@ … || true` — a missing precompiled tarball is non-fatal and CMake falls back to source compilation. That means a packing-path bug (binary placed under `libraries/linux/amd64/` when `make deps` looked for `libraries/linux/x86_64/`, say) won't fail `make deps` outright. The smoke-build's "Analyze dependency usage" step is what catches this, by grepping the build log for `Performing build step for '<dep>_external'` / `Building … ext_<dep>.dir/` and emitting `::warning::` for each.

Treat any smoke-build warning as a blocker. The whole point of a deps release is that everything ships precompiled.

### Consolidate tie-breaking

On Linux, both the agent and manager legs build the agent dep set, so each Linux arch yields two copies of every agent dep. `consolidate` decides per `<dep>.tar.gz` from the build output itself, with no dependency list:

1. A copy that carries a compiled library (`.a`, `.so` or `.lib`) replaces one that carries only sources. This is what keeps a manager-only dep that the agent leg cannot compile (rocksdb, for example) coming from the manager leg.
2. Between two compiled copies the agent leg wins: manager legs are processed first and agent legs last, and an agent copy replaces an existing compiled one. Its CentOS 6 / glibc 2.12 binaries link in every builder image.

Source snapshots (`libraries/sources/`) are byte-identical across legs, so the first writer wins for those.

### Debugging a single failed leg

Use GitHub's **Re-run failed jobs** on the workflow run rather than dispatching a fresh run. Re-running a single matrix entry uses the same triggering branch + inputs and slots into the same overall run, so `consolidate` still runs against the full set when every leg has eventually succeeded.

If a leg keeps failing locally, you can reproduce it by pulling the same builder image from GHCR and running `packages/externals/generate_external.sh --system <sys> --architecture <arch> --target <agent|manager> --verbose` against a checkout of your branch.

## Related

- [Package generation](package-generation.md) — `generate_package.sh`, the script that turns built sources + deps into shipped `.rpm`/`.deb`.
- `src/Makefile` — `DEPS_VERSION`, `RESOURCES_URL`, `PRECOMPILED_RES`, `EXTERNAL_RES`.
- `src/external/CMakeLists.txt` — the consumer side; decides which deps short-circuit to precompiled archives vs build from source.
