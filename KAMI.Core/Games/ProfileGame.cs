using KAMI.Core.Cameras;
using KAMI.Core.Utilities;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text.Json;

namespace KAMI.Core.Games
{
    /// <summary>
    /// A game described by a JSON profile instead of a compiled class.
    ///
    /// Profiles live in the "profiles" folder next to the executable. A profile says which pointer
    /// chains lead to the camera values and how a mouse delta is applied to them, using the same
    /// three camera models as the built-in games:
    ///
    ///   HAVA    horizontal and vertical angle, two scalars            (HAVACamera)
    ///   HVecVA  horizontal sin/cos pair plus a vertical scalar        (HVecVACamera)
    ///   HVVec   one 3D direction vector                               (HVVecCamera)
    ///
    /// The file is re-read when it changes, so addresses, signs and limits can be tuned while the
    /// game and KAMI keep running. See profiles/_example.json for the format; the Python twin of
    /// this class (kami-toolkit, rpcs3mem/profile.py) defines the reference behaviour.
    /// </summary>
    public class ProfileGame : IGame
    {
        public const string ProfileFolder = "profiles";
        const double TwoPi = Math.PI * 2;
        // Nothing is ever mapped below the PS3 executable base; an address down there means a
        // pointer in the chain is still null (game not in gameplay yet).
        const uint MinAddress = 0x10000;

        public float SensModifier { get; set; } = 0.003f;
        public float ScopedSensModifier { get; set; } = 1f;

        readonly IntPtr m_ipc;
        readonly string m_path;
        readonly string m_id;
        readonly string m_version;
        readonly string m_hash;
        DateTime m_stamp;
        Build m_build;
        StreamWriter m_trace;
        readonly Stopwatch m_reloadTimer = Stopwatch.StartNew();
        readonly Stopwatch m_traceIdle = Stopwatch.StartNew();
        readonly Stopwatch m_clock = Stopwatch.StartNew();

        // Files that failed to load, by timestamp: a broken profile is reported once per edit.
        static readonly Dictionary<string, DateTime> s_rejected = new Dictionary<string, DateTime>();

        ProfileGame(IntPtr ipc, string path, DateTime stamp, string id, string version, string hash, Build build)
        {
            m_ipc = ipc;
            m_path = path;
            m_stamp = stamp;
            m_id = id;
            m_version = version;
            m_hash = hash;
            m_build = build;
        }

        /// <summary>
        /// Looks for a profile matching the running game. Returns false when there is none.
        /// Throws when a profile exists but cannot be parsed, naming the file.
        /// </summary>
        public static bool TryCreate(IntPtr ipc, string id, string version, out IGame game)
        {
            game = null;
            string dir = Path.Combine(AppContext.BaseDirectory, ProfileFolder);
            if (!Directory.Exists(dir))
            {
                return false;
            }

            string hash = null;
            foreach (string path in Directory.GetFiles(dir, "*.json"))
            {
                if (Path.GetFileName(path).StartsWith("_", StringComparison.Ordinal))
                {
                    continue;
                }
                DateTime stamp = File.GetLastWriteTimeUtc(path);
                if (s_rejected.TryGetValue(path, out DateTime bad) && bad == stamp)
                {
                    continue;
                }

                Build build;
                try
                {
                    hash ??= PineIPC.GetGameUUID(ipc) ?? "";
                    build = LoadBuild(ipc, path, id, version, hash);
                }
                catch (Exception ex)
                {
                    s_rejected[path] = stamp;
                    throw new Exception($"Profile '{Path.GetFileName(path)}': {ex.Message}", ex);
                }

                if (build != null)
                {
                    game = new ProfileGame(ipc, path, stamp, id, version, hash, build);
                    return true;
                }
            }
            return false;
        }

        public void InjectionStart()
        {
            foreach (Camera camera in m_build.Cameras)
            {
                camera.ResetHold();
            }
        }

        public void UpdateCamera(int diffX, int diffY)
        {
            ReloadIfChanged();
            Build build = m_build;

            foreach (ForcedWrite write in build.Writes)
            {
                write.Apply(m_ipc);
            }

            double dx = diffX * SensModifier;
            double dy = diffY * SensModifier;
            Camera active = null;
            foreach (Camera camera in build.Cameras)
            {
                if (camera.When != null && !camera.When.Test(m_ipc))
                {
                    continue;
                }
                active = camera;
                double k = camera.Scoped ? ScopedSensModifier : 1.0;
                bool ok = camera.Update(m_ipc, dx * k, dy * k);
                if (build.Trace)
                {
                    WriteTrace(camera, diffX, diffY, ok);
                }
                break;
            }

            // A camera that was not driven this tick (its condition is false, or another camera
            // came first) forgets what it held: the game may have moved the angle meanwhile, and
            // writing the old one back when the camera is picked again would snap the view.
            foreach (Camera camera in build.Cameras)
            {
                if (camera != active)
                {
                    camera.ResetHold();
                }
            }
        }

