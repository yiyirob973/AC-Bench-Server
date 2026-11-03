-- AC Bench logger (CSP Lua app)
-- Writes the lap times of every AI car (CAR_1..CAR_n) to result.json in this app's folder, which ac_lap_bench.py reads.
-- It only logs in benchmark races, which it recognises by the AI drivers being named "ACBENCH n" (ac_lap_bench.py sets
-- that). In any other race all it does is keep the car class tags up to date (class_sync.lua).
-- There's a small status window ("AC Bench" in the apps sidebar).

local MARKER = 'ACBENCH'
local PARK_PLAYER = true      -- experimental: send the idle player car to the pits (benchmark races only)
local STILL_KMH = 3           -- an AI car slower than this counts as standing still

local laps, lastCount = {}, {}
local still = {}              -- still[i] = true while AI car i stands still
local out = {}                -- out[i] = true while AI car i is retired or has vanished from the race
local started, first = false, true
local elapsed, parked = 0, false
local active, why = nil, ''
local okSync, sync = pcall(require, 'class_sync')   -- keeps the car class tags current (doesn't run in benchmark races)
if okSync and sync then
  local okInit, e = pcall(sync.init)
  if not okInit then ac.log('ac_bench: class_sync init failed: ' .. tostring(e)) sync = nil end
else
  ac.log('ac_bench: class_sync not loaded: ' .. tostring(sync))
  sync = nil
end
local status = { mode = 'checking...', save = 'not written yet', park = PARK_PLAYER and 'waiting' or 'off', cars = 0, laps = 0 }

local function scriptDir()
  if __dirname then return __dirname end
  local ok, d = pcall(function() return ac.getFolder(ac.FolderID.ScriptOrigin) end)
  if ok and d then return d end
  return nil
end

local dir = scriptDir()
local path = dir and (dir .. '/result.json') or nil
local flagPath = dir and (dir .. '/bench_active.flag') or nil
ac.log('ac_bench: loaded, result file = ' .. tostring(path))

-- Is this a benchmark race?
local function detectBench()
  local ok, name = pcall(function() return ac.getDriverName(1) end)
  if ok and type(name) == 'string' then
    return name:sub(1, #MARKER) == MARKER, 'driver name'
  end
  -- no driver-name API, so use the flag file ac_lap_bench.py keeps while it runs
  if flagPath then
    local ok2, res = pcall(function() return io.load(flagPath, nil) end)
    if ok2 and res ~= nil then return true, 'flag file' end
    local ok3, ex = pcall(function() return io.exists(flagPath) end)
    if ok3 and ex then return true, 'flag file' end
  end
  return false, 'no marker'
end

local function save(n)
  if not path then
    status.save = 'FAILED: script folder unknown'
    return
  end
  local cars, total, stillList, outList = {}, 0, {}, {}
  for i = 1, n - 1 do
    local list = laps[i] or {}
    local parts = {}
    for j, v in ipairs(list) do parts[j] = tostring(v) end
    total = total + #list
    if still[i] then stillList[#stillList + 1] = tostring(i) end
    if out[i] then outList[#outList + 1] = tostring(i) end
    cars[#cars + 1] = string.format('{"idx":%d,"car":"%s","laps_ms":[%s]}',
      i, tostring(ac.getCarID(i) or ''), table.concat(parts, ','))
  end
  status.cars, status.laps = n - 1, total
  local json = '{"started":' .. tostring(started) .. ',"still":[' .. table.concat(stillList, ',') ..
               '],"out":[' .. table.concat(outList, ',') .. '],"cars":[' .. table.concat(cars, ',') .. ']}'
  local ok, res = pcall(io.save, path, json)
  if ok and res ~= false then
    status.save = 'ok'
  else
    status.save = 'FAILED: ' .. tostring(res)
    ac.log('ac_bench: could not write ' .. tostring(path) .. ' (' .. tostring(res) .. ')')
  end
end

function script.update(dt)
  if active == nil then
    elapsed = elapsed + dt
    if elapsed < 1.5 then return end
    active, why = detectBench()
    status.mode = active and ('BENCHMARK race (' .. why .. ')') or 'idle: not a benchmark race, doing nothing'
    ac.log('ac_bench: ' .. status.mode)
    elapsed = 0
  end
  if not active then
    if sync then
      local okU, e = pcall(sync.update, dt)
      if not okU then ac.log('ac_bench: class_sync error: ' .. tostring(e)) sync = nil end
    end
    return
  end

  local sim = ac.getSim()
  local n = (sim and sim.carsCount) or 2

  if PARK_PLAYER and not parked then
    elapsed = elapsed + dt
    if elapsed > 3 then
      parked = true
      local ok, res = pcall(function() return ac.tryToTeleportToPits() end)
      status.park = ok and ('called, result ' .. tostring(res)) or ('error: ' .. tostring(res))
      ac.log('ac_bench: teleport to pits -> ' .. status.park)
    end
  end

  local changed = false
  for i = 1, n - 1 do
    local car = ac.getCar(i)
    local gone = (car == nil)          -- gone from the race
    if car then
      local okR, retired = pcall(function() return car.isRetired end)   -- not in every CSP version, skipped if missing
      if okR and retired == true then gone = true end
      if not started and car.speedKmh and car.speedKmh > 20 then
        started = true
        changed = true
      end
      local isStill = (car.speedKmh ~= nil) and (car.speedKmh < STILL_KMH)
      if isStill ~= (still[i] == true) then
        still[i] = isStill
        changed = true
      end
      local last = lastCount[i] or 0
      if car.lapCount > last then
        lastCount[i] = car.lapCount
        local t = car.previousLapTimeMs
        if t and t > 0 then
          laps[i] = laps[i] or {}
          laps[i][#laps[i] + 1] = t
          changed = true
        end
      end
    end
    if gone ~= (out[i] == true) then
      out[i] = gone
      changed = true
    end
  end
  if changed or first then
    first = false
    save(n)
  end
end

function script.windowMain(dt)
  ui.text('AC Bench logger')
  ui.text('Mode: ' .. status.mode)
  if active then
    ui.text('AI cars: ' .. status.cars .. '   laps logged: ' .. status.laps)
    ui.text('Race running: ' .. tostring(started))
    ui.text('Result file: ' .. status.save)
    ui.text('Player to pits: ' .. status.park)
  elseif sync then
    ui.separator()
    pcall(sync.ui)
  end
end
