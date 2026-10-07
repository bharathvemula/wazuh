# Run: python3 -m pytest packages/externals/tests -q

import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "deps.py"
INVENTORY = HERE.parent / "dependencies.json"
SRC = HERE.parents[2] / "src"

spec = importlib.util.spec_from_file_location("deps", SCRIPT)
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)

SHA = "a" * 64


def entry(name, **overrides):
    base = {
        "name": name, "version": "1.0.0", "revision": f"v1.0.0-{name}", "source": "upstream",
        "url": f"https://example.com/{name}-{{version}}.tar.gz", "upstream_sha256": SHA,
        "format": "tar.gz", "strip": 1, "patches": [], "targets": ["agent", "manager"],
        "platforms": ["linux"], "cpe": f"cpe:2.3:a:{name}:{name}:*:*:*:*:*:*:*:*",
        "purl": f"pkg:generic/{name}@1.0.0", "license": "MIT", "author": "upstream",
        "homepage": f"https://example.com/{name}",
    }
    base.update(overrides)
    return base


def doc(*entries):
    return {"schema": 1, "entries": list(entries)}


def minimal():
    return doc(entry("curl"), entry("openssl"), entry("zlib"))


def errors(document, external=None, patches_dir=None, repo_root=None, overlays_dir=None):
    return deps.validate(document, external, patches_dir or HERE, repo_root or deps.REPO, overlays_dir or HERE)


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_valid_minimal():
    assert errors(minimal()) == []


def test_external_res_match():
    external = {("agent", "linux"): {"curl", "openssl", "zlib"}, ("manager", "linux"): {"curl", "openssl", "zlib"}}
    assert errors(minimal(), external) == []


def test_external_res_extra():
    external = {("agent", "linux"): {"curl", "openssl"}}
    assert errors(minimal(), external) == ["zlib: declared for agent/linux but not in EXTERNAL_RES"]


def test_external_res_missing():
    external = {("manager", "linux"): {"curl", "openssl", "zlib", "sqlite"}}
    assert errors(minimal(), external) == ["sqlite: in EXTERNAL_RES for manager/linux but missing from inventory"]


def test_duplicate_name():
    assert errors(doc(entry("zlib"), entry("zlib"))) == ["zlib: duplicate name"]


def test_unsorted():
    assert errors(doc(entry("zlib"), entry("curl"))) == ["<inventory>: entries must be sorted by `name`"]


def test_upstream_requires_sha():
    item = entry("zlib")
    del item["upstream_sha256"]
    assert errors(doc(item)) == ["zlib: upstream entry needs `upstream_sha256`"]


def test_snapshot_requires_sha():
    item = entry("rpm", source="snapshot", url=f"{deps.MIRROR}/5/externals/1/libraries/sources/rpm.tar.gz")
    del item["upstream_sha256"]
    assert errors(doc(item)) == ["rpm: snapshot entry needs `snapshot_sha256`"]


def test_bad_sha():
    assert errors(doc(entry("zlib", upstream_sha256="a" * 63))) == ["zlib: `upstream_sha256` is not a lowercase sha256"]


def test_patch_missing(tmp_path):
    result = errors(doc(entry("date", patches=["date/0001-fix.patch"])), patches_dir=tmp_path)
    assert result == [f"date: patch 'date/0001-fix.patch' not found under {tmp_path}"]


def test_patch_present(tmp_path):
    (tmp_path / "date").mkdir()
    (tmp_path / "date" / "0001-fix.patch").write_text("")
    assert errors(doc(entry("date", patches=["date/0001-fix.patch"])), patches_dir=tmp_path) == []


def test_scan_false_needs_reason():
    assert errors(doc(entry("jemalloc", cpe=None, scan=False))) == ["jemalloc: `scan: false` requires `reason`"]


def test_null_cpe_needs_scan_false():
    assert errors(doc(entry("jemalloc", cpe=None))) == ["jemalloc: `cpe: null` requires `scan: false`"]


def test_cpe_with_version():
    result = errors(doc(entry("curl", cpe="cpe:2.3:a:haxx:curl:8.12.1:*:*:*:*:*:*:*")))
    assert result == ["curl: CPE 'cpe:2.3:a:haxx:curl:8.12.1:*:*:*:*:*:*:*' carries version '8.12.1'; versions belong in `version`"]


def test_cpe_list_and_malformed():
    good = ["cpe:2.3:a:procps_project:procps:*:*:*:*:*:*:*:*", "cpe:2.3:a:procps-ng_project:procps-ng:*:*:*:*:*:*:*:*"]
    assert errors(doc(entry("procps", cpe=good))) == []
    bad = errors(doc(entry("procps", cpe="cpe:2.3:a:procps_project:procps:*:*:*:*:*:*:*")))
    assert len(bad) == 1 and bad[0].startswith("procps: malformed CPE")


def test_unknown_field_and_types():
    result = errors(doc(entry("zlib", strip="1", colour="blue")))
    assert result == ["zlib: `strip` must be int", "zlib: unknown field `colour`"]


def test_unknown_target_and_platform():
    result = errors(doc(entry("zlib", targets=["server"], platforms=["beos"])))
    assert result == ["zlib: unknown target 'server'", "zlib: unknown platform 'beos'"]


def test_format_and_strip():
    result = errors(doc(entry("zlib", format="rar", strip=-1)))
    assert result == ["zlib: `format` must be one of ['file', 'tar.bz2', 'tar.gz', 'tar.xz', 'zip']",
                      "zlib: `strip` must be >= 0"]


def test_url_rules():
    assert errors(doc(entry("pcre2", url="gh:PCRE2Project/pcre2:pcre2-{version}"))) == [
        "pcre2: `url` must be an http(s) URL, got 'gh:PCRE2Project/pcre2:pcre2-{version}'"]
    assert errors(doc(entry("zlib", url="https://x/{foo}.tar.gz"))) == ["zlib: `url` uses unknown placeholder(s) foo"]
    assert errors(doc(entry("date", url="https://x/{revision}.tar.gz"))) == []


