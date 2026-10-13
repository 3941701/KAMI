-- Runs cheatengine/rpcs3_kami.lua under plain Lua 5.3 with a stand-in for Cheat Engine's API.
-- Checks the script's own logic (base detection, big-endian access, chain resolution, JSON
-- import/export); it cannot check Cheat Engine itself.
local args = { ... }
local script, camfind, chains, out = args[1], args[2], args[3], args[4]

local BASE = 0x300000000
local mem = {}                      -- host address -> byte
local function poke(guest, bytes)
  for i = 1, #bytes do
    mem[BASE + guest + i - 1] = bytes:byte(i)
    mem[BASE + 0x100000000 + guest + i - 1] = bytes:byte(i)
  end
end
poke(0x10000, "\x7fELF" .. string.rep("\1", 60))
poke(0x00A12340, string.pack(">I4", 0x30250370))            -- game manager
poke(0x30250370 + 0x5C, string.pack(">I4", 0x30250800))     -- camera manager
poke(0x30250800 + 0x118, string.pack(">I4", 0x30251720))    -- camera
poke(0x30251720 + 0x1A8, string.pack(">f", 0.75))
poke(0x30251720 + 0x1C4, string.pack(">f", -0.25))

local symbols, types, records, messages = {}, {}, {}, {}
function getOpenedProcessID() return 0 end
function openProcess(name) return name == "rpcs3.exe" end
function readBytes(addr, n, asTable)
  local t = {}
  for i = 0, n - 1 do
    local b = mem[addr + i]
    if b == nil then return nil end
    t[#t + 1] = b
  end
  return t
end
function writeBytes(addr, t)
  for i, b in ipairs(t) do mem[addr + i - 1] = b end
  return true
end
function registerSymbol(name, addr) symbols[name] = addr end
function getCustomType(name) return types[name] end
function registerCustomTypeAutoAssembler(scriptText)
  local name = scriptText:match("db '([^']+)',0")
  assert(name, "custom type script has no name")
  assert(scriptText:find("ConvertRoutine:", 1, true) and scriptText:find("ConvertBackRoutine:", 1, true))
  types[name] = scriptText
end
vtCustom = 13
local function resolve(text)
  local sym, off = text:match("^(%a%w*)%+(%x+)$")
  if sym then return symbols[sym] + tonumber(off, 16) end
  return tonumber(text, 16) or 0
end
function getAddressList()
  return {
    Count = #records,
    createMemoryRecord = function()
      local rec = { Description = "", Address = "0", IsGroupHeader = false }
      rec.appendToEntry = function(parent) rec.parent = parent end
      setmetatable(rec, { __index = function(t, k)
        if k == "CurrentAddress" then return resolve(rawget(t, "Address")) end
        if k == "Value" then
          local a = resolve(rawget(t, "Address"))
          local b = readBytes(a, 4, true)
          return b and tostring(string.unpack(">f", string.char(table.unpack(b)))) or "??"
        end
      end })
      records[#records + 1] = rec
      return rec
    end,
    getMemoryRecord = function(i) return records[i + 1] end,
  }
end
function createTimer() return {} end
function showMessage(m) messages[#messages + 1] = m end
getMainForm = nil                    -- no GUI: the script must cope and skip its menu

dofile(script)
local function check(cond, what)
  print((cond and "   ok   " or "   FAIL ") .. what)
  if not cond then os.exit(1) end
end

check(kami.attach() == true, "attach finds the PS3 memory")
check(kami.base == BASE and symbols.ps3 == BASE, "base detected and symbol registered")
check(types["Float Big Endian"] and types["4 Byte Big Endian"] and types["2 Byte Big Endian"], "big-endian types registered")
check(kami.u32(0x00A12340) == 0x30250370, "u32 is read big-endian")
check(kami.f(0x30251720 + 0x1A8) == 0.75, "float is read big-endian")
local a = kami.chain(0x00A12340, 0x5C, 0x118, 0x1A8)
check(a == 0x30251720 + 0x1A8, "chain resolves with DerefChain semantics")
check(kami.chain(0x00A12340, { 0x5C, 0x118, 0x1C4 }) == 0x30251720 + 0x1C4, "chain accepts an offset table")
check(kami.chain(0x00A12348, 0x10) == nil, "chain through an unmapped/null pointer gives nil")
kami.setf(a, 1.5)
check(kami.f(a) == 1.5, "float write round-trips")
local rec = kami.addChain("yaw", 0x00A12340, { 0x5C, 0x118, 0x1A8 })
check(rec.Address == "ps3+303218C8" or rec.Address == string.format("ps3+%08X", a), "live chain record points at the field")
poke(0x30250800 + 0x118, string.pack(">I4", 0x30400000))     -- the camera object moves
kami.refreshChains()
check(rec.Address == string.format("ps3+%08X", 0x30400000 + 0x1A8), "record follows the object after it moves")

local doc = kami.json_decode('{"a": [1, 2.5, -3e2, "x\\"y\\u0041", true, false, null], "b": {"c": "0x10"}}')
check(doc.a[1] == 1 and doc.a[2] == 2.5 and doc.a[3] == -300 and doc.a[4] == 'x"yA' and doc.a[5] == true
      and doc.a[6] == false and doc.b.c == "0x10", "JSON reader handles the usual cases")

if camfind then
  local before = #records
  local n = kami.importCamfind(camfind)
  check(n > 0 and #records > before, "camfind.json imported: " .. n .. " candidates")
end
if chains then
  local n = kami.importChains(chains, 10)
  check(n > 0, "chains.json imported: " .. n .. " chain records")
end
if out then
  local n = kami.export(out)
  local back = kami.json_decode(io.open(out, "rb"):read("a"))
  check(n > 0 and #back.records == n and back.records[1].addr:match("^0x%x+$"), "export writes valid JSON with PS3 addresses (" .. n .. " records)")
end
print("CE SCRIPT LOGIC OK")
