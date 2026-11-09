using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;

namespace SysmacMcp.Runtime
{
    public sealed class BuildContext
    {
        public string SolutionId, DeviceId, SolutionName, DeviceName;
        public bool Same(BuildContext other)
        {
            Guid leftSolution, rightSolution, leftDevice, rightDevice;
            return other != null
                && Guid.TryParse(SolutionId, out leftSolution) && leftSolution != Guid.Empty
                && Guid.TryParse(other.SolutionId, out rightSolution) && leftSolution == rightSolution
                && Guid.TryParse(DeviceId, out leftDevice) && leftDevice != Guid.Empty
                && Guid.TryParse(other.DeviceId, out rightDevice) && leftDevice == rightDevice;
        }
    }

    public sealed class BuildSignal
    {
        public string DeviceId;
        public bool IsBuildAction;
    }

    public sealed class BuildSnapshot
    {
        public bool NativeSuccess, ControllerCompleted, DiagnosticsComplete;
        public int ErrorCount, WarningCount;
        public List<Dictionary<string, object>> Diagnostics = new List<Dictionary<string, object>>();
    }

    public interface IBuildRuntime
    {
        void OnUi(Action action);
        BuildContext Context();
        bool CanRebuild();
        int ApplicableTargets();
        IDisposable Subscribe(Action<BuildSignal> started, Action<BuildSignal> completed);
        void Rebuild();
        BuildSnapshot Snapshot();
    }

    public sealed class BuildOutcome
    {
        public bool Success, Completed, OperationStarted, OutcomeUnknown, NativeSuccess, CleanupVerified = true;
        public string Status = "not_started", Error;
        public int ApplicableTargets, TargetsAfter, Errors, Warnings;
        public BuildContext Before, After;
        public List<Dictionary<string, object>> Diagnostics = new List<Dictionary<string, object>>();

        public Dictionary<string, object> ToDictionary()
        {
            return new Dictionary<string, object> {
                { "success", Success }, { "completed", Completed }, { "operation_started", OperationStarted },
                { "outcome_unknown", OutcomeUnknown }, { "status", Status }, { "error", Error },
                { "native_success", NativeSuccess }, { "applicable_targets", ApplicableTargets },
                { "targets_after", TargetsAfter }, { "errors", Errors }, { "warnings", Warnings },
                { "cleanup_verified", CleanupVerified }, { "diagnostics", Diagnostics },
                { "solution_id", Before == null ? null : Before.SolutionId },
                { "controller_id", Before == null ? null : Before.DeviceId }
            };
        }
    }

