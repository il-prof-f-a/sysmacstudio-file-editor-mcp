using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Windows;

namespace SysmacMcp.Runtime
{
    // Binds only the observed native save/export contracts from the pinned local Sysmac build.
    internal sealed class ReflectionSolutionExportRuntime : ISolutionExportRuntime
    {
        private const BindingFlags Instance = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        private readonly object cfk, solutions, startPage, commandService, commandRules, aggregator;
        private readonly int timeout;
        private readonly RuntimeLease lease;
        private string callbackError;

        internal ReflectionSolutionExportRuntime(int timeoutMilliseconds, RuntimeLease lease)
        {
            if (Application.Current == null) throw new InvalidOperationException("Sysmac WPF context is unavailable.");
            this.timeout = timeoutMilliseconds;
            this.lease = lease;
            ReflectionRuntime.VerifyFile(ProcessPath(), "a74caabd98980331c63dabb49b2f7a977854c29f406d061f60feef467ab1248f");
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType("Omron.Cxap.ServiceLayer.CFkServ").Assembly.Location,
                "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType("Omron.Cxap.Modules.Solution.Services.SolutionService").Assembly.Location,
                "df52e2513c78cb1a52ba54084f07989920663935e8b899d35de4c4e3937d3cc1");
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType("FrameworkModules.Core.ServiceLayer.CFkModuleServ").Assembly.Location,
                "26e6ae5b087b8a9d4c218e310e637fd4e08e2ad914b5b5281f800fd0808a13b3");
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType("Omron.Cxap.Modules.StartPage.Services.StartPageService").Assembly.Location,
                "013837fb50a9ede6a9e8d943fc35036db61a92a5cd208c30e1cb32e419dfc393");
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType("Omron.Cxap.DataLayer.Persistence.Core.Events.PersistenceIOEvent").Assembly.Location,
                "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType("Omron.Cxap.Modules.Solution.Interfaces.ISolutionService").Assembly.Location,
                "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType("Omron.Cxap.Modules.Solution.Interfaces.Events.SolutionSavingEvent").Assembly.Location,
                "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");
            ReflectionRuntime.VerifyFile(ReflectionRuntime.FindType("Omron.Cxap.Rules.Interfaces.ICommandRulesService").Assembly.Location,
                "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");

            cfk = ReflectionRuntime.StaticProperty("Omron.Cxap.ServiceLayer.CFkServ", "Instance");
            solutions = ReflectionRuntime.Property(cfk, "SolutionService");
            startPage = ReflectionRuntime.Property(
                ReflectionRuntime.StaticProperty("FrameworkModules.Core.ServiceLayer.CFkModuleServ", "Instance"), "StartPageService");
            commandService = ReflectionRuntime.Property(cfk, "CommandService");
            commandRules = ReflectionRuntime.Property(cfk, "CommandRulesService");
            aggregator = ReflectionRuntime.Property(cfk, "EventAggregator");
        }

        public void OnUi(Action action) { ReflectionRuntime.Dispatch(action, timeout, lease); }

        public SolutionExportState State()
        {
            object solution = ReflectionRuntime.Property(solutions, "Solution");
            var fileMode = ReflectionRuntime.Property(cfk, "FileModeService");
            return new SolutionExportState {
                HasActiveSolution = solution != null,
                ActiveSolutionId = solution == null ? null : EntityId(solution).ToString(),
                StorageFolder = solution == null ? null : SolutionFolder(solution),
                FileMode = (bool)ReflectionRuntime.Property(fileMode, "IsFileMode"),
                AutoConnect = (bool)ReflectionRuntime.Property(solutions, "IsAutoConnectSolution"),
                SuppressSave = (bool)ReflectionRuntime.Property(solutions, "SuppressSaveSolution"),
                IsImporting = (bool)ReflectionRuntime.Property(startPage, "IsImporting"),
                OpeningDisabled = (bool)ReflectionRuntime.Property(startPage, "DisableOpenSolution"),
                CallbackError = Interlocked.CompareExchange(ref callbackError, null, null)
            };
        }

        public IDisposable Subscribe(Action<string> saving, Action<string> saved,
            Action<Dictionary<string, object>> io)
        {
            var subscriptions = new SubscriptionSet();
            try
            {
                subscriptions.Add(SubscribeEvent(
                    "Omron.Cxap.Modules.Solution.Interfaces.Events.SolutionSavingEvent",
                    args => saving(EntityId(ReflectionRuntime.Property(args, "Solution")).ToString())));
                subscriptions.Add(SubscribeEvent(
                    "Omron.Cxap.Modules.Solution.Interfaces.Events.SolutionSavedEvent",
                    args => saved(EntityId(ReflectionRuntime.Property(args, "Solution")).ToString())));
                subscriptions.Add(SubscribeEvent(
                    "Omron.Cxap.DataLayer.Persistence.Core.Events.PersistenceIOEvent",
                    args => io(IoRecord(args))));
                return subscriptions;
            }
            catch
            {
                subscriptions.Dispose();
                throw;
            }
        }

        public bool CanSave()
        {
            object solution = ActiveSolution();
            return (bool)ReflectionRuntime.Call(commandService, "CanExecute", "SaveSolution", solution);
        }

        public void Save()
        {
            ReflectionRuntime.Call(commandService, "Execute", "SaveSolution", ActiveSolution());
        }

        public bool CanExport()
        {
            object solution = ActiveSolution();
            return (bool)ReflectionRuntime.Call(commandService, "CanExecute", "ExportSolution", solution);
        }

        public bool PreExecuteExport()
        {
            object solution = ActiveSolution();
            Type contract = ReflectionRuntime.FindType("Omron.Cxap.Rules.Interfaces.ICommandRulesService");
            MethodInfo method = UniqueMethod(contract, "PreExecute", parameters =>
                parameters.Length == 2 && parameters[0].ParameterType == typeof(string)
                && parameters[1].ParameterType.IsInstanceOfType(solution));
            return (bool)method.Invoke(commandRules, new object[] { "ExportSolution", solution });
        }

        public void Export(string destination)
        {
            object solution = ActiveSolution();
            string output = Path.GetFullPath(destination);
            if (FileSystemEntryExists(output))
                throw new IOException("Export destination appeared before the native export call.");
            Type contract = ReflectionRuntime.FindType("Omron.Cxap.Modules.Solution.Interfaces.ISolutionService");
            Type entity = ReflectionRuntime.FindType("Omron.Cxap.DataLayer.Entities.Core.Entity");
            MethodInfo method = UniqueMethod(contract, "ExportSolution", parameters =>
                parameters.Length == 2 && parameters[0].ParameterType == entity
                && parameters[1].ParameterType == typeof(string));
            method.Invoke(solutions, new object[] { solution, output });
        }

        private object ActiveSolution()
        {
            object solution = ReflectionRuntime.Property(solutions, "Solution");
            if (solution == null) throw new InvalidOperationException("No active Sysmac solution.");
            EntityId(solution);
            return solution;
        }

        private string SolutionFolder(object solution)
        {
            Type contract = ReflectionRuntime.FindType("Omron.Cxap.Modules.Solution.Interfaces.ISolutionService");
            Type entity = ReflectionRuntime.FindType("Omron.Cxap.DataLayer.Entities.Core.Entity");
            MethodInfo method = UniqueMethod(contract, "GetSolutionFolder", parameters =>
                parameters.Length == 1 && parameters[0].ParameterType == entity);
            object folder = method.Invoke(solutions, new[] { solution });
            if (!(folder is string)) throw new InvalidOperationException("Native persisted solution folder is unavailable.");
            return (string)folder;
        }

        private IDisposable SubscribeEvent(string eventTypeName, Action<object> callback)
        {
            Type eventType = ReflectionRuntime.FindType(eventTypeName);
            Type aggregatorContract = ReflectionRuntime.FindType("Microsoft.Practices.Prism.Events.IEventAggregator");
            MethodInfo getter = aggregatorContract.GetMethods().Single(method => method.Name == "GetEvent"
                && method.IsGenericMethodDefinition && method.GetParameters().Length == 0);
            object eventInstance = getter.MakeGenericMethod(eventType).Invoke(aggregator, null);
            MethodInfo subscribe = eventInstance.GetType().GetMethods(Instance).Single(method =>
                method.Name == "Subscribe" && method.GetParameters().Length == 4);
            ParameterInfo[] parameters = subscribe.GetParameters();
            var listener = new EventListener(callback, error =>
                Interlocked.CompareExchange(ref callbackError, Describe(error), null));
            object token = subscribe.Invoke(eventInstance, new object[] {
                Delegate.CreateDelegate(parameters[0].ParameterType, listener,
                    typeof(EventListener).GetMethod("OnEvent")),
                Enum.Parse(parameters[1].ParameterType, "PublisherThread"), true,
                Delegate.CreateDelegate(parameters[3].ParameterType, listener,
                    typeof(EventListener).GetMethod("Filter"))
            });
            var disposable = token as IDisposable;
            if (disposable == null) throw new InvalidOperationException("Native event subscription has no disposable token.");
            return disposable;
        }

        private static Dictionary<string, object> IoRecord(object args)
        {
            object tag = ReflectionRuntime.Property(args, "Tag");
            object failure = ReflectionRuntime.Property(args, "Exception");
            object eventType = ReflectionRuntime.Property(args, "EventType");
            return new Dictionary<string, object> {
                { "event_type", Limited(eventType == null ? null : eventType.ToString(), 200) },
                { "tag_type", tag == null ? null : tag.GetType().FullName },
                { "tag", Limited(tag == null ? null : tag.ToString(), 1000) },
                { "exception_type", failure == null ? null : failure.GetType().FullName },
                { "exception", Limited(failure == null ? null : failure.ToString(), 2000) }
            };
        }

        private static Guid EntityId(object entity)
        {
            object value = ReflectionRuntime.Property(entity, "Id");
            if (!(value is Guid) || (Guid)value == Guid.Empty)
                throw new InvalidOperationException("Native solution entity has an invalid GUID.");
            return (Guid)value;
        }

        private static MethodInfo UniqueMethod(Type contract, string name, Func<ParameterInfo[], bool> signature)
        {
            MethodInfo[] methods = contract.GetMethods().Where(method => method.Name == name
                && signature(method.GetParameters())).ToArray();
            if (methods.Length != 1) throw new MissingMethodException("Unique " + contract.FullName + "." + name + " contract unavailable.");
            return methods[0];
        }

        private static string ProcessPath()
        {
            using (var process = System.Diagnostics.Process.GetCurrentProcess()) return process.MainModule.FileName;
        }

        private static string Limited(string value, int limit)
        {
            return value == null || value.Length <= limit ? value : value.Substring(0, limit);
        }

        private static bool FileSystemEntryExists(string path)
        {
            try { File.GetAttributes(path); return true; }
            catch (FileNotFoundException) { return false; }
            catch (DirectoryNotFoundException) { return false; }
        }

        private static string Describe(Exception error)
        {
            string value = error.GetType().Name + ": " + error.Message;
            return Limited(value, 2000);
        }

        private sealed class EventListener
        {
            private readonly Action<object> callback;
            private readonly Action<Exception> failure;
            internal EventListener(Action<object> callback, Action<Exception> failure)
            { this.callback = callback; this.failure = failure; }
            public bool Filter(object args) { return true; }
            public void OnEvent(object args)
            {
                try { callback(args); }
                catch (Exception error) { failure(error); }
            }
        }

        private sealed class SubscriptionSet : IDisposable
        {
            private readonly List<IDisposable> items = new List<IDisposable>();
            internal void Add(IDisposable item) { items.Add(item); }
            public void Dispose()
            {
                Exception failure = null;
                foreach (IDisposable item in items)
                    try { item.Dispose(); } catch (Exception error) { failure = error; }
                items.Clear();
                if (failure != null) throw new InvalidOperationException("Native event cleanup failed.", failure);
            }
        }
    }
}
