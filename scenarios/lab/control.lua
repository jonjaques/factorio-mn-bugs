-- Art lab: a flat, always-lit floor with a few of each replaced biter tier, for
-- checking sprite edits. The biters run the vanilla AI untouched. Launch it with
--   tools/lab.sh            (packs art/ first)
-- or from the main menu: New game > Scenarios > boxelder/lab.
--
-- The commands live here rather than in the mod's own control.lua so the mod
-- itself stays a pure art replacement with no runtime script.

local tiers =
{
  small = { "small-biter" },
  medium = { "medium-biter" },
  both = { "small-biter", "medium-biter" },
}

local function lab()
  return game.surfaces["boxelder-lab"]
end

local function spawn(names, count, center)
  local surface = lab()
  local spawned = 0
  for _, name in pairs(names) do
    for _ = 1, count do
      local angle = math.random() * 2 * math.pi
      local near = { center.x + 8 * math.cos(angle), center.y + 8 * math.sin(angle) }
      local position = surface.find_non_colliding_position(name, near, 8, 0.5)
      if position and surface.create_entity({ name = name, position = position, force = "enemy" }) then
        spawned = spawned + 1
      end
    end
  end
  return spawned
end

script.on_init(function()
  -- A separate surface, because generate_with_lab_tiles only affects chunks made
  -- after it is set, and nauvis has already generated its starting area by now.
  local surface = game.create_surface("boxelder-lab")
  surface.generate_with_lab_tiles = true
  surface.always_day = true
  surface.request_to_generate_chunks({ 0, 0 }, 2)
  surface.force_generate_chunk_requests()

  spawn(tiers.both, 5, { x = 0, y = 0 })
end)

script.on_event(defines.events.on_player_created, function(event)
  local player = game.get_player(event.player_index)
  -- God mode, as in the vanilla sandbox, so the biters have nobody to chase and
  -- the camera moves freely. Without a target they idle and wander.
  local character = player.character
  player.character = nil
  if character then
    character.destroy()
  end
  player.teleport({ 0, 0 }, lab())
  player.print("Boxelder lab. /boxelder-spawn [small|medium|both] [count] adds biters, /boxelder-kill [radius] kills them to show the death and decay art.")
end)

commands.add_command("boxelder-spawn", "[small|medium|both] [count] - spawn biters around you (default: both, 5 each).", function(command)
  local player = command.player_index and game.get_player(command.player_index)
  if not player then
    return
  end
  local tier, count = string.match(command.parameter or "", "^%s*(%a*)%s*(%d*)%s*$")
  local names = tiers[(tier == nil or tier == "") and "both" or tier]
  if not names then
    player.print("Usage: /boxelder-spawn [small|medium|both] [count]")
    return
  end
  local spawned = spawn(names, math.min(tonumber(count) or 5, 50), player.position)
  player.print("Spawned " .. spawned .. " biters.")
end)

commands.add_command("boxelder-kill", "[radius] - kill small and medium biters near you (default 32) to show their death and decay art.", function(command)
  local player = command.player_index and game.get_player(command.player_index)
  if not player then
    return
  end
  local units = lab().find_entities_filtered({
    name = tiers.both,
    position = player.position,
    radius = math.min(tonumber(command.parameter) or 32, 200),
  })
  -- die() rather than destroy(), so the corpse plays the die and decay art.
  for _, unit in pairs(units) do
    unit.die()
  end
  player.print("Killed " .. #units .. " biters.")
end)
