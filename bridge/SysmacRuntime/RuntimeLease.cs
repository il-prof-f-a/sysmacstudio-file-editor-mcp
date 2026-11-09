using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;

namespace SysmacMcp.Runtime
{
    // Named in the target process: different loaded adapter revisions share the lease.
    internal sealed class RuntimeLease : IDisposable
    {
        private Semaphore semaphore;
        private readonly object gate = new object();
        private readonly List<Work> callbacks = new List<Work>();
        private int references = 1;
        private bool closed;
        private RuntimeLease(Semaphore semaphore) { this.semaphore = semaphore; }
        internal static RuntimeLease TryEnter()
        {
            string name;
            using (var process = Process.GetCurrentProcess())
                name = @"Local\sysmac-mcp-runtime-" + process.Id + "-" + process.StartTime.ToUniversalTime().Ticks;
            var semaphore = new Semaphore(1, 1, name);
            if (semaphore.WaitOne(0)) return new RuntimeLease(semaphore);
            semaphore.Dispose();
            return null;
        }
        internal Work Track(Action action)
        {
            lock (gate) {
                if (closed) throw new OperationCanceledException("The runtime invocation has already ended.");
                var work = new Work(this, action);
                callbacks.Add(work); references++;
                return work;
            }
        }
        private void Release(Work work)
        {
            Semaphore owned = null;
            lock (gate) {
                if (work != null) callbacks.Remove(work);
                if (--references == 0) { owned = semaphore; semaphore = null; }
            }
            if (owned != null) try { owned.Release(); } finally { owned.Dispose(); }
        }
        internal bool CloseAndInspect()
        {
            Work[] pending = null;
            lock (gate) {
                if (!closed) { closed = true; pending = callbacks.ToArray(); }
            }
            if (pending != null) {
                foreach (var work in pending) work.Dispose(); // Cancels queued callbacks; never interrupts running work.
                Release(null);
            }
            lock (gate) return references == 0;
        }
        public void Dispose() { CloseAndInspect(); }

        internal sealed class Work : IDisposable
        {
            private readonly RuntimeLease owner;
            private readonly Action action;
            private int state; // 0 queued, 1 executing, 2 completed/cancelled.
            internal Work(RuntimeLease owner, Action action) { this.owner = owner; this.action = action; }
            internal void Invoke()
            {
                if (Interlocked.CompareExchange(ref state, 1, 0) != 0) return;
                try {
                    lock (owner.gate) { if (owner.closed) return; }
                    action();
                } finally { Interlocked.Exchange(ref state, 2); owner.Release(this); }
            }
            public void Dispose()
            {
                if (Interlocked.CompareExchange(ref state, 2, 0) == 0) owner.Release(this);
            }
        }
    }
}