    public static class BuildCoordinator
    {
        public static BuildOutcome Execute(IBuildRuntime runtime, BuildContext expected, int timeoutMilliseconds)
        {
            if (runtime == null || expected == null || !expected.Same(expected))
                throw new ArgumentException("Expected solution and controller GUIDs are required.");
            if (timeoutMilliseconds < 1 || timeoutMilliseconds > 600000)
                throw new ArgumentException("Build deadline must be between 1ms and 600s.");
            var report = new BuildOutcome();
            var elapsed = Stopwatch.StartNew();
            var gate = new object();
            var ended = new ManualResetEvent(false);
            bool accepting = true, armed = false, started = false, completed = false;
            IDisposable subscription = null;
            Action<BuildSignal> onStart = delegate(BuildSignal signal) {
                lock (gate)
                {
                    if (accepting && armed && Matches(signal, expected)) started = true;
                }
            };
            Action<BuildSignal> onEnd = delegate(BuildSignal signal) {
                lock (gate)
                {
                    if (accepting && armed && started && Matches(signal, expected))
                    { completed = true; ended.Set(); }
                }
            };
            try
            {
                runtime.OnUi(delegate {
                    lock (gate) { if (!accepting) return; }
                    var before = runtime.Context();
                    bool stable = expected.Same(before);
                    bool allowed = stable && runtime.CanRebuild();
                    int targets = allowed ? runtime.ApplicableTargets() : 0;
                    lock (gate)
                    {
                        if (!accepting) return;
                        report.Before = before;
                        if (!stable) { report.Status = "context_changed"; return; }
                        if (!allowed) { report.Status = "build_unavailable"; return; }
                        report.ApplicableTargets = targets;
                        if (targets <= 0) { report.Status = "coverage_unavailable"; return; }
                    }
                    var created = runtime.Subscribe(onStart, onEnd);
                    lock (gate)
                    {
                        if (!accepting) { created.Dispose(); return; }
                        subscription = created;
                    }
                    var finalContext = runtime.Context(); // May block; do not hold the event/cleanup lock.
                    lock (gate)
                    {
                        if (!accepting) return;
                        if (elapsed.ElapsedMilliseconds >= timeoutMilliseconds)
                        { report.Status = "deadline_before_start"; return; }
                        if (!expected.Same(finalContext)) { report.Status = "context_changed"; return; }
                        armed = true; report.OperationStarted = true;
                    }
                    runtime.Rebuild();
                });
                if (!report.OperationStarted) return report;
                int remaining = timeoutMilliseconds - (int)Math.Min(elapsed.ElapsedMilliseconds, Int32.MaxValue);
                lock (gate) report.Completed = completed;
                if (remaining <= 0)
                {
                    report.Status = "completion_deadline_exceeded"; report.OutcomeUnknown = true; return report;
                }
                if (!report.Completed && (remaining <= 0 || !ended.WaitOne(remaining)))
                {
                    report.Status = "completion_unknown"; report.OutcomeUnknown = true; return report;
                }
                lock (gate) report.Completed = completed && started;
                runtime.OnUi(delegate {
                    lock (gate) { if (!accepting) return; }
                    var after = runtime.Context();
                    if (!expected.Same(after))
                    { lock (gate) { if (accepting) report.Status = "context_changed"; } return; }
                    int targetsAfter = runtime.ApplicableTargets();
                    var snapshot = runtime.Snapshot();
                    bool stable = snapshot != null && expected.Same(runtime.Context());
                    lock (gate)
                    {
                        if (!accepting) return;
                        if (elapsed.ElapsedMilliseconds >= timeoutMilliseconds)
                        { report.Status = "diagnostic_deadline_exceeded"; report.OutcomeUnknown = true; return; }
                        report.After = after; report.TargetsAfter = targetsAfter;
                        if (!stable) { report.Status = "context_changed"; return; }
                        report.NativeSuccess = snapshot.NativeSuccess;
                        report.Errors = snapshot.ErrorCount; report.Warnings = snapshot.WarningCount;
                        report.Diagnostics = snapshot.Diagnostics;
                        if (report.TargetsAfter <= 0 || !snapshot.DiagnosticsComplete)
                        { report.Status = "coverage_unavailable"; return; }
                        report.Success = report.Completed && snapshot.NativeSuccess
                            && snapshot.ControllerCompleted && snapshot.ErrorCount == 0;
                        report.Status = report.Success ? "build_succeeded" : "build_failed";
                    }
                });
                return report;
            }
            catch (Exception error)
            {
                report.Status = "runtime_error"; report.Error = error.GetType().Name + ": " + error.Message;
                report.Success = false; report.OutcomeUnknown = report.OperationStarted;
                return report;
            }
            finally
            {
                lock (gate) accepting = false;
                try { if (subscription != null) subscription.Dispose(); }
                catch (Exception error)
                {
                    report.CleanupVerified = false; report.Success = false; report.Status = "cleanup_failed";
                    report.Error = error.GetType().Name + ": " + error.Message;
                }
                ended.Dispose();
            }
        }

        private static bool Matches(BuildSignal signal, BuildContext expected)
        {
            Guid actual, wanted;
            return signal != null && signal.IsBuildAction
                && Guid.TryParse(signal.DeviceId, out actual)
                && Guid.TryParse(expected.DeviceId, out wanted) && actual == wanted;
        }
    }
}
