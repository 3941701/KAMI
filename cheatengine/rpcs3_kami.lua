--[[
  rpcs3_kami.lua - Cheat Engine helpers for hunting camera values in RPCS3 (PS3) games.

  Install:  copy this file into Cheat Engine's "autorun" folder, or open it with
            Table > Show Cheat Table Lua Script and press Execute.
  It adds a "KAMI" menu to the main window and a global table `kami` for the Lua console.

  What it takes care of:
    * attaches to rpcs3.exe and finds where the PS3 memory sits in the process
      (normally 0x300000000) - a symbol "ps3" is registered, so an address can be
      typed anywhere in CE as   ps3+00A12340
    * makes sure the big-endian value types exist ("Float Big Endian",
      "4 Byte Big Endian", "2 Byte Big Endian"); PS3 memory is big-endian
    * limits the scan range to the PS3 address space
    * imports the candidates found by camfind.py and the pointer chains found by
      ptrscan.py into the address list.  Chains are re-resolved a few times per
      second, so the records keep following the object when it moves
    * exports the address list (as PS3 addresses) to JSON for the other tools

  One setting cannot be changed from a script: in Settings > Scan Settings tick
  MEM_MAPPED, otherwise scans skip the emulated memory entirely.

  PS3 pointers are 32-bit big-endian, so CE's own pointer records and pointer
  scanner do not apply; use ptrscan.py for chains and kami.addChain to watch them.
]]

kami = kami or {}
kami.base = nil
kami.HOST_FIRST = 0x300000000
kami.HOST_STEP = 0x100000000
kami.MIRROR = 0x100000000
kami.chains = kami.chains or {}

