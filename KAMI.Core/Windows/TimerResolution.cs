using System;
using System.Runtime.InteropServices;

namespace KAMI.Core.Windows
{
    /// <summary>
    /// Windows sleeps in steps of its timer period, 15.6 ms by default, so a 1 ms Thread.Sleep
    /// lasts a whole step unless a 1 ms period is asked for. Asked for only while a profile wants
    /// a fast tick, and given back when it no longer does.
    /// </summary>
    internal static class TimerResolution
    {
        [DllImport("winmm.dll")]
        static extern uint timeBeginPeriod(uint uPeriod);

        [DllImport("winmm.dll")]
        static extern uint timeEndPeriod(uint uPeriod);

        static bool s_fine;

        public static void Fine(bool on)
        {
            if (on == s_fine || !OperatingSystem.IsWindows())
            {
                return;
            }
            if (on)
            {
                timeBeginPeriod(1);
            }
            else
            {
                timeEndPeriod(1);
            }
            s_fine = on;
        }
    }
}
