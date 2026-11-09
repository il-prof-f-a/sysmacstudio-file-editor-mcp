// Experimental offline client. Vendor binaries remain in their installed directory.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.ServiceModel;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using Omron.Cxap.Modules.Wcf.Core;

internal static class Program
{
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        try
        {
            if (args.Length == 0 || (Array.IndexOf(new[] { "open", "export", "probe", "self-test", "elevated", "transport-self-test", "pipe-test-child" }, args[0]) < 0
                && !RuntimeRunner.IsRuntime(args[0])))
                throw new ArgumentException("Unsupported command; use open, export, probe or self-test.");
            var options = new Dictionary<string, string>();
            var allowed = new HashSet<string>(new[] { "vendor-root", "directory", "host-id", "path", "timeout-seconds", "operation", "reply-pipe", "reply-token", "parent-pid", "pipe-test-case", "runtime-profile", "request-sha256", "runtime-profile-sha256" });
            for (int n = 1; n < args.Length; n += 2)
            {
                if (!args[n].StartsWith("--") || n + 1 >= args.Length || !allowed.Contains(args[n].Substring(2)) || options.ContainsKey(args[n].Substring(2)))
                    throw new ArgumentException("Unsupported or duplicate option.");
                options.Add(args[n].Substring(2), args[n + 1]);
            }
            ElevatedTransport.Connect(options);
            if (args[0] == "transport-self-test") { Write(ElevatedTransport.SelfTest()); return 0; }
            if (args[0] == "pipe-test-child") { ElevatedTransport.TestChild(options); return 0; }
            if (RuntimeRunner.IsRuntime(args[0]))
            {
                var result = (Dictionary<string, object>)RuntimeRunner.Run(args[0], options);
                Write(result);
                return (bool)result["success"] ? 0 : 2;
            }
            if (args[0] == "elevated")
            {
                var result = (Dictionary<string, object>)ElevatedTransport.Run(options);
                Write(result);
                return result.ContainsKey("success") && result["success"] is bool && (bool)result["success"] ? 0 : 2;
            }
            string vendor = options.ContainsKey("vendor-root") ? options["vendor-root"] : @"C:\Program Files\OMRON\Sysmac Studio";
            vendor = Path.GetFullPath(vendor);
            if (!File.Exists(Path.Combine(vendor, "FrameworkModules.Core.dll")))
                throw new FileNotFoundException("Installed Sysmac contract assembly does not exist.");
            AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs request)
            {
                string name = new AssemblyName(request.Name).Name;
                if (!Regex.IsMatch(name, @"^[A-Za-z0-9_.-]+$")) return null;
                string path = Path.Combine(vendor, name + ".dll");
                return File.Exists(path) ? Assembly.LoadFrom(path) : null;
            };
            return Runner.Run(args[0], options);
        }
        catch (Exception error)
        {
            var details = new List<object>();
            for (Exception item = error; item != null && details.Count < 8; item = item.InnerException)
                details.Add(new { error_type = item.GetType().Name, error = item.Message, hresult = item.HResult });
            Write(new { success = false, error_type = error.GetType().Name, error = error.Message, details = details });
            return 1;
        }
    }

    internal static void Write(object value)
    {
        if (ElevatedTransport.Write(value)) return;
        Console.WriteLine(new JavaScriptSerializer().Serialize(value));
    }
}

internal static class Runner
{
    internal static void ValidateRequest(string command, Dictionary<string, string> options)
    {
        string hostId = Required(options, "host-id");
        if (!Regex.IsMatch(hostId, @"^[A-Za-z0-9_-]{1,80}$")) throw new ArgumentException("Invalid host-id.");
        if (command != "probe") ValidatePath(command, Required(options, "path"));
        if (command == "export")
            throw new InvalidOperationException("Use guarded runtime export; legacy WCF Export can save an active file-mode original.");
        int timeout = options.ContainsKey("timeout-seconds") ? int.Parse(options["timeout-seconds"]) : 180;
        if (timeout < 1 || timeout > 600) throw new ArgumentException("timeout-seconds must be between 1 and 600.");
    }
    [MethodImpl(MethodImplOptions.NoInlining)]
    internal static int Run(string command, Dictionary<string, string> options)
    {
        if (command == "self-test")
        {
            Program.Write(SelfTest(Required(options, "directory")));
            return 0;
        }
        string hostId = Required(options, "host-id");
        if (!Regex.IsMatch(hostId, @"^[A-Za-z0-9_-]{1,80}$"))
            throw new ArgumentException("Invalid host-id; remote endpoints and path separators are not accepted.");
        string path = command == "probe" ? null : ValidatePath(command, Required(options, "path"));
        if (command == "export")
            throw new InvalidOperationException("Use guarded runtime export; legacy WCF Export can save an active file-mode original.");
        int timeout = options.ContainsKey("timeout-seconds") ? int.Parse(options["timeout-seconds"]) : 180;
        if (timeout < 1 || timeout > 600) throw new ArgumentException("timeout-seconds must be between 1 and 600.");
        var reply = Invoke(command, hostId, path, timeout);
        Program.Write(new { success = reply.Success, status = reply.Status,
            callback_received = reply.CallbackReceived, endpoint = reply.Endpoint,
            execution_name = reply.ExecutionName, result = reply.Result, error = reply.ErrorMessage });
        return reply.Success ? 0 : 2;
    }

