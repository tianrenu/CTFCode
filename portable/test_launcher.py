"""实际编译的Windows入口：引号、中文路径、退出码及不回退全局环境。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('entry_build', Path(__file__).resolve().parents[1] / 'script/launcher.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class Icon(unittest.TestCase):
    def test_icon(self):
        value = entry.icon()
        self.assertEqual(struct.unpack('<HHH', value[:6]), (0, 1, 1))
        self.assertEqual(value[22:30], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', value[38:46]), (256, 256))


@unittest.skipUnless(os.name == 'nt' and os.environ.get('CTFCODE_LAUNCHER') and os.environ.get('CTFCODE_PYTHON'),
                     '须指定已编译入口和便携Python；不运行全局CLI')
class Launcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='CTFCode 启动器 中文 & 空格 ')
        cls.root = Path(cls.temp.name)
        cls.exe = cls.root / 'CTFCode.exe'
        shutil.copyfile(os.environ['CTFCODE_LAUNCHER'], cls.exe)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def runexe(self, *args):
        return subprocess.run([str(self.exe), *args], capture_output=True, text=True, encoding='utf-8', timeout=30)

    def test_version(self):
        result = self.runexe('--version')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CTFCode v0.1.0-preview.4', result.stdout)

    def test_help(self):
        result = self.runexe('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/ctf', result.stdout)

    def test_missing(self):
        root = self.root / '缺少依赖'
        root.mkdir(exist_ok=True)
        exe = root / 'CTFCode.exe'
        shutil.copyfile(self.exe, exe)
        result = subprocess.run([str(exe), '--dry-run'], capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('完整解压', result.stderr)

    def test_forwarding(self):
        dest = self.root / '本地模型配置与日志/Strata验证/runtime/control-0.1.52/python'
        shutil.copytree(Path(os.environ['CTFCODE_PYTHON']).parent, dest, dirs_exist_ok=True)
        # A recording fixture tests the executable boundary, not CTF/model behavior.
        file = self.root / 'bootstrap.py'
        file.write_text('import json,os,sys\nprint(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd(),"utf8":os.environ.get("PYTHONUTF8")}))\n', encoding='utf-8')
        for args, expected in [([], ['ui']), (['--check'], ['check', '--menu']), (['--stop'], ['stop']),
            (['C:\\题 目 & 数据\\'], ['ui', '--target', 'C:\\题 目 & 数据\\']),
            (['--target', 'C:\\a"b\\'], ['ui', '--target', 'C:\\a"b\\'])]:
            with self.subTest(args=args):
                result = self.runexe(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['args'], expected)
                self.assertEqual(Path(data['cwd']), self.root)
                self.assertEqual(data['utf8'], '1')
        file.write_text('raise SystemExit(17)\n', encoding='utf-8')
        self.assertEqual(self.runexe('--dry-run').returncode, 17)


if __name__ == '__main__':
    unittest.main()
