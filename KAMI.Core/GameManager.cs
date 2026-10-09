using KAMI.Core.Games;
using System;
using System.Diagnostics;
using System.Text.RegularExpressions;

namespace KAMI.Core
{
    public class GameManager
    {
        const string PCSX2BiosPattern = @"\d{8}-\d{6}";

        public string CurrentGameId { get; private set; }
        public string CurrentGameVersion { get; private set; }

        // The game last reported as unsupported, so the caller hears about it once instead of on
        // every update tick, and so the profiles folder is rescanned at a sane rate while waiting.
        (string, string) m_unsupported;
        readonly Stopwatch m_retryTimer = Stopwatch.StartNew();

        /// <summary>
        /// Returns the game implementation for the running title, or null while an unsupported
        /// title keeps running (the first request for such a title throws, later ones do not).
        /// A JSON profile in the "profiles" folder takes precedence over a built-in class and is
        /// picked up without restarting.
        /// </summary>
        public IGame GetGame(IntPtr ipc, string id, string version)
        {
            CurrentGameId = id;
            CurrentGameVersion = version;
            if (Regex.IsMatch(id, PCSX2BiosPattern)) return new MockGame();

            bool repeat = m_unsupported == (id, version);
            if (repeat && m_retryTimer.ElapsedMilliseconds < 1000) return null;
            m_retryTimer.Restart();

            if (ProfileGame.TryCreate(ipc, id, version, out IGame profileGame))
            {
                m_unsupported = default;
                return profileGame;
            }

            try
            {
                IGame game = CreateBuiltIn(ipc, id, version);
                m_unsupported = default;
                return game;
            }
            catch (NotImplementedException)
            {
                if (repeat) return null;
                m_unsupported = (id, version);
                throw;
            }
        }

        static IGame CreateBuiltIn(IntPtr ipc, string id, string version)
        {
            switch (id)
            {
                case "BLUS31006":
                case "NPUB31136": return new Xillia1(ipc);
                case "BLUS31604":
                case "BLES02247":
                case "NPUB31848":
                case "NPEB02436": return new Persona5PS3(ipc);
                case "BLUS30481": return new NierPS3(ipc);
                case "NPEB00833":
                case "NPUB30638":
                case "BLES00680":
                case "BLUS30418":
                case "BLES01294":
                case "BLUS30758": return new RedDeadRedemption(ipc, id, version);
                case "BCES00052": return new RatchetToD(ipc);
                case "BCES00511": return new RatchetACiT(ipc);
                case "BCES01503": return new Ratchet3PS3(ipc);
                case "NPUA80646": return new RatchetDLPS3(ipc);
                case "BCES01743": return new Killzone1PS3(ipc);
                case "BCAS20066":
                case "BCUS98116":
                case "BCJS30032":
                case "BCES00081": return new Killzone2PS3(ipc, version);
                case "BCUS98234":
                case "BCES01007": return new Killzone3PS3(ipc, version);
                case "BCES00001": return new Resistance1(ipc);
                case "BCES00226": return new Resistance2(ipc);
                case "BCES01118": return new Resistance3(ipc);
                case "BLES00016": return new CallOfDuty3(ipc);
                case "BLES00261":
                case "BLUS30118": return new BattlefieldBadCompany(ipc, id);
                case "SCUS-97353": return new Ratchet3PS2(ipc);
                case "SCUS-97465": return new RatchetDLPS2(ipc);
                case "SLUS-21376": return new BlackPS2(ipc);
                case "NPUB31633":
                case "NPEB02182":
                case "NPJB00698":
                case "BLES00246":
                case "BLJM67001":
                case "BLUS30109": return new MetalGearSolid4(ipc, id, version);
                case "BLES01292": return new GoldenEyeReloadedPS3(ipc, version);
                default:
                    throw new NotImplementedException($"Game with id '{id}' not implemented");
            }
        }
    }
}