def test_removed_fields():
    assert errors(doc(entry("zlib", links=[], build=1, subpins={}))) == [
        "zlib: unknown field `build`", "zlib: unknown field `links`", "zlib: unknown field `subpins`"]


def pin_root(tmp_path, libbpf_tag="v1.7.0", python="3.12.14"):
    (tmp_path / "packages/externals/ebpf").mkdir(parents=True)
    (tmp_path / "packages/externals/ebpf/build_ebpf.sh").write_text(f'#!/bin/sh\nLIBBPF_TAG="{libbpf_tag}"\n')
    (tmp_path / "framework").mkdir()
    (tmp_path / "framework/.python-version").write_text(f"{python}\n")
    return tmp_path


def test_libbpf_tag_mismatch(tmp_path):
    root = pin_root(tmp_path, libbpf_tag="v1.6.0")
    result = errors(doc(entry("libbpf-bootstrap", version="1.7.0", revision="v1.7.0")), repo_root=root)
    assert result == ["libbpf-bootstrap: `revision` is 'v1.7.0' but packages/externals/ebpf/build_ebpf.sh pins 'v1.6.0'"]
    assert errors(doc(entry("libbpf-bootstrap", revision="v1.6.0")), repo_root=root) == []


def test_python_version_mismatch(tmp_path):
    root = pin_root(tmp_path, python="3.12.13")
    result = errors(doc(entry("cpython", version="3.12.14")), repo_root=root)
    assert result == ["cpython: `version` is '3.12.14' but framework/.python-version pins '3.12.13'"]


def test_pin_file_missing(tmp_path):
    result = errors(doc(entry("cpython", version="3.12.14")), repo_root=tmp_path)
    assert result == ["cpython: cannot read framework/.python-version to check `version`"]


def test_cli_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(minimal()))
    bad = tmp_path / "bad.json"
    broken = minimal()
    broken["entries"][0]["cpe"] = "cpe:2.3:a:haxx:curl:8.12.1:*:*:*:*:*:*:*"
    bad.write_text(json.dumps(broken))
    assert run("check", "--no-make", "--inventory", str(good)).returncode == 0
    result = run("check", "--no-make", "--inventory", str(bad))
    assert result.returncode == 1 and "ERROR: curl: CPE" in result.stderr
    assert run().returncode == 2


def overlay_tree(root, name, files):
    for rel, content in files.items():
        (root / name / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / name / rel).write_text(content)
    return root


@pytest.mark.parametrize("field,value,message", [
    ("overlay", "", "`overlay` must not be empty"),
    ("overlay", "../patches", "`overlay` '../patches' is not a directory name"),
    ("overlay", ".", "`overlay` '.' is not a directory name"),
    ("overlay", "..", "`overlay` '..' is not a directory name"),
    ("overlay", "absent", "overlay 'absent' not found or empty under {root}"),
    ("overlay", "empty", "overlay 'empty' not found or empty under {root}"),
    ("subdir", "", "`subdir` must not be empty"),
    ("subdir", "../x", "`subdir` '../x' must be a relative path without empty, . or .. segments"),
    ("subdir", "/x", "`subdir` '/x' must be a relative path without empty, . or .. segments"),
    ("subdir", ".", "`subdir` '.' must be a relative path without empty, . or .. segments"),
    ("subdir", "proc/.", "`subdir` 'proc/.' must be a relative path without empty, . or .. segments"),
])
def test_overlay_schema(field, value, message, tmp_path):
    root = overlay_tree(tmp_path, "rpm", {"sub/.keep": ""})
    (root / "empty" / "sub").mkdir(parents=True)
    assert errors(doc(entry("rpm", overlay="rpm", subdir="proc")), overlays_dir=root) == []
    result = errors(doc(entry("rpm", **{"overlay": "rpm", "subdir": "proc", field: value})), overlays_dir=root)
    assert result == ["rpm: " + message.format(root=root)]


def test_repo_inventory():
    assert run("check", "--no-make", "--inventory", str(INVENTORY)).returncode == 0
    entries = deps.load(INVENTORY)["entries"]
    assert [e["name"] for e in entries if e["source"] == "snapshot"] == ["geo_db"]
    assert [e["name"] for e in entries if e["source"] == "upstream" and "snapshot_sha256" in e] == []
    for e in entries:
        assert all((deps.PATCHES_DIR / p).is_file() for p in e["patches"]), e["name"]
        assert not e.get("overlay") or any(p.is_file() for p in (deps.OVERLAYS_DIR / e["overlay"]).rglob("*")), e["name"]


def test_patch_headers():
    patches = sorted(deps.PATCHES_DIR.rglob("*.patch"))
    assert patches and any(p.parent.name == "rpm" for p in patches)
    for patch in patches:
        text = patch.read_text()
        header = re.split(r"^(?:diff |--- )", text, maxsplit=1, flags=re.M)[0]
        for label in ("Why", "Upstream", "Remove when"):
            assert re.search(rf"^{label}:[ \t]*\S", header, re.M), f"{patch}: no {label}: before the first diff"
        if patch.parent.name == "rpm":
            assert "f85845c26cc8" in text and "CVE-" not in text, patch


@pytest.mark.skipif(shutil.which("make") is None or not (SRC / "Makefile").is_file(), reason="needs make and src/Makefile")
def test_repo_external_res(tmp_path):
    result = run("check", "--inventory", str(INVENTORY), "--src", str(SRC))
    assert result.returncode == 0, result.stderr
    trimmed = deps.load(INVENTORY)
    trimmed["entries"] = [e for e in trimmed["entries"] if e["name"] != "zlib"]
    path = tmp_path / "trimmed.json"
    path.write_text(json.dumps(trimmed))
    result = run("check", "--inventory", str(path), "--src", str(SRC))
    assert result.returncode == 1 and "zlib: in EXTERNAL_RES" in result.stderr


