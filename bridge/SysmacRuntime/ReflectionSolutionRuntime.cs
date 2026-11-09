using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Reflection;
using System.Threading;

namespace SysmacMcp.Runtime
{
    internal sealed class ReflectionSolutionRuntime : IExistingSolutionRuntime
    {
        private readonly object cfk, solutions, startPage;
        private readonly int timeout;
        private readonly RuntimeLease lease;
        private Exception callbackFailure;
        private const BindingFlags I = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        private static Type T(string name) { return ReflectionRuntime.FindType(name); }
        private static object P(object target, string name) { return ReflectionRuntime.Property(target, name); }
        private static object Call(object target, string name, params object[] args) { return ReflectionRuntime.Call(target, name, args); }

        internal ReflectionSolutionRuntime(int timeoutMilliseconds, RuntimeLease lease)
        {
            this.timeout = timeoutMilliseconds; this.lease = lease;
            ReflectionRuntime.VerifyFile(Process.GetCurrentProcess().MainModule.FileName, "a74caabd98980331c63dabb49b2f7a977854c29f406d061f60feef467ab1248f");
            ReflectionRuntime.VerifyFile(T("Omron.Cxap.ServiceLayer.CFkServ").Assembly.Location, "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");
            ReflectionRuntime.VerifyFile(T("FrameworkModules.Core.ServiceLayer.CFkModuleServ").Assembly.Location, "26e6ae5b087b8a9d4c218e310e637fd4e08e2ad914b5b5281f800fd0808a13b3");
            ReflectionRuntime.VerifyFile(T("Omron.Cxap.Modules.Solution.Services.SolutionService").Assembly.Location, "df52e2513c78cb1a52ba54084f07989920663935e8b899d35de4c4e3937d3cc1");
            ReflectionRuntime.VerifyFile(T("Omron.Cxap.Modules.StartPage.Services.StartPageService").Assembly.Location, "013837fb50a9ede6a9e8d943fc35036db61a92a5cd208c30e1cb32e419dfc393");
            cfk = ReflectionRuntime.StaticProperty("Omron.Cxap.ServiceLayer.CFkServ", "Instance");
            solutions = P(cfk, "SolutionService");
            startPage = P(ReflectionRuntime.StaticProperty("FrameworkModules.Core.ServiceLayer.CFkModuleServ", "Instance"), "StartPageService");
        }
        public void OnUi(Action action) { ReflectionRuntime.Dispatch(action, timeout, lease); }
        public SolutionState State()
        {
            if (callbackFailure != null) throw new InvalidOperationException("Native solution callback failed.", callbackFailure);
            object solution = P(solutions, "Solution");
            return new SolutionState {
                HasActiveSolution = solution != null,
                ActiveSolutionId = solution == null ? null : Id(solution).ToString(),
                IsImporting = (bool)P(startPage, "IsImporting"),
                OpeningDisabled = (bool)P(startPage, "DisableOpenSolution"),
                ApplicationReady = (bool)P(startPage, "IsApplicationReady")
            };
        }
        private static Guid Id(object entity)
        {
            object id = P(entity, "Id");
            if (!(id is Guid) || (Guid)id == Guid.Empty) throw new InvalidOperationException("Invalid persisted solution identity.");
            return (Guid)id;
        }
        private string SolutionFolder(object candidate)
        {
            // Sysmac implements this contract explicitly; the concrete type does not expose the method.
            var entityType = T("Omron.Cxap.DataLayer.Entities.Core.Entity");
            var methods = T("Omron.Cxap.Modules.Solution.Interfaces.ISolutionService").GetMethods()
                .Where(method => method.Name == "GetSolutionFolder" && method.GetParameters().Length == 1
                    && method.GetParameters()[0].ParameterType == entityType).ToArray();
            if (methods.Length != 1) throw new MissingMethodException("Unique ISolutionService.GetSolutionFolder(Entity) contract unavailable.");
            object folder = methods[0].Invoke(solutions, new[] { candidate });
            if (!(folder is string)) throw new InvalidOperationException("Native persisted solution folder is unavailable.");
            return (string)folder;
        }
        private List<object> Projects()
        {
            var source = P(solutions, "Solutions") as IEnumerable;
            if (source == null) throw new InvalidOperationException("Native persisted solution inventory unavailable.");
            var result = new List<object>();
            foreach (object candidate in source) {
                if (result.Count >= 5000) throw new InvalidOperationException("Persisted solution inventory exceeds the limit.");
                if (candidate == null || !Object.Equals(P(candidate, "EntityType"), "Solution"))
                    throw new InvalidOperationException("Unexpected persisted entity type.");
                Id(candidate); result.Add(candidate);
            }
            return result;
        }
        public int MatchingProjects(string solutionId)
        {
            Guid expected = Guid.Parse(solutionId);
            return Projects().Count(candidate => Id(candidate) == expected);
        }
        private object Resolve(string solutionId)
        {
            Guid expected = Guid.Parse(solutionId);
            var matches = Projects().Where(candidate => Id(candidate) == expected).ToArray();
            if (matches.Length != 1) throw new InvalidOperationException("Persisted solution GUID is missing or ambiguous.");
            return matches[0];
        }
        private bool Permission(object candidate)
        {
            var method = T("Omron.Cxap.Security.Core.IAccessControlService").GetMethod("EvaluatePermission")
                .MakeGenericMethod(T("Omron.Cxap.DataLayer.Entities.Core.Entity"));
            object rights = Enum.Parse(method.GetParameters()[1].ParameterType, "ViewContent");
            if (Convert.ToInt32(rights) != 128) throw new InvalidOperationException("Unexpected ViewContent contract.");
            return (bool)method.Invoke(P(cfk, "AccessControlService"), new[] { candidate, rights });
        }
        public bool CanView(string solutionId) { return Permission(Resolve(solutionId)); }
        public bool CanOpen(string solutionId)
        {
            return (bool)Call(P(cfk, "CommandService"), "CanExecute", "OpenSolution", Resolve(solutionId));
        }
        public IDisposable SubscribeOpened(Action<string> opened)
        {
            object aggregator = P(cfk, "EventAggregator");
            var getter = T("Microsoft.Practices.Prism.Events.IEventAggregator").GetMethod("GetEvent")
                .MakeGenericMethod(T("Omron.Cxap.Modules.Solution.Interfaces.Events.SolutionOpenedEvent"));
            object instance = getter.Invoke(aggregator, null);
            var subscribe = instance.GetType().GetMethods(I).Single(m => m.Name == "Subscribe" && m.GetParameters().Length == 4);
            var parameters = subscribe.GetParameters();
            var listener = new Listener(opened, error => Interlocked.CompareExchange(ref callbackFailure, error, null));
            var token = subscribe.Invoke(instance, new object[] {
                Delegate.CreateDelegate(parameters[0].ParameterType, listener, typeof(Listener).GetMethod("OnEvent")),
                Enum.Parse(parameters[1].ParameterType, "PublisherThread"), true,
                Delegate.CreateDelegate(parameters[3].ParameterType, listener, typeof(Listener).GetMethod("Filter"))
            }) as IDisposable;
            if (token == null) throw new InvalidOperationException("Native solution event has no disposable token.");
            return token;
        }
        public void InitializeController()
        {
            var state = State();
            if (state.HasActiveSolution || state.IsImporting || state.OpeningDisabled)
                throw new InvalidOperationException("The start page is no longer idle.");
            if (!state.ApplicationReady) Call(startPage, "InitializeControllerView", false);
        }
        public void WhenReady(Action action)
        {
            var work = lease.Track(action);
            Action guarded = delegate {
                try { work.Invoke(); }
                catch (Exception error) { Interlocked.CompareExchange(ref callbackFailure, error, null); }
            };
            try { Call(startPage, "InvokeAfterApplicationReady", guarded); }
            catch { work.Dispose(); throw; }
        }
        public bool TryOpen(string solutionId, out string error)
        {
            object candidate = Resolve(solutionId);
            bool allowed = (bool)Call(P(cfk, "CommandService"), "CanExecute", "OpenSolution", candidate);
            if (!allowed || !Permission(candidate)) { error = "Native opening permission/availability changed."; return false; }
            var state = State();
            if (state.HasActiveSolution || state.IsImporting || state.OpeningDisabled || !state.ApplicationReady) {
                error = "The start page is no longer ready and empty."; return false;
            }
            var methods = solutions.GetType().GetInterfaces().SelectMany(type => type.GetMethods())
                .Where(method => method.Name == "TryOpenSolution" && method.GetParameters().Length == 2
                    && method.GetParameters()[1].IsOut).Distinct().ToArray();
            if (methods.Length != 1) throw new MissingMethodException("Unique ISolutionService.TryOpenSolution contract unavailable.");
            object[] args = { candidate, null };
            bool accepted = (bool)methods[0].Invoke(solutions, args);
            error = args[1] as string;
            return accepted;
        }
        internal Dictionary<string, object> ListProjects(int offset, int limit)
        {
            if (offset < 0 || limit < 1 || limit > 200) throw new ArgumentException("Invalid project inventory page.");
            var all = Projects();
            if (all.Select(Id).Distinct().Count() != all.Count) throw new InvalidOperationException("Persisted solution GUIDs are ambiguous.");
            all = all.OrderBy(item => Id(item)).ToList();
            var state = State();
            var items = new List<Dictionary<string, object>>();
            foreach (object candidate in all.Skip(offset).Take(limit)) {
                bool truncated = false;
                string name = Limited(P(candidate, "Name"), 500, ref truncated);
                string folder = Limited(SolutionFolder(candidate), 1024, ref truncated);
                items.Add(new Dictionary<string, object> {
                    { "solution_id", Id(candidate).ToString() }, { "name", name }, { "storage_folder", folder },
                    { "can_view", Permission(candidate) }, { "fields_truncated", truncated }
                });
            }
            int next = offset < all.Count ? offset + items.Count : all.Count;
            return new Dictionary<string, object> {
                { "status", "projects_observed" }, { "success", true }, { "operation_started", false },
                { "items", items }, { "total", all.Count }, { "next_offset", next < all.Count ? (object)next : null },
                { "active_solution_id", state.ActiveSolutionId }
            };
        }
        private static string Limited(object raw, int limit, ref bool truncated)
        {
            string value = raw == null ? null : raw.ToString();
            if (value != null && value.Length > limit) { truncated = true; return value.Substring(0, limit); }
            return value;
        }
        private sealed class Listener
        {
            private readonly Action<string> callback;
            private readonly Action<Exception> failure;
            internal Listener(Action<string> callback, Action<Exception> failure) { this.callback = callback; this.failure = failure; }
            public bool Filter(object args) { return true; }
            public void OnEvent(object args)
            {
                try { callback(Id(P(args, "Solution")).ToString()); }
                catch (Exception error) { failure(error); }
            }
        }
    }
}
