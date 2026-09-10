using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("My AI Twin Windows Private Alpha")]
[assembly: System.Reflection.AssemblyProduct("My AI Twin")]
[assembly: System.Reflection.AssemblyVersion("0.1.0.0")]

internal static class MouchenLauncher
{
    [STAThread]
    private static void Main()
    {
        string script = FindLauncherScript();
        if (script == null)
        {
            MessageBox.Show(
                "My AI Twin desktop files were not found.",
                "My AI Twin",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return;
        }

        var start = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"",
            WorkingDirectory = Path.GetDirectoryName(script),
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        Process.Start(start);
    }

    private static string FindLauncherScript()
    {
        string[] starts =
        {
            AppDomain.CurrentDomain.BaseDirectory,
            Environment.CurrentDirectory,
        };
        foreach (string start in starts)
        {
            var current = new DirectoryInfo(start);
            for (int depth = 0; current != null && depth < 6; depth++, current = current.Parent)
            {
                string candidate = Path.Combine(current.FullName, "desktop", "Start-My AI Twin-Windows.ps1");
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
        }
        return null;
    }
}
