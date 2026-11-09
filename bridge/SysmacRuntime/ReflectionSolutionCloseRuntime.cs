using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Windows;

namespace SysmacMcp.Runtime
{
    // Binds the close workflow to the pinned Sysmac build and its registered command handler.
    internal sealed class ReflectionSolutionCloseRuntime : ISolutionCloseRuntime
    {
        private const BindingFlags Instance = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        private readonly object cfk, solutions, startPage, commandService, aggregator;
        private readonly int timeout;
        private readonly RuntimeLease lease;
        private string callbackFailure;

        internal ReflectionSolutionCloseRuntime(int timeoutMilliseconds, RuntimeLease lease)
        {
            if (Application.Current == null) throw new InvalidOperationException("Sysmac WPF context is unavailable.");
            timeout = timeoutMilliseconds; this.lease = lease;
            ReflectionRuntime.VerifyFile(Process.GetCurrentProcess().MainModule.FileName,
                "a74caabd98980331c63dabb49b2f7a977854c29f406d061f60feef467ab1248f");
            VerifyType("Omron.Cxap.ServiceLayer.CFkServ", "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");
            VerifyType("Omron.Cxap.Modules.Solution.Services.SolutionService", "df52e2513c78cb1a52ba54084f07989920663935e8b899d35de4c4e3937d3cc1");
            VerifyType("Omron.Cxap.Modules.Solution.SolutionModuleController", "df52e2513c78cb1a52ba54084f07989920663935e8b899d35de4c4e3937d3cc1");
            VerifyType("FrameworkModules.Core.ServiceLayer.CFkModuleServ", "26e6ae5b087b8a9d4c218e310e637fd4e08e2ad914b5b5281f800fd0808a13b3");
            VerifyType("Omron.Cxap.Modules.StartPage.Services.StartPageService", "013837fb50a9ede6a9e8d943fc35036db61a92a5cd208c30e1cb32e419dfc393");
            VerifyType("Omron.Cxap.Modules.Solution.Interfaces.ISolutionService", "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");
            VerifyType("Omron.Cxap.Modules.Solution.Interfaces.Events.SolutionClosingEvent", "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");

            cfk = ReflectionRuntime.StaticProperty("Omron.Cxap.ServiceLayer.CFkServ", "Instance");
            solutions = ReflectionRuntime.Property(cfk, "SolutionService");
            startPage = ReflectionRuntime.Property(
                ReflectionRuntime.StaticProperty("FrameworkModules.Core.ServiceLayer.CFkModuleServ", "Instance"),
                "StartPageService");
            commandService = ReflectionRuntime.Property(cfk, "CommandService");
            aggregator = ReflectionRuntime.Property(cfk, "EventAggregator");
        }

        public void OnUi(Action action) { ReflectionRuntime.Dispatch(action, timeout, lease); }

        public SolutionCloseState State()
        {
            string error = Interlocked.CompareExchange(ref callbackFailure, null, null);
            if (error != null) throw new InvalidOperationException("Native close callback failed: " + error);
            object solution = ReflectionRuntime.Property(solutions, "Solution");
            var fileMode = ReflectionRuntime.Property(cfk, "FileModeService");
            return new SolutionCloseState {
                HasActiveSolution = solution != null,
                ActiveSolutionId = solution == null ? null : EntityId(solution).ToString(),
                FileMode = (bool)ReflectionRuntime.Property(fileMode, "IsFileMode"),
                AutoConnect = (bool)ReflectionRuntime.Property(solutions, "IsAutoConnectSolution"),
                SuppressSave = (bool)ReflectionRuntime.Property(solutions, "SuppressSaveSolution"),
                IsImporting = (bool)ReflectionRuntime.Property(startPage, "IsImporting"),
                OpeningDisabled = (bool)ReflectionRuntime.Property(startPage, "DisableOpenSolution"),
                ApplicationReady = (bool)ReflectionRuntime.Property(startPage, "IsApplicationReady")
            };
        }

        public void CommitEditorChanges()
        {
            Type controllerType = ReflectionRuntime.FindType("Omron.Cxap.Modules.Solution.SolutionModuleController");
            object controller = RegisteredCloseController(controllerType);
            MethodInfo commit = UniqueMethod(controllerType, "CommitEditorChanges", parameters =>
                parameters.Length == 0 && typeof(void) == controllerType.GetMethod("CommitEditorChanges", Instance).ReturnType);
            commit.Invoke(controller, null);
            FieldInfo renaming = controllerType.GetField("currentRenameEntity", Instance);
            if (renaming == null || renaming.GetValue(controller) != null)
                throw new InvalidOperationException("Sysmac inline rename did not settle before close preflight.");
        }