        void ReloadIfChanged()
        {
            if (m_reloadTimer.ElapsedMilliseconds < 500)
            {
                return;
            }
            m_reloadTimer.Restart();

            DateTime stamp = File.GetLastWriteTimeUtc(m_path);
            if (stamp == m_stamp)
            {
                return;
            }
            // Editors save in several writes; wait until the file has been quiet for a moment.
            if ((DateTime.UtcNow - stamp).TotalMilliseconds < 300)
            {
                return;
            }
            m_stamp = stamp;

            Build build;
            try
            {
                build = LoadBuild(m_ipc, m_path, m_id, m_version, m_hash);
            }
            catch (Exception ex)
            {
                // The previous, working build stays active.
                throw new Exception($"Profile '{Path.GetFileName(m_path)}': {ex.Message}", ex);
            }
            if (build != null)
            {
                m_build = build;
            }
        }

        void WriteTrace(Camera camera, int diffX, int diffY, bool ok)
        {
            if (diffX == 0 && diffY == 0 && m_traceIdle.ElapsedMilliseconds < 250)
            {
                return;
            }
            m_traceIdle.Restart();

            if (m_trace == null)
            {
                string path = Path.Combine(AppContext.BaseDirectory, "kami_trace.csv");
                bool fresh = !File.Exists(path);
                m_trace = new StreamWriter(path, true) { AutoFlush = true };
                if (fresh)
                {
                    m_trace.WriteLine("time_ms,camera,ok,diff_x,diff_y,hor_addr,hor_rad,vert_addr,vert_rad,ipc_error");
                }
            }
            m_trace.WriteLine(string.Format(CultureInfo.InvariantCulture,
                "{0},{1},{2},{3},{4},0x{5:X8},{6:R},0x{7:X8},{8:R},{9}",
                m_clock.ElapsedMilliseconds, camera.Name, ok ? 1 : 0, diffX, diffY,
                camera.HorAddress, camera.Hor, camera.VertAddress, camera.Vert, IPCUtils.Error));
        }

        // ---------------------------------------------------------------- loading

        static Build LoadBuild(IntPtr ipc, string path, string id, string version, string hash)
        {
            var options = new JsonDocumentOptions
            {
                CommentHandling = JsonCommentHandling.Skip,
                AllowTrailingCommas = true,
            };
            using JsonDocument doc = JsonDocument.Parse(File.ReadAllText(path), options);
            JsonElement root = doc.RootElement;

            if (root.TryGetProperty("schema", out JsonElement schema) && ToLong(schema) != 1)
            {
                throw new Exception("unsupported schema version");
            }
            if (!root.TryGetProperty("builds", out JsonElement builds) || builds.ValueKind != JsonValueKind.Array)
            {
                throw new Exception("no 'builds' array");
            }
            foreach (JsonElement build in builds.EnumerateArray())
            {
                if (Matches(build, id, version, hash))
                {
                    return new Build(ipc, build);
                }
            }
            return null;
        }