    private static string Required(Dictionary<string, string> options, string key)
    {
        if (!options.ContainsKey(key)) throw new ArgumentException("Missing --" + key);
        return options[key];
    }

    private static string ValidatePath(string command, string value)
    {
        string path = Path.GetFullPath(value);
        if (!String.Equals(Path.GetExtension(path), ".smc2", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("This prototype accepts only SMC2 files.");
        if (command == "open" && !File.Exists(path)) throw new FileNotFoundException("Input file does not exist.");
        if (command == "export" && (File.Exists(path) || Directory.Exists(path))) throw new IOException("Destination already exists.");
        if (command == "export" && !Directory.Exists(Path.GetDirectoryName(path))) throw new DirectoryNotFoundException("Destination parent directory does not exist.");
        return path;
    }

    private static string Address(string id)
    {
        return "net.pipe://localhost/" + id + "/V1.0/SolutionService/";
    }

    private static NetNamedPipeBinding Binding(int timeout)
    {
        return new NetNamedPipeBinding(NetNamedPipeSecurityMode.Transport) {
            OpenTimeout = TimeSpan.FromSeconds(Math.Min(timeout, 10)),
            CloseTimeout = TimeSpan.FromSeconds(2), SendTimeout = TimeSpan.FromSeconds(timeout),
            ReceiveTimeout = TimeSpan.FromSeconds(timeout), MaxReceivedMessageSize = 1024 * 1024
        };
    }

    private static Reply Invoke(string command, string id, string path, int timeout)
    {
        using (var callback = new Callback(command == "open" ? "OpenFromFile" : "Export"))
        {
            var factory = new DuplexChannelFactory<ISolutionService>(new InstanceContext(callback), Binding(timeout), new EndpointAddress(Address(id)));
            IClientChannel channel = null;
            try
            {
                var service = factory.CreateChannel();
                channel = (IClientChannel)service;
                channel.Open();
                if (command == "probe") return new Reply { Success = true, Status = "connected", Endpoint = Address(id) };
                var file = new FileInformation { ProjectPath = path, Password = "", Overwrite = false };
                if (command == "open") service.OpenFromFile(file); else service.Export(file);
                if (!callback.Ready.WaitOne(TimeSpan.FromSeconds(timeout)))
                    return new Reply { Success = false, Status = "timeout", Endpoint = Address(id), ErrorMessage = "No completion callback received within the deadline." };
                var result = callback.Result;
                return new Reply { Success = result.Result == ResultType.Success, Status = "callback_received", CallbackReceived = true,
                    Endpoint = Address(id), ExecutionName = result.ExecutionName, Result = result.Result.ToString(), ErrorMessage = result.ErrorMessage };
            }
            finally
            {
                Close(channel);
                Close(factory);
            }
        }
    }

    private static void Close(ICommunicationObject item)
    {
        if (item == null) return;
        try { if (item.State == CommunicationState.Faulted) item.Abort(); else item.Close(TimeSpan.FromSeconds(2)); }
        catch { item.Abort(); }
    }

    private static object SelfTest(string directory)
    {
        directory = Path.GetFullPath(directory);
        if (!Directory.Exists(directory)) throw new DirectoryNotFoundException("Self-test directory does not exist.");
        string nonce = Guid.NewGuid().ToString("N"), id = "sysmac-mcp-test-" + nonce;
        string input = Path.Combine(directory, nonce + ".smc2"), output = Path.Combine(directory, nonce + "-export.smc2");
        File.WriteAllText(input, "source-owned mock fixture");
        var mock = new MockService();
        var host = new ServiceHost(mock, new Uri(Address(id)));
        host.AddServiceEndpoint(typeof(ISolutionService), Binding(5), "");
        try
        {
            host.Open();
            var opened = Invoke("open", id, input, 5);
            bool inputCorrect = mock.LastPath == input && !mock.LastOverwrite;
            var exported = Invoke("export", id, output, 5);
            mock.Mode = "unrelated";
            var correlated = Invoke("open", id, input, 5);
            mock.Mode = "unrelated-only";
            var mismatched = Invoke("open", id, input, 1);
            mock.Mode = "error";
            var failed = Invoke("open", id, input, 5);
            mock.Mode = "timeout";
            var expired = Invoke("open", id, input, 1);
            if (!opened.Success || !exported.Success || !inputCorrect || !File.Exists(output) || !correlated.Success || correlated.ExecutionName != "OpenFromFile" || mismatched.Status != "timeout" || failed.Success || failed.Result != "RuntimeError" || expired.Status != "timeout")
                throw new InvalidOperationException("Mock WCF transport verification failed.");
            return new { mock_transport = true, real_sysmac_invoked = false, open_callback = opened.CallbackReceived,
                export_callback = exported.CallbackReceived, unrelated_and_null_callbacks_ignored = correlated.ExecutionName == "OpenFromFile",
                runtime_error_reported = !failed.Success, timeout_reported = expired.Status == "timeout" };
        }
        finally { Close(host); }
    }

    public sealed class Reply
    {
        public bool Success { get; set; }
        public string Status { get; set; }
        public bool CallbackReceived { get; set; }
        public string Endpoint { get; set; }
        public string ExecutionName { get; set; }
        public string Result { get; set; }
        public string ErrorMessage { get; set; }
    }

    [CallbackBehavior(UseSynchronizationContext = false, ConcurrencyMode = ConcurrencyMode.Multiple)]
    private sealed class Callback : ISolutionServiceCallback, IDisposable
    {
        internal readonly ManualResetEvent Ready = new ManualResetEvent(false);
        private readonly object gate = new object();
        private readonly string expectedName;
        internal ServiceResult Result;
        private bool closed;
        internal Callback(string expectedName) { this.expectedName = expectedName; }
        public void OnResultCallback(ServiceResult result) {
            if (result == null || !String.Equals(result.ExecutionName, expectedName, StringComparison.Ordinal)) return;
            lock (gate) { if (!closed && Result == null) { Result = result; Ready.Set(); } }
        }
        public void Dispose() { lock (gate) { closed = true; Ready.Dispose(); } }
    }

    [ServiceBehavior(InstanceContextMode = InstanceContextMode.Single, ConcurrencyMode = ConcurrencyMode.Multiple)]
    private sealed class MockService : ISolutionService
    {
        internal string Mode, LastPath;
        internal bool LastOverwrite;
        public string ProjectName { get { return "Source-owned mock"; } }
        public string ProjectPath { get { return LastPath; } }
        public IEnumerable<Omron.Cxap.DataLayer.Entities.Core.Entity> Devices { get { throw new NotSupportedException(); } }
        public void ClearSolutionModified() { throw new NotSupportedException(); }
        public bool IsDisabledWcfInterface() { return false; }
        public void OpenFromFile(FileInformation file) { Complete("OpenFromFile", file); }
        public void Export(FileInformation file)
        {
            using (var stream = new FileStream(file.ProjectPath, FileMode.CreateNew)) { stream.WriteByte(1); }
            Complete("Export", file);
        }
        public void DeleteProject() { throw new NotSupportedException("Not exposed by this client."); }
        private void Complete(string name, FileInformation file)
        {
            LastPath = file.ProjectPath; LastOverwrite = file.Overwrite;
            if (Mode == "timeout") return;
            var callback = OperationContext.Current.GetCallbackChannel<ISolutionServiceCallback>();
            if (Mode == "unrelated" || Mode == "unrelated-only") {
                callback.OnResultCallback(new ServiceResult { ExecutionName = "UnrelatedOperation", Result = ResultType.Success });
                callback.OnResultCallback(null);
                if (Mode == "unrelated-only") return;
            }
            callback.OnResultCallback(new ServiceResult {
                ExecutionName = name, Result = Mode == "error" ? ResultType.RuntimeError : ResultType.Success,
                ErrorMessage = Mode == "error" ? "Synthetic test error" : "" });
        }
    }
}
