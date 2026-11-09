using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;

namespace SysmacMcp.Runtime
{
    // Names/signatures are bound to the locally fingerprinted Omron installation.
    public sealed class ReflectionRuntime : IBuildRuntime
    {
        private readonly object cfk, builder;
        private readonly RuntimeLease lease;
        private readonly int timeout;
        private string callbackError;
        private const BindingFlags Instance = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
        private const BindingFlags Static = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;

        public ReflectionRuntime(int timeoutMilliseconds) : this(timeoutMilliseconds, null) { }
        internal ReflectionRuntime(int timeoutMilliseconds, RuntimeLease lease)
        {
            if (Application.Current == null) throw new InvalidOperationException("Sysmac WPF context is unavailable.");
            this.lease = lease;
            timeout = timeoutMilliseconds;
            VerifyFile(Process.GetCurrentProcess().MainModule.FileName, "a74caabd98980331c63dabb49b2f7a977854c29f406d061f60feef467ab1248f");
            VerifyFile(FindType("Omron.Cxap.ServiceLayer.CFkServ").Assembly.Location, "6d9d4360e74d10113b8654fbb2853b955ccf788722898ac60bace5c547fb9c05");
            VerifyFile(FindType("NexCore.ServiceLayer.NexProgServ").Assembly.Location, "7ba2c17c013bec2efe4b73784b279ce1ae926f90ee874a816fd8fc86d4ebcf45");
            VerifyFile(FindType("Omron.Cxap.Modules.NexBuild.Services.NexBuildThreadWorker").Assembly.Location, "7e4ba2edc6e21b3f058976a8acfc33e37bd8799d11a72ec13570dfba4c83f211");
            cfk = StaticProperty("Omron.Cxap.ServiceLayer.CFkServ", "Instance");
            builder = Property(StaticProperty("NexCore.ServiceLayer.NexProgServ", "Instance"), "NexBuildService");
        }

        public void OnUi(Action action)
        {
            Dispatch(action, timeout, lease);
        }

        internal static void Dispatch(Action action, int timeout, RuntimeLease lease)
        {
            var dispatcher = Application.Current.Dispatcher;
            Exception failure = null;
            Action run = delegate {
                try { action(); } catch (Exception error) { failure = error; }
            };
            RuntimeLease.Work work = lease == null ? null : lease.Track(run);
            if (work != null) run = work.Invoke;
            if (dispatcher.CheckAccess()) {
                run();
                if (failure != null) throw new InvalidOperationException("Application dispatcher operation failed.", failure);
                return;
            }
            DispatcherOperation operation;
            try { operation = dispatcher.BeginInvoke(run, DispatcherPriority.Background); }
            catch { if (work != null) work.Dispose(); throw; }
            if (work != null) operation.Task.ContinueWith(task => work.Dispose(), TaskScheduler.Default);
            if (!operation.Task.Wait(timeout))
            {
                operation.Abort(); // Removes a queued callback, never aborts an executing compiler.
                throw new TimeoutException("Application dispatcher deadline expired.");
            }
            operation.Task.GetAwaiter().GetResult();
            if (failure != null) throw new InvalidOperationException("Application dispatcher operation failed.", failure);
        }

        public BuildContext Context()
        {
            object solution = Property(Property(cfk, "SolutionService"), "Solution");
            object device = Device();
            return new BuildContext {
                SolutionId = Text(Property(solution, "Id")), DeviceId = Text(Property(device, "Id")),
                SolutionName = Text(Property(solution, "Name")), DeviceName = Text(Property(device, "Name"))
            };
        }

        private object Device() { return Call(Property(cfk, "SelectionService"), "GetSelection", "Device"); }

        public bool CanRebuild()
        {
            var context = Context();
            if (!context.Same(context)) return false;
            object member = ValidatedDevice();
            return member != null && (bool)Call(Property(cfk, "CommandService"), "CanExecute", "NexReBuild", member);
        }

        private object ValidatedDevice()
        {
            object solution = Property(Property(cfk, "SolutionService"), "Solution"), selected = Device();
            if (solution == null || selected == null) return null;
            object member = Call(Property(cfk, "EntityService"), "FindDescendant", solution, Property(selected, "Id"));
            return member != null && Equals(Property(member, "Id"), Property(selected, "Id"))
                && member.GetType() == selected.GetType() ? member : null;
        }