------------------------------------------------------------------ small JSON reader/writer
local function json_decode(text)
  local pos = 1
  local function ws()
    pos = text:find("%S", pos) or (#text + 1)
  end
  local value
  local function str()
    local out = {}
    pos = pos + 1
    while true do
      local c = text:sub(pos, pos)
      if c == "" then error("JSON: unterminated string") end
      if c == '"' then pos = pos + 1; break end
      if c == "\\" then
        local e = text:sub(pos + 1, pos + 1)
        local map = { b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }
        if e == "u" then
          local code = tonumber(text:sub(pos + 2, pos + 5), 16) or 63
          out[#out + 1] = code < 128 and string.char(code) or utf8.char(code)
          pos = pos + 6
        else
          out[#out + 1] = map[e] or e
          pos = pos + 2
        end
      else
        out[#out + 1] = c
        pos = pos + 1
      end
    end
    return table.concat(out)
  end
  function value()
    ws()
    local c = text:sub(pos, pos)
    if c == "{" then
      local obj = {}
      pos = pos + 1
      ws()
      if text:sub(pos, pos) == "}" then pos = pos + 1; return obj end
      while true do
        ws()
        local key = str()
        ws()
        if text:sub(pos, pos) ~= ":" then error("JSON: expected ':' at " .. pos) end
        pos = pos + 1
        obj[key] = value()
        ws()
        local d = text:sub(pos, pos)
        pos = pos + 1
        if d == "}" then return obj end
        if d ~= "," then error("JSON: expected ',' or '}' at " .. pos) end
      end
    elseif c == "[" then
      local arr = {}
      pos = pos + 1
      ws()
      if text:sub(pos, pos) == "]" then pos = pos + 1; return arr end
      while true do
        arr[#arr + 1] = value()
        ws()
        local d = text:sub(pos, pos)
        pos = pos + 1
        if d == "]" then return arr end
        if d ~= "," then error("JSON: expected ',' or ']' at " .. pos) end
      end
    elseif c == '"' then
      return str()
    elseif text:sub(pos, pos + 3) == "true" then pos = pos + 4; return true
    elseif text:sub(pos, pos + 4) == "false" then pos = pos + 5; return false
    elseif text:sub(pos, pos + 3) == "null" then pos = pos + 4; return nil
    else
      local s, e = text:find("^-?%d+%.?%d*[eE]?[+-]?%d*", pos)
      if not s then error("JSON: unexpected character at " .. pos) end
      pos = e + 1
      return tonumber(text:sub(s, e))
    end
  end
  return value()
end

local function json_string(s)
  return '"' .. tostring(s):gsub('[%c"\\]', function(c)
    if c == '"' then return '\\"' elseif c == "\\" then return "\\\\" end
    return string.format("\\u%04x", c:byte())
  end) .. '"'
end

local function read_file(path)
  local f, err = io.open(path, "rb")
  if not f then error("cannot open " .. tostring(path) .. ": " .. tostring(err)) end
  local text = f:read("a")
  f:close()
  return text
end

kami.json_decode = json_decode

------------------------------------------------------------------ memory access (PS3 addresses)
local function host(addr)
  if not kami.base then error("not attached: run kami.attach() first") end
  return kami.base + addr
end

local function raw(addr, n)
  -- the mirror 4 GiB above stays readable when the emulator has protected a page
  local t = readBytes(host(addr), n, true) or readBytes(host(addr) + kami.MIRROR, n, true)
  if not t or #t < n then return nil end
  return string.char(table.unpack(t))
end

function kami.u32(addr)
  local s = raw(addr, 4)
  return s and string.unpack(">I4", s) or nil
end

function kami.u16(addr)
  local s = raw(addr, 2)
  return s and string.unpack(">I2", s) or nil
end

function kami.f(addr)
  local s = raw(addr, 4)
  return s and string.unpack(">f", s) or nil
end

function kami.setf(addr, v)
  return writeBytes(host(addr), { string.pack(">f", v):byte(1, 4) })
end

function kami.setu32(addr, v)
  return writeBytes(host(addr), { string.pack(">I4", v & 0xFFFFFFFF):byte(1, 4) })
end

-- KAMI's DerefChain: value0 = base, value[i] = read32(value[i-1]) + offset[i]
function kami.chain(base, ...)
  local offsets = { ... }
  if type(offsets[1]) == "table" then offsets = offsets[1] end
  local cur = base
  for _, off in ipairs(offsets) do
    local p = kami.u32(cur)
    if not p or p == 0 then return nil end
    cur = (p + off) & 0xFFFFFFFF
  end
  return cur
end

function kami.ps3(hostAddress)
  return hostAddress - kami.base
end

------------------------------------------------------------------ attaching
local BE_TYPES = {
  { name = "Float Big Endian", size = 4, float = true },
  { name = "4 Byte Big Endian", size = 4, float = false },
  { name = "2 Byte Big Endian", size = 2, float = false },
}

local function be_type_script(def)
  local swap64 = def.size == 4 and "  mov eax,[rcx]\n  bswap eax\n" or "  movzx eax,word ptr [rcx]\n  xchg al,ah\n"
  local swap32 = def.size == 4 and "  mov eax,[eax]\n  bswap eax\n" or "  movzx eax,word ptr [eax]\n  xchg al,ah\n"
  local back64 = def.size == 4 and "  bswap ecx\n  mov [rdx],ecx\n" or "  xchg cl,ch\n  mov [rdx],cx\n"
  local back32 = def.size == 4 and "  bswap eax\n  mov [ebx],eax\n" or "  xchg al,ah\n  mov [ebx],ax\n"
  return table.concat({
    "alloc(TypeName,256)", "alloc(ByteSize,4)", "alloc(ConvertRoutine,1024)", "alloc(ConvertBackRoutine,1024)",
    def.float and "alloc(UsesFloat,1)" or "",
    "TypeName:", "db '" .. def.name .. "',0",
    "ByteSize:", "dd " .. def.size,
    def.float and "UsesFloat:\ndb 1" or "",
    "ConvertRoutine:", "[64-bit]", "  xor eax,eax", swap64 .. "  ret", "[/64-bit]",
    "[32-bit]", "  push ebp", "  mov ebp,esp", "  mov eax,[ebp+8]", swap32 .. "  pop ebp", "  ret 4", "[/32-bit]",
    "ConvertBackRoutine:", "[64-bit]", back64 .. "  ret", "[/64-bit]",
    "[32-bit]", "  push ebp", "  mov ebp,esp", "  push eax", "  push ebx", "  mov eax,[ebp+8]", "  mov ebx,[ebp+c]",
    back32 .. "  pop ebx", "  pop eax", "  pop ebp", "  ret 8", "[/32-bit]",
  }, "\n")
end

kami.be_type_script = be_type_script

local function ensure_types()
  local added = {}
  for _, def in ipairs(BE_TYPES) do
    if not getCustomType(def.name) then
      registerCustomTypeAutoAssembler(be_type_script(def))
      added[#added + 1] = def.name
    end
  end
  return added
end

local function same(a, b)
  if not a or not b or #a ~= #b then return false end
  for i = 1, #a do
    if a[i] ~= b[i] then return false end
  end
  return true
end

local function find_base()
  for k = 0, 15 do
    local cand = kami.HOST_FIRST + k * kami.HOST_STEP
    -- the PS3 executable is loaded at 0x10000 and the same bytes appear in the mirror
    local a = readBytes(cand + kami.MIRROR + 0x10000, 64, true)
    local b = readBytes(cand + 0x10000, 64, true)
    if a and #a == 64 and (b == nil or same(a, b)) then
      if a[1] == 0x7F and a[2] == 0x45 and a[3] == 0x4C and a[4] == 0x46 then
        return cand
      end
      if b and same(a, b) then
        local nonzero = false
        for i = 1, 64 do
          if a[i] ~= 0 then nonzero = true end
        end
        if nonzero then return cand end
      end
    end
  end
  return nil
end

function kami.attach()
  local pid = getOpenedProcessID()
  if not pid or pid == 0 then
    if not openProcess("rpcs3.exe") then
      print("KAMI: rpcs3.exe is not running")
      return false
    end
  end
  kami.base = find_base()
  if not kami.base then
    print("KAMI: PS3 memory not found - boot a game first. (RPCS3.log shows 'vm::g_base_addr'; " ..
          "set kami.base by hand if it is unusual.)")
    return false
  end
  registerSymbol("ps3", kami.base, true)
  local added = ensure_types()
  print(string.format("KAMI: attached, PS3 memory at %X (symbol 'ps3'); type addresses as ps3+00A12340", kami.base))
  if #added > 0 then print("KAMI: value types added: " .. table.concat(added, ", ")) end
  kami.scanRange()
  local ok, mapped = pcall(function() return getSettingsForm().cbMemMapped.Checked end)
  if ok and mapped == false then
    print("KAMI: WARNING - Settings > Scan Settings > MEM_MAPPED is off: scans will not see PS3 memory.")
  elseif not ok then
    print("KAMI: check that Settings > Scan Settings > MEM_MAPPED is ticked.")
  end
  return true
end

-- Scan only the PS3 address space; the default end skips RSX memory, stacks and SPU areas.
function kami.scanRange(lo, hi)
  lo, hi = lo or 0x10000, hi or 0xBFFFFFFF
  local ok = pcall(function()
    local form = getMainForm()
    form.FromAddress.Text = string.format("%X", kami.base + lo)
    form.ToAddress.Text = string.format("%X", kami.base + hi)
  end)
  if not ok then
    print(string.format("KAMI: set the scan range by hand: %X - %X", kami.base + lo, kami.base + hi))
  end
  return ok
end

------------------------------------------------------------------ address list
local function new_record(description, parent)
  local rec = getAddressList().createMemoryRecord()
  rec.Description = description
  if parent then rec.appendToEntry(parent) end
  return rec
end

local function group(description)
  local rec = new_record(description)
  rec.IsGroupHeader = true
  return rec
end

function kami.add(addr, description, typename, parent)
  local rec = new_record(description or string.format("%08X", addr), parent)
  rec.Address = string.format("ps3+%08X", addr)
  rec.Type = vtCustom
  rec.CustomTypeName = typename or "Float Big Endian"
  return rec
end

-- A record that follows a pointer chain: its address is recomputed by a timer.
function kami.addChain(description, base, offsets, typename, parent)
  local rec = new_record(description, parent)
  rec.Type = vtCustom
  rec.CustomTypeName = typename or "Float Big Endian"
  local entry = { record = rec, base = base, offsets = offsets, description = description }
  kami.chains[#kami.chains + 1] = entry
  kami.refreshChains()
  kami.startTimer()
  return rec
end

function kami.refreshChains()
  local alive = {}
  for _, entry in ipairs(kami.chains) do
    local ok, addr = pcall(kami.chain, entry.base, entry.offsets)
    local okRecord = pcall(function()
      if ok and addr then
        local text = string.format("ps3+%08X", addr)
        if entry.record.Address ~= text then entry.record.Address = text end
      else
        entry.record.Address = "0"
      end
    end)
    if okRecord then alive[#alive + 1] = entry end   -- a deleted record drops out of the list
  end
  kami.chains = alive
end

function kami.startTimer()
  if kami.timer then return end
  kami.timer = createTimer(nil, false)
  kami.timer.Interval = 250
  kami.timer.OnTimer = function() kami.refreshChains() end
  kami.timer.Enabled = true
end

local function hex(v)
  if type(v) == "number" then return v end
  return tonumber((tostring(v):gsub("^0[xX]", "")), 16)
end

function kami.importCamfind(path)
  local doc = json_decode(read_file(path))
  local n = 0
  for _, axis in ipairs({ "hor", "vert" }) do
    local g = group("camfind " .. axis .. " (best first)")
    for i, c in ipairs(doc[axis] or {}) do
      local desc = string.format("%s #%d %s%s score %s", axis, i, c.kind or "?",
        c.sticky and " STICKY" or "", tostring(c.score))
      kami.add(hex(c.addr), desc, "Float Big Endian", g)
      n = n + 1
      if c.partner_offset then
        kami.add(hex(c.addr) + c.partner_offset, desc .. " (pair)", "Float Big Endian", g)
      end
    end
  end
  print("KAMI: " .. n .. " candidates imported from " .. path)
  return n
end

function kami.importChains(path, limit)
  local doc = json_decode(read_file(path))
  local n = 0
  local g = group("pointer chains (live)")
  for i, c in ipairs(doc.chains or {}) do
    if limit and i > limit then break end
    for name, off in pairs(c.fields or {}) do
      local offsets = {}
      for _, o in ipairs(c.offsets or {}) do offsets[#offsets + 1] = hex(o) end
      offsets[#offsets + 1] = hex(off)
      local parts = {}
      for _, o in ipairs(offsets) do parts[#parts + 1] = string.format("+%X", o) end
      kami.addChain(string.format("chain %d %s [%s]%s", i, name, c.base, table.concat(parts)),
        hex(c.base), offsets, "Float Big Endian", g)
      n = n + 1
    end
  end
  print("KAMI: " .. n .. " chain records imported from " .. path)
  return n
end

-- Address list -> JSON with PS3 addresses, for ptrscan.py --target and for notes.
function kami.export(path)
  local al = getAddressList()
  local rows = {}
  for i = 0, al.Count - 1 do
    local rec = al.getMemoryRecord(i)
    local ok, address = pcall(function() return rec.CurrentAddress end)
    if ok and address and address >= kami.base and address < kami.base + 0x100000000 and not rec.IsGroupHeader then
      rows[#rows + 1] = string.format('  {"description": %s, "addr": "0x%08X", "type": %s, "value": %s}',
        json_string(rec.Description), address - kami.base, json_string(rec.CustomTypeName or ""),
        json_string(rec.Value or ""))
    end
  end
  local f, err = io.open(path, "w")
  if not f then error("cannot write " .. path .. ": " .. tostring(err)) end
  f:write('{"tool": "cheatengine", "records": [\n' .. table.concat(rows, ",\n") .. "\n]}\n")
  f:close()
  print("KAMI: " .. #rows .. " records written to " .. path)
  return #rows
end

------------------------------------------------------------------ menu
local function pick_file(save)
  local dialog = save and createSaveDialog(nil) or createOpenDialog(nil)
  dialog.Filter = "JSON (*.json)|*.json|All files (*.*)|*.*"
  dialog.DefaultExt = "json"
  if dialog.Execute() then return dialog.FileName end
  return nil
end

function kami.installMenu()
  if kami.menu then return end
  local ok = pcall(function()
    local main = getMainForm()
    local top = createMenuItem(main.Menu)
    top.Caption = "KAMI"
    main.Menu.Items.add(top)
    local function item(caption, action)
      local mi = createMenuItem(top)
      mi.Caption = caption
      mi.OnClick = function()
        local okAction, err = pcall(action)
        if not okAction then showMessage("KAMI: " .. tostring(err)) end
      end
      top.add(mi)
    end
    item("Attach to RPCS3", kami.attach)
    item("Import camfind.json...", function()
      local path = pick_file(false)
      if path then kami.importCamfind(path) end
    end)
    item("Import chains.json...", function()
      local path = pick_file(false)
      if path then kami.importChains(path, 20) end
    end)
    item("Export address list...", function()
      local path = pick_file(true)
      if path then kami.export(path) end
    end)
    kami.menu = top
  end)
  if not ok then print("KAMI: menu not installed; use the kami.* functions from the Lua console") end
end

if getMainForm then
  kami.installMenu()
end