        /// A build with a hash is matched by the executable hash alone (what KAMI shows as "Hash");
        /// otherwise by title id and, if any are listed, version.
        static bool Matches(JsonElement build, string id, string version, string hash)
        {
            string wanted = GetString(build, "hash", "").Trim();
            if (wanted.Length > 0)
            {
                return string.Equals(wanted, hash, StringComparison.OrdinalIgnoreCase);
            }
            if (!ArrayContains(build, "ids", id, false, StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }
            return ArrayContains(build, "versions", version, true, StringComparison.Ordinal);
        }

        static bool ArrayContains(JsonElement obj, string name, string value, bool emptyMeansAny, StringComparison comparison)
        {
            if (!obj.TryGetProperty(name, out JsonElement array) || array.ValueKind != JsonValueKind.Array)
            {
                return emptyMeansAny;
            }
            int count = 0;
            foreach (JsonElement item in array.EnumerateArray())
            {
                count++;
                if (item.ValueKind == JsonValueKind.String && string.Equals(item.GetString(), value, comparison))
                {
                    return true;
                }
            }
            return count == 0 && emptyMeansAny;
        }

        static string GetString(JsonElement obj, string name, string fallback)
        {
            return obj.TryGetProperty(name, out JsonElement e) && e.ValueKind == JsonValueKind.String ? e.GetString() : fallback;
        }

        static bool GetBool(JsonElement obj, string name, bool fallback)
        {
            if (obj.TryGetProperty(name, out JsonElement e))
            {
                if (e.ValueKind == JsonValueKind.True) return true;
                if (e.ValueKind == JsonValueKind.False) return false;
            }
            return fallback;
        }

        /// Integers may be written as JSON numbers or as strings ("0x1A8", "424").
        static long ToLong(JsonElement e)
        {
            if (e.ValueKind == JsonValueKind.Number)
            {
                return e.GetInt64();
            }
            if (e.ValueKind != JsonValueKind.String)
            {
                throw new Exception("expected a number or a hex string");
            }
            string text = e.GetString().Trim().Replace("_", "");
            if (text.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
            {
                return long.Parse(text.Substring(2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            }
            return long.Parse(text, NumberStyles.Integer, CultureInfo.InvariantCulture);
        }

        static double ToDouble(JsonElement e)
        {
            if (e.ValueKind == JsonValueKind.Number)
            {
                return e.GetDouble();
            }
            if (e.ValueKind != JsonValueKind.String)
            {
                throw new Exception("expected a number");
            }
            return double.Parse(e.GetString().Trim(), NumberStyles.Float, CultureInfo.InvariantCulture);
        }

        static long GetLong(JsonElement obj, string name, long fallback)
        {
            return obj.TryGetProperty(name, out JsonElement e) && e.ValueKind != JsonValueKind.Null ? ToLong(e) : fallback;
        }

        static double GetDouble(JsonElement obj, string name, double fallback)
        {
            return obj.TryGetProperty(name, out JsonElement e) && e.ValueKind != JsonValueKind.Null ? ToDouble(e) : fallback;
        }

        static double? GetOptionalDouble(JsonElement obj, string name)
        {
            if (obj.TryGetProperty(name, out JsonElement e) && e.ValueKind != JsonValueKind.Null)
            {
                return ToDouble(e);
            }
            return null;
        }

        static JsonElement Require(JsonElement obj, string name)
        {
            if (!obj.TryGetProperty(name, out JsonElement e) || e.ValueKind != JsonValueKind.Object)
            {
                throw new Exception($"missing '{name}' object");
            }
            return e;
        }

        static bool IpcOk()
        {
            return IPCUtils.Error == PineIPC.IPCStatus.Success;
        }

        static double ReadValue(IntPtr ipc, uint address, string type)
        {
            switch (type)
            {
                case "float": return IPCUtils.ReadFloat(ipc, address);
                case "u8": return IPCUtils.ReadU8(ipc, address);
                case "u16": return IPCUtils.ReadU16(ipc, address);
                default: return IPCUtils.ReadU32(ipc, address);
            }
        }

        static void CheckType(string type)
        {
            if (type != "float" && type != "u8" && type != "u16" && type != "u32")
            {
                throw new Exception($"unknown value type '{type}'");
            }
        }

        // ---------------------------------------------------------------- model

        /// A pointer chain with KAMI's DerefChain semantics; no offsets means a static address.
        sealed class ChainSpec
        {
            public readonly DerefChain Chain;

            public ChainSpec(IntPtr ipc, JsonElement e)
            {
                if (!e.TryGetProperty("base", out JsonElement baseElement))
                {
                    throw new Exception("chain without 'base'");
                }
                long address = ToLong(baseElement);
                var offsets = new List<long>();
                if (e.TryGetProperty("offsets", out JsonElement array) && array.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement offset in array.EnumerateArray())
                    {
                        offsets.Add(ToLong(offset));
                    }
                }
                Chain = DerefChain.CreateDerefChain(ipc, address, offsets.ToArray());
            }

            public uint Address => (uint)Chain.Value;
        }

        /// A scalar angle: where it is, its unit, its direction and its limits.
        /// Everything is handled in radians internally.
        sealed class Axis
        {
            public readonly ChainSpec Where;
            public readonly double Sign;
            public readonly double Scale;
            readonly bool m_isU16;
            readonly double m_toRad;
            readonly double? m_min;
            readonly double? m_max;
            readonly double? m_wrapLo;
            readonly double? m_wrapHi;

            public Axis(IntPtr ipc, JsonElement e)
            {
                Where = new ChainSpec(ipc, e);
                string unit = GetString(e, "unit", "rad");
                switch (unit)
                {
                    case "rad": m_toRad = 1.0; break;
                    case "deg": m_toRad = Math.PI / 180.0; break;
                    case "u16": m_toRad = TwoPi / 65536.0; m_isU16 = true; break;
                    default: throw new Exception($"unknown unit '{unit}' (use rad, deg or u16)");
                }
                Sign = GetDouble(e, "sign", 1.0);
                Scale = GetDouble(e, "scale", 1.0);
                double? min = GetOptionalDouble(e, "min");
                double? max = GetOptionalDouble(e, "max");
                m_min = min.HasValue ? min.Value * m_toRad : (double?)null;
                m_max = max.HasValue ? max.Value * m_toRad : (double?)null;
                if (e.TryGetProperty("wrap", out JsonElement wrap) && wrap.ValueKind == JsonValueKind.Array && wrap.GetArrayLength() == 2)
                {
                    m_wrapLo = ToDouble(wrap[0]) * m_toRad;
                    m_wrapHi = ToDouble(wrap[1]) * m_toRad;
                }
            }

            /// Radians per unit of the stored value ("unit").
            public double ToRad => m_toRad;

            /// Length of the wrap interval in radians, or 0 when the angle does not wrap.
            public double WrapSpan => m_wrapLo.HasValue && m_wrapHi.HasValue ? m_wrapHi.Value - m_wrapLo.Value : 0;

            public double Read(IntPtr ipc, uint address)
            {
                return m_isU16 ? IPCUtils.ReadU16(ipc, address) * m_toRad : IPCUtils.ReadFloat(ipc, address) * m_toRad;
            }

            public void Write(IntPtr ipc, uint address, double radians)
            {
                if (m_isU16)
                {
                    IPCUtils.WriteU16(ipc, address, (ushort)((long)Math.Round(radians / m_toRad) & 0xFFFF));
                }
                else
                {
                    IPCUtils.WriteFloat(ipc, address, (float)(radians / m_toRad));
                }
            }

            public double Apply(double radians, double delta)
            {
                radians += delta * Sign * Scale;
                if (m_min.HasValue) radians = Math.Max(m_min.Value, radians);
                if (m_max.HasValue) radians = Math.Min(m_max.Value, radians);
                if (m_wrapLo.HasValue && m_wrapHi.HasValue)
                {
                    double span = m_wrapHi.Value - m_wrapLo.Value;
                    if (span > 0)
                    {
                        double r = (radians - m_wrapLo.Value) % span;
                        if (r < 0) r += span;
                        radians = m_wrapLo.Value + r;
                    }
                }
                return radians;
            }
        }

        sealed class Condition
        {
            readonly ChainSpec m_where;
            readonly string m_type;
            readonly string m_op;
            readonly double m_value;
            // A list given as "when": every entry must hold.
            readonly Condition[] m_all;
            // "rate": compare how fast the value changes (per second) instead of the value.
            readonly bool m_rate;
            double m_lastValue;
            long m_lastTime;
            // "for": stay true this long (seconds) after the condition last held.
            readonly long m_for;
            long m_trueUntil;

            /// The "when" of an entry: one condition, or a list of conditions that must all
            /// hold; null when there is none.
            public static Condition FromWhen(IntPtr ipc, JsonElement e)
            {
                if (!e.TryGetProperty("when", out JsonElement when))
                {
                    return null;
                }
                switch (when.ValueKind)
                {
                    case JsonValueKind.Object:
                        return new Condition(ipc, when);
                    case JsonValueKind.Array:
                        var all = new List<Condition>();
                        foreach (JsonElement item in when.EnumerateArray())
                        {
                            if (item.ValueKind != JsonValueKind.Object)
                            {
                                throw new Exception("'when' list entries must be conditions");
                            }
                            all.Add(new Condition(ipc, item));
                        }
                        if (all.Count == 0)
                        {
                            throw new Exception("empty 'when' list");
                        }
                        return all.Count == 1 ? all[0] : new Condition(all.ToArray());
                    default:
                        throw new Exception("'when' must be a condition or a list of conditions");
                }
            }

            Condition(Condition[] all)
            {
                m_all = all;
            }

            public Condition(IntPtr ipc, JsonElement e)
            {
                m_where = new ChainSpec(ipc, e);
                m_type = GetString(e, "type", "u32");
                m_op = GetString(e, "op", "==");
                m_rate = GetBool(e, "rate", false);
                CheckType(m_type);
                if (m_op != "==" && m_op != "!=" && m_op != "&" && m_op != "<" && m_op != ">")
                {
                    throw new Exception($"unknown condition operator '{m_op}'");
                }
                if (!e.TryGetProperty("value", out JsonElement value))
                {
                    throw new Exception("condition without 'value'");
                }
                m_value = m_type == "float" || m_rate ? ToDouble(value) : ToLong(value);
                if (m_rate && m_op != "<" && m_op != ">")
                {
                    throw new Exception("a 'rate' condition takes '<' or '>'");
                }
                double hold = GetDouble(e, "for", 0.0);
                if (!(hold >= 0))
                {
                    throw new Exception("'for' must not be negative (seconds)");
                }
                m_for = (long)(hold * Stopwatch.Frequency);
            }

            public bool Test(IntPtr ipc)
            {
                bool result = Evaluate(ipc);
                if (m_for > 0)
                {
                    long now = Stopwatch.GetTimestamp();
                    if (result)
                    {
                        m_trueUntil = now + m_for;
                    }
                    return result || now < m_trueUntil;
                }
                return result;
            }

            bool Evaluate(IntPtr ipc)
            {
                if (m_all != null)
                {
                    foreach (Condition condition in m_all)
                    {
                        if (!condition.Test(ipc))
                        {
                            return false;
                        }
                    }
                    return true;
                }
                if (!m_where.Chain.Verify())
                {
                    return false;
                }
                uint address = m_where.Address;
                if (address < MinAddress)
                {
                    return false;
                }
                double current = ReadValue(ipc, address, m_type);
                if (!IpcOk())
                {
                    return false;
                }
                if (m_rate)
                {
                    // Change per second since the previous test; nothing after a pause, so a
                    // value that moved while the condition was not looked at does not count.
                    long now = Stopwatch.GetTimestamp();
                    double dt = (double)(now - m_lastTime) / Stopwatch.Frequency;
                    double previous = m_lastValue;
                    bool fresh = m_lastTime != 0 && dt > 0 && dt <= 0.25;
                    m_lastValue = current;
                    m_lastTime = now;
                    if (!fresh || !double.IsFinite(current) || !double.IsFinite(previous))
                    {
                        return false;
                    }
                    current = (current - previous) / dt;
                }
                switch (m_op)
                {
                    case "==": return current == m_value;
                    case "!=": return current != m_value;
                    case "&": return ((long)current & (long)m_value) != 0;
                    case "<": return current < m_value;
                    default: return current > m_value;
                }
            }
        }

        /// A value forced on every tick while injecting, e.g. to switch off auto-centering.
        sealed class ForcedWrite
        {
            readonly ChainSpec m_where;
            readonly string m_type;
            readonly double m_value;
            // "copy": the value is read from this chain every tick instead of being fixed,
            // e.g. a game clock copied into a "last input" time stamp.
            readonly ChainSpec m_copy;
            readonly Condition m_when;

            public ForcedWrite(IntPtr ipc, JsonElement e)
            {
                m_where = new ChainSpec(ipc, e);
                m_type = GetString(e, "type", "float");
                CheckType(m_type);
                bool hasValue = e.TryGetProperty("value", out JsonElement value);
                if (e.TryGetProperty("copy", out JsonElement copy))
                {
                    if (hasValue)
                    {
                        throw new Exception("write with both 'value' and 'copy'");
                    }
                    if (copy.ValueKind != JsonValueKind.Object)
                    {
                        throw new Exception("'copy' must be a chain (base, offsets)");
                    }
                    m_copy = new ChainSpec(ipc, copy);
                }
                else if (!hasValue)
                {
                    throw new Exception("write without 'value' or 'copy'");
                }
                else
                {
                    m_value = m_type == "float" ? ToDouble(value) : ToLong(value);
                }
                m_when = Condition.FromWhen(ipc, e);
            }

            public void Apply(IntPtr ipc)
            {
                if (m_when != null && !m_when.Test(ipc))
                {
                    return;
                }
                if (!m_where.Chain.Verify())
                {
                    return;
                }
                uint address = m_where.Address;
                if (address < MinAddress)
                {
                    return;
                }
                double value = m_value;
                if (m_copy != null)
                {
                    if (!m_copy.Chain.Verify() || m_copy.Address < MinAddress)
                    {
                        return;
                    }
                    value = ReadValue(ipc, m_copy.Address, m_type);
                    if (!IpcOk() || !double.IsFinite(value))
                    {
                        return;
                    }
                }
                switch (m_type)
                {
                    case "float":
                        IPCUtils.WriteFloat(ipc, address, (float)value);
                        break;
                    case "u8":
                        PineIPC.Write(ipc, address, (ulong)((long)value & 0xFF), PineIPC.IPCCommand.MsgWrite8);
                        break;
                    case "u16":
                        IPCUtils.WriteU16(ipc, address, (ushort)((long)value & 0xFFFF));
                        break;
                    default:
                        IPCUtils.WriteU32(ipc, address, (uint)((long)value & 0xFFFFFFFF));
                        break;
                }
            }
        }

        /// A pair of angles elsewhere in memory kept equal to a camera's own ("mirror"), for
        /// example an aim camera the game snaps the view to when aiming starts or ends. Either
        /// side may be left out.
        sealed class Mirror
        {
            readonly MirrorAxis m_hor;
            readonly MirrorAxis m_vert;

            public Mirror(IntPtr ipc, JsonElement e)
            {
                if (e.TryGetProperty("hor", out JsonElement hor) && hor.ValueKind == JsonValueKind.Object)
                {
                    m_hor = new MirrorAxis(ipc, hor);
                }
                if (e.TryGetProperty("vert", out JsonElement vert) && vert.ValueKind == JsonValueKind.Object)
                {
                    m_vert = new MirrorAxis(ipc, vert);
                }
                if (m_hor == null && m_vert == null)
                {
                    throw new Exception("'mirror' entry without 'hor' or 'vert'");
                }
            }

            public void Apply(IntPtr ipc, double hor, double vert)
            {
                m_hor?.Apply(ipc, hor);
                m_vert?.Apply(ipc, vert);
            }
        }

        sealed class MirrorAxis
        {
            readonly Axis m_axis;
            readonly double m_factor;
            readonly double m_offset;

            public MirrorAxis(IntPtr ipc, JsonElement e)
            {
                // Chain, unit, min/max and wrap as for a camera axis. "factor" multiplies the
                // camera's angle; without it "sign" is taken, so "sign": -1 mirrors inverted.
                m_axis = new Axis(ipc, e);
                m_factor = e.TryGetProperty("factor", out _) ? GetDouble(e, "factor", 1.0) : m_axis.Sign;
                m_offset = GetDouble(e, "offset", 0.0) * m_axis.ToRad;
            }

            /// source: the camera's angle in radians.
            public void Apply(IntPtr ipc, double source)
            {
                if (!m_axis.Where.Chain.Verify())
                {
                    return;
                }
                uint address = m_axis.Where.Address;
                if (address < MinAddress)
                {
                    return;
                }
                double target = m_axis.Apply(m_factor * source + m_offset, 0);
                double current = m_axis.Read(ipc, address);
                if (!IpcOk())
                {
                    return;
                }
                if (double.IsFinite(current) && Math.Abs(current - target) <= AngleHold.WriteEpsilon)
                {
                    return;
                }
                m_axis.Write(ipc, address, target);
            }
        }

        sealed class Camera
        {
            public readonly string Name;
            public readonly Condition When;
            /// Mouse movement is scaled by KAMI's "Scoped Sensitivity" while this camera is used.
            public readonly bool Scoped;

            // Last resolved addresses and values (radians), for the trace file.
            public uint HorAddress;
            public uint VertAddress;
            public double Hor;
            public double Vert;

            readonly string m_type;
            readonly Axis m_hor;
            readonly Axis m_vert;
            readonly long m_xOffset;
            readonly ChainSpec m_vec;
            readonly long m_vecX;
            readonly long m_vecY;
            readonly long m_vecZ;
            readonly double m_signH;
            readonly double m_signV;
            readonly double m_vertMin;
            readonly double m_vertMax;

            // "hold": keep the mouse's angle and rewrite it every tick instead of adding to
            // whatever the game has moved it to meanwhile. See AngleHold.
            readonly bool m_hold;
            readonly AngleHold m_holdHor;
            readonly AngleHold m_holdVert;
            uint m_holdHorAddress;
            uint m_holdVertAddress;
            long m_holdLast;
            // "hold_grace": for this long after the camera is picked up again, follow the game
            // instead of holding, so a snap the game makes on a mode switch is not undone.
            readonly long m_holdGrace;
            long m_graceUntil;

            // "mirror": other angles kept equal to this camera's while it is used.
            readonly Mirror[] m_mirrors = Array.Empty<Mirror>();

            // "max_speed": the most the mouse may turn this camera, radians per second; the
            // rest of a faster movement is dropped. 0 means no limit.
            readonly double m_maxSpeed;
            long m_lastUpdate;

            public Camera(IntPtr ipc, JsonElement e)
            {
                Name = GetString(e, "name", "");
                m_type = GetString(e, "type", "HAVA");
                When = Condition.FromWhen(ipc, e);

                switch (m_type)
                {
                    case "HAVA":
                        m_hor = new Axis(ipc, Require(e, "hor"));
                        m_vert = new Axis(ipc, Require(e, "vert"));
                        break;
                    case "HVecVA":
                        m_hor = new Axis(ipc, Require(e, "hor"));
                        m_xOffset = GetLong(Require(e, "hor"), "x_offset", 4);
                        m_vert = new Axis(ipc, Require(e, "vert"));
                        break;
                    case "HVVec":
                        JsonElement vec = Require(e, "vec");
                        m_vec = new ChainSpec(ipc, vec);
                        m_vecX = GetLong(vec, "x_offset", 0);
                        m_vecY = GetLong(vec, "y_offset", 4);
                        m_vecZ = GetLong(vec, "z_offset", 8);
                        m_signH = GetDouble(vec, "sign_h", 1.0);
                        m_signV = GetDouble(vec, "sign_v", 1.0);
                        m_vertMin = GetDouble(vec, "vert_min", -Math.PI / 2 + 0.01);
                        m_vertMax = GetDouble(vec, "vert_max", Math.PI / 2 - 0.01);
                        break;
                    default:
                        throw new Exception($"unknown camera type '{m_type}' (use HAVA, HVecVA or HVVec)");
                }

                m_hold = GetBool(e, "hold", false);
                if (m_hold)
                {
                    if (m_type != "HAVA")
                    {
                        throw new Exception("'hold' is only supported for HAVA cameras");
                    }
                    double resync = GetDouble(e, "hold_resync", 0.5);
                    if (!(resync > 0))
                    {
                        throw new Exception("'hold_resync' must be greater than 0 (radians)");
                    }
                    m_holdHor = new AngleHold(resync, m_hor.WrapSpan);
                    m_holdVert = new AngleHold(resync, m_vert.WrapSpan);
                    double grace = GetDouble(e, "hold_grace", 0.0);
                    if (!(grace >= 0))
                    {
                        throw new Exception("'hold_grace' must not be negative (seconds)");
                    }
                    m_holdGrace = (long)(grace * Stopwatch.Frequency);
                }

                Scoped = GetBool(e, "scoped", false);

                m_maxSpeed = GetDouble(e, "max_speed", 0.0);
                if (!(m_maxSpeed >= 0))
                {
                    throw new Exception("'max_speed' must not be negative (radians per second)");
                }

                if (e.TryGetProperty("mirror", out JsonElement mirrors) && mirrors.ValueKind == JsonValueKind.Array)
                {
                    if (m_type != "HAVA")
                    {
                        throw new Exception("'mirror' is only supported for HAVA cameras");
                    }
                    var list = new List<Mirror>();
                    foreach (JsonElement mirror in mirrors.EnumerateArray())
                    {
                        list.Add(new Mirror(ipc, mirror));
                    }
                    m_mirrors = list.ToArray();
                }
            }

            /// Forget the held angles; the next tick picks them up from the game again.
            public void ResetHold()
            {
                m_holdHor?.Reset();
                m_holdVert?.Reset();
                m_graceUntil = Stopwatch.GetTimestamp() + m_holdGrace;
            }

            /// dx, dy: mouse delta times sensitivity, in radians. Returns false when the camera
            /// cannot be reached right now (null pointer in a chain, IPC error).
            public bool Update(IntPtr ipc, double dx, double dy)
            {
                if (m_maxSpeed > 0)
                {
                    LimitSpeed(ref dx, ref dy);
                }
                switch (m_type)
                {
                    case "HAVA": return UpdateAngles(ipc, dx, dy);
                    case "HVecVA": return UpdateHorVector(ipc, dx, dy);
                    default: return UpdateDirection(ipc, dx, dy);
                }
            }

            void LimitSpeed(ref double dx, ref double dy)
            {
                long now = Stopwatch.GetTimestamp();
                // The time since this camera was last updated, but no more than a few ticks: a
                // camera picked up again after a while gets no extra allowance.
                double dt = m_lastUpdate == 0 ? 0.01 : (double)(now - m_lastUpdate) / Stopwatch.Frequency;
                m_lastUpdate = now;
                double limit = m_maxSpeed * Math.Min(dt, 0.05);
                double length = Math.Sqrt(dx * dx + dy * dy);
                if (length > limit)
                {
                    double k = limit / length;
                    dx *= k;
                    dy *= k;
                }
            }

            bool UpdateAngles(IntPtr ipc, double dx, double dy)
            {
                if (!DerefChain.VerifyChains(m_hor.Where.Chain, m_vert.Where.Chain))
                {
                    return false;
                }
                uint horAddress = m_hor.Where.Address;
                uint vertAddress = m_vert.Where.Address;
                if (horAddress < MinAddress || vertAddress < MinAddress)
                {
                    return false;
                }

                double hor = m_hor.Read(ipc, horAddress);
                if (!IpcOk()) return false;
                double vert = m_vert.Read(ipc, vertAddress);
                if (!IpcOk()) return false;
                if (!double.IsFinite(hor) || !double.IsFinite(vert))
                {
                    return false;
                }

                bool settling = false;
                if (m_hold)
                {
                    // The camera object moved (new level) or no tick for a while (injection
                    // paused, camera condition false): what was held is stale.
                    long now = Stopwatch.GetTimestamp();
                    if (horAddress != m_holdHorAddress || vertAddress != m_holdVertAddress
                        || now - m_holdLast > Stopwatch.Frequency / 4)
                    {
                        ResetHold();
                    }
                    m_holdHorAddress = horAddress;
                    m_holdVertAddress = vertAddress;
                    m_holdLast = now;
                    settling = now < m_graceUntil;
                    if (settling)
                    {
                        // Still in the grace period: take whatever the game has.
                        m_holdHor.Reset();
                        m_holdVert.Reset();
                    }

                    double targetHor = m_holdHor.Begin(hor);
                    double targetVert = m_holdVert.Begin(vert);
                    if (dx != 0)
                    {
                        targetHor = m_hor.Apply(targetHor, dx);
                    }
                    if (dy != 0)
                    {
                        targetVert = m_vert.Apply(targetVert, dy);
                    }
                    if (m_holdHor.Commit(hor, targetHor))
                    {
                        m_hor.Write(ipc, horAddress, targetHor);
                    }
                    if (m_holdVert.Commit(vert, targetVert))
                    {
                        m_vert.Write(ipc, vertAddress, targetVert);
                    }
                    hor = targetHor;
                    vert = targetVert;
                }
                else
                {
                    // Only touch an axis the mouse actually moved: rewriting an unchanged value
                    // would race the game's own update for no benefit.
                    if (dx != 0)
                    {
                        hor = m_hor.Apply(hor, dx);
                        m_hor.Write(ipc, horAddress, hor);
                    }
                    if (dy != 0)
                    {
                        vert = m_vert.Apply(vert, dy);
                        m_vert.Write(ipc, vertAddress, vert);
                    }
                }

                // Not while settling: the angle may still be the one from before the game's
                // switch, and copying it over would undo the switch in the other camera too.
                if (!settling)
                {
                    foreach (Mirror mirror in m_mirrors)
                    {
                        mirror.Apply(ipc, hor, vert);
                    }
                }

                HorAddress = horAddress;
                VertAddress = vertAddress;
                Hor = hor;
                Vert = vert;
                return true;
            }

            bool UpdateHorVector(IntPtr ipc, double dx, double dy)
            {
                if (!DerefChain.VerifyChains(m_hor.Where.Chain, m_vert.Where.Chain))
                {
                    return false;
                }
                uint horAddress = m_hor.Where.Address;
                uint vertAddress = m_vert.Where.Address;
                if (horAddress < MinAddress || vertAddress < MinAddress)
                {
                    return false;
                }
                uint xAddress = (uint)(horAddress + m_xOffset);

                double y = IPCUtils.ReadFloat(ipc, horAddress);
                if (!IpcOk()) return false;
                double x = IPCUtils.ReadFloat(ipc, xAddress);
                if (!IpcOk()) return false;
                double vert = m_vert.Read(ipc, vertAddress);
                if (!IpcOk()) return false;

                // Keep whatever length the game uses for the pair instead of assuming 1.
                double radius = Math.Sqrt(x * x + y * y);
                if (!double.IsFinite(radius) || !double.IsFinite(vert) || radius < 1e-6)
                {
                    return false;
                }

                double angle = Math.Atan2(y, x);
                if (dx != 0)
                {
                    angle += dx * m_hor.Sign * m_hor.Scale;
                    IPCUtils.WriteFloat(ipc, horAddress, (float)(Math.Sin(angle) * radius));
                    IPCUtils.WriteFloat(ipc, xAddress, (float)(Math.Cos(angle) * radius));
                }
                if (dy != 0)
                {
                    vert = m_vert.Apply(vert, dy);
                    m_vert.Write(ipc, vertAddress, vert);
                }

                HorAddress = horAddress;
                VertAddress = vertAddress;
                Hor = angle;
                Vert = vert;
                return true;
            }

            bool UpdateDirection(IntPtr ipc, double dx, double dy)
            {
                if (!m_vec.Chain.Verify())
                {
                    return false;
                }
                uint address = m_vec.Address;
                if (address < MinAddress)
                {
                    return false;
                }
                uint xAddress = (uint)(address + m_vecX);
                uint yAddress = (uint)(address + m_vecY);
                uint zAddress = (uint)(address + m_vecZ);

                double x = IPCUtils.ReadFloat(ipc, xAddress);
                if (!IpcOk()) return false;
                double y = IPCUtils.ReadFloat(ipc, yAddress);
                if (!IpcOk()) return false;
                double z = IPCUtils.ReadFloat(ipc, zAddress);
                if (!IpcOk()) return false;

                double length = Math.Sqrt(x * x + y * y + z * z);
                if (!double.IsFinite(length) || length < 1e-6)
                {
                    return false;
                }

                double hor = Math.Atan2(z, x);
                double vert = Math.Asin(Math.Clamp(y / length, -1.0, 1.0));
                if (dx != 0 || dy != 0)
                {
                    hor += dx * m_signH;
                    vert = Math.Clamp(vert + dy * m_signV, m_vertMin, m_vertMax);
                    IPCUtils.WriteFloat(ipc, xAddress, (float)(Math.Cos(hor) * Math.Cos(vert) * length));
                    IPCUtils.WriteFloat(ipc, zAddress, (float)(Math.Sin(hor) * Math.Cos(vert) * length));
                    IPCUtils.WriteFloat(ipc, yAddress, (float)(Math.Sin(vert) * length));
                }

                HorAddress = address;
                VertAddress = yAddress;
                Hor = hor;
                Vert = vert;
                return true;
            }
        }

        sealed class Build
        {
            public readonly bool Trace;
            public readonly List<Camera> Cameras = new List<Camera>();
            public readonly List<ForcedWrite> Writes = new List<ForcedWrite>();

            public Build(IntPtr ipc, JsonElement e)
            {
                Trace = GetBool(e, "trace", false);
                if (e.TryGetProperty("cameras", out JsonElement cameras) && cameras.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement camera in cameras.EnumerateArray())
                    {
                        Cameras.Add(new Camera(ipc, camera));
                    }
                }
                if (e.TryGetProperty("writes", out JsonElement writes) && writes.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement write in writes.EnumerateArray())
                    {
                        Writes.Add(new ForcedWrite(ipc, write));
                    }
                }
                if (Cameras.Count == 0)
                {
                    throw new Exception("build has no cameras");
                }
            }
        }
    }
}
