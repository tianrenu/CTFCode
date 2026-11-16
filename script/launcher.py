"""用Windows内置.NET Framework编译小型原生入口；不打包另一个Web/桌面运行时。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import zlib

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def icon():
    # Same geometric mark as logo.svg, with 2x antialiasing. No third-party image assets.
    lines = [(92,65,62,65),(62,65,62,191),(62,191,92,191),(164,65,194,65),
             (194,65,194,191),(194,191,164,191),(91,107,114,128),(114,128,91,149),(134,151,164,151)]
    def pixel(x,y):
        dx,dy=max(60-x,0,x-196),max(60-y,0,y-196)
        if math.hypot(dx,dy)>52:
            return (0,0,0,0)
        for index,(a,b,c,d) in enumerate(lines):
            ratio=max(0,min(1,((x-a)*(c-a)+(y-b)*(d-b))/((c-a)**2+(d-b)**2)))
            if math.hypot(x-a-ratio*(c-a),y-b-ratio*(d-b))<=6:
                return (56,217,176,255) if index<6 else (226,244,255,255)
        return (16,29,45,255)
    def chunk(tag,data):
        return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data))
    data=bytearray()
    for y in range(256):
        data.append(0)
        for x in range(256):
            samples=[pixel(x+i/2,y+j/2) for i in (0,1) for j in (0,1)]
            data.extend(round(sum(row[k] for row in samples)/4) for k in range(4))
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',256,256,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(data))+chunk(b'IEND',b'')
    return struct.pack('<HHH',0,1,1)+struct.pack('<BBBBHHII',0,0,0,0,1,32,len(png),22)+png


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    out=Path(args.output).resolve()
    if out.exists():
        raise ValueError('不覆盖已有构建目录')
    compiler=Path(os.environ.get('WINDIR','C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if os.name!='nt' or not compiler.is_file():
        raise OSError('本构建需要Windows内置.NET Framework 4.x编译器')
    out.mkdir(parents=True)
    (out / 'CTFCode.ico').write_bytes(icon())
    subprocess.run([str(compiler),'/nologo','/target:exe','/platform:x64','/optimize+',
        '/out:'+str(out / 'CTFCode.exe'),'/win32icon:'+str(out / 'CTFCode.ico'),
        '/win32manifest:'+str(ROOT / 'launcher/app.manifest'),str(ROOT / 'launcher/Program.cs')],check=True)
    report={'format':1,'version':'0.1.0-preview.4','file':'CTFCode.exe','sha256':digest(out / 'CTFCode.exe'),
        'bytes':(out / 'CTFCode.exe').stat().st_size,'sources':{name:digest(ROOT / name) for name in
        ('launcher/Program.cs','launcher/app.manifest','launcher/logo.svg','script/launcher.py')},
        'runtime':'.NET Framework 4.x (included with supported Windows 10/11)',
        'scope':'console entry to bundled Python and native OpenCode TUI; no model or shell command execution'}
    (out / 'launcher.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()
