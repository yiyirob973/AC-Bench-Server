-- Class sync, part of the AC Bench Lua app.
-- Keeps the "#Pride-<class>" tag in every installed car's ui/ui_car.json up to date:
--   1. works out each car's physics hash (same SHA-256 recipe as ac_carid.py in the desktop app),
--   2. asks the server about those hashes only (POST /v1/cars/lookup, the answer stays in memory),
--   3. writes one class tag per car. An older "#Pride-..." tag is replaced and other tags are left alone. The first
--      original is kept as ui_car.json.gridbak, so the desktop app's Restore still works.
-- ac_bench.lua only calls update() outside benchmark races, and gate() below also skips online races.
-- It works while you drive too, in a way that shouldn't be noticeable: small steps a few times a second, nothing that
-- takes more than a couple of milliseconds while the car is moving (big cars wait until you stop), and no steps at all
-- while the frame rate is low.
-- Settings are read from config.json next to this file (the desktop app writes it): server, include_provisional, auto.

-- Class sync, part of the AC Bench Lua app.
-- Keeps the "#Pride-<class>" tag in every installed car's ui/ui_car.json up to date:
--   1. works out each car's physics hash (same SHA-256 recipe as ac_carid.py in the desktop app),
--   2. asks the server about those hashes only (POST /v1/cars/lookup, the answer stays in memory),
--   3. writes one class tag per car. An older "#Pride-..." tag is replaced and other tags are left alone. The first
--      original is kept as ui_car.json.gridbak, so the desktop app's Restore still works.
-- ac_bench.lua only calls update() outside benchmark races, and gate() below also skips online races.
--
-- It starts as soon as the race has loaded and can tell it's an offline one (no waiting). The slow part of the job is
-- reading and hashing car files, so the desktop app does that outside the game and leaves the result in
-- car_hashes.json next to this file (ac_live.py). A car whose file names and sizes match that list is identified
-- without opening a single file here. Only cars the list doesn't cover (new or changed mods) are hashed in the game.
--
-- It has to stay invisible while you play, so it works in tiny pieces:
--   * every frame it gets a time budget (about 2 ms while the car is stopped, 0.5 ms while it's moving) and stops as
--     soon as the budget is used up, so a frame is never held up by more than that;
--   * reading and hashing a big file is the one thing that can't be split, so it's only done one file at a time, with
--     a pause after it that is several times as long as the file took. While driving only small files are touched,
--     and cars with big files wait until you stop or are left to the desktop app (they're counted in the window);
--   * a car's file list comes from one directory scan with the sizes and dates included, instead of several file system
--     calls for every file;
--   * it stands back for a few seconds when a frame takes very long, and does nothing while the frame rate is low.
-- Settings are read from config.json next to this file (the desktop app writes it): server, include_provisional, auto.

local M = {}

local PREFIX_MIN = 3
local BUDGET_STOPPED = 0.003        -- seconds of work per frame while the car is stopped
local BUDGET_DRIVING = 0.0005       -- ... and while it's moving
local UNITS_STOPPED = 12            -- most small steps per frame, whatever the clock says
local UNITS_DRIVING = 2
local HEAVY_GAP_STOPPED = 0.06      -- seconds to wait after reading and hashing a big file (at least; see runBudget)
local HEAVY_GAP_DRIVING = 0.6
local SMALL_FILE = 24 * 1024        -- files up to this size count as cheap steps
local FILE_MAX_DRIVING = 96 * 1024  -- while driving, a car with a file bigger than this waits until you stop
local FILE_MAX_STOPPED = 768 * 1024 -- a car with a bigger file is left alone (the desktop app tags it)
local FILE_MAX_MANUAL = 3 * 1024 * 1024   -- the same limit when "Check now" was pressed
local SLOW_FRAME = 0.022            -- no steps while the smoothed frame time is above this (below about 45 fps)
local SPIKE = 0.06                  -- a single frame this long makes the sync stand back for BACKOFF seconds
local BACKOFF = 4
local GATE_EVERY = 0.25             -- how often to look at the race and the car's speed
local BATCH = 250                   -- hashes per lookup request (a smaller answer is cheaper to read)
local MOVING_KMH = 2                -- at or above this speed the car counts as moving
local VERIFY_CARS = 3               -- the first few cars with a data folder are checked against the plain scan method

local clock = os.preciseClock or os.clock   -- may be missing; then only the step counts above limit the work

-- Timing of every kind of step, so it's clear what costs time. Written to the CSP log when a check ends
-- (Documents/Assetto Corsa/logs/custom_shaders_patch.log, lines starting with "ac_bench/class_sync: profile").
local prof = {}
local function mark(name, t0, a, b)
  if not (clock and t0) then return end
  local d = clock() - t0
  local e = prof[name]
  if not e then e = { total = 0, n = 0, max = 0 } prof[name] = e end
  e.total, e.n = e.total + d, e.n + 1
  if d > e.max then e.max, e.a, e.b = d, a, b end
end

local dir
local cfg = { server = '', include_provisional = false, auto = false, prefix = '#Pride-' }
local state = { hashes = {}, applied = {}, table_version = nil, last_check = nil }   -- hashes[carId] = { sig = string, hash = hex }
local st = {
  paused = false, phase = 'wait', note = 'starting', elapsed = 0, fdt = 0.016,
  now = 0, nextHeavy = 0, backoff = 0, gateAt = GATE_EVERY, gate = 'unknown', driving = false, manual = false,
  deferred = {}, incomplete = false, job = nil, scanNames = nil, si = 0,
  ids = {}, i = 0, hashOf = {}, batches = {}, b = 0, pending = false,
  labels = {}, todo = {}, t = 0,
  n = { cars = 0, hashed = 0, seeded = 0, known = 0, tagged = 0, same = 0, skipped = 0, errors = 0, big = 0 },
  worst = {}, slowest = nil,
}
local seed = {}           -- seed[carId] = { fp = string, hash = hex }, from car_hashes.json (written by the desktop app)
local legacyScan = false      -- set if the directory scan with attributes turns out not to work on this CSP
local verified = 0

-- ---------------------------------------------------------------- helpers
local function log(s) ac.log('ac_bench/class_sync: ' .. tostring(s)) end

local function folderOf()
  if __dirname then return __dirname end
  local ok, d = pcall(function() return ac.getFolder(ac.FolderID.ScriptOrigin) end)
  if ok and d then return d end
  return nil
end

local function readFile(p)
  local ok, s = pcall(io.load, p, nil)
  if ok and type(s) == 'string' then return s end
  return nil
end

local function writeFile(p, s)
  local ok, res = pcall(io.save, p, s)
  return ok and res ~= false
end

local function isDir(p)
  local ok, r = pcall(io.dirExists, p)
  return ok and r == true
end

local function isFile(p)
  local ok, r = pcall(io.fileExists, p)
  return ok and r == true
end

local function listNames(p)
  local ok, r = pcall(io.scanDir, p, '*')
  if ok and type(r) == 'table' then return r end
  return {}
end

local function parses(s)
  local ok, v = pcall(JSON.parse, (s:gsub('^\239\187\191', '')))
  return ok and v ~= nil
end

local function lowerHex(s) return (s or ''):lower() end

local function sha256(s)
  return lowerHex(ac.checksumSHA256(s))
end

-- ---------------------------------------------------------------- car identity (mirrors ac_carid.car_hash)
-- Python sorts the data/ files by path parts, not by the joined string, so do the same here.
local function splitPath(s)
  local t = {}
  for part in s:gmatch('[^/]+') do t[#t + 1] = part end
  return t
end

local function pathLess(a, b)
  local x, y = splitPath(a), splitPath(b)
  for k = 1, math.min(#x, #y) do
    if x[k] ~= y[k] then return x[k] < y[k] end
  end
  return #x < #y
end

local function collect(base, rel, out)
  for _, name in ipairs(listNames(base .. '/' .. rel)) do
    local r = rel == '' and name or (rel .. '/' .. name)
    local full = base .. '/' .. r
    if isDir(full) then collect(base, r, out)
    elseif isFile(full) then out[#out + 1] = r end
  end
end

-- list of { rel = 'data.acd' | 'data/x' | 'collider.ini', path = full }, in the order the hash uses
local function physicsFiles(carDir)
  local parts = {}
  if isFile(carDir .. '/data.acd') then parts[#parts + 1] = { rel = 'data.acd', path = carDir .. '/data.acd' } end
  if isDir(carDir .. '/data') then
    local rels = {}
    collect(carDir .. '/data', '', rels)
    table.sort(rels, pathLess)
    for _, r in ipairs(rels) do parts[#parts + 1] = { rel = 'data/' .. r, path = carDir .. '/data/' .. r } end
  end
  if isFile(carDir .. '/collider.ini') then parts[#parts + 1] = { rel = 'collider.ini', path = carDir .. '/collider.ini' } end
  return parts
end

local hasAttr = nil
local function attrsOf(p)
  if hasAttr == nil then hasAttr = type(io.getAttributes) == 'function' end
  if not hasAttr then return nil end
  local ok, a = pcall(io.getAttributes, p)
  if ok and type(a) == 'table' then return a end
  return nil
end

-- CSP calls the size "fileSize"; older code here read "size", which isn't there
local function fileSizeOf(a) return tonumber(a.fileSize or a.size) or 0 end

local function sizeOfPath(p, a)
  if a then return fileSizeOf(a) end
  local ok, n = pcall(function() return io.fileSize(p) end)
  n = ok and tonumber(n) or 0
  return n > 0 and n or 0
end

-- The signature tells whether a car's files changed since the hash was cached: name, size and date of every file.
local function makeSig(parts)
  local sig, total = {}, 0
  for _, p in ipairs(parts) do
    if not p.known then return 'n/a', total end
    sig[#sig + 1] = p.rel .. ':' .. tostring(p.size) .. ':' .. tostring(p.stamp)
    total = total + p.size
  end
  return table.concat(sig, '|'), total
end

-- A short fingerprint of a car's physics files by name and size only (the desktop app computes the same one).
local function fingerprint(parts)
  local strs = {}
  for _, p in ipairs(parts) do
    if not p.known then return nil end
    strs[#strs + 1] = p.rel .. ':' .. string.format('%.0f', p.size)
  end
  table.sort(strs)
  return sha256(table.concat(strs, '|')):sub(1, 16)
end

-- Lists a folder with sizes and dates in one call (the callback gets the attributes of every entry).
-- Returns false if that doesn't work here.
local function scanInto(base, rel, out)
  local dirPath = rel == '' and base or (base .. '/' .. rel)
  local subdirs = {}
  local ok = pcall(io.scanDir, dirPath, '*', function(name, attrs)
    if type(name) ~= 'string' or type(attrs) ~= 'table' then error('no attributes') end
    name = name:match('[^/\\]+$') or name
    local r = rel == '' and name or (rel .. '/' .. name)
    if attrs.isDirectory then subdirs[#subdirs + 1] = r
    else out[#out + 1] = { rel = r, size = fileSizeOf(attrs), stamp = tostring(attrs.lastWriteTime) } end
  end)
  if not ok then return false end
  for _, r in ipairs(subdirs) do
    if not scanInto(base, r, out) then return false end
  end
  return true
end

local function partFrom(rel, path, size, stamp, known)
  return { rel = rel, path = path, size = size or 0, stamp = stamp or '?', known = known }
end

-- the fast way: a few calls per car, however many files it has
local function fastInfo(carDir)
  local parts = {}
  local acd = carDir .. '/data.acd'
  if isFile(acd) then
    local a = attrsOf(acd)
    parts[#parts + 1] = partFrom('data.acd', acd, sizeOfPath(acd, a), a and tostring(a.lastWriteTime), a ~= nil)
  end
  if isDir(carDir .. '/data') then
    local found = {}
    if not scanInto(carDir .. '/data', '', found) then return nil end
    table.sort(found, function(x, y) return pathLess(x.rel, y.rel) end)
    for _, f in ipairs(found) do
      parts[#parts + 1] = partFrom('data/' .. f.rel, carDir .. '/data/' .. f.rel, f.size, f.stamp, true)
    end
  end
  local col = carDir .. '/collider.ini'
  if isFile(col) then
    local a = attrsOf(col)
    parts[#parts + 1] = partFrom('collider.ini', col, sizeOfPath(col, a), a and tostring(a.lastWriteTime), a ~= nil)
  end
  return parts
end

-- the slow way, the one that always worked: used if the fast scan doesn't behave
local function slowInfo(carDir)
  local parts = {}
  for _, p in ipairs(physicsFiles(carDir)) do
    local a = attrsOf(p.path)
    parts[#parts + 1] = partFrom(p.rel, p.path, sizeOfPath(p.path, a), a and tostring(a.lastWriteTime), a ~= nil)
  end
  return parts
end

-- Returns parts, sig, total (parts is empty for a folder without physics files).
local function physicsInfo(carDir)
  local parts
  if not legacyScan then
    parts = fastInfo(carDir)
    if parts == nil then
      legacyScan = true
      log('directory scan with attributes is not available, using the slower way')
    elseif verified < VERIFY_CARS and isDir(carDir .. '/data') then
      -- make sure the fast list is the same as the slow one before trusting it
      verified = verified + 1
      local slow = physicsFiles(carDir)
      local same = (#slow == #parts)
      if same then
        for k = 1, #slow do if slow[k].rel ~= parts[k].rel then same = false break end end
      end
      if not same then
        legacyScan = true
        parts = nil
        log('directory scan listed different files than the plain way, using the slower way')
      end
    end
  end
  if parts == nil then parts = slowInfo(carDir) end
  local sig, total = makeSig(parts)
  return parts, sig, total
end

local function carHash(parts)
  if #parts == 0 then return nil end
  local buf = {}
  for _, p in ipairs(parts) do
    local data = readFile(p.path)
    if data == nil then return nil end
    buf[#buf + 1] = p.rel .. '\0' .. sha256(data) .. '\n'
  end
  return sha256(table.concat(buf))
end

-- ---------------------------------------------------------------- tag editing (works on the text, so the rest of the file stays as it was)
local function decodeString(token)
  local ok, v = pcall(JSON.parse, token)
  if ok and type(v) == 'string' then return v end
  return nil
end

local function encodeString(s)
  return '"' .. s:gsub('\\', '\\\\'):gsub('"', '\\"') .. '"'
end

-- Finds the tags array. Returns openIdx, closeIdx, { {raw=, val=}, ... }, or nil if there is no tags key, or false and a reason.
local function findTags(raw)
  local from = 1
  while true do
    local s, e = raw:find('"tags"%s*:%s*%[', from)
    if not s then return nil end
    if s == 1 or raw:sub(s - 1, s - 1) ~= '\\' then
      local open, k, items = e, e + 1, {}
      while true do
        local c = raw:sub(k, k)
        if c == '' then return false, 'tags array is not closed' end
        if c == ']' then return open, k, items end
        if c == '"' then
          local j = k + 1
          while true do
            local d = raw:sub(j, j)
            if d == '' then return false, 'unterminated string in tags' end
            if d == '\\' then j = j + 2
            elseif d == '"' then break
            else j = j + 1 end
          end
          local token = raw:sub(k, j)
          local val = decodeString(token)
          if val == nil then return false, 'unreadable tag string' end
          items[#items + 1] = { raw = token, val = val }
          k = j + 1
        elseif c:match('[%s,]') then k = k + 1
        else return false, 'tags is not a list of strings' end
      end
    end
    from = e + 1
  end
end

-- Returns the new file text, nil if nothing needs to change, or false and a reason.
local function withClassTag(raw, prefix, label)
  local bom = ''
  if raw:sub(1, 3) == '\239\187\191' then bom, raw = raw:sub(1, 3), raw:sub(4) end
  local nl = raw:find('\r\n', 1, true) and '\r\n' or '\n'
  local want = prefix .. label
  local lp = prefix:lower()

  local open, close, items = findTags(raw)
  if open == false then return false, close end

  if open == nil then   -- no tags key, add one before the final }
    local last = raw:match('.*()}')
    if not last then return false, 'no closing brace' end
    local before = raw:sub(1, last - 1):gsub('%s+$', '')
    local indent = raw:match('\n([ \t]+)"') or '    '
    local comma = before:sub(-1) == '{' and '' or ','
    return bom .. before .. comma .. nl .. indent .. '"tags": [' .. encodeString(want) .. ']' .. nl .. raw:sub(last)
  end

  local keptRaw, keptVal, seen, oldVal = {}, {}, {}, {}
  for _, it in ipairs(items) do
    oldVal[#oldVal + 1] = it.val
    if it.val:lower():sub(1, #lp) ~= lp then
      local key = it.val:match('^%s*(.-)%s*$'):lower()
      if not seen[key] then
        seen[key] = true
        keptRaw[#keptRaw + 1] = it.raw
        keptVal[#keptVal + 1] = it.val
      end
    end
  end
  local key = want:lower()
  if not seen[key] then
    keptRaw[#keptRaw + 1] = encodeString(want)
    keptVal[#keptVal + 1] = want
  end

  local same = (#oldVal == #keptVal)
  if same then
    for k = 1, #oldVal do if oldVal[k] ~= keptVal[k] then same = false break end end
  end
  if same then return nil end

  local inner = raw:sub(open + 1, close - 1)
  local text
  if inner:find('\n', 1, true) then
    local indent = inner:match('\n([ \t]*)"') or '        '
    local closeIndent = inner:match('\n([ \t]*)$') or ''
    text = nl .. indent .. table.concat(keptRaw, ',' .. nl .. indent) .. nl .. closeIndent
  else
    text = table.concat(keptRaw, ', ')
  end
  return bom .. raw:sub(1, open) .. text .. raw:sub(close)
end

-- Returns 'changed', 'same', 'skipped', 'error', or 'defer' (a write is needed but it isn't the right moment for one).
local function applyTag(carId, label, allowWrite)
  local carsDir = M.carsDir
  local f = carsDir .. '/' .. carId .. '/ui/ui_car.json'
  if not validLabel(label) or tostring(carId):find('[/\\]') or tostring(carId):find('..', 1, true) then return 'skipped' end
  local raw = readFile(f)
  if raw == nil then return 'skipped' end
  local new, why = withClassTag(raw, cfg.prefix, label)     -- plain text work, no JSON parsing needed to find out if it's already right
  if new == nil then return 'same' end
  if new == false then log(carId .. ': left untouched (' .. tostring(why) .. ')') return 'skipped' end
  if not allowWrite then return 'defer' end
  if parses(raw) then
    if not parses(new) then log(carId .. ': edit would break the JSON; left untouched') return 'skipped' end
  end
  local bak = f .. '.gridbak'
  if not isFile(bak) and not writeFile(bak, raw) then
    log(carId .. ': could not write backup; left untouched')
    return 'error'
  end
  if not writeFile(f, new) or readFile(f) ~= new then
    writeFile(f, raw)        -- put the original back
    log(carId .. ': write failed; original restored')
    return 'error'
  end
  return 'changed'
end

-- ---------------------------------------------------------------- state & config
-- Class labels look like 1A .. 1Z, 2A .. 2Z. Anything else from the server is ignored, never written to a car.
local function validLabel(label)
  return type(label) == 'string' and #label <= 4 and label:match('^[1-9]%d?[A-Z]$') ~= nil
end

local function validHash(h)
  return type(h) == 'string' and #h == 64 and h:match('^%x+$') ~= nil
end

-- https only; plain http is accepted for this computer, so a local test server still works.
local function secureServer(url)
  local u = url:lower()
  if u:find('[@%c%s\\]') then return false end
  if u:match('^https://[^/]+') then return true end
  for _, host in ipairs({ 'localhost', '127%.0%.0%.1' }) do
    -- the host must end right after its name: localhost.example.com is somebody else's machine
    if u:match('^http://' .. host .. '$') or u:match('^http://' .. host .. '[:/]') then return true end
  end
  return false
end

local function loadConfig()
  local s = dir and readFile(dir .. '/config.json')
  if not s then return end
  local ok, c = pcall(JSON.parse, s)
  if not ok or type(c) ~= 'table' then return end
  if type(c.server) == 'string' then
    local url = c.server:gsub('%s+', ''):gsub('/+$', '')
    if url == '' or secureServer(url) then cfg.server = url
    else cfg.server = ''; log('the server address must start with https://, class tags are off') end
  end
  if c.include_provisional ~= nil then cfg.include_provisional = (c.include_provisional == true) end
  if c.auto ~= nil then cfg.auto = (c.auto == true) end
  if type(c.prefix) == 'string' and #c.prefix >= PREFIX_MIN then cfg.prefix = c.prefix end
end

local function loadState()
  local s = dir and readFile(dir .. '/class_sync_state.json')
  if not s then return end
  local ok, c = pcall(JSON.parse, s)
  if ok and type(c) == 'table' and type(c.hashes) == 'table' then
    state.hashes = c.hashes
    if type(c.applied) == 'table' then state.applied = c.applied end
    state.table_version = c.table_version
    state.last_check = tonumber(c.last_check)
  end
end

-- car_hashes.json: { "v": 1, "cars": { "<car id>": [ "<fingerprint>", "<physics hash>" ] } }
local function loadSeed()
  seed = {}
  local s = dir and readFile(dir .. '/car_hashes.json')
  if not s then return end
  local ok, c = pcall(JSON.parse, s)
  if not ok or type(c) ~= 'table' or type(c.cars) ~= 'table' then return end
  for id, e in pairs(c.cars) do
    if type(e) == 'table' and type(e[1]) == 'string' and type(e[2]) == 'string' then seed[id] = { fp = e[1], hash = e[2] } end
  end
end

local function saveState()
  if not dir then return end
  local t0 = clock and clock()
  local parts = {}
  for id, e in pairs(state.hashes) do
    if type(e) == 'table' and e.hash then
      parts[#parts + 1] = string.format('%s:{"sig":%s,"hash":"%s"}', encodeString(id), encodeString(e.sig or ''), e.hash)
    end
  end
  local applied = {}
  for id, v in pairs(state.applied) do
    if type(v) == 'string' then applied[#applied + 1] = encodeString(id) .. ':' .. encodeString(v) end
  end
  writeFile(dir .. '/class_sync_state.json', '{"table_version":' .. tostring(tonumber(state.table_version) or 'null') ..
    ',"last_check":' .. tostring(tonumber(state.last_check) or 'null') ..
    ',"hashes":{' .. table.concat(parts, ',') .. '},"applied":{' .. table.concat(applied, ',') .. '}}')
  mark('save state', t0)
end

-- ---------------------------------------------------------------- phases
local reportProfile   -- defined below

local function setPhase(p, note)
  st.phase = p
  st.paused = false
  if p == 'done' and reportProfile then reportProfile() end
  if note then st.note = note end
end

local function begin(manual)
  st.ids, st.i, st.hashOf, st.batches, st.b, st.pending = {}, 0, {}, {}, 0, false
  st.deferred, st.incomplete, st.job, st.scanNames, st.si = {}, false, nil, nil, 0
  st.labels, st.todo, st.t = {}, {}, 0
  st.manual = manual == true
  st.n = { cars = 0, hashed = 0, seeded = 0, known = 0, tagged = 0, same = 0, skipped = 0, errors = 0, big = 0 }
  prof, st.worst, st.slowest = {}, {}, nil
  local t0 = clock and clock()
  loadSeed()
  mark('load seed', t0)
  setPhase('scan', 'looking for installed cars')
end

reportProfile = function()
  local names = {}
  for name in pairs(prof) do names[#names + 1] = name end
  table.sort(names, function(a, b) return prof[a].max > prof[b].max end)
  for _, name in ipairs(names) do
    local e = prof[name]
    log(string.format('profile %-12s n=%-5d total=%6.0f ms  slowest=%5.1f ms  (%s %s)', name, e.n, e.total * 1000,
      e.max * 1000, tostring(e.a or ''), tostring(e.b or '')))
  end
  for ph, d in pairs(st.worst) do log(string.format('profile worst frame while in %s: %.0f ms', ph, d * 1000)) end
  local top = names[1]
  if top and prof[top].max >= 0.004 then
    st.slowest = string.format('slowest step: %s, %.0f ms', top, prof[top].max * 1000)
  end
end

-- Every phase is a function that does one small step and says how it went:
--   'cheap' (done, more can follow this frame), 'heavy' (done, and it was a big one: wait a while before the next),
--   'idle'  (nothing can be done right now), 'end' (the phase is over).
local function scanWork()
  if not st.scanNames then
    local base
    local ok, d = pcall(function() return ac.getFolder(ac.FolderID.ContentCars) end)
    if ok and d then base = d end
    if not base then
      local ok2, root = pcall(function() return ac.getFolder(ac.FolderID.Root) end)
      if ok2 and root then base = root .. '/content/cars' end
    end
    if not base or not isDir(base) then setPhase('off', 'cars folder not found') return 'end' end
    M.carsDir = base
    st.scanNames, st.si = listNames(base), 0
    return 'cheap'
  end
  st.si = st.si + 1
  local name = st.scanNames[st.si]
  if not name then
    table.sort(st.ids)
    st.n.cars = #st.ids
    st.scanNames = nil
    setPhase('hash', 'checking installed cars')
    return 'end'
  end
  if isFile(M.carsDir .. '/' .. name .. '/ui/ui_car.json') then st.ids[#st.ids + 1] = name end
  st.note = string.format('looking for installed cars %d / %d', st.si, #st.scanNames)
  return 'cheap'
end

local function startLookup()
  saveState()
  local seen, list = {}, {}
  for _, h in pairs(st.hashOf) do
    if not seen[h] then seen[h] = true list[#list + 1] = h end
  end
  table.sort(list)
  for k = 1, #list, BATCH do
    local b = {}
    for j = k, math.min(k + BATCH - 1, #list) do b[#b + 1] = list[j] end
    st.batches[#st.batches + 1] = b
  end
  if #st.batches == 0 then setPhase('done', 'couldn\'t identify any car') return end
  setPhase('lookup', 'asking the server')
end

-- Hashes one file of the car in progress. Returns 'more', 'done' (and the hash) or 'fail'.
local function stepJob(job)
  local p = job.parts[job.k + 1]
  if not p then return 'done', sha256(table.concat(job.buf)) end
  local t0 = clock and clock()
  local data = readFile(p.path)
  mark('read file', t0, job.id .. '/' .. p.rel, p.size)
  if data == nil then return 'fail' end
  local t1 = clock and clock()
  job.buf[#job.buf + 1] = p.rel .. '\0' .. sha256(data) .. '\n'
  mark('sha256', t1, job.id .. '/' .. p.rel, p.size)
  job.k = job.k + 1
  return 'more'
end

local function hashWork(driving, heavyOk)
  local job = st.job
  if job then
    local p = job.parts[job.k + 1]
    if p and driving and p.size > FILE_MAX_DRIVING then      -- the car started moving: put this one off until it stops
      st.deferred[#st.deferred + 1] = job
      st.job = nil
      return 'cheap'
    end
    local heavy = p ~= nil and p.size > SMALL_FILE
    if heavy and not heavyOk then return 'idle' end
    local r, h = stepJob(job)
    if r == 'more' then return heavy and 'heavy' or 'cheap' end
    st.job = nil
    if r == 'done' and h then
      state.hashes[job.id] = { sig = job.sig, hash = h }
      st.hashOf[job.id] = h
      st.n.hashed = st.n.hashed + 1
    else
      st.n.errors = st.n.errors + 1
    end
    return 'cheap'
  end

  if st.i < #st.ids then
    st.i = st.i + 1
    local id = st.ids[st.i]
    st.note = string.format('checking cars %d / %d', st.i, #st.ids)
    local t0 = clock and clock()
    local parts, sig = physicsInfo(M.carsDir .. '/' .. id)
    mark('list files', t0, id, #parts)
    if #parts == 0 then return 'cheap' end
    local cached = state.hashes[id]
    if type(cached) == 'table' and cached.hash and (cached.sig == sig or sig == 'n/a') then
      st.hashOf[id] = cached.hash
      return 'cheap'
    end
    -- The desktop app's list is only trusted for a car this app hasn't seen before. If it has seen the car and its files
    -- changed since (the date moved), the car is hashed again here, even if names and sizes still match the list.
    local sd = not (type(cached) == 'table' and cached.hash) and seed[id] or nil
    if sd then
      local fp = fingerprint(parts)
      if fp and fp == sd.fp then       -- the desktop app already hashed exactly these files
        state.hashes[id] = { sig = sig, hash = sd.hash }
        st.hashOf[id] = sd.hash
        st.n.seeded = st.n.seeded + 1
        return 'cheap'
      end
    end
    local biggest = 0
    for _, p in ipairs(parts) do if p.size > biggest then biggest = p.size end end
    if biggest > (st.manual and FILE_MAX_MANUAL or FILE_MAX_STOPPED) then
      st.n.big = st.n.big + 1          -- too big to read here without a hitch; the desktop app handles it
      return 'cheap'
    end
    local newJob = { id = id, parts = parts, sig = sig, k = 0, buf = {} }
    if driving and biggest > FILE_MAX_DRIVING then
      st.deferred[#st.deferred + 1] = newJob
    else
      st.job = newJob
    end
    return 'cheap'
  end

  if #st.deferred > 0 then
    if driving then
      -- don't hold everything up: look up what we have, the big cars are picked up by the next check
      st.incomplete = true
      startLookup()
      return 'end'
    end
    st.job = table.remove(st.deferred)
    return 'cheap'
  end
  startLookup()
  return 'end'
end

local function lookupWork()
  if st.pending then return 'idle' end
  st.b = st.b + 1
  local hashes = st.batches[st.b]
  if not hashes then
    -- all answers are in, build the work list
    for id, h in pairs(st.hashOf) do
      local label = st.labels[h]
      if label then st.todo[#st.todo + 1] = { id = id, label = label } end
    end
    table.sort(st.todo, function(a, b) return a.id < b.id end)
    st.n.known = #st.todo
    setPhase('apply', 'writing class tags')
    return 'end'
  end
  st.pending = true
  local body = JSON.stringify({ hashes = hashes })
  web.post(cfg.server .. '/v1/cars/lookup',
    { ['Content-Type'] = 'application/json', ['Accept'] = 'application/json', ['User-Agent'] = 'ac_bench-lua/1' },
    body,
    function(err, response)
      st.pending = false
      local okBody, data = false, nil
      if not err and response and tonumber(response.status) == 200 then
        local tp = clock and clock()
        okBody, data = pcall(JSON.parse, response.body)
        mark('parse answer', tp, #(response.body or ''), '')
      end
      if not okBody or type(data) ~= 'table' then
        setPhase('done', 'server unreachable or returned an error (' .. tostring(err or (response and response.status)) .. ')')
        st.n.errors = st.n.errors + 1
        return
      end
      for _, rec in ipairs(data.cars or {}) do
        local status = tostring(rec.status or ''):lower()
        local label = type(rec) == 'table' and rec.class
        if validHash(rec.car_hash) and validLabel(label) and (status == 'verified' or (cfg.include_provisional and status == 'provisional')) then
          st.labels[rec.car_hash] = label
        end
      end
      if data.table_version then state.table_version = data.table_version end
    end)
  return 'cheap'
end

local function stampOf(f)
  local a = attrsOf(f)
  if not a then return nil end
  return string.format('%.0f', fileSizeOf(a)) .. ':' .. tostring(a.lastWriteTime)
end

local function applyWork(_, heavyOk)
  local job = st.todo[st.t + 1]
  if not job then
    if not st.incomplete then state.last_check = os.time and os.time() or nil end
    saveState()
    setPhase('done', string.format('done, %d tag(s) written, %d already right', st.n.tagged, st.n.same))
    log(st.note)
    return 'end'
  end
  local f = M.carsDir .. '/' .. job.id .. '/ui/ui_car.json'
  -- A car whose ui_car.json is still exactly as it was left after the last tagging doesn't need to be opened at all
  local t0 = clock and clock()
  local before = stampOf(f)
  mark('stat ui json', t0, job.id, '')
  -- This only skips the car if the server's class is the same as the one written last time AND the file hasn't been
  -- touched since. A new class from the server, or any edit to the file, gives a different value and the car is redone.
  local want = job.label .. '|' .. tostring(before)
  if before and state.applied[job.id] == want then
    st.t = st.t + 1
    st.n.same = st.n.same + 1
    st.note = string.format('writing tags %d / %d', st.t, #st.todo)
    return 'cheap'
  end
  local t1 = clock and clock()
  local r = applyTag(job.id, job.label, heavyOk)
  mark(r == 'changed' and 'write tag' or 'read ui json', t1, job.id, '')
  if r == 'defer' then return 'idle' end
  st.t = st.t + 1
  if r == 'changed' then st.n.tagged = st.n.tagged + 1
  elseif r == 'same' then st.n.same = st.n.same + 1
  elseif r == 'error' then st.n.errors = st.n.errors + 1
  else st.n.skipped = st.n.skipped + 1 end
  if r == 'changed' or r == 'same' then
    local after = stampOf(f)
    if after then state.applied[job.id] = job.label .. '|' .. after end
  end
  st.note = string.format('writing tags %d / %d', st.t, #st.todo)
  return r == 'changed' and 'heavy' or 'cheap'
end

-- Runs small steps until the frame's time is used up. After a big step the next big one waits several times as long
-- as the first one took, so the work never takes more than a small share of the time.
local function runBudget(fn, driving)
  local budget = driving and BUDGET_DRIVING or BUDGET_STOPPED
  local units = driving and UNITS_DRIVING or UNITS_STOPPED
  local t0 = clock and clock()
  for _ = 1, units do
    local tu = clock and clock()
    local r = fn(driving, st.now >= st.nextHeavy)
    if r == 'heavy' then
      local spent = (clock and tu) and (clock() - tu) or 0.004
      st.nextHeavy = st.now + math.max(driving and HEAVY_GAP_DRIVING or HEAVY_GAP_STOPPED, spent * 8)
      return
    end
    if r == 'idle' or r == 'end' then return end
    if clock and t0 and clock() - t0 >= budget then return end
  end
end

-- ---------------------------------------------------------------- public API
M._withClassTag = withClassTag      -- exposed for offline tests
M._carHash = function(d) return carHash(physicsFiles(d)) end
function M.init()
  dir = folderOf()
  loadConfig()
  loadState()
  if type(ac.checksumSHA256) ~= 'function' then
    setPhase('off', 'this CSP version has no ac.checksumSHA256, so cars can\'t be identified')
  elseif cfg.server == '' then
    setPhase('off', 'no server address, open the desktop app once so it can write config.json')
  elseif not cfg.auto then
    setPhase('off', 'automatic tagging is off (use Check now)')
  end
end

-- Returns 'ok', 'online' or 'unknown', and whether the car is moving.
local function gate()
  local sim = ac.getSim()
  if not sim then return 'unknown' end
  local online = sim.isOnlineRace
  if online == nil then online = sim.isOnline end
  if online == nil then return 'unknown' end
  if online then return 'online' end
  local car = ac.getCar(0)
  if not car or car.speedKmh == nil then return 'unknown' end
  return 'ok', car.speedKmh >= MOVING_KMH
end

-- called every frame by ac_bench.lua, but only outside benchmark races
function M.update(dt)
  local p = st.phase
  if p == 'off' or p == 'done' then return end
  st.now = st.now + dt
  st.fdt = st.fdt * 0.95 + math.min(dt, 0.25) * 0.05          -- smoothed frame time
  if st.now >= st.gateAt then
    st.gateAt = st.now + GATE_EVERY
    local g, moving = gate()
    st.gate, st.driving = g, moving == true
  end
  if st.gate == 'online' then setPhase('off', 'online race, class tagging doesn\'t run online') return end
  if st.gate == 'unknown' then
    if st.now > 2 then setPhase('off', 'can\'t tell if this is an offline race, so doing nothing') end
    return
  end
  if p == 'wait' then begin(false) return end         -- no waiting: start as soon as it's known to be an offline race
  local w = st.worst[p]
  if not w or dt > w then st.worst[p] = dt end
  if dt > SPIKE then st.backoff = st.now + BACKOFF end
  if st.now < st.backoff or st.fdt > SLOW_FRAME then
    st.paused = true
    return
  end
  st.paused = false
  if p == 'scan' then runBudget(scanWork, st.driving)
  elseif p == 'hash' then runBudget(hashWork, st.driving)
  elseif p == 'lookup' then runBudget(lookupWork, st.driving)
  elseif p == 'apply' then runBudget(applyWork, st.driving)
  end
end

function M.ui()
  ui.text('Class tags: ' .. (st.paused and 'paused while the frame rate is low' or st.note))
  if st.phase == 'done' or st.phase == 'apply' then
    ui.text(string.format('%d cars, %d known to the server, %d tagged, %d already right, %d problems',
      st.n.cars, st.n.known, st.n.tagged, st.n.same, st.n.errors))
    ui.text(string.format('%d cars identified from the desktop app\'s list, %d hashed here', st.n.seeded, st.n.hashed))
    if st.slowest then ui.text(st.slowest) end
    if st.n.big > 0 then
      ui.text(string.format('%d big cars skipped, the desktop app tags those', st.n.big))
    end
  end
  if type(ac.checksumSHA256) == 'function' and cfg.server ~= '' then
    if (st.phase == 'done' or st.phase == 'off' or st.phase == 'wait') and ui.button('Check now') then
      loadConfig()
      begin(true)
    end
  end
end

return M