def bash_lookup(env_text, *expressions, tmp_path):
    env = tmp_path / "deps.env"
    env.write_text(env_text)
    script = f"source {env}; " + "; ".join(f"printf '%s\\n' \"{x}\"" for x in expressions)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.splitlines()


def test_flatten_expands_urls():
    document = doc(entry("asio", version="1.38.2", url="https://x/asio-{version_dash}.tar.gz"),
                   entry("date", revision="8a93211", url="https://x/{revision}.tar.gz"),
                   entry("sqlite", version="3.51.1", url="https://x/sqlite-{version_concat}-{version_us}.tar.gz"))
    text = deps.flatten(document)
    assert "[asio]=https://x/asio-1-38-2.tar.gz" in text
    assert "[date]=https://x/8a93211.tar.gz" in text
    assert "[sqlite]=https://x/sqlite-3510100-3_51_1.tar.gz" in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_flatten_sources_in_bash(tmp_path):
    result = run("flatten", "--inventory", str(INVENTORY))
    assert result.returncode == 0, result.stderr
    values = bash_lookup(result.stdout, "${EXT_SHA256[geo_db]}", "${EXT_TARGET[nlohmann]}", "${EXT_FORMAT[nlohmann]}",
                         "${EXT_SHA256[zlib]}", "${#EXT_URL[@]}", tmp_path=tmp_path)
    entries = {e["name"]: e for e in deps.load(INVENTORY)["entries"]}
    assert values == [entries["geo_db"]["snapshot_sha256"], "nlohmann", "file", entries["zlib"]["upstream_sha256"],
                      str(len(entries))]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_flatten_quotes(tmp_path):
    odd = "https://x/it's $HOME `id` {version}.tar.gz"
    text = deps.flatten(doc(entry("zlib", url=odd)))
    assert bash_lookup(text, "${EXT_URL[zlib]}", "${EXT_TARGET[zlib]}", tmp_path=tmp_path) == [
        "https://x/it's $HOME `id` 1.0.0.tar.gz", "zlib"]


def test_flatten_rejects_invalid(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc(entry("zlib", format="rar"))))
    result = run("flatten", "--inventory", str(bad))
    assert result.returncode == 1 and result.stdout == "" and "ERROR: zlib:" in result.stderr


README_TEXT = "# Wazuh\n\n<!-- deps-table:begin -->\nold\n<!-- deps-table:end -->\n\n* tail\n"


