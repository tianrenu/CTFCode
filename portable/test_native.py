"""测试真实源码包解包器；不联网、不编译、不加载模型。"""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("native_build", Path(__file__).resolve().parents[1] / "script/native.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class Native(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="CTFCode 构建夹具 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "source.tgz"
        self.out = self.root / "source"

    def pack(self, rows):
        with tarfile.open(self.archive, "w:gz") as tar:
            for name, text, link in rows:
                item = tarfile.TarInfo(name)
                if link:
                    item.type = tarfile.SYMTYPE
                    item.linkname = link
                    tar.addfile(item)
                    continue
                data = text.encode()
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))

    def test_link(self):
        self.pack([("upstream/src/hello.txt", "中文 fixture", None),
                   ("upstream/public/hello.txt", "", "../src/hello.txt")])
        native.unpack(self.archive, self.out)
        self.assertEqual((self.out / "public/hello.txt").read_text(encoding="utf-8"), "中文 fixture")
        self.assertFalse((self.out / "public/hello.txt").is_symlink())

    def test_traversal(self):
        self.pack([("upstream/../outside.txt", "forbidden", None)])
        with self.assertRaisesRegex(ValueError, "路径异常"):
            native.unpack(self.archive, self.out)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_external(self):
        self.pack([("upstream/src/input.txt", "keep", None), ("upstream/public/link", "", "../../outside.txt")])
        with self.assertRaisesRegex(ValueError, "包内普通文件"):
            native.unpack(self.archive, self.out)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_existing(self):
        file = self.root / "keep.txt"
        file.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不覆盖"):
            native.main(["--archive", "not-read.tgz", "--bun", "not-run.exe", "--output", str(self.root)])
        self.assertEqual(file.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
