-- Data stage. Reference: https://lua-api.factorio.com/stable/
--
-- Swaps the small and medium biters' sprites for the sheets tools/pack-art.py
-- builds from art/. Vanilla draws a base layer, two tinted mask layers and a
-- shadow (__base__/prototypes/entity/biter-animations.lua); the packed art has
-- the tint baked in, so each animation here is just a colour layer and a shadow.

-- scale matches the vanilla tier (enemy-constants.lua) so each creature fills the
-- biter's footprint. Change it to draw the art bigger or smaller in game.
local targets =
{
  { art = "boxelder-bug", unit = "small-biter",  corpse = "small-biter-corpse",  scale = 0.5 },
  { art = "asian-beetle", unit = "medium-biter", corpse = "medium-biter-corpse", scale = 0.7 },
}

local function sheet(art, name, scale, params)
  local path = "__boxelder__/graphics/entity/" .. art .. "/" .. name
  local ok, meta = pcall(require, path)
  if not ok then
    error("Boxelder: " .. path .. ".lua is missing. Run `uv run tools/pack-art.py` and restart Factorio.\n" .. tostring(meta))
  end

  local filenames = {}
  for i, suffix in ipairs(meta.filenames) do
    filenames[i] = path .. suffix
  end

  params.filenames = filenames
  params.lines_per_file = meta.lines_per_file
  params.width = meta.width
  params.height = meta.height
  -- Every vanilla biter sheet sets `slice` to its real column count, and some set
  -- a line_length that disagrees (attack: 16 vs 11 columns). Setting both to the
  -- same value keeps this layout identical to one the engine is known to read.
  params.line_length = meta.line_length
  params.slice = meta.line_length
  params.frame_count = meta.frame_count
  params.direction_count = meta.direction_count
  -- The sheets are drawn at half size, and vanilla scales the shift with the tier
  -- (util.sprite_load's multiply_shift) so a bigger biter keeps its anchor.
  params.scale = scale * 0.5
  params.shift = { meta.shift[1] * scale, meta.shift[2] * scale }
  params.allow_forced_downscale = true
  params.surface = "nauvis"
  params.usage = params.usage or "enemy"
  return params
end

for _, t in pairs(targets) do
  local unit = data.raw.unit[t.unit]
  local corpse = data.raw.corpse[t.corpse]

  unit.icon = "__boxelder__/graphics/icons/" .. t.art .. ".png"
  unit.run_animation =
  {
    layers =
    {
      sheet(t.art, "run", t.scale, {}),
      sheet(t.art, "run-shadow", t.scale, { draw_as_shadow = true }),
    }
  }
  unit.attack_parameters.animation =
  {
    layers =
    {
      sheet(t.art, "attack", t.scale, { animation_speed = 0.4 }),
      sheet(t.art, "attack-shadow", t.scale, { animation_speed = 0.4, draw_as_shadow = true }),
    }
  }

  -- Only the two animations are replaced. The corpse's timings, ground patch and
  -- direction shuffle from add_biter_die_animation() stay as vanilla set them.
  corpse.icon = "__boxelder__/graphics/icons/" .. t.art .. "-corpse.png"
  corpse.animation =
  {
    layers =
    {
      sheet(t.art, "die", t.scale, { flags = { "corpse-decay" } }),
      sheet(t.art, "die-shadow", t.scale, { draw_as_shadow = true }),
    }
  }
  corpse.decay_animation =
  {
    layers =
    {
      sheet(t.art, "decay", t.scale, { flags = { "corpse-decay" }, usage = "corpse-decay" }),
    }
  }
end