def test_readme_roundtrip(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(README_TEXT)
    inventory = tmp_path / "deps.json"
    inventory.write_text(json.dumps(minimal()))
    assert run("readme", "--inventory", str(inventory), "--readme", str(readme)).returncode == 0
    text = readme.read_text()
    assert text.startswith("# Wazuh\n\n<!-- deps-table:begin -->\n| Software") and text.endswith("<!-- deps-table:end -->\n\n* tail\n")
    assert "| [zlib](https://example.com/zlib)       | 1.0.0   | upstream | MIT     |" in text
    assert run("readme", "--inventory", str(inventory), "--readme", str(readme), "--check").returncode == 0


def test_readme_check_detects_edit(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(deps.render_readme(README_TEXT, minimal()).replace("zlib)       | 1.0.0", "zlib)       | 9.9.9"))
    inventory = tmp_path / "deps.json"
    inventory.write_text(json.dumps(minimal()))
    result = run("readme", "--inventory", str(inventory), "--readme", str(readme), "--check")
    assert result.returncode == 1 and "-| [zlib](https://example.com/zlib)       | 9.9.9" in result.stderr


def test_readme_needs_markers():
    with pytest.raises(ValueError, match="deps-table:begin"):
        deps.render_readme("# no table\n", minimal())


def test_drift_content_fields():
    published = {"entries": copy.deepcopy(minimal()["entries"])}
    published["entries"][2]["version"] = "1.0.1"
    assert deps.drift(minimal(), published) == ["zlib: `version` is '1.0.0' here but '1.0.1' in the set manifest"]
    published = {"entries": copy.deepcopy(minimal()["entries"])}
    published["entries"][2]["license"] = "Zlib"
    assert deps.drift(minimal(), published) == []


def test_drift_names():
    published = {"entries": minimal()["entries"][:2] + [entry("sqlite")]}
    assert deps.drift(minimal(), published) == ["zlib: in the inventory but not in the set manifest",
                                                "sqlite: in the set manifest but not in the inventory"]


def test_drift_missing_manifest(tmp_path):
    inventory = tmp_path / "deps.json"
    inventory.write_text(json.dumps(minimal()))
    result = run("drift", "--inventory", str(inventory), "--manifest", str(tmp_path / "nope.json"))
    assert result.returncode == 1 and "no manifest at" in result.stderr and "nope.json" in result.stderr


def test_seeds():
    files = {f"libraries/sources/{n}.tar.gz": c * 64 for n, c in (("curl", "1"), ("openssl", "2"))}
    published = {"entries": copy.deepcopy(minimal()["entries"]), "files": files}
    inventory = minimal()
    inventory["entries"][1]["version"] = "1.0.1"
    assert deps.seeds(inventory, published, "5/externals/1") == {
        "curl": (f"{deps.MIRROR}/5/externals/1/libraries/sources/curl.tar.gz", "1" * 64)}


def test_seeds_patch_edited_in_place(tmp_path):
    (tmp_path / "rpm").mkdir()
    patch = tmp_path / "rpm" / "0001-wazuh.patch"
    patch.write_text("old\n")
    inventory = doc(entry("rpm", patches=["rpm/0001-wazuh.patch"]))
    published = deps.manifest(inventory, "5/externals/1", patches_dir=tmp_path)
    published["files"] = {"libraries/sources/rpm.tar.gz": "e" * 64}
    assert list(deps.seeds(inventory, published, "5/externals/1", tmp_path)) == ["rpm"]
    patch.write_text("new\n")
    assert deps.seeds(inventory, published, "5/externals/1", tmp_path) == {}


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_flatten_seeded(tmp_path):
    inventory, published = tmp_path / "deps.json", tmp_path / "manifest.json"
    item = entry("zlib", format="zip", strip=2)
    inventory.write_text(json.dumps(doc(entry("curl"), item)))
    published.write_text(json.dumps({"entries": [item], "files": {"libraries/sources/zlib.tar.gz": "c" * 64}}))
    (tmp_path / "Makefile").write_text("DEPS_VERSION = 5/externals/7\n")
    result = run("flatten", "--inventory", str(inventory), "--seed", "--manifest", str(published), "--src", str(tmp_path))
    assert result.returncode == 0, result.stderr
    values = bash_lookup(result.stdout, "${EXT_URL[zlib]}", "${EXT_SHA256[zlib]}", "${EXT_FORMAT[zlib]}",
                         "${EXT_STRIP[zlib]}", "${EXT_PATCHES[zlib]}", "${EXT_URL[curl]}", tmp_path=tmp_path)
    assert values == [f"{deps.MIRROR}/5/externals/7/libraries/sources/zlib.tar.gz", "c" * 64, "set", "0", "",
                      "https://example.com/curl-1.0.0.tar.gz"]
    # The published tree already carries the patches.
    patched = doc(entry("rpm", patches=["rpm/0001.patch"]))
    assert "[rpm]=''" in deps.flatten(patched, {"rpm": ("https://x/rpm.tar.gz", "d" * 64)})


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_flatten_seeded_overlay_subdir(tmp_path):
    item = doc(entry("procps", overlay="procps", subdir="proc", patches=["procps/0001.patch"]))
    names = ("${EXT_OVERLAY[procps]}", "${EXT_SUBDIR[procps]}", "${EXT_PATCHES[procps]}")
    assert bash_lookup(deps.flatten(item), *names, tmp_path=tmp_path) == ["procps", "proc", "procps/0001.patch"]
    seeded = deps.flatten(item, {"procps": ("https://x/procps.tar.gz", "d" * 64)})
    assert bash_lookup(seeded, *names, tmp_path=tmp_path) == ["", "", ""]


def overlay_set(tmp_path):
    """popt, rpm and rpm-x (a name sharing rpm's prefix) with overlays, zlib without, and their set manifest."""
    root = overlay_tree(tmp_path / "overlay", "rpm", {"CMakeLists.txt": "rpm\n", "sub/config.h": "h\n"})
    overlay_tree(root, "popt", {"CMakeLists.txt": "popt\n"})
    overlay_tree(root, "rpm-x", {"CMakeLists.txt": "rpm-x\n"})
    inventory = doc(entry("popt", overlay="popt"), entry("rpm", overlay="rpm"), entry("rpm-x", overlay="rpm-x"),
                    entry("zlib"))
    published = deps.manifest(inventory, "5/externals/1", overlays_dir=root)
    published["files"] = {f"libraries/sources/{n}.tar.gz": SHA for n in ("popt", "rpm", "rpm-x", "zlib")}
    return inventory, published, root


OVERLAY_EDITS = {
    "edited": lambda root: (root / "rpm/sub/config.h").write_text("changed\n"),
    "file_removed": lambda root: (root / "rpm/sub/config.h").unlink(),
    "file_added": lambda root: (root / "rpm/sub/new.h").write_text("new\n"),
}


def seeds_after(tmp_path, edit):
    inventory, published, root = overlay_set(tmp_path)
    assert sorted(deps.seeds(inventory, published, "5/externals/1", overlays_dir=root)) == ["popt", "rpm", "rpm-x", "zlib"]
    OVERLAY_EDITS[edit](root)
    return sorted(deps.seeds(inventory, published, "5/externals/1", overlays_dir=root))


def test_seeds_overlay_edited(tmp_path):
    assert seeds_after(tmp_path, "edited") == ["popt", "rpm-x", "zlib"]


def test_seeds_overlay_file_removed(tmp_path):
    assert seeds_after(tmp_path, "file_removed") == ["popt", "rpm-x", "zlib"]


def test_seeds_overlay_file_added(tmp_path):
    assert seeds_after(tmp_path, "file_added") == ["popt", "rpm-x", "zlib"]


def drift_after(tmp_path, edit):
    inventory, published, root = overlay_set(tmp_path)
    assert deps.drift(inventory, published, overlays_dir=root) == []
    OVERLAY_EDITS[edit](root)
    return deps.drift(inventory, published, overlays_dir=root)


def test_drift_overlay_edited(tmp_path):
    assert drift_after(tmp_path, "edited") == [
        "rpm: overlay file rpm/sub/config.h differs from the one the set was built with"]


def test_drift_overlay_file_removed(tmp_path):
    assert drift_after(tmp_path, "file_removed") == [
        "rpm: overlay file rpm/sub/config.h differs from the one the set was built with"]


def test_drift_overlay_file_added(tmp_path):
    assert drift_after(tmp_path, "file_added") == [
        "rpm: overlay file rpm/sub/new.h differs from the one the set was built with"]


def test_seeds_overlay_dropped(tmp_path):
    inventory, published, root = overlay_set(tmp_path)
    # manifest() keeps the inventory's entry objects: change a copy.
    inventory["entries"][1] = {k: v for k, v in inventory["entries"][1].items() if k != "overlay"}
    assert sorted(deps.seeds(inventory, published, "5/externals/1", overlays_dir=root)) == ["popt", "rpm-x", "zlib"]


def test_seeds_manifest_without_overlays(tmp_path):
    root = overlay_tree(tmp_path, "rpm", {"CMakeLists.txt": "rpm\n"})
    inventory = doc(entry("curl"), entry("rpm", overlay="rpm"), entry("zlib"))
    # A set published before overlays existed, such as 99-37702.
    published = deps.manifest(inventory, "5/externals/1", overlays_dir=root)
    del published["overlays"]
    published["files"] = {f"libraries/sources/{n}.tar.gz": SHA for n in ("curl", "rpm", "zlib")}
    assert sorted(deps.seeds(inventory, published, "5/externals/1", overlays_dir=root)) == ["curl", "zlib"]


def test_manifest_overlays(tmp_path):
    root = overlay_tree(tmp_path, "rpm", {"sub/config.h": "h\n", ".keep": ""})
    result = deps.manifest(doc(entry("rpm", overlay="rpm"), entry("zlib")), "5/externals/1", overlays_dir=root)
    assert result["overlays"] == {"rpm/.keep": hashlib.sha256(b"").hexdigest(),
                                  "rpm/sub/config.h": hashlib.sha256(b"h\n").hexdigest()}


def test_sbom_real_tree(tmp_path):
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    assert run("sbom", "--output", str(out1)).returncode == 0
    assert run("sbom", "--output", str(out2)).returncode == 0
    assert out1.read_bytes() == out2.read_bytes()
    bom = json.loads(out1.read_text())
    assert (bom["bomFormat"], bom["specVersion"]) == ("CycloneDX", "1.5")
    refs = [c["bom-ref"] for c in bom["components"]]
    assert refs == sorted(refs) and len(set(refs)) == len(refs)
    pypi = [c for c in bom["components"] if c["purl"].startswith("pkg:pypi/")]
    requirements = deps.read_requirements(deps.REQUIREMENTS)
    assert len(pypi) == len(requirements) and len(bom["components"]) == len(requirements) + len(deps.load(INVENTORY)["entries"])
    cpython = next(c for c in bom["components"] if c["bom-ref"] == "cpython")
    assert cpython["cpe"].startswith("cpe:2.3:a:python:python:*")
    assert all("version" in c and c["properties"][0]["name"] == "syft:location:0:path" for c in bom["components"])


def test_sbom_from_manifest(tmp_path):
    published = tmp_path / "manifest.json"
    published.write_text(json.dumps(deps.manifest(doc(entry("zlib", cpe=None, scan=False, reason="none")), "55")))
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("# comment\nPyYAML==6.0.1\n")
    result = run("sbom", "--manifest", str(published), "--requirements", str(requirements))
    bom = json.loads(result.stdout)
    assert [c["bom-ref"] for c in bom["components"]] == ["pypi:pyyaml", "zlib"]
    assert "cpe" not in bom["components"][1]


def test_sbom_target(tmp_path):
    inventory = tmp_path / "deps.json"
    inventory.write_text(json.dumps(doc(entry("rpm", targets=["agent"]), entry("zlib"), entry("rocksdb", targets=["manager"]))))
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("PyYAML==6.0.1\n")
    result = run("sbom", "--inventory", str(inventory), "--requirements", str(requirements), "--target", "manager")
    assert [c["bom-ref"] for c in json.loads(result.stdout)["components"]] == ["pypi:pyyaml", "rocksdb", "zlib"]
    published = tmp_path / "manifest.json"
    published.write_text(json.dumps({"entries": [{k: v for k, v in entry("zlib").items() if k != "targets"}]}))
    result = run("sbom", "--manifest", str(published), "--requirements", str(requirements), "--target", "manager")
    assert result.returncode == 1 and "--target needs `targets` in every entry" in result.stderr


def test_manifest_files(tmp_path):
    (tmp_path / "libraries" / "sources").mkdir(parents=True)
    (tmp_path / "libraries" / "sources" / "zlib.tar.gz").write_bytes(b"z")
    (tmp_path / "libraries" / "windows.tar.gz").write_bytes(b"w")
    result = deps.manifest(minimal(), "56", "c" * 40, "123", tmp_path)
    assert result["entries"] == minimal()["entries"] and result["deps_version"] == "56"
    assert list(result["files"]) == ["libraries/sources/zlib.tar.gz", "libraries/windows.tar.gz"]
    assert result["files"]["libraries/windows.tar.gz"] == hashlib.sha256(b"w").hexdigest()
    assert deps.drift(minimal(), result) == []


def test_sbom_skips_unpinned_requirements(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("PyYAML==6.0.1\nchardet>=3.0.4\n")
    inventory = tmp_path / "deps.json"
    inventory.write_text(json.dumps(doc(entry("zlib"))))
    result = run("sbom", "--inventory", str(inventory), "--requirements", str(requirements))
    assert result.returncode == 0 and "WARNING:" in result.stderr and "chardet>=3.0.4" in result.stderr
    assert [c["bom-ref"] for c in json.loads(result.stdout)["components"]] == ["pypi:pyyaml", "zlib"]


def test_sbom_skips_validation(tmp_path):
    inventory = tmp_path / "deps.json"
    inventory.write_text(json.dumps(doc(entry("cpython", version="3.0.0"))))
    assert run("check", "--no-make", "--inventory", str(inventory)).returncode == 1
    result = run("sbom", "--inventory", str(inventory))
    assert result.returncode == 0 and json.loads(result.stdout)["components"][0]["version"] == "3.0.0"


def with_cpython():
    return doc(entry("cpython", version="3.12.14"), entry("openssl"), entry("zlib"))


def python_manifest(**overrides):
    result = deps.manifest(with_cpython(), "5/python/1",
                           python={"built_against": "5/externals/1", "requirements": [("pyyaml", "6.0.1")]})
    result.update(overrides)
    return result


def test_manifest_excludes_cpython():
    assert [e["name"] for e in deps.manifest(with_cpython(), "5/externals/1")["entries"]] == ["openssl", "zlib"]


def test_python_manifest():
    result = python_manifest()
    assert [e["name"] for e in result["entries"]] == ["cpython"]
    assert (result["built_against"], result["requirements"]) == ("5/externals/1", ["pyyaml==6.0.1"])
    assert deps.drift_python(with_cpython(), result, [("pyyaml", "6.0.1")], "5/externals/1") == []
    assert deps.drift(with_cpython(), deps.manifest(with_cpython(), "5/externals/1")) == []


def test_drift_python_built_against():
    assert deps.drift_python(with_cpython(), python_manifest(), [("pyyaml", "6.0.1")], "5/externals/2") == [
        "cpython: the Python set was built against '5/externals/1', but DEPS_VERSION is '5/externals/2'; rebuild it"]


def test_drift_python_requirements():
    result = deps.drift_python(with_cpython(), python_manifest(), [("pyyaml", "6.0.2"), ("idna", "3.7")], "5/externals/1")
    assert result == ["cpython: idna==3.7 is in framework/requirements.txt but not in the Python set",
                      "cpython: pyyaml==6.0.2 is in framework/requirements.txt but not in the Python set",
                      "cpython: pyyaml==6.0.1 is in the Python set but not in framework/requirements.txt"]


def test_drift_python_content():
    newer = doc(entry("cpython", version="3.12.15"), entry("openssl"), entry("zlib"))
    assert deps.drift_python(newer, python_manifest(), [("pyyaml", "6.0.1")], "5/externals/1") == [
        "cpython: `version` is '3.12.15' here but '3.12.14' in the set manifest"]


def test_deps_version_vars(tmp_path):
    (tmp_path / "Makefile").write_text("PYTHON_DEPS_VERSION = 5/python/1\nDEPS_VERSION = 5/externals/1\n")
    assert deps.deps_version(tmp_path) == "5/externals/1"
    assert deps.deps_version(tmp_path, "PYTHON_DEPS_VERSION") == "5/python/1"


def test_manifest_python_needs_built_against(tmp_path):
    inventory = tmp_path / "deps.json"
    inventory.write_text(json.dumps(with_cpython()))
    result = run("manifest", "--inventory", str(inventory), "--deps-version", "5/python/1", "--python")
    assert result.returncode == 1 and "--built-against" in result.stderr


def test_drift_targets_and_platforms():
    published = {"entries": copy.deepcopy(minimal()["entries"]), "patches": {}}
    published["entries"][2]["platforms"] = ["linux", "darwin"]
    assert deps.drift(minimal(), published) == [
        "zlib: `platforms` is ['linux'] here but ['linux', 'darwin'] in the set manifest"]


def test_drift_patch_edited_in_place(tmp_path):
    (tmp_path / "rpm").mkdir()
    patch = tmp_path / "rpm" / "0001-wazuh.patch"
    patch.write_text("old\n")
    inventory = doc(entry("rpm", patches=["rpm/0001-wazuh.patch"]))
    published = deps.manifest(inventory, "5/externals/1", patches_dir=tmp_path)
    assert deps.drift(inventory, published, patches_dir=tmp_path) == []
    patch.write_text("new\n")
    assert deps.drift(inventory, published, patches_dir=tmp_path) == [
        "rpm: patch rpm/0001-wazuh.patch differs from the one the set was built with"]


def test_flatten_checksum_follows_source():
    item = entry("procps", source="snapshot", snapshot_sha256="b" * 64)
    assert "[procps]=" + "b" * 64 in deps.flatten(doc(item))


def test_purl_rules():
    assert errors(doc(entry("llhttp", purl="pkg:github/nodejs/llhttp@release%2Fv9.4.2"))) == []
    assert errors(doc(entry("llhttp", purl="pkg:github/nodejs/llhttp@release/v9.4.2"))) == [
        "llhttp: `purl` 'pkg:github/nodejs/llhttp@release/v9.4.2' is not pkg:<type>/<name>@<version> with an encoded version"]


def test_sbom_extra_cpes():
    item = entry("procps", cpe=["cpe:2.3:a:procps_project:procps:*:*:*:*:*:*:*:*",
                                "cpe:2.3:a:procps-ng_project:procps-ng:*:*:*:*:*:*:*:*"])
    component = deps.sbom([item], [])["components"][0]
    assert component["cpe"].startswith("cpe:2.3:a:procps_project")
    assert {"name": "syft:cpe23", "value": "cpe:2.3:a:procps-ng_project:procps-ng:*:*:*:*:*:*:*:*"} in component["properties"]


def test_linux_combos_force_uname():
    assert all("uname_S=Linux" in args for (target, platform), args in deps.MAKE_COMBOS.items() if platform == "linux")


def test_snapshot_url_on_mirror():
    published = f"{deps.MIRROR}/5/externals/1/libraries/sources/rpm.tar.gz"
    assert errors(doc(entry("rpm", source="snapshot", snapshot_sha256=SHA, url=published))) == []
    assert errors(doc(entry("rpm", source="snapshot", snapshot_sha256=SHA,
                            url="https://github.com/rpm-software-management/rpm/archive/rpm-{version}-release.tar.gz"))) == [
        f"rpm: snapshot entry must point under {deps.MIRROR}/, where its published tree lives"]


CHANGED = {"version": "1.0.1", "revision": "v1.0.1-zlib", "source": "snapshot", "url": "https://example.com/other.tar.gz",
           "upstream_sha256": "b" * 64, "snapshot_sha256": "c" * 64, "patches": ["zlib/0001-wazuh.patch"],
           "overlay": "zlib", "subdir": "proc", "targets": ["agent"], "platforms": ["darwin"]}


def patched(tmp_path):
    (tmp_path / "zlib").mkdir(exist_ok=True)
    (tmp_path / "zlib" / "0001-wazuh.patch").write_text("patch\n")
    overlay_tree(tmp_path / "overlay", "zlib", {"CMakeLists.txt": "zlib\n"})
    return tmp_path


# Literal copies: a field dropped from deps.py must fail here, not just lose its case.
SEED = ("version", "revision", "source", "url", "upstream_sha256", "snapshot_sha256", "patches", "overlay", "subdir")
CONTENT = SEED + ("targets", "platforms")


def test_field_lists():
    assert (deps.SEED_FIELDS, deps.CONTENT_FIELDS) == (SEED, CONTENT)


@pytest.mark.parametrize("field", SEED)
def test_seeds_every_field(field, tmp_path):
    published = deps.manifest(minimal(), "5/externals/1", patches_dir=patched(tmp_path))
    published["files"] = {f"libraries/sources/{n}.tar.gz": SHA for n in ("curl", "openssl", "zlib")}
    inventory = minimal()
    inventory["entries"][2][field] = CHANGED[field]
    assert sorted(deps.seeds(inventory, published, "5/externals/1", tmp_path, tmp_path / "overlay")) == ["curl", "openssl"]


def test_seeds_patch_removed(tmp_path):
    with_patch = minimal()
    with_patch["entries"][2]["patches"] = ["zlib/0001-wazuh.patch"]
    published = deps.manifest(with_patch, "5/externals/1", patches_dir=patched(tmp_path))
    published["files"] = {f"libraries/sources/{n}.tar.gz": SHA for n in ("curl", "openssl", "zlib")}
    # The set ships zlib already patched; dropping the patch must download it again.
    assert sorted(deps.seeds(minimal(), published, "5/externals/1", tmp_path)) == ["curl", "openssl"]


@pytest.mark.parametrize("field", CONTENT)
def test_drift_every_content_field(field, tmp_path):
    inventory = minimal()
    inventory["entries"][2][field] = CHANGED[field]
    published = deps.manifest(minimal(), "5/externals/1")
    assert [e for e in deps.drift(inventory, published, patches_dir=patched(tmp_path), overlays_dir=tmp_path / "overlay")
            if e.startswith(f"zlib: `{field}`")] == [
        f"zlib: `{field}` is {CHANGED[field]!r} here but {minimal()['entries'][2].get(field)!r} in the set manifest"]


@pytest.mark.parametrize("field,value", [("license", "Zlib"), ("cpe", None), ("purl", "pkg:generic/zlib@1"),
                                         ("notes", "n"), ("author", "a"), ("homepage", "https://zlib.net")])
def test_drift_ignores_metadata(field, value):
    inventory = minimal()
    inventory["entries"][2][field] = value
    assert deps.drift(inventory, deps.manifest(minimal(), "5/externals/1")) == []


def test_drift_set_version():
    published = deps.manifest(minimal(), "unassigned")
    assert deps.drift(minimal(), published, set_version="5/externals/1") == [
        "<manifest>: it describes set 'unassigned', not '5/externals/1'"]
    assert deps.drift(minimal(), deps.manifest(minimal(), "5/externals/1"), set_version="5/externals/1") == []


def test_drift_cli(tmp_path):
    (tmp_path / "Makefile").write_text("DEPS_VERSION = 5/externals/1\nPYTHON_DEPS_VERSION = 5/python/1\n")
    (tmp_path / "requirements.txt").write_text("pyyaml==6.0.1\n")
    inventory, externals, python = tmp_path / "deps.json", tmp_path / "externals.json", tmp_path / "python.json"
    inventory.write_text(json.dumps(with_cpython()))
    externals.write_text(json.dumps(deps.manifest(with_cpython(), "5/externals/1")))
    args = ("drift", "--inventory", str(inventory), "--manifest", str(externals), "--python-manifest", str(python),
            "--requirements", str(tmp_path / "requirements.txt"), "--src", str(tmp_path))
    python.write_text(json.dumps(python_manifest()))
    assert run(*args).returncode == 0
    python.write_text(json.dumps(python_manifest(built_against="5/externals/0")))
    result = run(*args)
    assert result.returncode == 1 and "the Python set was built against '5/externals/0'" in result.stderr
    python.write_text(json.dumps(python_manifest(deps_version="5/python/2")))
    result = run(*args)
    assert result.returncode == 1 and "it describes set '5/python/2', not '5/python/1'" in result.stderr
    python.write_text(json.dumps(python_manifest()))
    externals.write_text(json.dumps(deps.manifest(with_cpython(), "5/externals/0")))
    result = run(*args)
    assert result.returncode == 1 and "it describes set '5/externals/0', not '5/externals/1'" in result.stderr


BUILD_EXTERNAL = HERE.parent / "build_external.sh"


def shell_functions(*names):
    """The bodies of build_external.sh functions, to test them without running the leg."""
    text = BUILD_EXTERNAL.read_text()
    bodies = []
    for name in names:
        match = re.search(rf"^{name}\(\) \{{(?: [^\n]*\}}\n|\n.*?^\}}\n)", text, re.M | re.S)
        assert match, f"{name}() not found in {BUILD_EXTERNAL}"
        bodies.append(match.group(0))
    return "".join(bodies)


def tarball(path, members, compress="gz"):
    with tarfile.open(path, f"w:{compress}") as archive:
        for member, content in members.items():
            info = tarfile.TarInfo(member)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


def fetch(tmp_path, dep, seeded=None):
    env = tmp_path / "deps.env"
    env.write_text(deps.flatten(doc(dep), seeded))
    script = shell_functions("log", "err", "download", "extract", "sha256_of", "fetch_dep")
    script += f"source {shlex.quote(str(env))}\nfetch_dep {dep['name']}\n"
    external = tmp_path / "external"
    external.mkdir(exist_ok=True)
    # Own process group: a regression that copies the wrong tree is killed whole on timeout.
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True,
                            env={"PATH": os.environ["PATH"], "WAZUH_SRC": str(tmp_path),
                                 "EXTERNAL_DIR": str(external), "DOWNLOAD_DIR": str(tmp_path / "dl")})
    try:
        stdout, stderr = proc.communicate(timeout=20)
    except BaseException:
        # Also on Ctrl-C: the new session does not get the terminal's SIGINT.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr), external / dep["name"]


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


needs_shell = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("curl")), reason="needs bash and curl")