        public bool IsSolutionModified(string solutionId)
        {
            object solution = ActiveSolution(solutionId);
            Type contract = ReflectionRuntime.FindType("Omron.Cxap.Modules.Solution.Interfaces.ISolutionService");
            MethodInfo method = UniqueMethod(contract, "IsSolutionModified", parameters =>
                parameters.Length == 1 && parameters[0].ParameterType.IsInstanceOfType(solution));
            return (bool)method.Invoke(solutions, new[] { solution });
        }

        public bool CanClose(string solutionId)
        {
            return (bool)ReflectionRuntime.Call(commandService, "CanExecute", "UserCloseSolution", ActiveSolution(solutionId));
        }

        public IDisposable SubscribeClose(Action<string> closing, Action<string> closed, Action<string> cancelled)
        {
            var subscriptions = new SubscriptionSet();
            try
            {
                subscriptions.Add(SubscribeEvent("Omron.Cxap.Modules.Solution.Interfaces.Events.SolutionClosingEvent", closing));
                subscriptions.Add(SubscribeEvent("Omron.Cxap.Modules.Solution.Interfaces.Events.SolutionClosedEvent", closed));
                subscriptions.Add(SubscribeEvent("Omron.Cxap.Modules.Solution.Interfaces.Events.SolutionCloseCancelledEvent", cancelled));
                return subscriptions;
            }
            catch { subscriptions.Dispose(); throw; }
        }

        public void Close(string solutionId)
        {
            ReflectionRuntime.Call(commandService, "Execute", "UserCloseSolution", ActiveSolution(solutionId));
        }

        private object ActiveSolution(string solutionId)
        {
            object solution = ReflectionRuntime.Property(solutions, "Solution");
            Guid expected;
            if (solution == null || !Guid.TryParse(solutionId, out expected) || EntityId(solution) != expected)
                throw new InvalidOperationException("Expected solution is no longer active.");
            return solution;
        }

        private object RegisteredCloseController(Type controllerType)
        {
            FieldInfo registryField = commandService.GetType().GetField("registeredHandlers", Instance);
            if (registryField == null) throw new MissingFieldException(commandService.GetType().FullName, "registeredHandlers");
            var registry = registryField.GetValue(commandService) as IDictionary;
            if (registry == null || !registry.Contains("UserCloseSolution"))
                throw new InvalidOperationException("Pinned UserCloseSolution registration is unavailable.");
            var matches = new List<object>();
            var visited = new HashSet<object>(new ReferenceComparer());
            CollectCommandTargets(registry["UserCloseSolution"], controllerType, matches, visited, 0);
            var unique = matches.Where(item => item != null).Distinct(new ReferenceComparer()).ToArray();
            if (unique.Length != 1) throw new InvalidOperationException("Unique registered UserCloseSolution controller is unavailable.");
            FieldInfo serviceField = controllerType.GetField("cfkServ", Instance);
            if (serviceField == null || !Object.ReferenceEquals(serviceField.GetValue(unique[0]), cfk))
                throw new InvalidOperationException("Registered UserCloseSolution controller belongs to another Sysmac service instance.");
            return unique[0];
        }

        private static void CollectCommandTargets(object value, Type controllerType, List<object> matches,
            HashSet<object> visited, int depth)
        {
            if (value == null || depth > 8 || visited.Count > 2048 || !visited.Add(value)) return;
            var handler = value as Delegate;
            if (handler != null)
            {
                if (handler.Target != null && handler.Target.GetType() == controllerType
                    && handler.Method.Name == "ExecuteUserCloseSolution") matches.Add(handler.Target);
                return;
            }
            if (value is string || value.GetType().IsPrimitive || value.GetType().IsEnum || value is Type) return;
            var dictionary = value as IDictionary;
            if (dictionary != null)
            {
                int seen = 0;
                foreach (DictionaryEntry entry in dictionary) {
                    if (++seen > 500) throw new InvalidOperationException("Command registration inventory exceeds the limit.");
                    CollectCommandTargets(entry.Value, controllerType, matches, visited, depth + 1);
                }
                return;
            }
            var sequence = value as IEnumerable;
            if (sequence != null)
            {
                int seen = 0;
                foreach (object item in sequence) {
                    if (++seen > 500) throw new InvalidOperationException("Command registration inventory exceeds the limit.");
                    CollectCommandTargets(item, controllerType, matches, visited, depth + 1);
                }
                return;
            }
            Type current = value.GetType();
            if (current.Assembly != controllerType.Assembly
                && current.Assembly != commandAssembly(controllerType)) return;
            foreach (FieldInfo field in current.GetFields(Instance | BindingFlags.DeclaredOnly))
            {
                if (field.IsStatic || field.FieldType.IsPrimitive || field.FieldType.IsEnum || field.FieldType == typeof(string)) continue;
                if (!typeof(Delegate).IsAssignableFrom(field.FieldType)
                    && field.FieldType.Assembly != controllerType.Assembly
                    && !typeof(IEnumerable).IsAssignableFrom(field.FieldType)) continue;
                CollectCommandTargets(field.GetValue(value), controllerType, matches, visited, depth + 1);
            }
        }