        public int ApplicableTargets()
        {
            object device = Device();
            var worker = FindType("Omron.Cxap.Modules.NexBuild.Services.NexBuildThreadWorker");
            var manager = FindType("Omron.Cxap.Modules.NexBuild.Services.NexBuilderManager");
            var targets = Invoke(worker, null, "GetProgramBuildTargetEntities", Static, new[] { device }) as IEnumerable;
            if (targets == null) throw new InvalidOperationException("Native build target collection is unavailable.");
            int applicable = 0, visited = 0;
            foreach (object target in targets)
            {
                if (++visited > 50000) throw new InvalidOperationException("Native build target limit exceeded.");
                if (Invoke(manager, null, "GetBuilder", Static, new[] { target }) != null) applicable++;
            }
            return applicable;
        }

        public IDisposable Subscribe(Action<BuildSignal> started, Action<BuildSignal> completed)
        {
            var group = new Subscriptions();
            try
            {
                group.Add(SubscribeEvent("Omron.Cxap.Modules.NexBuild.Core.ProgramBuildStartedEvent", started));
                group.Add(SubscribeEvent("Omron.Cxap.Modules.NexBuild.Core.ProgramBuildCompletedEvent", completed));
                return group;
            }
            catch { group.Dispose(); throw; }
        }

        private IDisposable SubscribeEvent(string typeName, Action<BuildSignal> action)
        {
            var aggregator = Property(cfk, "EventAggregator");
            var contract = FindType("Microsoft.Practices.Prism.Events.IEventAggregator");
            var getter = contract.GetMethod("GetEvent").MakeGenericMethod(FindType(typeName));
            var instance = getter.Invoke(aggregator, null);
            var subscribe = instance.GetType().GetMethods(Instance).Single(m => m.Name == "Subscribe" && m.GetParameters().Length == 4);
            var parameters = subscribe.GetParameters();
            var listener = new Listener(action, delegate(string error) { Interlocked.CompareExchange(ref callbackError, error, null); });
            var handler = Delegate.CreateDelegate(parameters[0].ParameterType, listener, typeof(Listener).GetMethod("OnEvent"));
            var filter = Delegate.CreateDelegate(parameters[3].ParameterType, listener, typeof(Listener).GetMethod("Filter"));
            var token = subscribe.Invoke(instance, new object[] {
                handler, Enum.Parse(parameters[1].ParameterType, "PublisherThread"), true, filter
            }) as IDisposable;
            if (token == null) throw new InvalidOperationException("Native event did not provide a disposable subscription.");
            return token;
        }

        public void Rebuild() { Call(builder, "ReBuild"); }

        public BuildSnapshot Snapshot()
        {
            object device = Device();
            var result = new BuildSnapshot {
                NativeSuccess = (bool)Call(builder, "IsProgramBuildSuccess", device),
                ControllerCompleted = (bool)Call(builder, "IsControllerBuildCompleted", device),
                DiagnosticsComplete = callbackError == null
            };
            var messages = Property(Property(cfk, "BuildLogService"), "BuildMessages") as IEnumerable;
            if (messages == null) throw new InvalidOperationException("Controller build log is unavailable.");
            int count = 0;
            foreach (object message in messages)
            {
                if (++count > 50000) { result.DiagnosticsComplete = false; break; }
                int category = Convert.ToInt32(Property(message, "Category"));
                if (category == 1) result.ErrorCount++;
                else if (category == 2) result.WarningCount++;
                else if (category < 0 || category > 3) result.DiagnosticsComplete = false;
                if (result.Diagnostics.Count < 100)
                {
                    var row = new Dictionary<string, object> { { "category", category } };
                    foreach (string name in new[] { "Description", "ProgramEntityId", "ProgramLocation", "LocationDescription", "DefaultOrder", "DisplayId" })
                    {
                        string value = Text(Property(message, name));
                        row[name] = value == null || value.Length <= 500 ? value : value.Substring(0, 500);
                    }
                    result.Diagnostics.Add(row);
                }
            }
            return result;
        }

