using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using static KAMI.Core.PineIPC;

namespace KAMI.Core.Windows
{
    /// <summary>
    /// A PS3 game running natively in a static-recompilation PC port instead of RPCS3. Such a port keeps the
    /// game's whole address space as one 4 GB-aligned reservation in its own process (ps3recomp,
    /// runtime/memory/vm.h: host address = base + guest address, big-endian, main memory committed read/write
    /// at +0x10000, the stack region at +0xD0000000). KAMI reads and writes it directly, in place of PINE.
    /// </summary>
    internal static class NativePort
    {
        class Port
        {
            public string Process;
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
                Process = "inFamousRecomp",
                Title = "inFAMOUS (PC port)",
                Id = "BCES00220",
                Version = "01.00",
                Uuid = "PPU-d0c3e2a107c47d20d3316325ce6f75464a8f4f1d",
            },
        };

        const long GuestSpace = 0x100000000;
        const long MainMemory = 0x10000;
        const long StackRegion = 0xD0000000;
        const long ProbeInterval = 1000;

        const uint PROCESS_VM_OPERATION = 0x0008;
        const uint PROCESS_VM_READ = 0x0010;
        const uint PROCESS_VM_WRITE = 0x0020;
        const uint PROCESS_QUERY_INFORMATION = 0x0400;
        const uint MEM_COMMIT = 0x1000;
        const uint PAGE_READWRITE = 0x04;

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
        static IntPtr s_handle;
        static long s_base;
        static Port s_port;
        static long s_nextProbe;
        static IPCStatus s_error = IPCStatus.NoConnection;
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
            foreach (Port port in s_ports)
            {
                foreach (Process process in Process.GetProcessesByName(port.Process))
                {
                    if (TryAttach(process, port))
                    {
                        return true;
                    }
                    process.Dispose();
                }
            }
            return false;
        }

        static bool TryAttach(Process process, Port port)
        {
            IntPtr handle = OpenProcess(PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION, false, process.Id);
            if (handle == IntPtr.Zero)
            {
                return false;
            }
            long guestBase = FindGuestBase(handle);
            if (guestBase == 0)
            {
                // Not started far enough yet to have reserved the PS3 memory; looked for again next probe.
                CloseHandle(handle);
                return false;
            }
            s_process = process;
            s_handle = handle;
            s_base = guestBase;
            s_port = port;
            s_error = IPCStatus.Success;
            // The port renders far above 64 Hz: tick in milliseconds rather than the default timer's 15.6 ms.
            timeBeginPeriod(1);
            return true;
        }

        static void Detach()
        {
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

        static bool CommittedReadWrite(MEMORY_BASIC_INFORMATION info)
        {
            return info.State == MEM_COMMIT && info.Protect == PAGE_READWRITE;
        }

        /// <summary>
        /// The reservation whose base is 4 GB-aligned, with read/write memory committed at +0x10000 and at
        /// +0xD0000000 inside the same reservation; 0 when there is none.
        /// </summary>
        static long FindGuestBase(IntPtr handle)
        {
            long address = 0;
            while (Query(handle, address, out MEMORY_BASIC_INFORMATION info))
            {
                long regionBase = (long)info.BaseAddress;
                long allocationBase = (long)info.AllocationBase;
                if (allocationBase != 0 && allocationBase % GuestSpace == 0 && regionBase - allocationBase == MainMemory
                    && CommittedReadWrite(info)
                    && Query(handle, allocationBase + StackRegion, out MEMORY_BASIC_INFORMATION stack)
                    && (long)stack.AllocationBase == allocationBase && CommittedReadWrite(stack))
                {
                    return allocationBase;
                }
                long next = regionBase + (long)info.RegionSize;
                if (next <= address)
                {
                    break;
                }
                address = next;
            }
            return 0;
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
        public static string Title => s_port.Title;
        public static string Id => s_port.Id;
        public static string Version => s_port.Version;
        public static string Uuid => s_port.Uuid;
    }
}
