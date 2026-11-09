using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Threading;

namespace SysmacMcp.Runtime
{
    public static class Entry
    {
        // Closed entry point: context/build and persisted solution list/open.
        public static int Invoke(string requestJson)
        {
            string output = null;
            Dictionary<string, object> result = null;
            FileStream stream = null;
            RuntimeLease lease = null;
            RuntimeProgress progress = null;
            try
            {
                if (requestJson == null || requestJson.Length > 16384) throw new ArgumentException("Invalid runtime request.");
                var request = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(requestJson);
                foreach (var key in request.Keys)
                    if (Array.IndexOf(new[] { "operation", "output", "progress_output", "solution_id", "controller_id", "timeout_ms", "process_id", "process_started_utc", "storage_folder", "archive_output", "offset", "limit" }, key) < 0)
                        throw new ArgumentException("Unknown runtime request field.");
                output = Required(request, "output");
                if (!Regex.IsMatch(output, @"^[A-Za-z]:[\\/]") || output.Substring(2).Contains(":")
                    || Path.GetExtension(output) != ".json")
                    throw new ArgumentException("Output must be a fully-qualified local-drive JSON path.");
                output = Path.GetFullPath(output);
                string operation = Required(request, "operation");
                if (operation == "export" || operation == "status")
                {
                    string progressOutput = Required(request, "progress_output");
                    if (!Regex.IsMatch(progressOutput, @"^[A-Za-z]:[\\/]") || progressOutput.Substring(2).Contains(":")
                        || !String.Equals(Path.GetExtension(progressOutput), ".jsonl", StringComparison.OrdinalIgnoreCase))
                        throw new ArgumentException("Progress output must be a fully-qualified local JSONL path.");
                    progressOutput = Path.GetFullPath(progressOutput);
                    if (!String.Equals(Path.GetDirectoryName(output), Path.GetDirectoryName(progressOutput), StringComparison.OrdinalIgnoreCase)
                        || String.Equals(output, progressOutput, StringComparison.OrdinalIgnoreCase))
                        throw new ArgumentException("Progress output must be a separate file beside the runtime response.");
                    progress = new RuntimeProgress(progressOutput);
                    progress.Record("entry_invoked");
                }
                else if (request.ContainsKey("progress_output"))
                    throw new ArgumentException("Progress output is only valid for status or export.");
                // Reserve before any operation; a duplicate request cannot rerun a build.
                stream = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
                if (progress != null) progress.Record("response_reserved");
                if (Array.IndexOf(new[] { "status", "rebuild", "list-projects", "open-existing", "close-active", "export" }, operation) < 0)
                    result = Failure("unsupported_operation", "Unsupported typed runtime operation.");
                else if (((operation == "rebuild" || operation == "open-existing" || operation == "close-active" || operation == "export") && (!request.ContainsKey("process_id") || !request.ContainsKey("process_started_utc")))
                    || (request.ContainsKey("process_id") && !Object.Equals(request["process_id"], Process.GetCurrentProcess().Id))
                    || (request.ContainsKey("process_started_utc") && !Object.Equals(request["process_started_utc"],
                        Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("O"))))
                    result = Failure("process_identity_changed", "The expected Sysmac process identity differs.");
                else if (Application.Current == null)
                    result = Failure("framework_unavailable", "No running Sysmac WPF application exists in this domain.");
                else
                {
                    int timeout = Number(request, "timeout_ms", 180000);
                    if (timeout < 1 || timeout > 600000) throw new ArgumentException("Runtime deadline outside allowed bounds.");
                    lease = RuntimeLease.TryEnter();
                    if (lease != null && progress != null) progress.Record("runtime_lease_acquired");
                    if (lease == null) result = Failure("runtime_busy", "Another runtime invocation or its UI work is still active.");
                    else if (operation == "list-projects" || operation == "open-existing")
                    {
                        ReflectionSolutionRuntime runtime = null;
                        ReflectionRuntime.Dispatch(delegate { runtime = new ReflectionSolutionRuntime(timeout, lease); }, timeout, lease);
                        if (operation == "list-projects") {
                            Dictionary<string, object> observed = null;
                            runtime.OnUi(delegate { observed = runtime.ListProjects(Number(request, "offset", 0), Number(request, "limit", 100)); });
                            result = observed;
                        } else {
                            result = ExistingSolutionCoordinator.Open(runtime, Required(request, "solution_id"), timeout).ToDictionary();
                        }
                    }
                    else if (operation == "close-active")
                    {
                        ReflectionSolutionCloseRuntime runtime = null;
                        ReflectionRuntime.Dispatch(delegate { runtime = new ReflectionSolutionCloseRuntime(timeout, lease); }, timeout, lease);
                        result = SolutionCloseCoordinator.Close(runtime, Required(request, "solution_id"), timeout).ToDictionary();
                    }
                    else if (operation == "export")
                    {
                        ReflectionSolutionExportRuntime runtime = null;
                        ReflectionRuntime.Dispatch(delegate {
                            runtime = new ReflectionSolutionExportRuntime(timeout, lease);
                            if (progress != null) progress.Record("export_runtime_created");
                        }, timeout, lease);
                        string solutionId = Required(request, "solution_id");
                        string storageFolder = Required(request, "storage_folder");
                        string archiveOutput = Required(request, "archive_output");
                        if (progress != null) progress.Record("export_request_parameters_loaded");
                        result = SolutionExportCoordinator.Execute(runtime, solutionId,
                            storageFolder, archiveOutput, timeout,
                            progress == null ? null : new Action<string>(progress.Record)).ToDictionary();
                    }
                    else
                    {
                        ReflectionRuntime runtime = null;
                        if (operation == "status" && progress != null) progress.Record("status_runtime_creation_started");
                        ReflectionRuntime.Dispatch(delegate { runtime = new ReflectionRuntime(timeout, lease); }, timeout, lease);
                        if (operation == "status" && progress != null) progress.Record("status_runtime_created");
                        if (operation == "status") {
                            Dictionary<string, object> observed = null;
                            runtime.OnUi(delegate {
                                if (progress != null) progress.Record("status_context_started");
                                observed = runtime.Status();
                                if (progress != null) progress.Record("status_context_observed");
                            });
                            result = observed;
                            result["success"] = true; result["status"] = "context_observed"; result["operation_started"] = false;
                            if (progress != null) progress.Record("status_result_prepared");
                        } else {
                            var expected = new BuildContext { SolutionId = Required(request, "solution_id"), DeviceId = Required(request, "controller_id") };
                            result = BuildCoordinator.Execute(runtime, expected, timeout).ToDictionary();
                        }
                    }
                    result["process_id"] = Process.GetCurrentProcess().Id;
                    result["process_started_utc"] = Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("O");
                }
            }
            catch (Exception error) {
                if (progress != null) progress.Record("managed_exception_caught");
                result = Failure("runtime_error", Describe(error));
            }
            bool settled = true;
            try { if (lease != null) settled = lease.CloseAndInspect(); }
            catch (Exception error) { settled = false; result["error"] = Describe(error); }
            if (!settled) {
                result["success"] = false; result["native_status"] = result["status"];
                result["status"] = "runtime_work_unsettled"; result["cleanup_verified"] = false;
                result["outcome_unknown"] = result.ContainsKey("operation_started") && Object.Equals(result["operation_started"], true);
            }
            result["runtime_work_settled"] = settled;
            if (!result.ContainsKey("cleanup_verified")) result["cleanup_verified"] = settled;
            try
            {
                if (stream == null) return 3; // Never overwrite an existing receipt.
                if (progress != null) progress.Record("response_serialization_started");
                result["process_id"] = Process.GetCurrentProcess().Id;
                result["process_started_utc"] = Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("O");
                result["schema_version"] = 1;
                var serializer = new JavaScriptSerializer { MaxJsonLength = 1024 * 1024 };
                byte[] data = Encoding.UTF8.GetBytes(serializer.Serialize(result));
                if (data.Length > 1024 * 1024) throw new InvalidDataException("Runtime response limit exceeded.");
                stream.Write(data, 0, data.Length); stream.Flush(true);
                if (progress != null) progress.Record("response_written");
                return result.ContainsKey("success") && (bool)result["success"] ? 0 : 2;
            }
            catch { return 3; }
            finally {
                if (stream != null) stream.Dispose();
                if (progress != null) progress.Dispose();
            }
        }

        private static string Required(Dictionary<string, object> request, string key)
        {
            object value;
            if (!request.TryGetValue(key, out value) || !(value is string) || String.IsNullOrEmpty((string)value))
                throw new ArgumentException("Missing runtime field: " + key);
            return (string)value;
        }
        private static int Number(Dictionary<string, object> request, string key, int fallback)
        {
            object value;
            if (!request.TryGetValue(key, out value)) return fallback;
            if (!(value is int)) throw new ArgumentException("Runtime numeric field must be an integer: " + key);
            return (int)value;
        }
        private static string Describe(Exception error)
        {
            string text = error.ToString();
            return text.Length <= 5000 ? text : text.Substring(0, 5000);
        }
        private static Dictionary<string, object> Failure(string status, string error)
        {
            return new Dictionary<string, object> {
                { "success", false }, { "status", status }, { "error", error }, { "operation_started", false }
            };
        }
    }
}
