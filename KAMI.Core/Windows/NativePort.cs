using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using static KAMI.Core.PineIPC;

namespace KAMI.Core.Windows
{
    /// <summary>
    /// A PS3 game running natively in a static-recompilation PC port instead of RPCS3. Such a port keeps the
    /// game's whole address space in its own process (infamous-recomp/main.cpp alloc_guest_vm: one
    /// VirtualAlloc(MEM_RESERVE) of the 32-bit guest space plus a 64 KB guard at whatever base Windows picks,
    /// each 64 KB page committed read/write on first touch; host address = base + guest address, big-endian).
    /// KAMI reads and writes it directly, in place of PINE.
    /// </summary>
    internal static class NativePort
    {
        class Port
        {
            public string[] Processes;
            public string Title;
            public string Id;
            public string Version;
            public string Uuid;
        }

        // inFAMOUS (whompay/inFAMOUSreborn and its forks) is built from the European disc's EBOOT.elf
        // (SHA-256 095d8514...f494fd): byte for byte the executable RPCS3 hashes as below, with the same
        // addresses. Reporting RPCS3's id, version and hash lets a profile made on RPCS3 apply unchanged.
        static readonly Port[] s_ports =
        {
            new Port
            {
                // inFamousRecomp_sse4 is the build for CPUs without AVX2.
                Processes = new[] { "inFamousRecomp", "inFamousRecomp_sse4" },
                Title = "inFAMOUS (PC port)",
                Id = "BCES00220",
                Version = "01.00",
                Uuid = "PPU-d0c3e2a107c47d20d3316325ce6f75464a8f4f1d",
            },
        };

        const long GuestReservation = 0x100010000;
        // Where a PS3 executable is loaded: committed once the port has loaded the game.
        const long ElfBase = 0x10000;
        const long ProbeInterval = 1000;
        const string LogName = "native_port.log";

        const uint PROCESS_VM_OPERATION = 0x0008;
        const uint PROCESS_VM_READ = 0x0010;
        const uint PROCESS_VM_WRITE = 0x0020;
        const uint PROCESS_QUERY_INFORMATION = 0x0400;
        const uint MEM_COMMIT = 0x1000;
        const uint MEM_PRIVATE = 0x20000;

        [StructLayout(LayoutKind.Sequential)]
        struct MEMORY_BASIC_INFORMATION
        {
            public IntPtr BaseAddress;
            public IntPtr AllocationBase;
            public uint AllocationProtect;
            public ushort PartitionId;
            public UIntPtr RegionSize;
            public uint State;
            public uint Protect;
            public uint Type;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);

        [DllImport("kernel32.dll")]
        static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll")]
        static extern UIntPtr VirtualQueryEx(IntPtr hProcess, IntPtr lpAddress, out MEMORY_BASIC_INFORMATION lpBuffer, UIntPtr dwLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool ReadProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, IntPtr nSize, out IntPtr lpNumberOfBytesRead);

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool WriteProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, IntPtr nSize, out IntPtr lpNumberOfBytesWritten);

        [DllImport("winmm.dll")]
        static extern uint timeBeginPeriod(uint uPeriod);

        [DllImport("winmm.dll")]
        static extern uint timeEndPeriod(uint uPeriod);

        static Process s_process;
        static string s_name;
        static IntPtr s_handle;
        static long s_base;
        static Port s_port;
        static long s_nextProbe;
        static IPCStatus s_error = IPCStatus.NoConnection;
        static string s_lastLog;
        [ThreadStatic] static byte[] t_buffer;

        /// <summary>True while a port is attached; every call then goes to it instead of PINE.</summary>
        public static bool Active => s_port != null;

