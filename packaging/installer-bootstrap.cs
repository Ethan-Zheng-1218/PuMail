using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Forms;

internal static class PuMailInstallerBootstrap
{
    [STAThread]
    private static int Main(string[] args)
    {
        bool check = args.Length == 1 && args[0] == "--check-installer";
        string workspace = null;
        string source = Assembly.GetExecutingAssembly().Location;
        try
        {
            workspace = PrepareWorkspace(source);
            string payload = Path.Combine(workspace, "PuMail-install.exe");
            using (Stream embedded = Assembly.GetExecutingAssembly().GetManifestResourceStream("PuMail.Installer"))
            using (FileStream output = new FileStream(payload, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                if (embedded == null) throw new IOException("安装包缺少程序文件，请重新下载。");
                embedded.CopyTo(output);
            }
            using (SHA256 sha = SHA256.Create())
            using (FileStream input = File.OpenRead(payload))
            {
                string digest = BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "");
                if (!String.Equals(digest, InstallerPayload.Sha256, StringComparison.OrdinalIgnoreCase))
                    throw new IOException("安装包完整性校验失败，请重新下载。");
            }

            ProcessStartInfo start = new ProcessStartInfo(payload);
            start.UseShellExecute = false;
            start.WorkingDirectory = workspace;
            // These variables belong to the installer child only. Windows settings are unchanged.
            // Keep the temp root alive for any app launched by the finish page.
            string tempDirectory = Path.Combine(Directory.GetParent(workspace).FullName, "Temp");
            start.EnvironmentVariables["TEMP"] = tempDirectory;
            start.EnvironmentVariables["TMP"] = tempDirectory;
            start.EnvironmentVariables["PUMAIL_INSTALLER_WORKSPACE"] = workspace;
            start.EnvironmentVariables["PUMAIL_INSTALLER_CHECK"] = check ? "1" : "0";
            start.Arguments = check ? "/D=" + Path.Combine(workspace, "check-only") : ArgumentsTail();
            int result;
            using (Process child = Process.Start(start))
            {
                child.WaitForExit();
                result = child.ExitCode;
            }
            if (check)
            {
                string marker = Path.Combine(workspace, "startup-ok.txt");
                if (result != 0 || !File.Exists(marker) || File.ReadAllText(marker) != "PuMail installer ready")
                    throw new IOException("安装程序启动检查未通过，退出码：" + result);
                Console.WriteLine("PASS: installer integrity, writable workspace, NSIS startup and plugin extraction; no installation performed.");
            }
            else if (File.Exists(Path.Combine(workspace, "delete-source.flag")))
            {
                ScheduleSourceDeletion(source, Directory.GetParent(workspace).FullName);
            }
            return result;
        }
        catch (Exception error)
        {
            string message = "PuMail 安装程序无法启动：\r\n" + error.Message;
            if (check) Console.Error.WriteLine(message);
            else MessageBox.Show(message, "PuMail 安装程序", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            if (workspace != null)
            {
                // Only the unique directory created by this invocation is removed.
                try { Directory.Delete(workspace, true); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }

    private static string PrepareWorkspace(string source)
    {
        string[] roots = {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PuMail", "InstallerCache"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".PuMail", "InstallerCache"),
            Path.Combine(Path.GetDirectoryName(source), ".pumail-installer-cache")
        };
        foreach (string root in roots)
        {
            string workspace = Path.Combine(root, Guid.NewGuid().ToString("N"));
            try
            {
                // NSIS uses MAX_PATH APIs internally; prefer a short fallback over a truncated path.
                if (workspace.Length > 200 || !Path.IsPathRooted(workspace)) continue;
                ValidateWritableDirectory(workspace);
                ValidateWritableDirectory(Path.Combine(root, "Temp"));
                return workspace;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            catch (System.Security.SecurityException) { }
            try { if (Directory.Exists(workspace)) Directory.Delete(workspace, true); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        throw new IOException("无法创建安装临时目录。请把安装包移到可写入的本地文件夹，并确认磁盘空间充足。");
    }

    private static void ValidateWritableDirectory(string directory)
    {
        Directory.CreateDirectory(directory);
        string probe = Path.Combine(directory, Guid.NewGuid().ToString("N") + ".tmp");
        using (FileStream file = new FileStream(probe, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None))
            file.WriteByte(1);
        File.Delete(probe);
    }

    private static string ArgumentsTail()
    {
        // Preserve NSIS's unquoted, final /D= argument (including spaces).
        string commandLine = Environment.CommandLine;
        int end = commandLine.StartsWith("\"") ? commandLine.IndexOf('"', 1) + 1 : commandLine.IndexOf(' ');
        return end > 0 ? commandLine.Substring(end).TrimStart() : "";
    }

    private static void ScheduleSourceDeletion(string source, string cacheRoot)
    {
        // The existing installer option may request deletion of the downloaded EXE.
        // Keep this helper outside the per-run workspace so workspace cleanup cannot race it.
        string script = Path.Combine(cacheRoot, "cleanup-" + Guid.NewGuid().ToString("N") + ".cmd");
        File.WriteAllText(script,
            "@echo off\r\nfor /l %%i in (1,1,30) do (\r\n" +
            "  if not exist \"%PUMAIL_INSTALLER_SOURCE%\" goto done\r\n" +
            "  del /f /q \"%PUMAIL_INSTALLER_SOURCE%\" >nul 2>&1\r\n" +
            "  \"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoProfile -NonInteractive -Command \"Start-Sleep -Milliseconds 500\" >nul 2>&1\r\n)\r\n:done\r\ndel /f /q \"%~f0\"\r\n",
            Encoding.ASCII);
        ProcessStartInfo start = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"));
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.Arguments = "/d /c \"\"" + script + "\"\"";
        start.EnvironmentVariables["PUMAIL_INSTALLER_SOURCE"] = source;
        using (Process child = Process.Start(start)) { }
    }
}
