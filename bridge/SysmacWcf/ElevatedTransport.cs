using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

// One child, one typed operation, one response. No privileged listener persists.
internal static class ElevatedTransport
{
    private const int MaximumFrame = 1024 * 1024;
    private static NamedPipeClientStream response;
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe, out uint pid);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint pid);

    internal static void Connect(Dictionary<string, string> options)
    {
        if (!options.ContainsKey("reply-pipe"))
        {
            if (options.ContainsKey("reply-token") || options.ContainsKey("parent-pid"))
                throw new ArgumentException("Incomplete response pipe options.");
            return;
        }
        if (!options.ContainsKey("reply-token") || !options.ContainsKey("parent-pid")
            || !Regex.IsMatch(options["reply-pipe"], "^sysmac-mcp-[a-f0-9]{32}$")
            || !Regex.IsMatch(options["reply-token"], "^[a-f0-9]{32}$"))
            throw new ArgumentException("Invalid response pipe.");
        uint parent;
        if (!UInt32.TryParse(options["parent-pid"], out parent) || parent == 0)
            throw new ArgumentException("Invalid parent PID.");
        var pipe = new NamedPipeClientStream(".", options["reply-pipe"], PipeDirection.InOut,
            PipeOptions.Asynchronous, TokenImpersonationLevel.Anonymous);
        try
        {
            pipe.Connect(5000);
            uint server;
            if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle, out server) || server != parent)
                throw new InvalidDataException("Response server PID differs.");
            string token = options["reply-token"];
            bool wrong = options.ContainsKey("pipe-test-case") && options["pipe-test-case"] == "wrong-token";
            Send(pipe, new { token = wrong ? "invalid" : token }, 1000);
            var go = Receive(pipe, 5000, 1000);
            if (!go.ContainsKey("go") || !(go["go"] is bool) || !(bool)go["go"]
                || !go.ContainsKey("token") || !String.Equals(go["token"] as string, token, StringComparison.Ordinal))
                throw new InvalidDataException("Parent did not authorize this child.");
            response = pipe;
        }
        catch { pipe.Dispose(); throw; }
    }

    internal static bool Write(object value)
    {
        if (response == null) return false;
        using (var pipe = response)
        {
            response = null;
            Send(pipe, value, MaximumFrame);
        }
        return true;
    }

    internal static string Quote(string value)
    {
        // CommandLineToArgvW-compatible quoting, including trailing backslashes.
        var text = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') text.Append('\\', slashes * 2 + 1);
            else text.Append('\\', slashes);
            text.Append(ch); slashes = 0;
        }
        text.Append('\\', slashes * 2); text.Append('"');
        return text.ToString();
    }

    internal static object Run(Dictionary<string, string> options)
    {
        string operation;
        if (!options.TryGetValue("operation", out operation)
            || (Array.IndexOf(new[] { "probe", "open", "export" }, operation) < 0 && !RuntimeRunner.IsRuntime(operation)))
            throw new ArgumentException("Unsupported typed elevated IDE operation.");
        foreach (string key in options.Keys)
            if (Array.IndexOf(new[] { "operation", "host-id", "path", "timeout-seconds", "vendor-root", "runtime-profile", "request-sha256", "runtime-profile-sha256" }, key) < 0)
                throw new ArgumentException("Unsupported elevated operation option.");
        if (RuntimeRunner.IsRuntime(operation)) {
            RuntimeRunner.Validate(operation, options);
            RuntimeRunner.VerifyInputs(options);
        }
        else Runner.ValidateRequest(operation, options);
        int timeout = options.ContainsKey("timeout-seconds") ? Int32.Parse(options["timeout-seconds"]) : 180;
        return Child(operation, options, true, null, timeout + (RuntimeRunner.IsRuntime(operation) ? 50 : 20));
    }

    private static Dictionary<string, object> Child(string operation, Dictionary<string, string> options,
        bool elevate, string testCase, int timeout)
    {
        string name = "sysmac-mcp-" + Guid.NewGuid().ToString("N"), token = Guid.NewGuid().ToString("N");
        var security = new PipeSecurity();
        security.SetAccessRuleProtection(true, false);
        security.AddAccessRule(new PipeAccessRule(WindowsIdentity.GetCurrent().User,
            PipeAccessRights.FullControl, AccessControlType.Allow));
        // A different administrator account cannot connect: SID is deliberately fixed.
        using (var pipe = new NamedPipeServerStream(name, PipeDirection.InOut, 1,
            PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 4096, 4096, security))
        {
            var ready = Task.Factory.FromAsync(pipe.BeginWaitForConnection, pipe.EndWaitForConnection, null);
            var arguments = new List<string> { operation };
            foreach (var pair in options)
                if (pair.Key != "operation") {
                    arguments.Add("--" + pair.Key);
                    arguments.Add(pair.Key == "path" || pair.Key == "vendor-root" || pair.Key == "runtime-profile" ? Path.GetFullPath(pair.Value) : pair.Value);
                }
            arguments.AddRange(new[] { "--reply-pipe", name, "--reply-token", token, "--parent-pid", Process.GetCurrentProcess().Id.ToString() });
            if (testCase != null) arguments.AddRange(new[] { "--pipe-test-case", testCase });
            var encoded = new List<string>();
            foreach (string part in arguments) encoded.Add(Quote(part));
            var start = new ProcessStartInfo(Assembly.GetExecutingAssembly().Location, String.Join(" ", encoded)) {
                UseShellExecute = elevate, CreateNoWindow = !elevate, WindowStyle = ProcessWindowStyle.Hidden
            };
            if (elevate) start.Verb = "runas";
            if (testCase == "relative-path") start.WorkingDirectory = Path.GetTempPath();
            Process child;
            try { child = Process.Start(start); }
            catch (Win32Exception error)
            {
                if (error.NativeErrorCode == 1223)
                    throw new OperationCanceledException("UAC elevation cancelled; no IDE operation was started.", error);
                throw;
            }
            using (child)
            {
                if (child == null) throw new IOException("Windows did not return the helper process.");
                if (!ready.Wait(TimeSpan.FromSeconds(10))) throw new TimeoutException("Helper did not connect; no GO was sent.");
                uint client;
                int expectedPid = child.Id + (testCase == "wrong-process" ? 1 : 0);
                if (!GetNamedPipeClientProcessId(pipe.SafePipeHandle, out client) || client != expectedPid)
                    throw new InvalidDataException("Response client PID differs.");
                var hello = Receive(pipe, 5000, 1000);
                if (!hello.ContainsKey("token") || !String.Equals(hello["token"] as string, token, StringComparison.Ordinal))
                    throw new InvalidDataException("Response token differs.");
                if (testCase == "no-go")
                {
                    pipe.Dispose();
                    if (!child.WaitForExit(5000) || child.ExitCode == 0)
                        throw new InvalidOperationException("Child continued without GO.");
                    return new Dictionary<string, object> { { "success", true } };
                }
                Send(pipe, new { go = true, token = token }, 1000);
                var payload = Receive(pipe, timeout * 1000, MaximumFrame);
                payload["helper_pid"] = child.Id;
                payload["helper_transport"] = elevate ? "one_shot_uac_named_pipe" : "owned_child_test";
                return payload;
            }
        }
    }

    private static void Send(Stream pipe, object value, int limit)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(new JavaScriptSerializer { MaxJsonLength = MaximumFrame }.Serialize(value));
        if (bytes.Length > limit) throw new InvalidDataException("Response exceeds frame limit.");
        byte[] size = BitConverter.GetBytes(bytes.Length);
        pipe.Write(size, 0, size.Length); pipe.Write(bytes, 0, bytes.Length); pipe.Flush();
    }

    private static Dictionary<string, object> Receive(Stream pipe, int timeout, int limit)
    {
        var deadline = Stopwatch.StartNew();
        var size = Read(pipe, 4, timeout, deadline);
        int length = BitConverter.ToInt32(size, 0);
        if (length < 2 || length > limit) throw new InvalidDataException("Invalid response frame length.");
        var data = Read(pipe, length, timeout, deadline);
        return new JavaScriptSerializer { MaxJsonLength = MaximumFrame }.Deserialize<Dictionary<string, object>>(Encoding.UTF8.GetString(data));
    }

    private static byte[] Read(Stream pipe, int length, int timeout, Stopwatch deadline)
    {
        var bytes = new byte[length]; int offset = 0;
        while (offset < length)
        {
            int remaining = timeout - (int)deadline.ElapsedMilliseconds;
            if (remaining <= 0) throw new TimeoutException("Helper response deadline expired; operation outcome may be unknown.");
            var pending = pipe.ReadAsync(bytes, offset, length - offset);
            if (!pending.Wait(remaining)) throw new TimeoutException("Helper response deadline expired; operation outcome may be unknown.");
            int count = pending.Result;
            if (count == 0) throw new EndOfStreamException("Helper disconnected.");
            offset += count;
        }
        return bytes;
    }

    internal static void TestChild(Dictionary<string, string> options)
    {
        if (options["pipe-test-case"] == "oversize")
        {
            byte[] header = BitConverter.GetBytes(MaximumFrame + 1);
            response.Write(header, 0, 4); response.Flush(); response.Dispose(); response = null;
            return;
        }
        Program.Write(new { success = true, echo = options["path"] });
    }

    internal static object SelfTest()
    {
        var options = new Dictionary<string, string> { { "path", "C:\\spaces and unicode è\\" } };
        var good = Child("pipe-test-child", options, false, "normal", 5);
        if (!(bool)good["success"] || (string)good["echo"] != options["path"])
            throw new InvalidOperationException("Child response/quoting failed.");
        foreach (string failure in new[] { "wrong-token", "wrong-process", "oversize" })
        {
            bool rejected = false;
            try { Child("pipe-test-child", options, false, failure, 5); }
            catch (InvalidDataException) { rejected = true; }
            if (!rejected) throw new InvalidOperationException("Transport accepted " + failure);
        }
        Child("pipe-test-child", options, false, "no-go", 5);
        var relative = new Dictionary<string, string> { { "path", ".\\relative spaces\\" } };
        var canonical = Child("pipe-test-child", relative, false, "relative-path", 5);
        if ((string)canonical["echo"] != Path.GetFullPath(relative["path"]))
            throw new InvalidOperationException("Relative child path differs.");
        return new { no_elevation_requested = true, successful_child = true, wrong_token_rejected = true,
            wrong_process_rejected = true, oversized_message_rejected = true, child_without_go_rejected = true,
            relative_paths_canonicalized = true };
    }
}