        public Dictionary<string, object> Status()
        {
            var context = Context();
            object member = ValidatedDevice();
            var result = new Dictionary<string, object> {
                { "solution_id", context.SolutionId }, { "controller_id", context.DeviceId },
                { "solution_name", context.SolutionName }, { "controller_name", context.DeviceName },
                { "can_rebuild", CanRebuild() }, { "applicable_targets", ApplicableTargets() }
            };
            result["device_belongs_to_solution"] = member != null;
            result["command_can_rebuild"] = member != null && (bool)Call(Property(cfk, "CommandService"), "CanExecute", "NexReBuild", member);
            var fileMode = Property(cfk, "FileModeService");
            bool isFileMode = (bool)Property(fileMode, "IsFileMode");
            result["file_mode"] = isFileMode;
            var project = isFileMode ? Property(fileMode, "CurrentProject") : null;
            result["project_file"] = project == null ? null : Property(Property(project, "ProjectFile"), "FullName");
            return result;
        }

        private sealed class Listener
        {
            private readonly Action<BuildSignal> callback;
            private readonly Action<string> error;
            public Listener(Action<BuildSignal> callback, Action<string> error) { this.callback = callback; this.error = error; }
            public bool Filter(object args) { return true; }
            public void OnEvent(object args)
            {
                try
                {
                    callback(new BuildSignal {
                        DeviceId = Text(Property(Property(args, "ControllerEntity"), "Id")),
                        IsBuildAction = Convert.ToInt32(Property(args, "ProgramBuildAction")) == 1
                    });
                }
                catch (Exception failure) { error(failure.GetType().Name + ": " + failure.Message); }
            }
        }

        private sealed class Subscriptions : IDisposable
        {
            private readonly List<IDisposable> items = new List<IDisposable>();
            public void Add(IDisposable value) { items.Add(value); }
            public void Dispose()
            {
                Exception failure = null;
                foreach (var item in items)
                    try { item.Dispose(); } catch (Exception error) { failure = error; }
                items.Clear();
                if (failure != null) throw new InvalidOperationException("Native event cleanup failed.", failure);
            }
        }

        internal static Type FindType(string name)
        {
            foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                var type = assembly.GetType(name, false);
                if (type != null) return type;
            }
            throw new InvalidOperationException("Required loaded Sysmac type unavailable: " + name);
        }
        internal static object StaticProperty(string type, string name)
        {
            return FindType(type).GetProperty(name, Static).GetValue(null, null);
        }
        internal static object Property(object target, string name)
        {
            if (target == null) throw new InvalidOperationException("Missing context while reading " + name);
            var property = target.GetType().GetProperty(name, Instance);
            if (property == null)
                foreach (var contract in target.GetType().GetInterfaces())
                { property = contract.GetProperty(name); if (property != null) break; }
            if (property == null) throw new MissingMemberException(target.GetType().FullName, name);
            return property.GetValue(target, null);
        }
        internal static object Call(object target, string name, params object[] args)
        {
            if (target == null) throw new InvalidOperationException("Missing service for " + name);
            return Invoke(target.GetType(), target, name, Instance, args);
        }
        private static object Invoke(Type type, object target, string name, BindingFlags flags, object[] args)
        {
            var methods = type.GetMethods(flags).Where(m => m.Name == name && m.GetParameters().Length == args.Length
                && m.GetParameters().Select((p, index) => args[index] == null ? !p.ParameterType.IsValueType : p.ParameterType.IsInstanceOfType(args[index])).All(x => x)).ToArray();
            if (methods.Length != 1) throw new MissingMethodException(type.FullName, name);
            return methods[0].Invoke(target, args);
        }
        private static string Text(object value) { return value == null ? null : value.ToString(); }
        internal static void VerifyFile(string path, string expected)
        {
            using (var stream = File.OpenRead(path))
            using (var sha = SHA256.Create())
            {
                string actual = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
                if (actual != expected) throw new InvalidOperationException("Unsupported Sysmac binary fingerprint: " + Path.GetFileName(path));
            }
        }
    }
}