@needs_shell
def test_fetch_dep_checks_sha256(tmp_path):
    archive = tarball(tmp_path / "zlib-1.0.0.tar.gz", {"zlib-1.0.0/zlib.h": b"zlib\n"})
    result, tree = fetch(tmp_path, entry("zlib", url=archive.as_uri(), upstream_sha256=sha_of(archive)))
    assert result.returncode == 0, result.stderr
    assert (tree / "zlib.h").read_text() == "zlib\n"
    result, tree = fetch(tmp_path, entry("zlib", url=archive.as_uri(), upstream_sha256="b" * 64))
    assert result.returncode != 0 and "sha256 mismatch for zlib" in result.stderr


@needs_shell
def test_fetch_dep_seeded_set(tmp_path):
    good = tarball(tmp_path / "good.tar.gz", {"./zlib/zlib.h": b"seeded\n"})
    result, tree = fetch(tmp_path, entry("zlib"), {"zlib": (good.as_uri(), sha_of(good))})
    assert result.returncode == 0, result.stderr
    assert (tree / "zlib.h").read_text() == "seeded\n"
    bad = tarball(tmp_path / "bad.tar.gz", {"other/zlib.h": b"x\n"})
    result, _ = fetch(tmp_path, entry("zlib"), {"zlib": (bad.as_uri(), sha_of(bad))})
    assert result.returncode != 0 and "has no zlib/ directory" in result.stderr


