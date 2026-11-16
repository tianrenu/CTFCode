"""从已锁定的上游源码构建独立CTFCode CLI；不接触桌面版或模型。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def digest(file):
    with Path(file).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unpack(archive, out):
    """只在新目录提取普通文件；上游资源符号链接物化为包内目标文件。"""
    with tarfile.open(archive, "r:gz") as tar:
        rows = tar.getmembers()
        prefix = rows[0].name.split("/")[0]
        for row in rows:
            name = PurePosixPath(row.name)
            if name.is_absolute() or name.parts[0] != prefix or ".." in name.parts or "\\" in row.name or ":" in row.name:
                raise ValueError("源码包路径异常：" + row.name)
            file = out.joinpath(*name.parts[1:])
            if not file.resolve().is_relative_to(out.resolve()):
                raise ValueError("源码包越界")
            if row.isdir():
                file.mkdir(parents=True, exist_ok=True)
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            if row.issym() or row.islnk():
                target = posixpath.normpath(posixpath.join(posixpath.dirname(row.name), row.linkname)) if row.issym() else row.linkname
                if not target.startswith(prefix + "/") or not tar.getmember(target).isfile():
                    raise ValueError("源码资源链接目标不是包内普通文件")
                row = tar.getmember(target)
            if not row.isfile():
                raise ValueError("源码包包含不支持的节点")
            with tar.extractfile(row) as stream, file.open("xb") as dest:
                shutil.copyfileobj(stream, dest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, help="native-lock.json锁定的官方源码tgz")
    parser.add_argument("--bun", required=True, help="已解压的官方Bun 1.4.4 Windows x64程序")
    parser.add_argument("--output", required=True, help="不存在的新构建目录；不覆盖或删除旧目录")
    parser.add_argument("--prepare-only", action="store_true", help="仅核验、解包和应用补丁，不安装依赖或编译")
    args = parser.parse_args(argv)
    lock = json.loads((ROOT / "script/native-lock.json").read_text(encoding="utf-8"))
    archive = Path(args.archive).resolve()
    bun = Path(args.bun).resolve()
    patch = ROOT / lock["patch"]["path"]
    out = Path(args.output).absolute()
    if out.exists() or out.is_symlink() or out == ROOT or ROOT.is_relative_to(out):
        raise ValueError("只允许不存在的新构建目录；不覆盖现有内容")
    if digest(archive) != lock["upstream"]["sha256"] or digest(bun) != lock["bun"]["executable_sha256"] or digest(patch) != lock["patch"]["sha256"]:
        raise ValueError("源码、Bun或定制补丁的SHA256与锁定值不符")
    out.mkdir(parents=True)
    out = out.resolve()
    source = out / "source"
    unpack(archive, source)
    for name, hashes in lock["patch"]["files"].items():
        if digest(source / name) != hashes["before"]:
            raise ValueError("上游源码文件与补丁基线不符：" + name)
    env = dict(os.environ)
    # 源码包不是当前CTFCode checkout，不让git apply向上找到父仓库。
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_CEILING_DIRECTORIES"] = str(out)
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply", "--check", str(patch)], cwd=source, env=env, check=True)
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply", str(patch)], cwd=source, env=env, check=True)
    for name, hashes in lock["patch"]["files"].items():
        if digest(source / name) != hashes["after"]:
            raise ValueError("补丁后文件SHA256不符：" + name)
    report = {"source": str(source), "version": lock["version"], "upstream": lock["upstream"],
              "patch_sha256": lock["patch"]["sha256"], "model_loaded": False, "models_copied": False,
              "dependencies_installed": False, "compiled": False}
    file = out / "build.json"
    file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.prepare_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if os.name != "nt":
        raise ValueError("本构建锁定Windows x64；其他平台尚未验收")
    # Bun 1.4.4在中文可执行路径下无法自复制编译；只临时复制已核验构建器。
    compiler = Path(tempfile.mkdtemp(prefix="ctfcode-compiler-"))
    if not str(compiler).isascii():
        compiler.rmdir()
        raise ValueError("临时目录包含非ASCII字符；请为本次构建设置英文TEMP/TMP路径")
    exe = compiler / "bun.exe"
    try:
        shutil.copyfile(bun, exe)
        if digest(exe) != lock["bun"]["executable_sha256"]:
            raise ValueError("临时构建器SHA256不符")
        env.update(PATH=str(compiler) + os.pathsep + env.get("PATH", ""), OPENCODE_CHANNEL=lock["channel"],
                   OPENCODE_VERSION=lock["version"], OPENCODE_TARGET="windows-x64",
                   HTTP_PROXY="", HTTPS_PROXY="", ALL_PROXY="", NO_PROXY="*")
        env.pop("BUN_COMPILE_RELEASE", None)
        env.pop("OPENCODE_MODELS_SNAPSHOT", None)
        def run(label, cwd, *command):
            report["stage"] = label
            file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with (out / (label + ".log")).open("wb") as stream:
                result = subprocess.run([str(exe), *command], cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
            if result.returncode:
                report.update(failed=True, exit_code=result.returncode)
                file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                raise RuntimeError(label + "失败；证据：" + str(out / (label + ".log")))
        run("install", source, "install", "--frozen-lockfile", "--ignore-scripts", "--filter", "opencode",
            "--filter", "@opencode/cli", "--filter", "@opencode/core", "--filter", "@opencode/tui", "--filter", "@opencode/script",
            "--network-concurrency=8")
        report["dependencies_installed"] = True
        run("core-tests", source / "packages/core", "test", "test/instructions/builtins.test.ts")
        run("tui-tests", source / "packages/tui", "test", "test/prompt/autocomplete.test.ts", "test/util/presentation.test.ts",
            "test/app-lifecycle.test.tsx", "--timeout", "30000")
        for name in ("core", "tui", "cli"):
            run(name + "-typecheck", source / ("packages/" + name), "run", "typecheck")
        run("compile", source / "packages/cli", "run", "script/build.ts", "--single", "--skip-install", "--skip-web-ui",
            "--outdir=" + str(out / "artifacts"))
        cli = out / "artifacts/cli-windows-x64/bin/opencode.exe"
        version = subprocess.run([str(cli), "--version"], env=env, capture_output=True, text=True, timeout=15, check=True)
        if version.stdout.strip() != "opencode v" + lock["version"]:
            raise ValueError("编译后的CLI版本不符")
        report.update(stage="built", compiled=True, cli=str(cli), sha256=digest(cli), bytes=cli.stat().st_size,
                      note="构建SHA需经原生TUI验收后写入v2/cli.json；不自动替换用户入口")
        file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        # 只删除本次创建且哈希仍匹配的单一临时构建器，不递归清理其他目录。
        if exe.is_file() and not exe.is_symlink() and digest(exe) == lock["bun"]["executable_sha256"]:
            exe.unlink()
        if not any(compiler.iterdir()):
            compiler.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
