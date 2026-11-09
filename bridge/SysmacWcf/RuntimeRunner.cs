using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

// Closed, local runtime operations. Never exposes caller-provided scripts or command IDs.
internal static class RuntimeRunner
{
    private const int Limit = 1024 * 1024;
    internal static bool IsRuntime(string operation) {
        return Array.IndexOf(new[] { "runtime-status", "runtime-rebuild", "runtime-list-projects", "runtime-open-existing", "runtime-close-active", "runtime-export" }, operation) >= 0;
    }
    internal static string Absolute(string value)
    {
        if (value == null || !Regex.IsMatch(value, @"^[A-Za-z]:[\\/]") || value.Substring(2).Contains(":"))
            throw new ArgumentException("Runtime paths must be fully-qualified local-drive paths.");
        return Path.GetFullPath(value);
    }
    internal static string Hash(string file)
    {
        using (var algorithm = SHA256.Create())
        using (var stream = File.OpenRead(file))
            return BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }
    private static Dictionary<string, object> Json(string file, string expectedHash = null)
    {
        if (!File.Exists(file)) throw new FileNotFoundException("Runtime profile or request does not exist.");
        if (new FileInfo(file).Length > 16384) throw new InvalidDataException("Runtime JSON exceeds size limit.");
        byte[] data;
        using (var stream = File.OpenRead(file))
        using (var memory = new MemoryStream())
        {
            var buffer = new byte[4096]; int count;
            while ((count = stream.Read(buffer, 0, buffer.Length)) > 0)
            {
                if (memory.Length + count > 16384) throw new InvalidDataException("Runtime JSON exceeds size limit.");
                memory.Write(buffer, 0, count);
            }
            data = memory.ToArray();
        }
        if (expectedHash != null)
            using (var algorithm = SHA256.Create())
                if (BitConverter.ToString(algorithm.ComputeHash(data)).Replace("-", "").ToLowerInvariant() != expectedHash)
                    throw new InvalidDataException("Runtime JSON bytes differ from pinned input.");
        return new JavaScriptSerializer { MaxJsonLength = 16384 }.Deserialize<Dictionary<string, object>>(new UTF8Encoding(false, true).GetString(data));
    }
    private static string Required(Dictionary<string, string> options, string name)
    {
        string value;
        if (!options.TryGetValue(name, out value)) throw new ArgumentException("Missing runtime --" + name);
        return value;
    }
    private static string Component(Dictionary<string, object> profile, string name)
    {
        object raw;
        if (!profile.TryGetValue(name, out raw)) throw new InvalidDataException("Missing runtime component: " + name);
        var item = raw as Dictionary<string, object>;
        if (item == null || !item.ContainsKey("path") || !item.ContainsKey("sha256"))
            throw new InvalidDataException("Invalid runtime component.");
        string file = Absolute(item["path"] as string), expected = item["sha256"] as string;
        if (expected == null || !Regex.IsMatch(expected, "^[a-f0-9]{64}$") || !File.Exists(file) || Hash(file) != expected)
            throw new InvalidDataException("Runtime component hash differs: " + name);
        return file;
    }
    private static bool FileSystemEntryExists(string path)
    {
        try { File.GetAttributes(path); return true; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }
    internal static void Validate(string operation, Dictionary<string, string> options)
    {
        if (!IsRuntime(operation)) throw new ArgumentException("Unsupported runtime operation.");
        string profilePath = Absolute(Required(options, "runtime-profile"));
        if (!File.Exists(profilePath)) throw new FileNotFoundException("Runtime profile does not exist.");
        var profile = Json(profilePath);
        if (!profile.ContainsKey("schema_version") || !Object.Equals(profile["schema_version"], 1))
            throw new InvalidDataException("Unsupported runtime profile.");
        foreach (string name in new[] { "node", "worker", "assembly", "template", "frida_module" }) Component(profile, name);
        string requestPath = Absolute(Required(options, "path"));
        var request = Json(requestPath);
        foreach (string key in request.Keys)
            if (Array.IndexOf(new[] { "operation", "process_id", "process_started_utc", "solution_id", "controller_id", "storage_folder", "archive_output", "output", "progress_output", "timeout_ms", "offset", "limit" }, key) < 0)
                throw new ArgumentException("Unknown runtime request field.");
        string action = operation.Substring("runtime-".Length);
        if (!request.ContainsKey("operation") || !Object.Equals(request["operation"], action))
            throw new ArgumentException("Runtime operation differs from request.");
        if (!request.ContainsKey("process_id") || !(request["process_id"] is int) || (int)request["process_id"] < 1)
            throw new ArgumentException("A positive Sysmac process ID is required.");
        int timeout = Int32.Parse(Required(options, "timeout-seconds"));
        if (timeout < 1 || timeout > 600 || !request.ContainsKey("timeout_ms")
            || !Object.Equals(request["timeout_ms"], timeout * 1000)) throw new ArgumentException("Runtime deadlines differ.");
        string output = Absolute(request.ContainsKey("output") ? request["output"] as string : null);
        if (Path.GetExtension(output) != ".json" || FileSystemEntryExists(output)
            || !Directory.Exists(Path.GetDirectoryName(output))) throw new IOException("Runtime receipt must be a fresh local JSON path.");
        if (action == "export" || action == "status")
        {
            if (!request.ContainsKey("progress_output")) throw new ArgumentException("Missing runtime progress path.");
            string progress = Absolute(request.ContainsKey("progress_output") ? request["progress_output"] as string : null);
            if (!String.Equals(Path.GetExtension(progress), ".jsonl", StringComparison.OrdinalIgnoreCase)
                || FileSystemEntryExists(progress)
                || !String.Equals(Path.GetDirectoryName(progress), Path.GetDirectoryName(output), StringComparison.OrdinalIgnoreCase))
                throw new IOException("Runtime progress must be a fresh JSONL file beside the runtime receipt.");
        }
        else if (request.ContainsKey("progress_output"))
            throw new ArgumentException("Runtime progress output is only valid for status or export.");
        if (action == "rebuild" || action == "open-existing" || action == "close-active" || action == "export")
        {
            foreach (string key in action == "rebuild" ? new[] { "solution_id", "controller_id" } : new[] { "solution_id" })
            {
                Guid parsed;
                if (!request.ContainsKey(key) || !Guid.TryParse(request[key] as string, out parsed) || parsed == Guid.Empty)
                    throw new ArgumentException("Expected solution/controller GUID missing.");
            }
            if (!request.ContainsKey("process_started_utc") || !Regex.IsMatch(request["process_started_utc"] as string ?? "",
                @"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z$")) throw new ArgumentException("Expected process start identity missing.");
        }
        if (action == "export")
        {
            string folder = Absolute(request.ContainsKey("storage_folder") ? request["storage_folder"] as string : null);
            if (!Directory.Exists(folder)) throw new DirectoryNotFoundException("Attested Sysmac persistence folder does not exist.");
            string archive = Absolute(request.ContainsKey("archive_output") ? request["archive_output"] as string : null);
            if (!String.Equals(Path.GetExtension(archive), ".smc2", StringComparison.OrdinalIgnoreCase)
                || FileSystemEntryExists(archive)
                || !Directory.Exists(Path.GetDirectoryName(archive)))
                throw new IOException("Native export output must be a new local SMC2 file with an existing parent directory.");
            string boundary = folder.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (archive.StartsWith(boundary, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Native export output must be outside the attested Sysmac persistence folder.");
        }
        if (action == "list-projects") {
            foreach (string key in new[] { "offset", "limit" })
                if (request.ContainsKey(key) && (!(request[key] is int) || (int)request[key] < (key == "limit" ? 1 : 0)
                    || (key == "limit" && (int)request[key] > 200))) throw new ArgumentException("Invalid project inventory page.");
        }
    }
    internal static object Run(string operation, Dictionary<string, string> options)
    {
        VerifyInputs(options); // Hashes originate in the medium parent, before UAC/GO.
        Validate(operation, options);
        string profilePath = Absolute(options["runtime-profile"]), requestPath = Absolute(options["path"]);
        string profileHash = options["runtime-profile-sha256"], requestHash = options["request-sha256"];
        var profile = Json(profilePath, profileHash);
        var request = Json(requestPath, requestHash);
        VerifyInputs(options);
        string vendor = options.ContainsKey("vendor-root") ? Absolute(options["vendor-root"]) : @"C:\Program Files\OMRON\Sysmac Studio";
        string expectedExe = Path.Combine(vendor, "SysmacStudio.exe");
        string processStart;
        using (var target = Process.GetProcessById((int)request["process_id"]))
        {
            if (target.SessionId != Process.GetCurrentProcess().SessionId
                || !String.Equals(Path.GetFullPath(target.MainModule.FileName), expectedExe, StringComparison.OrdinalIgnoreCase)
                || Hash(expectedExe) != "a74caabd98980331c63dabb49b2f7a977854c29f406d061f60feef467ab1248f")
                throw new InvalidOperationException("The selected process is not the pinned local Sysmac build.");
            processStart = target.StartTime.ToUniversalTime().ToString("O");
            if ((operation == "runtime-rebuild" || operation == "runtime-open-existing" || operation == "runtime-close-active" || operation == "runtime-export")
                && !Object.Equals(request["process_started_utc"], processStart))
                throw new InvalidOperationException("Sysmac process start identity changed.");
        }
        string node = Component(profile, "node"), worker = Component(profile, "worker");
        var arguments = new[] { worker, "--request", requestPath, "--profile", profilePath,
            "--request-sha256", requestHash, "--profile-sha256", profileHash, "--process-start", processStart };
        var encoded = new List<string>();
        foreach (string part in arguments) encoded.Add(ElevatedTransport.Quote(part));
        using (var child = Process.Start(new ProcessStartInfo(node, String.Join(" ", encoded)) {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false), StandardErrorEncoding = new UTF8Encoding(false),
            WorkingDirectory = Path.GetDirectoryName(worker)
        }))
        {
            if (child == null) throw new IOException("Node worker did not start.");
            var stdout = Task.Run(() => Read(child.StandardOutput));
            var stderr = Task.Run(() => Read(child.StandardError));
            int timeout = Int32.Parse(options["timeout-seconds"]);
            if (!child.WaitForExit((timeout + 40) * 1000))
            {
                try { child.Kill(); } catch { }
                throw new TimeoutException("Owned runtime worker deadline expired; Sysmac outcome is unknown. Do not retry automatically.");
            }
            if (!Task.WaitAll(new Task[] { stdout, stderr }, 5000)) throw new TimeoutException("Worker output incomplete.");
            var result = new JavaScriptSerializer { MaxJsonLength = Limit }.Deserialize<Dictionary<string, object>>(stdout.Result);
            if (!result.ContainsKey("success") || !(result["success"] is bool)
                || ((bool)result["success"] && child.ExitCode != 0)) throw new InvalidDataException("Worker exit contradicts runtime result.");
            if (Hash(profilePath) != profileHash || Hash(requestPath) != requestHash)
                throw new InvalidDataException("Runtime inputs changed; outcome is unknown.");
            return result;
        }
    }
    internal static void VerifyInputs(Dictionary<string, string> options)
    {
        foreach (var pair in new[] { new[] { "path", "request-sha256" }, new[] { "runtime-profile", "runtime-profile-sha256" } })
        {
            string expected = Required(options, pair[1]);
            if (!Regex.IsMatch(expected, "^[a-f0-9]{64}$") || Hash(Absolute(Required(options, pair[0]))) != expected)
                throw new InvalidDataException("Runtime request/profile hash changed before execution.");
        }
    }
    private static string Read(StreamReader reader)
    {
        var text = new StringBuilder();
        var buffer = new char[4096];
        int count;
        while ((count = reader.Read(buffer, 0, buffer.Length)) > 0)
        {
            if (text.Length + count > Limit) throw new InvalidDataException("Worker output exceeds limit.");
            text.Append(buffer, 0, count);
        }
        return text.ToString();
    }
}
