"""白名单构建便携包；只从固定上游包和显式引擎目录取文件，不打包工作目录。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OC = "成熟Agent接入/OpenCode"
BASE = "本地模型配置与日志/Strata验证"
SOURCES = ["bootstrap.py", "toolbox.py", "python-dependencies.json",
           "3_CTF做题界面.bat", "7_自检.bat", "先读我.md"]
SOURCES += [f"{OC}/{name}" for name in (
    "opencode_runtime.py", "opencode_engine.py", "opencode_safety.py", "owned_process_job.py",
    "ctf-context.mjs", "ctf-context-64k.mjs", "ctf-compaction.mjs", "compaction-cache.mjs",
    "ctf-tools.mjs", "session-archive.mjs", "opencode.json", "64K摘要输出约束_分词器.template.json")]
SOURCES += [f"{OC}/v2/{name}" for name in ("package.json", "cli.json", "session-policy.mjs", "ctf-tui.mjs", "ctf-server.mjs",
    "ctf-center.mjs", "control.mjs", "settings.py", "controller.py", "launch.py", "prepared.py", "status.mjs", "journal.mjs", "activity.mjs")]
SOURCES += [f"{BASE}/{name}" for name in (
    "ctf_script_runner.py", "working_set_guard.py", "strata-feed.json", "strata-feed.json.sig", "官方模型哈希.json")]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def native(file):
    file = Path(file).absolute()
    cfg = json.loads((ROOT / f"portable/{OC}/v2/cli.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "script/native-lock.json").read_text(encoding="utf-8"))
    if cfg["version"] != lock["version"] or cfg["upstream"] != lock["upstream"]["version"]:
        raise ValueError("定制CLI声明与源码构建锁不符")
    if not file.is_file() or file.is_symlink() or file.resolve() != file or digest(file) != cfg["sha256"]:
        raise ValueError("必须提供已核验的CTFCode定制CLI；不打包原版或全局CLI")
    return cfg


def safe(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError("归档路径不安全：" + name)
    return path


def fetch(url, path):
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "CTFCode-portable"}), timeout=60) as src:
        with path.with_suffix(path.suffix + ".part").open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    path.with_suffix(path.suffix + ".part").replace(path)
    return path


def build(opts):
    out = Path(opts.output).resolve()
    if out.exists():
        raise ValueError("构建目录已存在，不覆盖：" + str(out))
    cache = Path(opts.cache).resolve()
    cfg = json.loads((ROOT / "config/engines/windows-cuda13.json").read_text(encoding="utf-8"))
    control = cache / cfg["control"]["file"]
    cli = Path(opts.cli).absolute()
    release = native(cli)
    if control.is_symlink() or digest(control) != cfg["control"]["sha256"]:
        raise ValueError("上游压缩包 SHA256 不符")
    out.mkdir(parents=True)
    files = []

    def put(name, data):
        safe(name)
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append(name)

    for name in SOURCES:
        data = (ROOT / "portable" / name).read_bytes()
        put(name, data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") if name.endswith(".bat") else data)
    put("licenses/OpenCode-MIT.txt", (ROOT / "LICENSE").read_bytes())
    put("licenses/CTFCode-OpenCode.patch", (ROOT / "patches/ctfcode-v2.3.0.patch").read_bytes())
    put("licenses/CTFCode-native-build.json", (ROOT / "script/native-lock.json").read_bytes())
    for name in ("模型下载与校验.md", "引擎下载与配置.md", "便携版使用.md", "定制CLI构建.md"):
        put("文档/" + name, (ROOT / "docs" / name).read_bytes())
    for name in ("models/gsq-rco-coder-iq1-m.json", "engines/windows-cuda13.json"):
        put("config/" + name, (ROOT / "config" / name).read_bytes())
    for name in ("Strata-LICENSE.txt", "NVIDIA-CUDA-EULA.html"):
        put("licenses/" + name, (cache / name).read_bytes())
    put(f"{OC}/程序/opencode.exe", cli.read_bytes())
    with zipfile.ZipFile(control) as z:
        for file in z.infolist():
            safe(file.filename)
            if file.is_dir() or "__pycache__" in PurePosixPath(file.filename).parts or PurePosixPath(file.filename).suffix in (".gguf", ".safetensors"):
                continue
            put(f"{BASE}/runtime/control-0.1.52/" + file.filename, z.read(file))
    for source, dest, version in (
        (opts.engine32, f"{BASE}/components/engine/0.1.52-windows-x86_64-cuda-cuda13", "0.1.52"),
        (opts.engine64, f"{BASE}/components/ctf-64k-0.1.59/engine/cuda13-0.1.59", "0.1.59"),
    ):
        source = Path(source).resolve()
        path = source / "engine-component.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["version"] != version or not manifest.get("signature"):
            raise ValueError("组件版本或签名缺失")
        if version == "0.1.59" and digest(path) != cfg["candidate"]["manifest_sha256"]:
            raise ValueError("64K manifest 不匹配")
        put(dest + "/engine-component.json", path.read_bytes())
        for file in manifest["files"]:
            safe(file["path"])
            path = source / file["path"]
            if path.is_symlink() or path.stat().st_size != file["bytes"] or digest(path) != file["sha256"]:
                raise ValueError("组件文件不匹配：" + file["path"])
            put(dest + "/" + file["path"], path.read_bytes())
        if (source / "BUILD.json").is_file():
            put(dest + "/BUILD.json", (source / "BUILD.json").read_bytes())
    libs = [name[:-len("psutil/__init__.py")] for name in files if name.endswith("site-packages/psutil/__init__.py")]
    if len(libs) != 1:
        raise ValueError("不能唯一定位内置 Python site-packages")
    for pkg in json.loads((ROOT / "portable/python-dependencies.json").read_text(encoding="utf-8"))["packages"]:
        path = fetch(pkg["url"], cache / pkg["file"])
        if digest(path) != pkg["sha256"]:
            raise ValueError("Python wheel SHA256 不符")
        with zipfile.ZipFile(path) as z:
            for file in z.infolist():
                safe(file.filename)
                if file.is_dir():
                    continue
                if ".data/" in file.filename:
                    raise ValueError("需要专门处理 wheel .data 目录，不猜测安装位置")
                put(libs[0] + file.filename, z.read(file))
        if pkg.get("source"):
            source = pkg["source"]
            path = fetch(source["url"], cache / source["file"])
            if digest(path) != source["sha256"]:
                raise ValueError("依赖对应源代码 SHA256 不符")
            put("licenses/sources/" + source["file"], path.read_bytes())
    rows = [{"path": name, "bytes": (out / name).stat().st_size, "sha256": digest(out / name)} for name in sorted(set(files))]
    report = {"format": 1, "contains_models": False, "contains_sessions": False,
              "source": "CTFCode native OpenCode " + release["version"], "cli": release, "files": rows}
    (out / "bundle-files.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(rows), "bytes": sum(row["bytes"] for row in rows), "output": str(out)}, ensure_ascii=False))


def pack(opts):
    root = Path(opts.input).resolve()
    data = json.loads((root / "bundle-files.json").read_text(encoding="utf-8"))
    out = Path(opts.output).resolve()
    if out.exists():
        raise ValueError("压缩包已存在，不覆盖")
    for row in data["files"]:
        safe(row["path"])
        path = root / row["path"]
        if path.is_symlink() or path.resolve() != path or path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
            raise ValueError("验收后包文件发生变化：" + row["path"])
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as z:
        for row in data["files"]:
            z.write(root / row["path"], "CTFCode/" + row["path"])
        z.write(root / "bundle-files.json", "CTFCode/bundle-files.json")
    print(json.dumps({"file": out.name, "bytes": out.stat().st_size, "sha256": digest(out)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    builder = sub.add_parser("build")
    for name in ("cache", "engine32", "engine64", "cli", "output"):
        builder.add_argument("--" + name, required=True)
    packer = sub.add_parser("pack")
    packer.add_argument("--input", required=True)
    packer.add_argument("--output", required=True)
    opts = parser.parse_args()
    if opts.command == "build":
        return build(opts)
    return pack(opts)


if __name__ == "__main__":
    main()
