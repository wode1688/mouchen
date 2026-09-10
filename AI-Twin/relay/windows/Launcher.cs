using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

// Build with scripts/build-launcher.ps1; the executable belongs in the checkout root.
internal static class Launcher {
    [STAThread]
    private static void Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        string source = Path.Combine(root, "companion.py");
        bool pause = Array.IndexOf(args, "--pause") >= 0;
        try {
            if (!File.Exists(python) || !File.Exists(source)) {
                throw new FileNotFoundException("Keep this launcher in the checkout root and run scripts/setup.ps1 first.");
            }
            Process.Start(new ProcessStartInfo {
                FileName = python,
                Arguments = "-B \"" + source + "\" " + (pause ? "--pause" : "--watch"),
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            });
            // Optional: launch a separately installed app after starting the companion.
            string application = Environment.GetEnvironmentVariable("AI_TWIN_APP_EXE");
            if (!pause && !String.IsNullOrWhiteSpace(application)) {
                application = Path.GetFullPath(application);
                Process.Start(new ProcessStartInfo {
                    FileName = application,
                    WorkingDirectory = Path.GetDirectoryName(application),
                    UseShellExecute = true
                });
            }
        } catch (Exception ex) {
            MessageBox.Show(ex.Message, "AI Twin Relay", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