@needs_shell
@pytest.mark.skipif(shutil.which("unzip") is None, reason="needs unzip")
def test_fetch_dep_broken_zip(tmp_path):
    archive = tmp_path / "zlib.zip"
    archive.write_bytes(b"not a zip")
    result, tree = fetch(tmp_path, entry("zlib", url=archive.as_uri(), upstream_sha256=sha_of(archive), format="zip",
                                         strip=0))
    assert result.returncode != 0 and not tree.exists()


@needs_shell
@pytest.mark.skipif(shutil.which("zip") is None, reason="needs zip")
def test_fetch_dep_zip_without_top_directory(tmp_path):
    (tmp_path / "flat").mkdir()
    (tmp_path / "flat" / "zlib.h").write_text("flat\n")
    archive = tmp_path / "zlib.zip"
    subprocess.run(["zip", "-qj", str(archive), str(tmp_path / "flat" / "zlib.h")], check=True)
    result, tree = fetch(tmp_path, entry("zlib", url=archive.as_uri(), upstream_sha256=sha_of(archive), format="zip"))
    assert result.returncode != 0 and "no directory to strip" in result.stderr and not tree.exists()


def fetch_procps(tmp_path, **overrides):
    """procps from pkg-1/{proc/a.c,x}, cut to proc/, overlaid and patched."""
    archive = tarball(tmp_path / "procps-1.tar.gz", {"pkg-1/proc/a.c": b"upstream\n", "pkg-1/x": b"x\n"})
    externals = tmp_path / "packages" / "externals"
    overlay_tree(externals / "overlay", "procps", {"a.c": "overlay\n", "sub/b.h": "b\n", ".keep": ""})
    overlay_tree(externals / "patches", "procps", {"0001.patch": "Why: test\n\ndiff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n"
                                                                  "@@ -1 +1 @@\n-overlay\n+patched\n"})
    item = entry("procps", url=archive.as_uri(), upstream_sha256=sha_of(archive), subdir="proc", overlay="procps",
                 patches=["procps/0001.patch"])
    return fetch(tmp_path, dict(item, **overrides))