        private static Assembly commandAssembly(Type controllerType)
        {
            return ReflectionRuntime.FindType("Omron.Cxap.PresentationLayer.Command.Services.CommandService").Assembly;
        }

        private IDisposable SubscribeEvent(string typeName, Action<string> callback)
        {
            Type eventType = ReflectionRuntime.FindType(typeName);
            Type aggregatorContract = ReflectionRuntime.FindType("Microsoft.Practices.Prism.Events.IEventAggregator");
            MethodInfo getter = aggregatorContract.GetMethods().Single(method => method.Name == "GetEvent"
                && method.IsGenericMethodDefinition && method.GetParameters().Length == 0);
            object eventInstance = getter.MakeGenericMethod(eventType).Invoke(aggregator, null);
            MethodInfo subscribe = eventInstance.GetType().GetMethods(Instance).Single(method =>
                method.Name == "Subscribe" && method.GetParameters().Length == 4);
            ParameterInfo[] parameters = subscribe.GetParameters();
            var listener = new EventListener(args => callback(EntityId(ReflectionRuntime.Property(args, "Solution")).ToString()),
                error => Interlocked.CompareExchange(ref callbackFailure, error.GetType().Name + ": " + error.Message, null));
            object token = subscribe.Invoke(eventInstance, new object[] {
                Delegate.CreateDelegate(parameters[0].ParameterType, listener, typeof(EventListener).GetMethod("OnEvent")),
                Enum.Parse(parameters[1].ParameterType, "PublisherThread"), true,
                Delegate.CreateDelegate(parameters[3].ParameterType, listener, typeof(EventListener).GetMethod("Filter"))
            });
            var disposable = token as IDisposable;
            if (disposable == null) throw new InvalidOperationException("Native close event subscription has no disposable token.");
            return disposable;
        }

        private static Guid EntityId(object entity)
        {
            object id = ReflectionRuntime.Property(entity, "Id");
            if (!(id is Guid) || (Guid)id == Guid.Empty) throw new InvalidOperationException("Invalid persisted solution identity.");
            return (Guid)id;
        }

        private static MethodInfo UniqueMethod(Type contract, string name, Func<ParameterInfo[], bool> signature)
        {
            MethodInfo[] methods = contract.GetMethods().Where(method => method.Name == name && signature(method.GetParameters())).ToArray();
            if (methods.Length != 1) throw new MissingMethodException("Unique native " + name + " contract unavailable.");
            return methods[0];
        }

        private static void VerifyType(string typeName, string hash)
        {
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType(typeName).Assembly.Location, hash);
        }

        private sealed class EventListener
        {
            private readonly Action<object> callback;
            private readonly Action<Exception> failure;
            internal EventListener(Action<object> callback, Action<Exception> failure) { this.callback = callback; this.failure = failure; }
            public bool Filter(object args) { return true; }
            public void OnEvent(object args) { try { callback(args); } catch (Exception error) { failure(error); } }
        }

        private sealed class SubscriptionSet : IDisposable
        {
            private readonly List<IDisposable> items = new List<IDisposable>();
            internal void Add(IDisposable item) { items.Add(item); }
            public void Dispose()
            {
                Exception failure = null;
                foreach (IDisposable item in items) try { item.Dispose(); } catch (Exception error) { failure = error; }
                items.Clear();
                if (failure != null) throw new InvalidOperationException("Native close event cleanup failed.", failure);
            }
        }

        private sealed class ReferenceComparer : IEqualityComparer<object>
        {
            public new bool Equals(object left, object right) { return Object.ReferenceEquals(left, right); }
            public int GetHashCode(object value) { return RuntimeHelpers.GetHashCode(value); }
        }
    }
}