        /// <summary>
        /// Checks that the attached port is still running, or looks for one (at most once a second).
        /// Called from the status query KAMI makes every tick.
        /// </summary>
        public static bool Poll()
        {
            if (s_port != null)
            {
                if (!s_process.HasExited)
                {
                    // Like a PINE status query, a fresh status clears the error of the previous call.
                    s_error = IPCStatus.Success;
                    return true;
                }
                Detach();
            }
            long now = Environment.TickCount64;
            if (!Environment.Is64BitProcess || now < s_nextProbe)
            {
                return false;
            }
            s_nextProbe = now + ProbeInterval;
            bool attached = false;
            string other = null;
            foreach (Process process in Process.GetProcesses())
            {
                Port port = attached ? null : Find(process.ProcessName);
                if (port != null && TryAttach(process, port))
                {
                    attached = true;
                    continue;
                }
                if (port == null && process.ProcessName.IndexOf("famous", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    other = process.ProcessName;
                }
                process.Dispose();
            }
            if (!attached && other != null)
            {
                Log($"{other}.exe is running but is not a known port executable");
            }
            return attached;
        }

        static Port Find(string processName)
        {
            foreach (Port port in s_ports)
            {
                foreach (string name in port.Processes)
                {
                    if (string.Equals(name, processName, StringComparison.OrdinalIgnoreCase))
                    {
                        return port;
                    }
                }
            }
            return null;
        }

        static bool TryAttach(Process process, Port port)
        {
            string name = $"{process.ProcessName}.exe (pid {process.Id})";
            IntPtr handle = OpenProcess(PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION, false, process.Id);
            if (handle == IntPtr.Zero)
            {
                int error = Marshal.GetLastWin32Error();
                Log($"{name}: cannot open the process (error {error}); if the port runs as administrator, run KAMI as administrator too");
                return false;
            }
            long guestBase = FindGuestBase(handle, out long largest);
            if (guestBase == 0)
            {
                // Not started far enough yet to have loaded the game; looked for again next probe.
                Log($"{name}: no PS3 memory with the game loaded yet (largest private reservation {largest >> 20} MB)");
                CloseHandle(handle);
                return false;
            }
            s_process = process;
            s_name = name;
            s_handle = handle;
            s_base = guestBase;
            s_port = port;
            s_error = IPCStatus.Success;
            // The port renders far above 64 Hz: tick in milliseconds rather than the default timer's 15.6 ms.
            timeBeginPeriod(1);
            Log($"{name}: connected, PS3 memory at 0x{guestBase:X}");
            return true;
        }

        static void Detach()
        {
            Log($"{s_name}: exited");
            timeEndPeriod(1);
            CloseHandle(s_handle);
            s_process.Dispose();
            s_process = null;
            s_handle = IntPtr.Zero;
            s_base = 0;
            s_port = null;
            s_error = IPCStatus.NoConnection;
        }

        static bool Query(IntPtr handle, long address, out MEMORY_BASIC_INFORMATION info)
        {
            return VirtualQueryEx(handle, (IntPtr)address, out info, (UIntPtr)Marshal.SizeOf<MEMORY_BASIC_INFORMATION>()) != UIntPtr.Zero;
        }

        /// <summary>
        /// The base of the private reservation exactly the size of the guest space whose page at 0x10000 is
        /// committed; 0 when there is none. <paramref name="largest"/> is the largest private reservation seen.
        /// </summary>
        static long FindGuestBase(IntPtr handle, out long largest)
        {
            largest = 0;
            long address = 0;
            long allocation = 0;
            long end = 0;
            bool loaded = false;
            while (true)
            {
                bool more = Query(handle, address, out MEMORY_BASIC_INFORMATION info);
                long owner = more && info.Type == MEM_PRIVATE ? (long)info.AllocationBase : 0;
                if (owner != allocation)
                {
                    // The previous reservation ended with the previous region.
                    if (allocation != 0)
                    {
                        largest = Math.Max(largest, end - allocation);
                        if (loaded && end - allocation == GuestReservation)
                        {
                            return allocation;
                        }
                    }
                    allocation = owner;
                    loaded = false;
                }
                if (!more)
                {
                    return 0;
                }
                long regionBase = (long)info.BaseAddress;
                end = regionBase + (long)info.RegionSize;
                if (owner != 0 && info.State == MEM_COMMIT && regionBase <= owner + ElfBase && owner + ElfBase < end)
                {
                    loaded = true;
                }
                if (end <= address)
                {
                    return 0;
                }
                address = end;
            }
        }

        /// <summary>
        /// Appends a line to native_port.log next to KAMI.exe when it differs from the last one, so a port that
        /// does not connect says why.
        /// </summary>
        static void Log(string line)
        {
            if (line == s_lastLog)
            {
                return;
            }
            s_lastLog = line;
            try
            {
                File.AppendAllText(Path.Combine(AppContext.BaseDirectory, LogName), $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {line}{Environment.NewLine}");
            }
            catch (Exception)
            {
                // A read-only folder only costs the diagnostics.
            }
        }

        static int Size(IPCCommand msg)
        {
            switch (msg)
            {
                case IPCCommand.MsgRead8:
                case IPCCommand.MsgWrite8:
                    return 1;
                case IPCCommand.MsgRead16:
                case IPCCommand.MsgWrite16:
                    return 2;
                case IPCCommand.MsgRead64:
                case IPCCommand.MsgWrite64:
                    return 8;
                default:
                    return 4;
            }
        }

        public static ulong Read(uint address, IPCCommand msg)
        {
            int size = Size(msg);
            byte[] buffer = t_buffer ??= new byte[8];
            if (!ReadProcessMemory(s_handle, (IntPtr)(s_base + address), buffer, (IntPtr)size, out IntPtr done) || (long)done != size)
            {
                s_error = IPCStatus.Fail;
                return 0;
            }
            ulong value = 0;
            for (int i = 0; i < size; i++)
            {
                value = (value << 8) | buffer[i];
            }
            s_error = IPCStatus.Success;
            return value;
        }

        public static void Write(uint address, ulong value, IPCCommand msg)
        {
            int size = Size(msg);
            byte[] buffer = t_buffer ??= new byte[8];
            for (int i = size - 1; i >= 0; i--)
            {
                buffer[i] = (byte)value;
                value >>= 8;
            }
            bool ok = WriteProcessMemory(s_handle, (IntPtr)(s_base + address), buffer, (IntPtr)size, out IntPtr done) && (long)done == size;
            s_error = ok ? IPCStatus.Success : IPCStatus.Fail;
        }

        public static IPCStatus Error => s_error;
        /// <summary>Shown where KAMI shows the emulator version: the port's executable and process id.</summary>
        public static string Host => s_name;
        public static string Title => s_port.Title;
        public static string Id => s_port.Id;
        public static string Version => s_port.Version;
        public static string Uuid => s_port.Uuid;
    }
}
