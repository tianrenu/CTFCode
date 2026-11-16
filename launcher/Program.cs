using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("CTFCode")]
[assembly: AssemblyDescription("Local CTF workspace powered by OpenCode and Strata")]
[assembly: AssemblyProduct("CTFCode")]
[assembly: AssemblyVersion("0.1.0.4")]
[assembly: AssemblyFileVersion("0.1.0.4")]
[assembly: AssemblyInformationalVersion("0.1.0-preview.4")]

internal static class Program
{
    // CreateProcess quoting, not shell escaping. Paths never pass through cmd/PowerShell.
    private static string Quote(string value)
    {
        var text = new StringBuilder("\"");
        var count = 0;
        foreach (var c in value)
        {
            if (c == '\\') { count++; continue; }
            text.Append('\\', c == '"' ? count * 2 + 1 : count);
            text.Append(c);
            count = 0;
        }
        return text.Append('\\', count * 2).Append('"').ToString();
    }

    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.Title = "CTFCode · 本地 CTF 工作台";
        try
        {
            if (args.Length == 1 && args[0] == "--version")
            {
                Console.WriteLine("CTFCode v0.1.0-preview.4");
                return 0;
            }
            if (args.Length == 1 && (args[0] == "--help" || args[0] == "-h"))
            {
                Console.WriteLine("CTFCode · 本地 CTF 工作台\n\n双击直接进入 TUI 开始页。\nCTFCode.exe [题目目录]\nCTFCode.exe --check\nCTFCode.exe --stop\nCTFCode.exe --context 32|64\n\n/ctf 配置模型 · /cd 换题 · /new 空白会话 · /exit 归档退出\n模型权重另行下载；64K 仍为候选。无需管理员权限。");
                return 0;
            }
            var dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            var root = File.Exists(Path.Combine(dir, "bootstrap.py")) ? dir : Path.Combine(dir, ".runtime", "CTFCode");
            var exe = Path.Combine(root, "本地模型配置与日志", "Strata验证", "runtime", "control-0.1.52", "python", "python.exe");
            var boot = Path.Combine(root, "bootstrap.py");
            if (!File.Exists(exe) || !File.Exists(boot))
                throw new FileNotFoundException("运行文件不完整。请完整解压 windows-x64 便携包，不要单独移动 CTFCode.exe，也不要只下载 Source code。");
            if ((File.GetAttributes(exe) & FileAttributes.ReparsePoint) != 0 ||
                (File.GetAttributes(boot) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("启动文件不能是重定向链接。未运行其他 Python 或全局 OpenCode。");
            var command = new List<string> { "-I", "-X", "utf8", "-u", boot };
            if (args.Length == 1 && args[0] == "--check") command.AddRange(new[] { "check", "--menu" });
            else if (args.Length == 1 && args[0] == "--stop") command.Add("stop");
            else
            {
                command.Add("ui");
                if (args.Length == 1 && !args[0].StartsWith("-")) command.AddRange(new[] { "--target", args[0] });
                else command.AddRange(args);
            }
            var info = new ProcessStartInfo(exe);
            info.Arguments = String.Join(" ", command.ConvertAll(Quote));
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.EnvironmentVariables["PYTHONUTF8"] = "1";
            info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            info.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
            // Let the TUI handle Ctrl+C; keep its owning controller alive for normal cleanup.
            Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs value) { value.Cancel = true; };
            using (var child = Process.Start(info))
            {
                child.WaitForExit();
                if (child.ExitCode != 0) Pause(args);
                return child.ExitCode;
            }
        }
        catch (Exception err)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.Error.WriteLine("CTFCode 启动失败：" + err.Message);
            Console.ResetColor();
            Console.Error.WriteLine("使用说明：https://github.com/tianrenu/CTFCode");
            Pause(args);
            return 1;
        }
    }

    private static void Pause(string[] args)
    {
        if (args.Length != 0 || Console.IsInputRedirected) return;
        Console.WriteLine("\n按回车关闭；错误不会闪退后消失。");
        Console.ReadLine();
    }
}
