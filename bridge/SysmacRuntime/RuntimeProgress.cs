using System;
using System.IO;
using System.Text;

namespace SysmacMcp.Runtime
{
    internal sealed class RuntimeProgress : IDisposable
    {
        private readonly object gate = new object();
        private FileStream stream;
        private int sequence;

        private static readonly string[] Stages = new[] {
            "entry_invoked", "response_reserved", "runtime_lease_acquired", "export_runtime_created",
            "export_request_parameters_loaded", "export_coordinator_entered", "export_arguments_validated",
            "export_storage_path_validated", "export_destination_path_validated",
            "status_runtime_creation_started", "status_runtime_created", "status_context_started",
            "status_context_observed", "status_result_prepared",
            "export_request_validated", "export_subscribed", "save_command_available",
            "save_command_unavailable", "save_command_started", "save_command_returned", "save_completed",
            "export_command_available", "export_command_unavailable", "export_rule_rejected",
            "export_preflight_passed", "native_export_started", "native_export_returned", "export_verified",
            "subscription_cleanup_started", "subscription_cleanup_completed", "subscription_cleanup_failed",
            "managed_exception_caught", "response_serialization_started", "response_written"
        };

        internal RuntimeProgress(string path)
        {
            try
            {
                stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read,
                    4096, FileOptions.WriteThrough);
            }
            catch
            {
                // Diagnostics are best effort and must never block the native operation.
                stream = null;
            }
        }

        internal void Record(string stage)
        {
            lock (gate)
            {
                if (stream == null || Array.IndexOf(Stages, stage) < 0) return;
                string line = "{\"schema_version\":1,\"sequence\":" + (sequence + 1)
                    + ",\"stage\":\"" + stage + "\"}\n";
                byte[] data = new UTF8Encoding(false).GetBytes(line);
                try
                {
                    stream.Write(data, 0, data.Length);
                    stream.Flush(true);
                    sequence++;
                }
                catch
                {
                    try { stream.Dispose(); } catch { }
                    stream = null;
                }
            }
        }

        public void Dispose()
        {
            lock (gate)
            {
                if (stream == null) return;
                try { stream.Dispose(); } catch { }
                stream = null;
            }
        }
    }
}
