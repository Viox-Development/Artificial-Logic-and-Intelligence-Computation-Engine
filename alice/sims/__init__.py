"""Flight simulations: DCS and War Thunder.

ALICE sends nothing to the game; it only reads and speaks.

  flight      -> shared flight state and flight wingman (fuel, approach, G, countermeasures, post-flight)
  dcs         -> Export.lua script, UDP listener, mission file reading
  kneeboard   -> turning the mission's frequencies/waypoints/notes into an in-game kneeboard page
  checklists  -> spoken checklists
  warthunder  -> reading the game's local indicators (localhost:8111)
"""
