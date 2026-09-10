using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

// Installed source layout: LocalMode/app/AI-Twin; runtime: LocalMode/.venv.
// This file contains no account, server address, or credential.
internal static class LocalModeLauncher {
    private static Process Run(string python, string arguments, string directory) {
        return Process.Start(new ProcessStartInfo {
            FileName = python, Arguments = arguments, WorkingDirectory = directory,
            UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden
        });
    }

    [STAThread]
    private static void Main(string[] args) {
        string profile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Mouchen", "LocalMode");
        string app = Path.Combine(profile, "app", "AI-Twin");
        string python = Path.Combine(profile, ".venv", "Scripts", "pythonw.exe");
        string config = Path.Combine(profile, "relay-config.json");
        string bridge = Path.Combine(app, "relay", "bridge.py");
        bool pause = Array.IndexOf(args, "--pause") >= 0;
#if PAUSE
        pause = true;
#endif
        try {
            if (!File.Exists(python) || !File.Exists(bridge) || !File.Exists(config)) {
                throw new InvalidOperationException("请先完成本机安装与手机配对配置，再启动本地处理版。");
            }
            if (!pause) {
                string localMode = Path.Combine(app, "desktop", "local_mode.py");
                using (Process setup = Run(python, "-B \"" + localMode + "\" start", app)) {
                    if (!setup.WaitForExit(30000) || setup.ExitCode != 0) {
                        throw new InvalidOperationException("本地处理服务未能启动。请检查本机配置及 LocalMode 目录中的运行记录。");
                    }
                }
            }
            Run(python, "-B \"" + bridge + "\" --config \"" + config + "\" " + (pause ? "--pause" : "--watch"), app);
        } catch (Exception ex) {
            MessageBox.Show(ex.Message, "AI替身 · 电脑本地处理", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
