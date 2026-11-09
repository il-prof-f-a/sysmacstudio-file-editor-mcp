using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace SysmacMcp.Runtime
{
    public sealed class SolutionState
    {
        public bool HasActiveSolution, IsImporting, OpeningDisabled, ApplicationReady;
        public string ActiveSolutionId;
    }
    public interface IExistingSolutionRuntime
    {
        void OnUi(Action action);
        SolutionState State();
        int MatchingProjects(string solutionId);
        bool CanView(string solutionId);
        bool CanOpen(string solutionId);
        IDisposable SubscribeOpened(Action<string> opened);
        void InitializeController();
        void WhenReady(Action action);
        bool TryOpen(string solutionId, out string error);
    }
    public sealed class SolutionOutcome
    {
        public bool Success, OperationStarted, ControllerInitializationStarted, OpenRequested,
            OpenAccepted, OpenedEvent, OutcomeUnknown, CleanupVerified = true;
        public string Status = "not_started", SolutionId, ActiveSolutionId, Error;
        public Dictionary<string, object> ToDictionary()
        {
            return new Dictionary<string, object> {
                { "success", Success }, { "status", Status }, { "solution_id", SolutionId },
                { "active_solution_id", ActiveSolutionId }, { "operation_started", OperationStarted },
                { "controller_initialization_started", ControllerInitializationStarted },
                { "open_requested", OpenRequested }, { "open_accepted", OpenAccepted },
                { "opened_event", OpenedEvent }, { "outcome_unknown", OutcomeUnknown },
                { "cleanup_verified", CleanupVerified }, { "error", Error }
            };
        }
    }
    public static class ExistingSolutionCoordinator
    {
        public static SolutionOutcome Open(IExistingSolutionRuntime runtime, string solutionId, int timeoutMilliseconds)
        {
            Guid expected;
            if (runtime == null || !Guid.TryParse(solutionId, out expected) || expected == Guid.Empty)
                throw new ArgumentException("Expected solution GUID is required.");
            if (timeoutMilliseconds < 1 || timeoutMilliseconds > 600000)
                throw new ArgumentException("Runtime deadline outside allowed bounds.");
            var report = new SolutionOutcome { SolutionId = expected.ToString() };
            var gate = new object();
            var done = new ManualResetEvent(false);
            var elapsed = Stopwatch.StartNew();
            bool active = true, armed = false, matched = false, nativeReturned = false, scheduled = false;
            IDisposable subscription = null;
            Action<string, string> fail = delegate(string status, string error) {
                lock (gate) {
                    if (!active) return;
                    report.Success = false; report.Status = status; report.Error = error;
                    report.OutcomeUnknown = report.OperationStarted;
                    done.Set();
                }
            };
            Func<bool> inTime = delegate {
                lock (gate) return active && elapsed.ElapsedMilliseconds < timeoutMilliseconds;
            };
            Func<SolutionState, string> idleError = delegate(SolutionState state) {
                if (state == null) throw new InvalidOperationException("Solution state unavailable.");
                if (state.HasActiveSolution) return "solution_already_open";
                return state.IsImporting || state.OpeningDisabled ? "opening_unavailable" : null;
            };
            Func<bool> validateCandidate = delegate {
                int matches = runtime.MatchingProjects(report.SolutionId);
                if (matches != 1) {
                    fail(matches == 0 ? "project_not_found" : "project_identity_ambiguous", null); return false;
                }
                if (!runtime.CanView(report.SolutionId)) { fail("permission_denied", null); return false; }
                return true;
            };
            Action requestOpen = delegate {
                try {
                    if (!inTime()) { fail("deadline_before_open", null); return; }
                    string idle = idleError(runtime.State());
                    if (idle != null) { fail(idle, null); return; }
                    if (!validateCandidate()) return;
                    bool allowed = runtime.CanOpen(report.SolutionId);
                    if (!validateCandidate()) return;
                    var finalState = runtime.State();
                    idle = idleError(finalState);
                    if (idle != null) { fail(idle, null); return; }
                    if (!finalState.ApplicationReady) { fail("application_not_ready", null); return; }
                    if (!inTime()) { fail("deadline_before_open", null); return; }
                    if (!allowed) { fail("open_command_unavailable", null); return; }
                    lock (gate) {
                        if (!active || elapsed.ElapsedMilliseconds >= timeoutMilliseconds) return;
                        armed = true; report.OpenRequested = true; report.OperationStarted = true;
                    }
                    string error;
                    bool accepted = runtime.TryOpen(report.SolutionId, out error);
                    lock (gate) {
                        if (!active) return;
                        nativeReturned = true; report.OpenAccepted = accepted; report.Error = error;
                        if (!accepted) { report.Status = "open_failed"; report.OutcomeUnknown = matched; done.Set(); }
                        else if (matched) done.Set();
                    }
                } catch (Exception error) { fail("runtime_error", Describe(error)); }
            };
            try {
                runtime.OnUi(delegate {
                    if (!inTime()) return;
                    var state = runtime.State();
                    string idle = idleError(state);
                    if (idle != null) { fail(idle, null); return; }
                    if (!validateCandidate()) return;
                    if (!inTime()) { fail("deadline_before_open", null); return; }
                    var created = runtime.SubscribeOpened(delegate(string id) {
                        Guid actual;
                        lock (gate) {
                            if (!active || !armed || !Guid.TryParse(id, out actual) || actual != expected) return;
                            matched = true; report.OpenedEvent = true;
                            if (nativeReturned && report.OpenAccepted) done.Set();
                        }
                    });
                    bool retained;
                    lock (gate) {
                        retained = active;
                        if (retained) subscription = created;
                    }
                    if (!retained) { created.Dispose(); return; }
                    // Permission/availability reads may have yielded to other UI work.
                    state = runtime.State();
                    idle = idleError(state);
                    if (idle != null) { fail(idle, null); return; }
                    if (!inTime()) { fail("deadline_before_open", null); return; }
                    if (!state.ApplicationReady) {
                        lock (gate) {
                            if (!active || elapsed.ElapsedMilliseconds >= timeoutMilliseconds) return;
                            report.ControllerInitializationStarted = true; report.OperationStarted = true;
                        }
                        runtime.InitializeController();
                    }
                    if (!inTime()) { fail("application_ready_timeout", null); return; }
                    lock (gate) scheduled = true;
                    // Vendor retains one callback: lease serializes requests and callback checks its deadline.
                    runtime.WhenReady(requestOpen);
                });
                if (!scheduled) return report;
                int remaining = timeoutMilliseconds - (int)Math.Min(elapsed.ElapsedMilliseconds, Int32.MaxValue);
                if (remaining <= 0 || !done.WaitOne(remaining)) {
                    fail(report.OpenRequested ? "open_completion_unknown" : "application_ready_timeout", null);
                    return report;
                }
                lock (gate) {
                    if (!nativeReturned || !report.OpenAccepted || !matched) return report;
                }
                runtime.OnUi(delegate {
                    if (!inTime()) { fail("snapshot_deadline_exceeded", null); return; }
                    var after = runtime.State();
                    lock (gate) {
                        if (!active) return;
                        if (elapsed.ElapsedMilliseconds >= timeoutMilliseconds) {
                            report.Status = "snapshot_deadline_exceeded"; report.OutcomeUnknown = true; return;
                        }
                        report.ActiveSolutionId = after == null ? null : after.ActiveSolutionId;
                        Guid actual;
                        bool correct = after != null && after.HasActiveSolution
                            && Guid.TryParse(after.ActiveSolutionId, out actual) && actual == expected;
                        report.OpenedEvent = matched;
                        report.Success = correct && report.OpenAccepted && nativeReturned && matched;
                        report.Status = report.Success ? "open_succeeded" : "context_changed";
                        report.OutcomeUnknown = !report.Success;
                    }
                });
                return report;
            } catch (Exception error) {
                fail("runtime_error", Describe(error)); return report;
            } finally {
                lock (gate) active = false;
                try { if (subscription != null) subscription.Dispose(); }
                catch (Exception error) {
                    report.CleanupVerified = false; report.Success = false; report.Status = "cleanup_failed";
                    report.Error = Describe(error);
                }
                done.Dispose();
            }
        }
        private static string Describe(Exception error)
        {
            string text = error.ToString();
            return text.Length <= 5000 ? text : text.Substring(0, 5000);
        }
    }

    public sealed class SolutionCloseState
    {
        public bool HasActiveSolution, FileMode, AutoConnect, SuppressSave, IsImporting,
            OpeningDisabled, ApplicationReady;
        public string ActiveSolutionId;
    }
    public interface ISolutionCloseRuntime
    {
        void OnUi(Action action);
        SolutionCloseState State();
        void CommitEditorChanges();
        bool IsSolutionModified(string solutionId);
        bool CanClose(string solutionId);
        IDisposable SubscribeClose(Action<string> closing, Action<string> closed, Action<string> cancelled);
        void Close(string solutionId);
    }
    public sealed class SolutionCloseOutcome
    {
        public bool Success, OperationStarted, EditorCommitCompleted, ModifiedAfterEditorCommit,
            CloseCommandAvailable, CloseRequested, CloseReturned, ClosingEvent, ClosedEvent,
            CancelledEvent, OutcomeUnknown, CleanupVerified = true;
        public string Status = "not_started", SolutionId, ActiveSolutionId, Error;
        public Dictionary<string, object> ToDictionary()
        {
            return new Dictionary<string, object> {
                { "success", Success }, { "status", Status }, { "solution_id", SolutionId },
                { "active_solution_id", ActiveSolutionId }, { "operation_started", OperationStarted },
                { "editor_commit_completed", EditorCommitCompleted },
                { "modified_after_editor_commit", ModifiedAfterEditorCommit },
                { "close_command_available", CloseCommandAvailable },
                { "close_requested", CloseRequested }, { "close_returned", CloseReturned },
                { "closing_event", ClosingEvent }, { "closed_event", ClosedEvent },
                { "cancelled_event", CancelledEvent }, { "outcome_unknown", OutcomeUnknown },
                { "cleanup_verified", CleanupVerified }, { "error", Error }
            };
        }
    }
    public static class SolutionCloseCoordinator
    {
        public static SolutionCloseOutcome Close(ISolutionCloseRuntime runtime, string solutionId, int timeoutMilliseconds)
        {
            Guid expected;
            if (runtime == null || !Guid.TryParse(solutionId, out expected) || expected == Guid.Empty)
                throw new ArgumentException("Expected solution GUID is required.");
            if (timeoutMilliseconds < 1 || timeoutMilliseconds > 600000)
                throw new ArgumentException("Runtime deadline outside allowed bounds.");

            var report = new SolutionCloseOutcome { SolutionId = expected.ToString() };
            var gate = new object();
            var elapsed = Stopwatch.StartNew();
            bool active = true, armed = false, initialStateChecked = false;
            IDisposable subscription = null;
            Func<bool> inTime = delegate { lock (gate) return active && elapsed.ElapsedMilliseconds < timeoutMilliseconds; };
            Action<string, string> fail = delegate(string status, string error) {
                lock (gate) {
                    if (!active) return;
                    report.Success = false; report.Status = status; report.Error = error;
                    report.OutcomeUnknown = report.OperationStarted;
                }
            };
            Func<SolutionCloseState, bool, string> guard = delegate(SolutionCloseState state, bool initial) {
                if (state == null) return "runtime_error";
                if (!state.HasActiveSolution) return initial ? "no_active_solution" : "solution_context_changed";
                Guid actual;
                if (!Guid.TryParse(state.ActiveSolutionId, out actual) || actual != expected) return "solution_context_changed";
                if (state.FileMode) return "file_mode_unsupported";
                if (state.AutoConnect) return "autoconnect_unsupported";
                if (state.SuppressSave) return "save_suppression_unsupported";
                if (state.IsImporting || state.OpeningDisabled) return "opening_unavailable";
                if (!state.ApplicationReady) return "application_not_ready";
                return null;
            };
            Func<string, bool> validateState = delegate(string status) {
                var state = runtime.State();
                report.ActiveSolutionId = state == null ? null : state.ActiveSolutionId;
                string problem = guard(state, !initialStateChecked);
                initialStateChecked = true;
                if (problem != null) { fail(problem, null); return false; }
                if (!inTime()) { fail(status, null); return false; }
                return true;
            };
            Action<string> observeClosing = delegate(string id) {
                Guid actual;
                lock (gate) if (active && armed && Guid.TryParse(id, out actual) && actual == expected) report.ClosingEvent = true;
            };
            Action<string> observeClosed = delegate(string id) {
                Guid actual;
                lock (gate) if (active && armed && Guid.TryParse(id, out actual) && actual == expected) report.ClosedEvent = true;
            };
            Action<string> observeCancelled = delegate(string id) {
                Guid actual;
                lock (gate) if (active && armed && Guid.TryParse(id, out actual) && actual == expected) report.CancelledEvent = true;
            };

            try {
                runtime.OnUi(delegate {
                    if (!inTime()) { fail("deadline_before_close", null); return; }
                    if (!validateState("deadline_before_close")) return;

                    runtime.CommitEditorChanges();
                    report.EditorCommitCompleted = true;
                    if (!validateState("deadline_before_close")) return;
                    report.ModifiedAfterEditorCommit = runtime.IsSolutionModified(report.SolutionId);
                    if (report.ModifiedAfterEditorCommit) { fail("unsaved_changes", null); return; }

                    report.CloseCommandAvailable = runtime.CanClose(report.SolutionId);
                    if (!inTime()) { fail("deadline_before_close", null); return; }
                    if (!validateState("deadline_before_close")) return;
                    if (!report.CloseCommandAvailable) { fail("close_command_unavailable", null); return; }
                    if (runtime.IsSolutionModified(report.SolutionId)) {
                        report.ModifiedAfterEditorCommit = true;
                        fail("unsaved_changes", null); return;
                    }

                    subscription = runtime.SubscribeClose(observeClosing, observeClosed, observeCancelled);
                    if (subscription == null) throw new InvalidOperationException("Native close event subscription unavailable.");
                    if (!validateState("deadline_before_close")) return;
                    if (runtime.IsSolutionModified(report.SolutionId)) {
                        report.ModifiedAfterEditorCommit = true;
                        fail("unsaved_changes", null); return;
                    }
                    if (!inTime()) { fail("deadline_before_close", null); return; }

                    lock (gate) {
                        if (!active || elapsed.ElapsedMilliseconds >= timeoutMilliseconds) return;
                        armed = true; report.OperationStarted = true; report.CloseRequested = true;
                    }
                    runtime.Close(report.SolutionId);
                    lock (gate) if (active) report.CloseReturned = true;
                });

                if (!report.CloseRequested) return report;
                if (!report.CloseReturned) {
                    fail("close_completion_unknown", null); return report;
                }
                if (!inTime()) { fail("close_completion_unknown", null); return report; }

                runtime.OnUi(delegate {
                    if (!inTime()) { fail("close_completion_unknown", null); return; }
                    var after = runtime.State();
                    report.ActiveSolutionId = after == null ? null : after.ActiveSolutionId;
                    Guid actual;
                    bool closed = after != null && !after.HasActiveSolution && after.ActiveSolutionId == null;
                    bool expectedEvents = report.ClosingEvent && report.ClosedEvent && !report.CancelledEvent;
                    lock (gate) {
                        if (!active) return;
                        if (elapsed.ElapsedMilliseconds >= timeoutMilliseconds) {
                            report.Status = "close_completion_unknown"; report.OutcomeUnknown = true; return;
                        }
                        if (after != null && after.HasActiveSolution && Guid.TryParse(after.ActiveSolutionId, out actual)
                            && actual != expected) {
                            report.Status = "solution_context_changed"; report.OutcomeUnknown = true; return;
                        }
                        if (report.CancelledEvent) {
                            bool stillExpected = after != null && after.HasActiveSolution
                                && Guid.TryParse(after.ActiveSolutionId, out actual) && actual == expected;
                            report.Success = false;
                            report.Status = stillExpected ? "close_cancelled" : "solution_context_changed";
                            report.OutcomeUnknown = !stillExpected;
                            return;
                        }
                        report.Success = report.CloseReturned && expectedEvents && closed;
                        report.Status = report.Success ? "close_succeeded" : "close_completion_unknown";
                        report.OutcomeUnknown = !report.Success;
                    }
                });
                return report;
            } catch (TimeoutException error) {
                fail(report.CloseRequested ? "close_completion_unknown" : "runtime_error", Describe(error));
                return report;
            } catch (Exception error) {
                fail("runtime_error", Describe(error));
                return report;
            } finally {
                lock (gate) active = false;
                try { if (subscription != null) subscription.Dispose(); }
                catch (Exception error) {
                    report.CleanupVerified = false; report.Success = false; report.Status = "cleanup_failed";
                    report.Error = Describe(error);
                }
            }
        }
        private static string Describe(Exception error)
        {
            string text = error.ToString();
            return text.Length <= 5000 ? text : text.Substring(0, 5000);
        }
    }

    public sealed class SolutionExportState
    {
        public bool HasActiveSolution, FileMode, AutoConnect, SuppressSave, IsImporting, OpeningDisabled;
        public string ActiveSolutionId, StorageFolder, CallbackError;
    }
    public interface ISolutionExportRuntime
    {
        void OnUi(Action action);
        SolutionExportState State();
        IDisposable Subscribe(Action<string> saving, Action<string> saved, Action<Dictionary<string, object>> io);
        bool CanSave();
        void Save();
        bool CanExport();
        bool PreExecuteExport();
        void Export(string destination);
    }
    public static class SolutionCapabilityCoordinator
    {
        public static Dictionary<string, object> Observe(ISolutionExportRuntime runtime)
        {
            if (runtime == null) throw new ArgumentNullException("runtime");
            SolutionExportState state = runtime.State();
            bool active = state != null && state.HasActiveSolution;
            return new Dictionary<string, object> {
                { "has_active_solution", active },
                { "can_save_solution", active && runtime.CanSave() },
                { "can_export_solution", active && runtime.CanExport() },
                { "file_mode", state != null && state.FileMode },
                { "auto_connect_solution", state != null && state.AutoConnect },
                { "save_suppressed", state != null && state.SuppressSave },
                { "is_importing", state != null && state.IsImporting },
                { "opening_disabled", state != null && state.OpeningDisabled }
            };
        }
    }
    public sealed class SolutionExportOutcome
    {
        public bool Success, OperationStarted, SaveRequested, SaveCommandReturned, SaveStarted,
            SaveCompleted, ExportPreflightPassed, ExportStarted, ExportReturned,
            OutcomeUnknown, CleanupVerified = true;
        public string Status = "not_started", SolutionId, ActiveSolutionId, StorageFolder, ArchiveOutput, Error;
        public List<Dictionary<string, object>> IoEvents = new List<Dictionary<string, object>>();
        public Dictionary<string, object> ToDictionary()
        {
            return new Dictionary<string, object> {
                { "success", Success }, { "status", Status }, { "solution_id", SolutionId },
                { "active_solution_id", ActiveSolutionId }, { "storage_folder", StorageFolder },
                { "archive_output", ArchiveOutput }, { "operation_started", OperationStarted },
                { "save_requested", SaveRequested }, { "save_command_returned", SaveCommandReturned },
                { "save_started", SaveStarted }, { "save_completed", SaveCompleted },
                { "export_preflight_passed", ExportPreflightPassed }, { "export_started", ExportStarted },
                { "export_returned", ExportReturned }, { "outcome_unknown", OutcomeUnknown },
                { "cleanup_verified", CleanupVerified }, { "io_events", IoEvents }, { "error", Error }
            };
        }
    }
    public static class SolutionExportCoordinator
    {
        public static SolutionExportOutcome Execute(ISolutionExportRuntime runtime, string solutionId,
            string storageFolder, string archiveOutput, int timeoutMilliseconds, Action<string> progress = null)
        {
            Record(progress, "export_coordinator_entered");
            Guid expected;
            if (runtime == null || !Guid.TryParse(solutionId, out expected) || expected == Guid.Empty)
                throw new ArgumentException("Expected solution GUID is required.");
            if (timeoutMilliseconds < 1 || timeoutMilliseconds > 600000)
                throw new ArgumentException("Runtime deadline outside allowed bounds.");
            Record(progress, "export_arguments_validated");
            string folder = CanonicalFolder(storageFolder);
            Record(progress, "export_storage_path_validated");
            string output = Path.GetFullPath(archiveOutput ?? "");
            if (!String.Equals(Path.GetExtension(output), ".smc2", StringComparison.OrdinalIgnoreCase)
                || Path.GetPathRoot(output) == null || Path.GetPathRoot(output).Length != 3 || output.Substring(2).Contains(":")
                || FileSystemEntryExists(output)
                || !Directory.Exists(Path.GetDirectoryName(output))
                || output.StartsWith(folder.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                    + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Export requires a new local SMC2 path outside the active persistence folder.");
            RejectReparseAncestors(Path.GetDirectoryName(output));
            Record(progress, "export_destination_path_validated");
            Record(progress, "export_request_validated");

            var report = new SolutionExportOutcome { SolutionId = expected.ToString(), StorageFolder = folder, ArchiveOutput = output };
            var gate = new object();
            var saving = new List<string>();
            var saved = new List<string>();
            var saveOrder = new List<string>();
            IDisposable subscription = null;
            Action<string> onSaving = delegate(string id) {
                Guid actual;
                if (Guid.TryParse(id, out actual) && actual == expected)
                    lock (gate) { if (saving.Count < 10) { saving.Add(actual.ToString()); saveOrder.Add("saving"); } }
            };
            Action<string> onSaved = delegate(string id) {
                Guid actual;
                if (Guid.TryParse(id, out actual) && actual == expected)
                    lock (gate) { if (saved.Count < 10) { saved.Add(actual.ToString()); saveOrder.Add("saved"); } }
            };
            Action<Dictionary<string, object>> onIo = delegate(Dictionary<string, object> item) {
                lock (gate) { if (report.IoEvents.Count < 100) report.IoEvents.Add(item); }
            };
            try
            {
                runtime.OnUi(delegate {
                    string issue = CheckState(runtime.State(), expected, folder);
                    if (issue != null) { report.Status = issue; return; }
                    subscription = runtime.Subscribe(onSaving, onSaved, onIo);
                    if (subscription == null) throw new InvalidOperationException("Native save/export subscriptions are unavailable.");
                    Record(progress, "export_subscribed");
                });
                if (subscription == null) return report;

                runtime.OnUi(delegate {
                    string issue = CheckState(runtime.State(), expected, folder);
                    if (issue != null) { report.Status = issue; return; }
                    if (!runtime.CanSave()) { report.Status = "save_command_unavailable"; Record(progress, "save_command_unavailable"); return; }
                    Record(progress, "save_command_available");
                    report.OperationStarted = true; report.SaveRequested = true;
                    Record(progress, "save_command_started");
                    runtime.Save();
                    report.SaveCommandReturned = true;
                    Record(progress, "save_command_returned");
                });
                if (!report.SaveCommandReturned) return report;

                runtime.OnUi(delegate {
                    var state = runtime.State();
                    string issue = CheckState(state, expected, folder);
                    if (issue != null) { report.Status = issue; return; }
                    report.ActiveSolutionId = state.ActiveSolutionId;
                    lock (gate) {
                        if (report.IoEvents.Count != 0) { report.Status = "persistence_io_failed"; return; }
                        report.SaveStarted = saving.Count == 1 && saveOrder.Count == 2
                            && saveOrder[0] == "saving" && saveOrder[1] == "saved";
                        report.SaveCompleted = saved.Count == 1 && report.SaveStarted;
                    }
                    if (!report.SaveStarted || !report.SaveCompleted) { report.Status = "save_completion_unverified"; return; }
                });
                if (!report.SaveCompleted || report.Status == "persistence_io_failed" || report.Status != "not_started") return report;
                Record(progress, "save_completed");

                runtime.OnUi(delegate {
                    var state = runtime.State();
                    string issue = CheckState(state, expected, folder);
                    if (issue != null) { report.Status = issue; return; }
                    if (!runtime.CanExport()) { report.Status = "export_command_unavailable"; Record(progress, "export_command_unavailable"); return; }
                    Record(progress, "export_command_available");
                    if (!runtime.PreExecuteExport()) { report.Status = "export_rule_rejected"; Record(progress, "export_rule_rejected"); return; }
                    report.ExportPreflightPassed = true;
                    Record(progress, "export_preflight_passed");
                    report.OperationStarted = true; report.ExportStarted = true;
                    Record(progress, "native_export_started");
                    runtime.Export(output);
                    report.ExportReturned = true;
                    Record(progress, "native_export_returned");
                });
                if (!report.ExportReturned) {
                    if (report.Status == "not_started") report.Status = "export_outcome_unknown";
                    report.OutcomeUnknown = report.ExportStarted;
                    return report;
                }

                runtime.OnUi(delegate {
                    var state = runtime.State();
                    string issue = CheckState(state, expected, folder);
                    if (issue != null) { report.Status = issue; return; }
                    report.ActiveSolutionId = state.ActiveSolutionId;
                    lock (gate) {
                        if (report.IoEvents.Count != 0) { report.Status = "persistence_io_failed"; return; }
                        if (saving.Count != 2 || saved.Count != 2 || saveOrder.Count != 4
                            || saveOrder[0] != "saving" || saveOrder[1] != "saved"
                            || saveOrder[2] != "saving" || saveOrder[3] != "saved")
                        { report.Status = "export_save_completion_unverified"; return; }
                    }
                    if (!File.Exists(output) || new FileInfo(output).Length == 0) { report.Status = "export_output_missing"; return; }
                    report.Success = true; report.Status = "export_succeeded";
                    Record(progress, "export_verified");
                });
                if (!report.Success && report.ExportStarted && report.Status == "not_started") {
                    report.Status = "export_outcome_unknown"; report.OutcomeUnknown = true;
                }
                return report;
            }
            catch (Exception error)
            {
                report.Error = Describe(error);
                report.Status = report.OperationStarted ? "operation_outcome_unknown" : "runtime_error";
                report.OutcomeUnknown = report.OperationStarted;
                return report;
            }
            finally
            {
                if (!report.Success && ((report.SaveRequested && !report.SaveCompleted) || report.ExportStarted))
                    report.OutcomeUnknown = true;
                try {
                    if (subscription != null) {
                        Record(progress, "subscription_cleanup_started");
                        runtime.OnUi(delegate { subscription.Dispose(); });
                        Record(progress, "subscription_cleanup_completed");
                    }
                }
                catch (Exception error) {
                    Record(progress, "subscription_cleanup_failed");
                    report.CleanupVerified = false; report.Success = false; report.Status = "cleanup_failed";
                    report.OutcomeUnknown = report.OperationStarted; report.Error = Describe(error);
                }
            }
        }
        private static void Record(Action<string> progress, string stage)
        {
            if (progress != null) progress(stage);
        }
        private static string CheckState(SolutionExportState state, Guid expected, string folder)
        {
            if (state == null) return "solution_state_unavailable";
            if (!String.IsNullOrEmpty(state.CallbackError)) return "native_callback_failed";
            Guid actual;
            if (!state.HasActiveSolution || !Guid.TryParse(state.ActiveSolutionId, out actual) || actual != expected)
                return "solution_identity_changed";
            if (!String.Equals(CanonicalFolder(state.StorageFolder), folder, StringComparison.OrdinalIgnoreCase))
                return "solution_storage_changed";
            if (state.FileMode) return "file_mode_solution_refused";
            if (state.AutoConnect) return "autoconnect_solution_refused";
            if (state.SuppressSave) return "save_suppressed";
            if (state.IsImporting || state.OpeningDisabled) return "solution_operation_busy";
            return null;
        }
        private static string CanonicalFolder(string path)
        {
            if (String.IsNullOrWhiteSpace(path) || path.StartsWith("\\\\", StringComparison.Ordinal))
                throw new ArgumentException("A local persisted solution folder is required.");
            string full = Path.GetFullPath(path);
            if (Path.GetPathRoot(full) == null || Path.GetPathRoot(full).Length != 3 || !Directory.Exists(full))
                throw new ArgumentException("The persisted solution folder must be an existing local directory.");
            return full.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        }
        private static void RejectReparseAncestors(string path)
        {
            string current = Path.GetFullPath(path);
            while (!String.IsNullOrEmpty(current))
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new ArgumentException("Export destination parent must not contain a reparse point.");
                string trimmed = current.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                string parent = Path.GetDirectoryName(trimmed);
                if (String.IsNullOrEmpty(parent) || String.Equals(parent, current, StringComparison.OrdinalIgnoreCase)) break;
                current = parent;
            }
        }
        private static bool FileSystemEntryExists(string path)
        {
            try { File.GetAttributes(path); return true; }
            catch (FileNotFoundException) { return false; }
            catch (DirectoryNotFoundException) { return false; }
        }
        private static string Describe(Exception error)
        {
            string text = error.ToString();
            return text.Length <= 5000 ? text : text.Substring(0, 5000);
        }
    }
}
