using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;

namespace MaxAgentSfx
{
    internal static class Program
    {
        private const string ResourceName = "MaxAgent.zip";
        private const string SubfolderName = "MaxAgent";

        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                string exePath = Process.GetCurrentProcess().MainModule.FileName;
                string exeDir = Path.GetDirectoryName(exePath) ?? Environment.CurrentDirectory;
                string targetDir = Path.Combine(exeDir, SubfolderName);
                string startBat = Path.Combine(targetDir, "Start-MaxAgent.bat");
                string markerFile = Path.Combine(targetDir, ".sfx_extracted");

                if (!File.Exists(startBat) || !File.Exists(markerFile))
                {
                    ExtractBundle(targetDir);
                    File.WriteAllText(markerFile, DateTime.UtcNow.ToString("O"));
                }

                string externalConnect = Path.Combine(exeDir, "connect.json");
                string targetConnect = Path.Combine(targetDir, "connect.json");
                if (File.Exists(externalConnect))
                {
                    File.Copy(externalConnect, targetConnect, overwrite: true);
                }

                if (!File.Exists(startBat))
                {
                    Console.Error.WriteLine("[MaxAgent-SFX] ERROR: Start-MaxAgent.bat not found after extraction.");
                    return 1;
                }

                var psi = new ProcessStartInfo
                {
                    FileName = "cmd.exe",
                    Arguments = BuildCmdArguments(startBat, args),
                    WorkingDirectory = targetDir,
                    UseShellExecute = false
                };

                using (Process proc = Process.Start(psi))
                {
                    if (proc != null)
                    {
                        proc.WaitForExit();
                        return proc.ExitCode;
                    }
                }
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[MaxAgent-SFX] ERROR: " + ex.Message);
                return 1;
            }
        }

        private static void ExtractBundle(string targetDir)
        {
            Directory.CreateDirectory(targetDir);
            string tempZip = Path.Combine(Path.GetTempPath(), "MaxAgent_" + Guid.NewGuid().ToString("N") + ".zip");
            try
            {
                Assembly asm = Assembly.GetExecutingAssembly();
                using (Stream res = asm.GetManifestResourceStream(ResourceName))
                {
                    if (res == null)
                    {
                        throw new InvalidOperationException("Embedded resource '" + ResourceName + "' not found.");
                    }
                    using (FileStream fs = File.Create(tempZip))
                    {
                        res.CopyTo(fs);
                    }
                }

                using (ZipArchive archive = ZipFile.OpenRead(tempZip))
                {
                    string fullTarget = Path.GetFullPath(targetDir);
                    if (!fullTarget.EndsWith(Path.DirectorySeparatorChar.ToString()))
                    {
                        fullTarget += Path.DirectorySeparatorChar;
                    }

                    foreach (ZipArchiveEntry entry in archive.Entries)
                    {
                        string destPath = Path.GetFullPath(Path.Combine(targetDir, entry.FullName));
                        if (!destPath.StartsWith(fullTarget, StringComparison.OrdinalIgnoreCase))
                        {
                            throw new IOException("Blocked unsafe zip entry: " + entry.FullName);
                        }

                        if (string.IsNullOrEmpty(entry.Name))
                        {
                            Directory.CreateDirectory(destPath);
                            continue;
                        }

                        string parent = Path.GetDirectoryName(destPath);
                        if (!string.IsNullOrEmpty(parent))
                        {
                            Directory.CreateDirectory(parent);
                        }
                        entry.ExtractToFile(destPath, overwrite: true);
                    }
                }
            }
            finally
            {
                if (File.Exists(tempZip))
                {
                    try { File.Delete(tempZip); } catch { }
                }
            }
        }

        private static string BuildCmdArguments(string startBat, string[] args)
        {
            string quotedBat = "\"" + startBat + "\"";
            if (args == null || args.Length == 0)
            {
                return "/c " + quotedBat;
            }
            var escaped = new string[args.Length];
            for (int i = 0; i < args.Length; i++)
            {
                escaped[i] = "\"" + args[i].Replace("\"", "\\\"") + "\"";
            }
            return "/c \"" + quotedBat + " " + string.Join(" ", escaped) + "\"";
        }
    }
}
