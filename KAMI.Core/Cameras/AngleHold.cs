using System;

namespace KAMI.Core.Cameras
{
    /// <summary>
    /// Keeps one camera angle where the mouse put it while the game keeps nudging the stored value
    /// on its own (an auto-follow camera that swings behind the player, an auto-level that pulls
    /// the pitch back to centre).
    ///
    /// Without it the angle is read, the mouse delta is added and the sum is written, so whatever
    /// the game added in between stays and the two movements pile up. With it the angle is kept
    /// here and written back every tick, so the mouse sets the angle outright and the game's own
    /// nudges do not accumulate.
    ///
    /// A jump bigger than the resync threshold (a cut-scene, a respawn, the game snapping the
    /// camera behind the player) is taken as the game's own decision: the angle is picked up from
    /// the game again instead of being fought.
    /// </summary>
    public sealed class AngleHold
    {
        /// <summary>Rewriting a value that is this close to the target is skipped (radians).</summary>
        public const double WriteEpsilon = 1e-4;

        readonly double m_resync;
        readonly double m_span;
        bool m_valid;
        double m_held;

        /// <param name="resync">Largest per-tick difference, in radians, that is still treated as
        /// the game's small nudge rather than a jump.</param>
        /// <param name="wrapSpan">Length of the angle's wrap interval (2*pi for a full circle),
        /// or 0 when the angle does not wrap.</param>
        public AngleHold(double resync, double wrapSpan)
        {
            m_resync = resync;
            m_span = wrapSpan;
        }

        /// <summary>True once an angle is being held.</summary>
        public bool Active => m_valid;

        /// <summary>Forget the held angle; the next tick picks it up from the game.</summary>
        public void Reset()
        {
            m_valid = false;
        }

        /// <summary>
        /// The angle to apply the mouse delta to. <paramref name="observed"/> is what the game has
        /// stored right now. Returns the held angle, or the observed one when nothing is held yet
        /// or the game moved it by more than the resync threshold.
        /// </summary>
        public double Begin(double observed)
        {
            if (m_valid && Math.Abs(Diff(observed, m_held)) <= m_resync)
            {
                return m_held;
            }
            m_valid = true;
            m_held = observed;
            return observed;
        }

        /// <summary>
        /// Records <paramref name="target"/> as the held angle. Returns true when the game's stored
        /// value differs from it enough to be worth writing.
        /// </summary>
        public bool Commit(double observed, double target)
        {
            m_held = target;
            m_valid = true;
            return Math.Abs(Diff(observed, target)) > WriteEpsilon;
        }

        /// <summary>a - b, taken the short way round when the angle wraps.</summary>
        public double Diff(double a, double b)
        {
            double d = a - b;
            if (m_span > 0)
            {
                d -= m_span * Math.Round(d / m_span);
            }
            return d;
        }
    }
}
