# Pokémon Autopilot RBox

This pack encapsulates the complete Pokémon Red/Gold autonomous agent as one
RBox cartridge. It owns its supervisor, Copilot decision sessions, PyBoy
emulator, recovery state, recordings, authenticated local viewer, manual
takeover, and objective verification.

The pack is deliberately ROM-free. Supply a legally obtained local ROM at
runtime; the cartridge never copies, uploads, serves, or publishes it.

```bash
bash ~/.brainstem/installer/install-expansion-pack.sh pokemon
export OPENRAPPTER_POKEMON_ROM="/absolute/path/to/Pokemon Red.gb"
bash ~/.brainstem/src/rapp_brainstem/start.sh
```

Ask the RBox to start Pokémon, or call `Pokemon(action="start")`. Generated
state remains inside `$RAPP_HOME/rboxes/pokemon-red` (default
`~/.rapp/rboxes/pokemon-red`). Use `status`, `view`, `pause`, `manual`,
`autonomy`, `checkpoint`, `rewind`, and `stop` for operator control.

The source snapshot does not contain a ROM, save, screenshot, recording,
credential, or private runtime file.