@needs_shell
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_fetch_dep_subdir_overlay_patch(tmp_path):
    result, tree = fetch_procps(tmp_path)
    assert result.returncode == 0, result.stderr
    assert sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")) == [".keep", "a.c", "sub", "sub/b.h"]
    assert (tree / "a.c").read_text() == "patched\n"
    assert "keeping proc/ of procps" in result.stdout and "copying overlay/procps/ over procps" in result.stdout


@needs_shell
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_fetch_dep_patch_does_not_apply(tmp_path):
    externals = tmp_path / "packages" / "externals"
    overlay_tree(externals / "patches", "procps", {"0002.patch": "Why: test\n\ndiff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n"
                                                                  "@@ -1 +1 @@\n-nothing like this\n+patched\n"})
    result, tree = fetch_procps(tmp_path, patches=["procps/0002.patch"])
    assert result.returncode != 0 and "patch procps/0002.patch does not apply to procps" in result.stderr
    assert not tree.exists()


@needs_shell
def test_fetch_dep_missing_subdir(tmp_path):
    result, tree = fetch_procps(tmp_path, subdir="nope")
    assert result.returncode != 0 and "nope/ not found" in result.stderr and not tree.exists()


@needs_shell
def test_fetch_dep_missing_overlay(tmp_path):
    result, tree = fetch_procps(tmp_path, overlay="absent")
    assert result.returncode != 0 and "cannot copy overlay/absent/" in result.stderr and not tree.exists()
